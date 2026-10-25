"""Checkpoint one research transition at a time, reserving before external work."""

import copy
import hashlib
import json
import logging
import time
from importlib.resources import files
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import JsonValue, ValidationError

from agent_system.company_intelligence.agent import (
    FetchResult,
    Generation,
    LiveProviders,
    ProviderFailure,
    Providers,
)
from agent_system.company_intelligence.artifacts import (
    event,
    job_lock,
    now,
    project,
    read_json,
    save_json,
)
from agent_system.company_intelligence.schemas import (
    Begin,
    Complete,
    Gap,
    Inputs,
    Operation,
    ReadArgs,
    Research,
    ResearchSettings,
    Source,
)
from agent_system.company_intelligence.tools import (
    RETRIEVALS,
    TOOL_MODELS,
    excerpt,
    public_url,
    source_id,
    tool_definitions,
)

logger = logging.getLogger(__name__)


class ResearchState(TypedDict):
    research: dict[str, JsonValue]


class LimitReached(Exception):
    pass


def initial_research(
    job_id: str, inputs: Inputs, settings: ResearchSettings
) -> Research:
    inputs = inputs.model_copy(deep=True)
    inputs.url = public_url(inputs.url)
    inputs.product_url = public_url(inputs.product_url)
    inputs.source_urls = [public_url(url) for url in inputs.source_urls]
    instructions = (
        files("agent_system.company_intelligence")
        .joinpath("research_instructions.md")
        .read_text()
    )
    state = Research(
        job_id=job_id, inputs=inputs, settings=settings, instructions=instructions
    )
    urls = list(dict.fromkeys([inputs.url, inputs.product_url]))
    state.orientation_queue = [
        Operation(operation_id="", kind="scrape_page", args={"url": url})
        for url in urls
    ]
    state.orientation_queue.append(
        Operation(
            operation_id="",
            kind="find_site_pages",
            args={"url": inputs.url, "query": ""},
        )
    )
    for url in inputs.source_urls:
        sid = source_id(url)
        state.sources[sid] = Source(source_id=sid, requested_url=url)
    state.messages = context(state)
    event(state, "phase_change", summary="Research started: bounded orientation")
    return state


def context(state: Research) -> list[dict[str, JsonValue]]:
    dynamic = state.model_dump(
        mode="json",
        include={
            "inputs",
            "profile",
            "gaps",
            "current",
            "phase",
            "return_question",
            "settings",
            "usage",
            "completed_investigations",
            "investigation_retrievals",
        },
    )
    dynamic["source_candidates"] = [
        s.model_dump(mode="json") for s in list(state.sources.values())[-40:]
    ]
    dynamic["recent_findings"] = state.investigations[-5:]
    return [{"role": "user", "content": json.dumps(dynamic)}]


def operation(
    state: Research,
    kind: str,
    args: dict[str, JsonValue] | None = None,
    call_id: str | None = None,
) -> None:
    state.operation_number += 1
    state.operation = Operation(
        operation_id=f"op_{state.operation_number:05d}",
        kind=kind,
        args=args or {},
        call_id=call_id,
    )
    state.stage = "reserve"


def cache_key(op: Operation) -> str:
    return hashlib.sha256(
        json.dumps([op.kind, op.args], sort_keys=True).encode()
    ).hexdigest()


def stop(state: Research, reason: str, resource: str | None = None) -> None:
    state.status = "completed"
    state.stop_reason = reason
    state.limiting_resource = resource
    state.stage = "halt"
    if reason == "budget_reached" or any(
        g.critical_boundary and g.state != "answered" for g in state.gaps
    ):
        state.readiness = "needs_review"
    if any(
        s.coverage in {"unassessed", "needs_internal_input"}
        for s in state.profile.values()
    ):
        state.readiness = "needs_review"
    event(state, "completion", summary=reason, limiting_resource=resource)


def plan(state: Research) -> None:
    if state.orientation_queue:
        queued = state.orientation_queue.pop(0)
        operation(state, queued.kind, queued.args)
        return
    if not state.messages:
        state.messages = context(state)
    operation(state, "count_input")


def guard(state: Research) -> None:
    if state.usage.active_seconds >= state.settings.active_seconds:
        raise LimitReached("active_seconds")
    if (
        state.usage.input_tokens
        + state.usage.output_tokens
        + state.usage.reserved_tokens
        >= state.settings.total_tokens
    ):
        raise LimitReached("total_tokens")


