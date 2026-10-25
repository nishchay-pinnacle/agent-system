"""Checkpoint one research transition at a time, reserving before external work."""

import copy
import hashlib
import json
import logging
import time
from importlib.resources import files
from pathlib import Path
from typing import Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import JsonValue, TypeAdapter, ValidationError

from agent_system.company_intelligence.agent import (
    FetchResult,
    Generation,
    LiveProviders,
    ProviderFailure,
    Providers,
    counting_kind,
    counting_payload,
    output_limit,
    request_payload,
)
from agent_system.company_intelligence.artifacts import (
    event,
    job_lock,
    now,
    project,
    read_json,
    save_json,
    source_path,
)
from agent_system.company_intelligence.call_traces import (
    finish_trace,
    start_trace,
    trace_directory,
    trace_id,
)
from agent_system.company_intelligence.orientation import (
    apply_submission,
    build_context,
    update_plan,
)
from agent_system.company_intelligence.page_content import (
    CLEANER_VERSION,
    artifact_path,
    discovery_links,
    load_cleaned,
    write_capture,
)
from agent_system.company_intelligence.schemas import (
    Begin,
    Complete,
    Gap,
    IndexSearchArgs,
    Inputs,
    Operation,
    OrientationSubmission,
    PlanUpdate,
    ReadArgs,
    Research,
    ResearchSettings,
    Source,
)
from agent_system.company_intelligence.site_index import (
    save_inventory,
    search_inventory,
)
from agent_system.company_intelligence.tools import (
    RETRIEVALS,
    TOOL_MODELS,
    excerpt,
    public_url,
    source_id,
    tool_definitions,
)

JSON_MESSAGES = TypeAdapter(list[dict[str, JsonValue]])

logger = logging.getLogger(__name__)


class ResearchState(TypedDict):
    research: dict[str, JsonValue]


class LimitReached(Exception):
    pass


def initial_research(
    job_id: str,
    inputs: Inputs,
    settings: ResearchSettings,
    *,
    run_mode: Literal["full", "orientation_only"] = "full",
    schema_version: int = 2,
) -> Research:
    if schema_version == 3:
        from agent_system.company_intelligence.execution import initial_run

        return initial_run(job_id, inputs, settings, run_mode)
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
        job_id=job_id,
        inputs=inputs,
        settings=settings,
        instructions=instructions,
        schema_version=2,
        run_mode=run_mode,
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
            args={"url": inputs.url, "query": "", "limit": settings.map_limit},
        )
    )
    for url in inputs.source_urls:
        sid = source_id(url)
        state.sources[sid] = Source(source_id=sid, requested_url=url)
    event(state, "phase_change", summary="Research started: full-page orientation")
    return state