def reserve(state: Research, directory: Path) -> None:
    op = state.operation
    assert op is not None
    if op.kind in RETRIEVALS and op.attempts == 0:
        if state.usage.retrievals >= state.settings.retrievals:
            raise LimitReached("retrievals")
        if (
            state.phase == "investigation"
            and state.investigation_retrievals
            >= state.settings.investigation_retrievals
        ):
            # Give the model a chance to complete the investigation without fetching.
            tool_result(
                state,
                {
                    "error": "Investigation retrieval limit reached; complete this investigation now."
                },
            )
            return
        state.usage.retrievals += 1
        if state.phase == "investigation":
            state.investigation_retrievals += 1
    if op.kind in RETRIEVALS and cache_key(op) in state.cache:
        event(
            state,
            "cache_hit",
            tool=op.kind,
            operation_id=op.operation_id,
            result=state.cache[cache_key(op)],
        )
        tool_result(state, state.cache[cache_key(op)])
        return
    if op.kind == "read_source":
        args = ReadArgs.model_validate(op.args)
        source = state.sources.get(args.source_id)
        if not source:
            raise ValueError("Unknown source ID")
        result = excerpt(directory, source, args.start_line, args.max_lines)
        event(state, "tool_end", tool=op.kind, result=result)
        tool_result(state, result)
        return
    guard(state)
    if op.kind in {"count_input", "generate"}:
        if state.usage.generation_requests >= state.settings.generation_requests:
            raise LimitReached("generation_requests")
        if op.kind == "count_input":
            if (
                state.usage.counting_requests
                >= state.settings.generation_requests * 2 + 2
            ):
                raise LimitReached("counting_requests")
            state.usage.counting_requests += 1
        else:
            assert op.counted_input is not None
            if op.counted_input > state.settings.input_tokens:
                raise LimitReached("context_size")
            allowance = op.counted_input + state.settings.output_tokens
            if (
                state.usage.input_tokens
                + state.usage.output_tokens
                + state.usage.reserved_tokens
                + allowance
                > state.settings.total_tokens
            ):
                raise LimitReached("total_tokens")
            op.reserved_tokens = allowance
            state.usage.reserved_tokens += allowance
            state.usage.generation_requests += 1
    else:
        if state.usage.firecrawl_requests >= state.settings.firecrawl_requests:
            raise LimitReached("firecrawl_requests")
        if op.kind == "scrape_page" and state.usage.pages >= state.settings.pages:
            raise LimitReached("pages")
        state.usage.firecrawl_requests += 1
    op.attempts += 1
    op.reserved_seconds = min(
        state.settings.request_seconds,
        state.settings.active_seconds - state.usage.active_seconds,
    )
    state.usage.active_seconds += op.reserved_seconds
    event(
        state,
        "model_request_start"
        if op.kind in {"generate", "count_input"}
        else "tool_start",
        operation_id=op.operation_id,
        tool=op.kind,
        arguments=op.args,
        attempt=op.attempts,
        reserved_tokens=op.reserved_tokens,
    )
    state.stage = "execute"  # This entire reservation is checkpointed before dispatch.


def receipt_path(directory: Path, op: Operation) -> Path:
    return directory / "operations" / f"{op.operation_id}_{op.attempts}.json"


def execute(state: Research, directory: Path, providers: Providers | None) -> None:
    op = state.operation
    assert op is not None
    path = receipt_path(directory, op)
    if path.exists():
        state.stage = "consume"
        return
    boundary = providers or LiveProviders()
    start = time.monotonic()
    try:
        if op.kind == "count_input":
            # Count the full Responses input including schemas and protocol items.
            result: dict[str, JsonValue] = {
                "input_tokens": boundary.count_input(state, op.reserved_seconds) + 1024
            }
        elif op.kind == "generate":
            result = Generation.model_dump(
                boundary.generate(state, op.reserved_seconds), mode="json"
            )
        else:
            result = FetchResult.model_dump(
                boundary.retrieve(op.kind, op.args, op.reserved_seconds), mode="json"
            )
        save_json(path, {"result": result, "duration": time.monotonic() - start})
        state.stage = "consume"
    except ProviderFailure as exc:
        elapsed = time.monotonic() - start
        state.usage.active_seconds += elapsed - op.reserved_seconds
        op.reserved_seconds = 0
        # Failed/uncertain requests retain their token reservation.
        if op.reserved_tokens:
            state.usage.unknown_requests.append(f"{op.operation_id}:{op.attempts}")
            op.reserved_tokens = 0
        event(state, "error", tool=op.kind, summary=str(exc), duration=elapsed)
        if exc.transient and op.attempts < 2:
            event(state, "retry", tool=op.kind, summary="One explicit transient retry")
            state.stage = "reserve"
        else:
            state.status = "blocked_by_error"
            state.error = str(exc)
            state.stage = "halt"


def tool_result(state: Research, result: JsonValue) -> None:
    op = state.operation
    assert op is not None
    if op.kind in RETRIEVALS:
        state.cache[cache_key(op)] = result
    if op.call_id:
        state.messages.append(
            {
                "type": "function_call_output",
                "call_id": op.call_id,
                "output": json.dumps(result),
            }
        )
    else:
        # Orientation fetches precede the first model conversation.
        state.messages.append(
            {"role": "user", "content": json.dumps({"orientation_evidence": result})}
        )
    state.operation = None
    state.stage = "plan"


def consume(state: Research, directory: Path) -> None:
    op = state.operation
    assert op is not None
    receipt = read_json(receipt_path(directory, op))
    duration = float(str(receipt["duration"]))
    state.usage.active_seconds += duration - op.reserved_seconds
    op.reserved_seconds = 0
    result = receipt["result"]
    event(
        state,
        "model_request_end" if op.kind in {"generate", "count_input"} else "tool_end",
        tool=op.kind,
        operation_id=op.operation_id,
        duration=duration,
    )
    if op.kind == "count_input":
        if not isinstance(result, dict):
            raise ValueError("Invalid count result")
        counted = int(str(result["input_tokens"]))
        operation(state, "generate")
        assert state.operation is not None
        state.operation.counted_input = counted
        return
    if op.kind == "generate":
        response = Generation.model_validate(result)
        if response.input_tokens is None or response.output_tokens is None:
            state.usage.unknown_requests.append(op.operation_id)
        else:
            state.usage.reserved_tokens -= op.reserved_tokens
            state.usage.input_tokens += response.input_tokens
            state.usage.output_tokens += response.output_tokens
            # Reasoning tokens are already INCLUDED in output_tokens.
            state.usage.reasoning_tokens += response.reasoning_tokens
            if response.cached_tokens is not None:
                state.usage.cached_tokens = (
                    state.usage.cached_tokens or 0
                ) + response.cached_tokens
        op.reserved_tokens = 0
        event(state, "usage", usage=state.usage.model_dump(mode="json"))
        state.messages.extend(response.output)
        # Do not consume this reconciled receipt again after a schema failure.
        state.operation = None
        state.stage = "plan"
        if response.status != "completed":
            correction(
                state, "Generation did not complete; call exactly one valid tool."
            )
            return
        calls = [
            item for item in response.output if item.get("type") == "function_call"
        ]
        if len(calls) != 1:
            correction(state, "Exactly one function call is required.")
            return
        call = calls[0]
        name, raw, call_id = (
            call.get("name"),
            call.get("arguments"),
            call.get("call_id"),
        )
        if (
            not isinstance(name, str)
            or not isinstance(raw, str)
            or not isinstance(call_id, str)
        ):
            correction(state, "Malformed function call.")
            return
        try:
            available = {str(tool.get("name")) for tool in tool_definitions(state)}
            if name not in available:
                raise ValueError("Tool unavailable in this phase")
            parsed = TOOL_MODELS[name].model_validate_json(raw)
            if name == "begin_investigation":
                begin(state, Begin.model_validate(parsed.model_dump()))
            elif name == "complete_investigation":
                complete(state, Complete.model_validate(parsed.model_dump()))
            else:
                arguments = parsed.model_dump(mode="json")
                if name in {"find_site_pages", "scrape_page"}:
                    arguments["url"] = public_url(str(arguments["url"]))
                if (
                    name == "read_source"
                    and arguments["source_id"] not in state.sources
                ):
                    raise ValueError("Unknown source ID")
                if state.phase == "orientation" and name == "scrape_page":
                    if state.orientation_extra:
                        raise ValueError(
                            "Orientation permits only one additional overview scrape"
                        )
                    state.orientation_extra = True
                operation(state, name, arguments, call_id)
                state.correction_used = False
                return
            state.correction_used = False
        except (ValueError, ValidationError) as exc:
            correction(state, "Invalid tool result: " + str(exc)[:800], call_id)
        return
    fetched = FetchResult.model_validate(result)
    if fetched.credits is not None:
        state.usage.firecrawl_credits = (
            state.usage.firecrawl_credits or 0
        ) + fetched.credits
    if op.kind == "scrape_page":
        url = str(op.args["url"])
        sid = source_id(url)
        if not fetched.markdown:
            state.sources[sid] = Source(
                source_id=sid, requested_url=url, outcome="failed"
            )
            tool_result(
                state,
                {
                    "source_id": sid,
                    "error": "No Markdown returned; this does not prove absence.",
                },
            )
            return
        source = Source(
            source_id=sid,
            requested_url=url,
            final_url=fetched.final_url,
            title=fetched.title,
            fetched_at=now(),
            filename=f"sources/{sid}.md",
            outcome="success",
        )
        target = directory / str(source.filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_text(fetched.markdown, encoding="utf-8")
            temporary.replace(target)
        state.sources[sid] = source
        state.usage.pages += 1
        result = excerpt(directory, source)
        event(state, "source_fetch", source_id=sid, result=result)
    else:
        candidates: list[JsonValue] = []
        for candidate in fetched.candidates:
            try:
                url = public_url(candidate["url"])
            except ValueError:
                continue
            sid = source_id(url)
            state.sources.setdefault(
                sid,
                Source(
                    source_id=sid, requested_url=url, title=candidate.get("title", "")
                ),
            )
            candidates.append({**candidate, "source_id": sid, "url": url})
        result = {
            "candidates": candidates,
            "evidence": "Discovery leads only; scrape before citing.",
        }
        save_json(directory / "discoveries" / f"{op.operation_id}.json", result)
    tool_result(state, result)


def correction(state: Research, message: str, call_id: str | None = None) -> None:
    if state.correction_used:
        raise ValueError("Schema/protocol correction failed: " + message)
    state.correction_used = True
    event(state, "correction", summary=message)
    # Satisfy every outstanding call even for an invalid multi-call response.
    answered = {
        m.get("call_id")
        for m in state.messages
        if m.get("type") == "function_call_output"
    }
    for item in list(state.messages):
        if item.get("type") == "function_call" and item.get("call_id") not in answered:
            state.messages.append(
                {
                    "type": "function_call_output",
                    "call_id": item.get("call_id"),
                    "output": json.dumps({"error": message}),
                }
            )
    state.messages.append(
        {
            "role": "user",
            "content": message
            + " One correction opportunity remains; use the required schema.",
        }
    )
    state.operation = None
    state.stage = "plan"


def begin(state: Research, args: Begin) -> None:
    if state.phase != "between":
        raise ValueError("Begin is only legal between investigations")
    if state.completed_investigations >= state.settings.investigations:
        raise LimitReached("investigations")
    gap = (
        next((g for g in state.gaps if g.gap_id == args.gap_id), None)
        if args.gap_id
        else None
    )
    if args.gap_id and gap is None:
        raise ValueError("Unknown gap ID")
    if state.return_question and args.question != state.return_question:
        raise ValueError(
            "Review return must investigate the specifically named question"
        )
    if gap and gap.parked:
        new_source = state.sources.get(args.new_source_id or "")
        if (
            not new_source
            or not args.reopen_reason
            or args.new_source_id in gap.history
        ):
            raise ValueError("Parked gap needs a named new source and reopen reason")
    if gap is None:
        gap = Gap(
            gap_id=f"g_{len(state.gaps) + 1:03d}",
            question=args.question,
            consequence=args.importance,
            state="open",
            critical_boundary=False,
            interview_question="",
        )
        state.gaps.append(gap)
    gap.parked = False
    gap.state = "open"
    gap.attempts += 1
    gap.history.append(args.new_source_id or "")
    if args.reopen_reason:
        gap.history.append("Reopened: " + args.reopen_reason)
    args.gap_id = gap.gap_id
    state.current = args
    state.phase = "investigation"
    state.investigation_retrievals = 0
    state.messages = context(state)
    state.operation = None
    state.stage = "plan"
    event(
        state,
        "phase_change",
        summary="Investigation started",
        investigation=args.model_dump(mode="json"),
    )


def complete(state: Research, args: Complete) -> None:
    # Validate the whole submission before applying ANY changes.
    if args.phase != state.phase:
        raise ValueError("Completion phase does not match saved phase")
    if state.phase == "between" and (
        args.sections or args.gaps or args.next_step != "review"
    ):
        raise ValueError("Between investigations only a transition to review is legal")
    evidence = set(args.source_ids)
    for section in args.sections:
        evidence.update(section.source_ids)
        if not section.content.strip():
            raise ValueError("Section content/reason cannot be empty")
        if section.coverage == "answered" and not section.source_ids:
            raise ValueError("Answered sections need saved source evidence")
    if any(
        sid not in state.sources or state.sources[sid].outcome != "success"
        for sid in evidence
    ):
        raise ValueError("Evidence must reference successfully saved sources")
    known = {g.gap_id for g in state.gaps}
    if any(g.gap_id is not None and g.gap_id not in known for g in args.gaps):
        raise ValueError("Gap update references an unknown ID")
    if state.phase == "review" and args.next_step == "review":
        raise ValueError("Final review must stop or name its one return investigation")
    if state.phase == "closing_review" and args.next_step not in {
        "sufficient_understanding",
        "public_sources_exhausted",
    }:
        raise ValueError("Closing review must stop")
    if (
        state.phase == "review"
        and args.next_step == "investigate"
        and (state.review_returned or not args.return_question)
    ):
        raise ValueError("Review allows one specifically named return investigation")
    if state.phase in {
        "orientation",
        "investigation",
        "between",
    } and args.next_step not in {"investigate", "review"}:
        raise ValueError("Final review must precede research completion")
    for update in args.sections:
        state.profile[update.key] = type(state.profile[update.key])(
            **update.model_dump(exclude={"key"})
        )
        event(state, "profile_change", section=update.key, summary=update.content[:300])
    for update_gap in args.gaps:
        old = (
            next((g for g in state.gaps if g.gap_id == update_gap.gap_id), None)
            if update_gap.gap_id
            else None
        )
        if old:
            for name, value in update_gap.model_dump(exclude={"gap_id"}).items():
                setattr(old, name, value)
        else:
            state.gaps.append(
                Gap(
                    **update_gap.model_dump(exclude={"gap_id"}),
                    gap_id=f"g_{len(state.gaps) + 1:03d}",
                )
            )
    record = args.model_dump(mode="json")
    record["investigation"] = (
        state.current.model_dump(mode="json") if state.current else None
    )
    state.investigations.append(record)
    event(
        state,
        "findings",
        summary=args.findings,
        source_ids=[s for s in args.source_ids],
        contradictions=[s for s in args.contradictions],
    )
    if state.phase == "investigation":
        state.completed_investigations += 1
        state.no_progress = 0 if args.meaningful_change else state.no_progress + 1
        gap = next(
            g for g in state.gaps if state.current and g.gap_id == state.current.gap_id
        )
        gap.history.append(args.findings)
        gap.history.extend(args.source_ids)
        if not args.meaningful_change:
            gap.unsuccessful += 1
        if gap.unsuccessful >= 3:
            gap.parked = True
            if gap.state not in {"partial", "needs_internal_input"}:
                gap.state = "partial"
    old_phase = state.phase
    if old_phase in {"review", "closing_review"} and args.next_step in {
        "sufficient_understanding",
        "public_sources_exhausted",
    }:
        state.readiness = args.readiness
        stop(state, args.next_step)
        return
    if old_phase == "review" and args.next_step == "investigate":
        state.review_returned = True
        state.return_question = args.return_question
        state.phase = "between"
    elif old_phase == "investigation" and state.review_returned:
        state.phase = "closing_review"
    elif (
        args.next_step == "review"
        or state.no_progress >= 3
        or state.completed_investigations >= state.settings.investigations
    ):
        state.phase = "review"
    else:
        state.phase = "between"
    state.current = None
    state.messages = []
    state.operation = None
    state.stage = "plan"
    event(state, "phase_change", summary=state.phase)


def transition(
    saved: ResearchState, directory: Path, providers: Providers | None
) -> ResearchState:
    state = Research.model_validate(saved["research"])
    before = copy.deepcopy(state)
    started = time.monotonic()
    try:
        if state.stage == "plan":
            plan(state)
        elif state.stage == "reserve":
            reserve(state, directory)
        elif state.stage == "execute":
            execute(state, directory, providers)
        elif state.stage == "consume":
            consume(state, directory)
    except LimitReached as exc:
        stop(state, "budget_reached", str(exc))
    except Exception as exc:
        # Roll back invalid submissions atomically while preserving reconciled usage
        # and response items from a successful provider dispatch.
        if (
            state.stage == "consume"
            and before.operation
            and before.operation.kind != "generate"
        ):
            state = before
        state.status = "blocked_by_error"
        state.error = f"{type(exc).__name__}: {str(exc)[:800]}"
        state.stage = "halt"
        event(state, "error", summary=state.error)
    if before.stage in {"plan", "reserve", "consume"}:
        state.usage.active_seconds += time.monotonic() - started
    return {"research": state.model_dump(mode="json")}


def build_research_workflow(
    directory: Path, checkpointer: SqliteSaver, providers: Providers | None = None
) -> CompiledStateGraph[ResearchState, None, ResearchState, ResearchState]:
    graph = StateGraph(ResearchState)
    graph.add_node("transition", lambda saved: transition(saved, directory, providers))
    graph.add_edge(START, "transition")
    graph.add_edge("transition", END)
    return graph.compile(checkpointer=checkpointer)


def load_research(
    checkpointer: SqliteSaver, directory: Path, job_id: str
) -> Research | None:
    graph = build_research_workflow(directory, checkpointer)
    snapshot = graph.get_state({"configurable": {"thread_id": job_id}})
    raw = snapshot.values.get("research")
    return Research.model_validate(raw) if raw else None


def run_research(
    data_dir: Path,
    job_id: str,
    initial: Research | None = None,
    providers: Providers | None = None,
    *,
    max_steps: int | None = None,
) -> Research:
    directory = data_dir / "jobs" / job_id
    config: RunnableConfig = {"configurable": {"thread_id": job_id}}
    with (
        job_lock(directory),
        SqliteSaver.from_conn_string(str(data_dir / "checkpoints.sqlite")) as saver,
    ):
        graph = build_research_workflow(directory, saver, providers)
        state = initial or load_research(saver, directory, job_id)
        if state is None:
            raise ValueError("Research job not found")
        if state.status == "completed":
            project(directory, state)
            return state
        if initial is None:
            op = state.operation
            if op and state.stage in {"execute", "halt"} and op.attempts:
                if receipt_path(directory, op).exists():
                    state.stage = "consume"
                else:
                    if op.reserved_tokens:
                        state.usage.unknown_requests.append(
                            f"{op.operation_id}:{op.attempts}"
                        )
                        op.reserved_tokens = 0
                    if op.attempts >= 2:
                        state.error = "Attempt limit exhausted for uncertain/failed operation; reservation retained"
                        state.status = "blocked_by_error"
                        graph.update_state(
                            config, {"research": state.model_dump(mode="json")}
                        )
                        project(directory, state)
                        return state
                    state.stage = "reserve"
            elif state.stage == "halt":
                state.stage = "plan"
            state.status = "running"
            state.error = None
            event(state, "resume", summary="Resuming saved progress; budgets retained")
        graph.update_state(config, {"research": state.model_dump(mode="json")})
        project(directory, state)
        steps = 0
        try:
            while state.status == "running":
                output = graph.invoke(
                    {"research": state.model_dump(mode="json")},
                    config,
                    durability="sync",
                )
                state = Research.model_validate(output["research"])
                project(directory, state)
                steps += 1
                if max_steps is not None and steps >= max_steps:
                    break  # Test harness only; production has no fake mode or step flag.
        except KeyboardInterrupt:
            # Reload the last durable reservation; in-flight charges remain unknown.
            state = load_research(saver, directory, job_id) or state
            state.status = "interrupted"
            event(
                state,
                "interrupted",
                summary="Ctrl+C: saved progress retained; in-flight usage may be unknown",
            )
            graph.update_state(config, {"research": state.model_dump(mode="json")})
            project(directory, state)
        logger.info("job=%s phase=%s status=%s", job_id, state.phase, state.status)
        return state