def context(state: Research) -> list[dict[str, JsonValue]]:
    dynamic = state.model_dump(
        mode="json",
        include={
            "inputs",
            "profile",
            "investigation_plan",
            "assessments",
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
        json.dumps(
            [op.kind, op.args, "cleaned", CLEANER_VERSION], sort_keys=True
        ).encode()
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


def plan(state: Research, directory: Path) -> None:
    if state.orientation_queue:
        queued = state.orientation_queue.pop(0)
        operation(state, queued.kind, queued.args)
        return
    if state.phase == "orientation" and state.schema_version >= 2:
        if state.orientation_context is None:
            build_context(state, directory)
    elif not state.messages:
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
    if op.kind == "search_site_index":
        args_index = IndexSearchArgs.model_validate(op.args)
        result_index = search_inventory(directory, args_index.query, args_index.limit)
        entries = result_index.get("entries", [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    sid = str(entry["source_id"])
                    state.sources.setdefault(
                        sid,
                        Source(
                            source_id=sid,
                            requested_url=str(entry["url"]),
                            title=str(entry.get("title", "")),
                        ),
                    )
        event(state, "tool_end", tool=op.kind, result=result_index)
        tool_result(state, result_index)
        return
    if op.kind == "scrape_page" and not (
        state.phase == "orientation" and state.orientation_queue
    ):
        saved_source = state.sources.get(source_id(str(op.args["url"])))
        if saved_source and saved_source.outcome == "success":
            tool_result(state, excerpt(directory, saved_source))
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
                state.error = (
                    "Full orientation content exceeded the input token cap"
                    if state.phase == "orientation"
                    else "Input exceeded the token cap"
                )
                raise LimitReached("input_tokens")
            allowance = op.counted_input + output_limit(state)
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
        state.settings.request_timeout(op.kind),
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
    identifier: str | None = None
    supplied = state.model_copy(deep=True)
    if (
        state.schema_version >= 2
        and state.phase == "orientation"
        and state.orientation_context
    ):
        context_file = read_json(directory / "orientation_context.json")
        supplied.messages = (
            JSON_MESSAGES.validate_python(context_file["messages"]) + supplied.messages
        )
    if op.kind in {"count_input", "generate"}:
        request = request_payload(supplied)
        kind = (
            "generation"
            if op.kind == "generate"
            else counting_kind(state.settings.model)
        )
        if op.kind == "count_input":
            request = counting_payload(request)
        identifier = start_trace(directory, state, request, kind, providers is not None)
    boundary = providers or LiveProviders(directory, identifier)
    start = time.monotonic()
    try:
        if op.kind == "count_input":
            # Count the full Responses input including schemas and protocol items.
            counted_input = boundary.count_input(supplied, op.reserved_seconds)
            result: dict[str, JsonValue] = {
                "input_tokens": counted_input + 1024,
                "counted_input": counted_input,
                "overhead_tokens": 1024,
            }
        elif op.kind == "generate":
            result = Generation.model_dump(
                boundary.generate(supplied, op.reserved_seconds), mode="json"
            )
        else:
            result = FetchResult.model_dump(
                boundary.retrieve(op.kind, op.args, op.reserved_seconds), mode="json"
            )
        if identifier:
            raw_response = result.get("raw_response")
            if op.kind == "count_input":
                response_path = trace_directory(directory, identifier) / "response.json"
                raw_response = (
                    read_json(response_path)
                    if response_path.exists()
                    else {"input_tokens": result["counted_input"], "kind": kind}
                )
            finish_trace(
                directory,
                identifier,
                response=raw_response if isinstance(raw_response, dict) else result,
                status=str(result.get("status", "completed")),
                duration=time.monotonic() - start,
                application_status="accepted"
                if op.kind == "count_input"
                else "pending",
                usage={
                    key: result.get(key)
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "reasoning_tokens",
                    )
                }
                if op.kind == "generate"
                else {},
            )
        save_json(path, {"result": result, "duration": time.monotonic() - start})
        state.stage = "consume"
    except ProviderFailure as exc:
        if identifier:
            finish_trace(
                directory,
                identifier,
                status="failed",
                application_status="not_evaluated",
                error=str(exc),
                duration=time.monotonic() - start,
            )
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
    if op.kind in RETRIEVALS and not (
        state.schema_version >= 2 and state.phase == "orientation" and not op.call_id
    ):
        state.cache[cache_key(op)] = result
    if isinstance(result, dict) and result.get("representation") == "cleaned":
        state.source_deliveries.append(
            {k: v for k, v in result.items() if k not in {"content", "heading_index"}}
        )
    if op.call_id:
        state.messages.append(
            {
                "type": "function_call_output",
                "call_id": op.call_id,
                "output": json.dumps(result),
            }
        )
    elif state.schema_version < 2:
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
        attempt=op.attempts,
        duration=duration,
    )
    if op.kind == "count_input":
        if not isinstance(result, dict):
            raise ValueError("Invalid count result")
        counted = int(str(result["input_tokens"]))
        operation(state, "generate")
        assert state.operation is not None
        state.operation.counted_input = counted
        state.operation.count_trace_id = trace_id(op)
        return
    if op.kind == "generate":
        state.last_trace_id = trace_id(op)
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
            if name == "complete_orientation":
                apply_submission(
                    state,
                    OrientationSubmission.model_validate(parsed.model_dump()),
                    directory,
                )
                state.messages = []
                state.source_deliveries = []
                if state.run_mode == "orientation_only":
                    state.readiness = "needs_review"
                    stop(state, "orientation_complete")
                else:
                    state.phase = "between"
                    event(
                        state,
                        "phase_change",
                        summary="Orientation complete; provisional profile and plan saved",
                    )
            elif name == "update_investigation_plan":
                update_plan(state, PlanUpdate.model_validate(parsed.model_dump()))
                state.messages = context(state)
            elif name == "begin_investigation":
                begin(state, Begin.model_validate(parsed.model_dump()))
            elif name == "complete_investigation":
                complete(state, Complete.model_validate(parsed.model_dump()))
            else:
                arguments = parsed.model_dump(mode="json")
                if name in {"find_site_pages", "scrape_page"}:
                    arguments["url"] = public_url(str(arguments["url"]))
                    if name == "find_site_pages":
                        arguments["limit"] = state.settings.map_limit
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
        if not any(
            value and value.strip()
            for value in (fetched.markdown, fetched.html, fetched.raw_html)
        ):
            state.sources[sid] = Source(
                source_id=sid, requested_url=url, outcome="failed"
            )
            if state.schema_version >= 2 and state.phase == "orientation":
                raise ValueError("Required orientation page returned no content")
            tool_result(
                state,
                {
                    "source_id": sid,
                    "error": "No page content returned; this does not prove absence.",
                },
            )
            return
        source = Source(
            source_id=sid,
            requested_url=url,
            final_url=public_url(fetched.final_url) if fetched.final_url else None,
            title=fetched.title,
            fetched_at=now(),
            filename=f"sources/{sid}.md",
            cleaner_version=CLEANER_VERSION,
            capture_metadata={
                "formats": ["markdown", "html", "rawHtml"],
                "only_main_content": False,
            },
            outcome="success",
        )
        write_capture(source_path(directory, source), fetched.markdown or "")
        for representation, value, suffix in (
            ("html_filename", fetched.html, ".html"),
            ("raw_html_filename", fetched.raw_html, ".raw.html"),
        ):
            if value is not None:
                filename = f"sources/{sid}{suffix}"
                write_capture(
                    artifact_path(directory, source, filename, {suffix}), value
                )
                setattr(source, representation, filename)
        state.sources[sid] = source
        cleaned = load_cleaned(directory, source)
        state.usage.pages += 1
        links = discovery_links(directory, source)
        if links:
            state.discovery = save_inventory(
                directory, links, f"cleaning:{sid}", state.settings.map_limit
            )
        if state.schema_version >= 2 and state.phase == "orientation":
            if not cleaned.strip():
                raise ValueError("Required orientation page has empty cleaned content")
            result = {"source_id": sid, "saved": True}
        else:
            result = excerpt(directory, source)
        event(state, "source_fetch", source_id=sid, result=result)
    elif op.kind == "find_site_pages":
        state.discovery = save_inventory(
            directory, fetched.candidates, op.operation_id, state.settings.map_limit
        )
        result = state.discovery
        save_json(
            directory / "discoveries" / f"{op.operation_id}.json",
            fetched.model_dump(mode="json"),
        )
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
    selected_plan = None
    if state.assessments:
        selected_plan = next(
            (p for p in state.investigation_plan if p.plan_id == args.plan_id), None
        )
        if (
            selected_plan is None
            or selected_plan.status != "pending"
            or args.question_id != selected_plan.question_id
        ):
            raise ValueError(
                "Begin must select a pending plan and its question ID; add a focused plan first for newly revealed gaps"
            )
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
    if selected_plan:
        selected_plan.status = "in_progress"
    state.source_deliveries = []
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
        if state.current and state.current.plan_id:
            for entry in state.investigation_plan:
                if entry.plan_id == state.current.plan_id:
                    entry.status = "completed"
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
    state.source_deliveries = []
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
            plan(state, directory)
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
    if (
        before.stage == "consume"
        and before.operation
        and before.operation.kind == "generate"
    ):
        identifier = trace_id(before.operation)
        correction_events = state.events[len(before.events) :]
        issue = next(
            (
                str(e.get("summary"))
                for e in correction_events
                if e.get("kind") == "correction"
            ),
            state.error,
        )
        output_items = read_json(receipt_path(directory, before.operation)).get(
            "result"
        )
        action = None
        if isinstance(output_items, dict):
            outputs = output_items.get("output", [])
            if isinstance(outputs, list):
                action = next(
                    (
                        str(item.get("name"))
                        for item in outputs
                        if isinstance(item, dict)
                        and item.get("type") == "function_call"
                    ),
                    None,
                )
        outcome: dict[str, JsonValue] = {
            "status": "rejected" if issue else "accepted",
            "error": issue,
        }
        target = trace_directory(directory, identifier)
        if (target / "metadata.json").exists():
            save_json(target / "application.json", outcome)
            finish_trace(
                directory,
                identifier,
                application_status=outcome["status"],
                validation_issue=issue,
                action=action,
            )
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
    if initial is not None and initial.schema_version == 3:
        from agent_system.company_intelligence.execution import run

        return run(data_dir, job_id, initial, providers, max_steps=max_steps)
    if initial is None:
        with SqliteSaver.from_conn_string(
            str(data_dir / "checkpoints.sqlite")
        ) as probe:
            existing = load_research(probe, data_dir / "jobs" / job_id, job_id)
        if existing is not None and existing.schema_version == 3:
            from agent_system.company_intelligence.execution import run

            return run(data_dir, job_id, None, providers, max_steps=max_steps)
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
