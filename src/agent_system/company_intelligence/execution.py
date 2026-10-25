"""Single-writer, checkpointed batch scheduler for version-three research."""

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import JsonValue

from agent_system.company_intelligence.agent import (
    FetchResult,
    Generation,
    LiveProviders,
    ProviderFailure,
    Providers,
    counting_payload,
)
from agent_system.company_intelligence.approvals import (
    UserCommand,
    blocked,
    decide,
    inbox,
)
from agent_system.company_intelligence.artifacts import (
    job_lock,
    now,
    read_json,
    save_json,
    source_path,
)
from agent_system.company_intelligence.call_traces import (
    finish_trace,
    safe_path,
    start_trace,
)
from agent_system.company_intelligence.contexts import (
    Action,
    messages,
    request,
    save_messages,
    start_context,
)
from agent_system.company_intelligence.coordination import (
    CoordinatorSubmission,
    Document,
    InvestigationResult,
    OperationRecord,
    PendingAction,
    Review,
    ReviewReport,
    Session,
    apply_proposal,
    catalog,
    interview_backlog,
)
from agent_system.company_intelligence.investigation import (
    record_findings,
    saved_action,
)
from agent_system.company_intelligence.orientation import apply_submission
from agent_system.company_intelligence.page_content import (
    CLEANER_VERSION,
    artifact_path,
    discovery_links,
    load_cleaned,
    write_capture,
)
from agent_system.company_intelligence.schemas import (
    Inputs,
    Operation,
    OrientationSubmission,
    Phase,
    Research,
    ResearchSettings,
    Source,
)
from agent_system.company_intelligence.site_index import save_inventory
from agent_system.company_intelligence.tools import public_url, source_id

REMOTE_TOOLS = {"scrape_page", "find_site_pages", "search_web"}
LOCAL_TOOLS = {"read_source", "read_profile", "read_findings", "search_site_index"}


def initial_run(
    job_id: str,
    inputs: Inputs,
    settings: ResearchSettings,
    run_mode: Literal["full", "orientation_only"],
) -> Research:
    inputs = inputs.model_copy(deep=True)
    inputs.url, inputs.product_url = (
        public_url(inputs.url),
        public_url(inputs.product_url),
    )
    inputs.source_urls = [public_url(u) for u in inputs.source_urls]
    return Research(
        job_id=job_id,
        schema_version=3,
        inputs=inputs,
        settings=settings,
        instructions="",
        run_mode=run_mode,
    )


def new_document(state: Research) -> Document:
    limits = {
        k: float(getattr(state.settings, k))
        for k in (
            "generation_requests",
            "firecrawl_requests",
            "pages",
            "investigations",
            "active_seconds",
            "total_tokens",
            "input_tokens",
            "tool_actions",
        )
    }
    limits["counting_requests"] = limits["generation_requests"] * 2 + 2
    doc = Document(questions=catalog(), limits=limits, original_limits=limits.copy())
    doc.orientation_queue = [
        {"tool": "scrape_page", "url": url}
        for url in dict.fromkeys([state.inputs.url, state.inputs.product_url])
    ]
    doc.orientation_queue.append(
        {
            "tool": "find_site_pages",
            "url": state.inputs.url,
            "query": "",
            "limit": state.settings.map_limit,
        }
    )
    for url in state.inputs.source_urls:
        doc.sources[source_id(url)] = Source(
            source_id=source_id(url), requested_url=url
        )
    doc.sessions["orientation"] = Session(
        session_id="orientation", role="orientation", transcript_path=""
    )
    return doc


def load_document(directory: Path, state: Research) -> Document:
    if state.runtime_path is None:
        return new_document(state)
    return Document.model_validate(read_json(safe_path(directory, state.runtime_path)))


def event(doc: Document, kind: str, session_id: str = "", **details: JsonValue) -> None:
    doc.events.append(
        {
            "sequence": len(doc.events) + 1,
            "timestamp": now(),
            "kind": kind,
            "session_id": session_id,
            **details,
        }
    )


def projection(directory: Path, state: Research, doc: Document) -> None:
    summary = state.model_dump(
        mode="json", exclude={"messages", "events", "cache", "instructions"}
    )
    summary["usage"] = {
        **state.usage.model_dump(mode="json"),
        "generation_requests": doc.usage.get("generation_requests", 0),
        "input_tokens": doc.usage.get("input_tokens_used", 0),
        "output_tokens": doc.usage.get("output_tokens", 0),
        "active_seconds": doc.usage.get("active_seconds", 0),
        "reserved_tokens": doc.usage.get("reserved_total_tokens", 0),
    }
    summary["assessments"] = [
        {
            "question_id": q.question_id,
            "question": q.text,
            "status": q.answer_status,
            "answer": q.answer,
            "source_ids": list(q.source_ids),
        }
        for q in doc.questions.values()
    ]
    summary["completed_investigations"] = sum(
        a.status in {"completed", "partial", "failed"} for a in doc.assignments.values()
    )
    summary.update(
        {
            "system": doc.model_dump(mode="json", exclude={"operations", "events"}),
            "profile": {k: s.model_dump(mode="json") for k, s in doc.profile.items()},
            "sources": {k: s.model_dump(mode="json") for k, s in doc.sources.items()},
        }
    )
    save_json(
        directory / "profile.json",
        {k: s.model_dump(mode="json") for k, s in doc.profile.items()},
    )
    save_json(
        directory / "sources.json",
        {k: s.model_dump(mode="json") for k, s in doc.sources.items()},
    )
    save_json(
        directory / "question_index.json",
        {
            "version": doc.profile_version,
            "questions": {
                k: q.model_dump(mode="json") for k, q in doc.questions.items()
            },
        },
    )
    save_json(directory / "dashboard.json", summary)
    # The projection is replaceable; authoritative events remain in the revision artifact.
    content = "".join(
        json.dumps({"job_id": state.job_id, "phase": e.get("session_id", ""), **e})
        + "\n"
        for e in doc.events
    )
    target = directory / "events.jsonl"
    temporary = directory / "events.jsonl.tmp"
    temporary.write_text(content)
    temporary.replace(target)
    if doc.stage == "done" and not (directory / "result.json").exists():
        save_json(
            directory / "result.json",
            {
                "job_id": state.job_id,
                "schema_version": 3,
                "inputs": state.inputs.model_dump(mode="json"),
                "stop_reason": doc.stop_reason,
                "profile_version": doc.profile_version,
                "profile": {
                    k: v.model_dump(mode="json") for k, v in doc.profile.items()
                },
                "questions": {
                    k: q.model_dump(mode="json") for k, q in doc.questions.items()
                },
                "interview_backlog": interview_backlog(doc),
                "unincorporated_findings": [
                    a.model_dump(mode="json")
                    for a in doc.assignments.values()
                    if any(
                        not b.incorporated and a.assignment_id in b.assignment_ids
                        for b in doc.batches
                    )
                ],
                "interview_questions": [
                    row["question"] for row in interview_backlog(doc)
                ],
                "usage": doc.usage,
                "artifacts": {
                    "profile": "profile.json",
                    "questions": "question_index.json",
                    "calls": "calls/",
                    "orientation": "orientation.json",
                },
            },
        )


def persist(
    directory: Path,
    state: Research,
    doc: Document,
    saver: SqliteSaver,
    config: RunnableConfig,
) -> None:
    from agent_system.company_intelligence.workflow import build_research_workflow

    doc.revision += 1
    path = f"state/revision_{doc.revision:08d}.json"
    save_json(directory / path, doc.model_dump(mode="json"))
    state.runtime_path = path
    state.phase = (
        "orientation"
        if doc.stage == "orientation"
        else "investigation"
        if doc.stage == "batch"
        else "closing_review"
        if doc.stage in {"final_check", "done"}
        else "between"
    )
    state.last_activity = now()
    state.stop_reason = doc.stop_reason
    state.status = "completed" if doc.stage == "done" else "running"
    active = [s for s in doc.sessions.values() if s.status in {"ready", "running"}]
    if not active and doc.stage != "done":
        state.status = (
            "paused"
            if any(s.status == "paused" for s in doc.sessions.values())
            else "blocked_by_error"
        )
    graph = build_research_workflow(directory, saver)
    graph.update_state(config, {"research": state.model_dump(mode="json")})
    projection(directory, state, doc)


def tool_output(
    directory: Path, doc: Document, session: Session, value: JsonValue
) -> None:
    if session.action is not None and session.transcript_path:
        items = messages(directory, session)
        items.append(
            {
                "type": "function_call_output",
                "call_id": session.action.call_id,
                "output": json.dumps(value),
            }
        )
        save_messages(directory, session, items, doc.revision + 1)
    session.action = None
    session.counted_input = None
    session.status = "ready"


def action_reason(doc: Document, session: Session) -> tuple[str, str, str]:
    if session.action:
        return (
            session.action.reason,
            session.action.remaining_question,
            session.action.expected_value,
        )
    if session.session_id in doc.assignments:
        a = doc.assignments[session.session_id]
        return (
            a.rationale,
            "; ".join(
                doc.questions[q].missing or doc.questions[q].text
                for q in a.question_ids
            ),
            a.completion_criterion,
        )
    if session.role == "orientation":
        return (
            "Establish the company and product boundaries from complete overview pages",
            "Initial product understanding and 49 question assessments",
            "Create an evidence-grounded provisional profile",
        )
    if session.role == "quality":
        return (
            "Review the explicitly selected frozen batch",
            "Are sampled claims supported and correctly qualified?",
            "Identify concrete evidence-linked corrections without altering research",
        )
    return (
        "Incorporate saved batch results and resolve next research steps",
        "Outstanding missing facts in the question index",
        "Prioritize executable investigations or finalize supported coverage",
    )


def needs(
    doc: Document,
    session: Session,
    requirements: list[tuple[str, str, float]],
    action: str,
) -> bool:
    reason, remaining, value = action_reason(doc, session)
    return blocked(
        doc,
        session,
        requirements,
        action=action,
        reason=reason,
        remaining=remaining,
        value=value,
    )


def bump(doc: Document, session: Session, resource: str, amount: float = 1) -> None:
    doc.usage[resource] = doc.usage.get(resource, 0) + amount
    if session.session_id in doc.assignments and resource in {
        "generation_requests",
        "tool_actions",
        "pages",
    }:
        a = doc.assignments[session.session_id]
        a.usage[resource] = a.usage.get(resource, 0) + int(amount)


def requirements(
    doc: Document, session: Session, resource: str, amount: float
) -> list[tuple[str, str, float]]:
    result = [("run", resource, amount)]
    if (
        session.session_id in doc.assignments
        and resource in doc.assignments[session.session_id].limits
    ):
        result.append((session.session_id, resource, amount))
    return result


def reserve_operation(
    doc: Document,
    state: Research,
    directory: Path,
    session: Session,
    kind: str,
    args: dict[str, JsonValue],
    *,
    retry: OperationRecord | None = None,
) -> OperationRecord | None:
    req = [("run", "active_seconds", 0.001)]
    tokens = 0
    if kind == "generate":
        assert session.counted_input is not None
        tokens = session.counted_input + int(
            getattr(state.settings, f"{session.role}_output_tokens")
        )
        req += requirements(doc, session, "generation_requests", 1)
        req += requirements(doc, session, "tool_actions", 1)
        req += [
            ("run", "input_tokens", session.counted_input),
            ("run", "total_tokens", tokens),
        ]
    elif kind == "count_input":
        req += [("run", "counting_requests", 1)]
    else:
        req += [("run", "firecrawl_requests", 1)]
        if kind == "scrape_page":
            req += requirements(doc, session, "pages", 1)
    action_label = (
        f"Continue {session.role} with complete saved context ({session.counted_input} input tokens and {tokens - (session.counted_input or 0)} output ceiling); next external source is not yet selected"
        if kind == "generate"
        else f"Count the complete {session.role} request before dispatch"
        if kind == "count_input"
        else f"{kind}: {json.dumps(args)}"
    )
    if needs(doc, session, req, action_label):
        return None
    oid = retry.operation_id if retry else f"op_{len(doc.operations) + 1:05d}"
    attempt = retry.attempt + 1 if retry else 1
    record = OperationRecord(
        operation_id=oid,
        session_id=session.session_id,
        kind=kind,
        args=args,
        attempt=attempt,
        tokens=tokens,
        input_tokens=session.counted_input,
        page_reserved=kind == "scrape_page",
        receipt_path=f"operations/{oid}_{attempt}.json",
        waiters=retry.waiters.copy() if retry else [],
    )
    if kind in {"generate", "count_input"}:
        payload = request(doc, state, directory, session)
        supplied = provider_state(doc, state, directory, session)
        supplied.operation = Operation(
            operation_id=oid,
            kind=kind,
            attempts=attempt,
            reserved_tokens=tokens,
            counted_input=session.counted_input,
            reserved_seconds=state.settings.request_timeout(kind),
        )
        trace = start_trace(
            directory,
            supplied,
            counting_payload(payload) if kind == "count_input" else payload,
            "remote_count" if kind == "count_input" else "generation",
            False,
        )
        record.request_path = f"calls/{trace}/request.json"
        metadata = read_json(directory / "calls" / trace / "metadata.json")
        metadata.update(
            {
                "role": session.role,
                "effort": getattr(state.settings, f"{session.role}_effort"),
                "assignment_id": session.session_id
                if session.role == "investigator"
                else None,
                "session_id": session.session_id,
                "profile_version": doc.profile_version,
                "batch_id": doc.assignments[session.session_id].batch_id
                if session.role == "investigator"
                else None,
            }
        )
        save_json(directory / "calls" / trace / "metadata.json", metadata)
        bump(
            doc,
            session,
            "generation_requests" if kind == "generate" else "counting_requests",
        )
        bump(doc, session, "reserved_total_tokens", tokens)
        if kind == "generate":
            bump(doc, session, "tool_actions")
    else:
        bump(doc, session, "firecrawl_requests")
        if kind == "scrape_page":
            bump(doc, session, "reserved_pages")
    doc.operations[oid] = record
    session.operation_id = oid
    session.status = "running"
    if session.session_id in doc.reviews:
        doc.reviews[session.session_id].status = "running"
    event(
        doc,
        "operation_reserved",
        session.session_id,
        operation_id=oid,
        tool=kind,
        summary=f"Reserved {kind}",
        attempt=attempt,
    )
    return record


def provider_state(
    doc: Document, state: Research, directory: Path, session: Session
) -> Research:
    supplied = state.model_copy(deep=True)
    supplied.settings.model = getattr(state.settings, f"{session.role}_model")
    supplied.settings.effort = getattr(state.settings, f"{session.role}_effort")
    supplied.settings.output_tokens = getattr(
        state.settings, f"{session.role}_output_tokens"
    )
    phases: dict[str, Phase] = {
        "orientation": "orientation",
        "coordinator": "between",
        "investigator": "investigation",
        "quality": "review",
    }
    supplied.phase = phases[session.role]
    supplied.messages = messages(directory, session) if session.transcript_path else []
    supplied.sources = doc.sources.copy()
    supplied.source_deliveries = [
        {
            "source_id": sid,
            "cleaner_version": doc.sources[sid].cleaner_version,
            "content_hash": doc.sources[sid].cleaned_hash,
            "delivery": "full_page",
        }
        for sid in session.delivered
    ]
    return supplied


def dispatch(
    directory: Path,
    state: Research,
    supplied: Research,
    op: OperationRecord,
    providers: Providers | None,
) -> None:
    """Only isolated receipts/traces are written off the scheduler thread."""
    trace = f"{op.operation_id}_{op.attempt}"
    boundary = providers or LiveProviders(directory, trace)
    started = time.monotonic()
    result: dict[str, JsonValue]
    try:
        timeout = state.settings.request_timeout(op.kind)
        if op.kind == "count_input":
            result = {"input_tokens": boundary.count_input(supplied, timeout) + 1024}
        elif op.kind == "generate":
            result = boundary.generate(supplied, timeout).model_dump(mode="json")
        else:
            result = boundary.retrieve(op.kind, op.args, timeout).model_dump(
                mode="json"
            )
        receipt: dict[str, JsonValue] = {
            "result": result,
            "duration": time.monotonic() - started,
        }
    except ProviderFailure as exc:
        receipt = {
            "error": str(exc),
            "transient": exc.transient,
            "provider_capacity": exc.capacity,
            "duration": time.monotonic() - started,
        }
    except Exception as exc:
        receipt = {
            "error": f"{type(exc).__name__}: {exc}",
            "transient": False,
            "duration": time.monotonic() - started,
        }
    save_json(directory / op.receipt_path, receipt)


def save_capture(
    doc: Document, directory: Path, op: OperationRecord, fetched: FetchResult
) -> str:
    url = str(op.args["url"])
    sid = source_id(url)
    source = Source(
        source_id=sid,
        requested_url=url,
        final_url=public_url(fetched.final_url) if fetched.final_url else None,
        title=fetched.title,
        fetched_at=now(),
        filename=f"sources/{sid}.md",
        cleaner_version=CLEANER_VERSION,
        outcome="success",
    )
    if not any(
        v and v.strip() for v in (fetched.markdown, fetched.html, fetched.raw_html)
    ):
        raise ValueError(
            "Source returned no usable content; this is not evidence of absence"
        )
    write_capture(source_path(directory, source), fetched.markdown or "")
    for field, value, suffix in (
        ("html_filename", fetched.html, ".html"),
        ("raw_html_filename", fetched.raw_html, ".raw.html"),
    ):
        if value is not None:
            filename = f"sources/{sid}{suffix}"
            write_capture(artifact_path(directory, source, filename, {suffix}), value)
            setattr(source, field, filename)
    load_cleaned(directory, source)
    doc.sources[sid] = source
    links = discovery_links(directory, source)
    if links:
        doc.discovery = save_inventory(directory, links, sid, 5000)
    return sid


def consume_generation(
    doc: Document,
    state: Research,
    directory: Path,
    session: Session,
    response: Generation,
) -> Document:
    items = messages(directory, session) + response.output
    save_messages(directory, session, items, doc.revision + 1)
    calls = [v for v in response.output if v.get("type") == "function_call"]
    if response.status != "completed" or len(calls) != 1:
        raise ValueError("Expected one completed structured tool submission")
    call = calls[0]
    call_id, name, raw = (
        str(call.get("call_id", "")),
        str(call.get("name", "")),
        str(call.get("arguments", "")),
    )
    if not call_id:
        raise ValueError("Missing tool call ID")
    if name == "act" and session.role in {"coordinator", "investigator"}:
        action = Action.model_validate_json(raw)
        if action.tool not in LOCAL_TOOLS | REMOTE_TOOLS or (
            session.role == "coordinator" and action.tool in REMOTE_TOOLS
        ):
            raise ValueError("Tool is unavailable to this role")
        if action.findings:
            if session.role != "investigator":
                raise ValueError(
                    "Only investigator actions may record incremental findings"
                )
            record_findings(doc, session, action.findings, final=False)
        session.action = PendingAction(
            name=action.tool,
            args={
                **action.arguments.model_dump(mode="json", exclude_none=True),
                "_counted": True,
            },
            call_id=call_id,
            reason=action.reason,
            remaining_question=action.remaining_question,
            expected_value=action.expected_value,
        )
        session.status = "ready"
        return doc
    if name != "submit":
        raise ValueError("Unknown submission tool")
    if session.role == "orientation":
        submission = OrientationSubmission.model_validate_json(raw)
        oriented = state.model_copy(deep=True)
        oriented.sources = doc.sources
        apply_submission(oriented, submission, directory)
        doc.profile = oriented.profile
        doc.profile_version = 1
        for assessment in submission.assessments:
            q = doc.questions[assessment.question_id]
            q.answer, q.answer_status, q.source_ids = (
                assessment.answer,
                assessment.status,
                assessment.source_ids,
            )
            q.missing = (
                (assessment.interview_question or q.text)
                if assessment.status in {"partial", "unassessed"}
                else ""
            )
            plans = [p for p in submission.plan if p.question_id == q.question_id]
            if plans:
                q.importance = min(p.priority for p in plans)
                q.importance_reason = "; ".join(p.importance for p in plans)
            q.work_status = (
                "finished"
                if assessment.status in {"answered", "not_applicable"}
                else "pending"
            )
        session.status = "done"
        doc.stage = "done" if state.run_mode == "orientation_only" else "coordinate"
        doc.stop_reason = (
            "orientation_complete" if state.run_mode == "orientation_only" else None
        )
    elif session.role == "coordinator":
        proposal = CoordinatorSubmission.model_validate_json(raw)
        if doc.stage == "final_check" and proposal.assignments:
            raise ValueError("Final coverage check cannot launch another investigation")
        final = doc.stage == "final_check"
        updated = apply_proposal(doc, proposal, state.settings)
        updated.sessions[session.session_id].status = "done"
        if final:
            updated.final_check_done = True
            updated.stage = "done" if proposal.decision != "blocked" else "final_check"
        if proposal.decision == "blocked":
            updated.sessions[session.session_id].status = "paused"
        return updated
    elif session.role == "investigator":
        record_findings(
            doc, session, InvestigationResult.model_validate_json(raw), final=True
        )
    else:
        report = ReviewReport.model_validate_json(raw)
        review = doc.reviews[session.session_id]
        if set(report.source_ids) - set(review.source_ids) or set(
            report.question_ids
        ) - set(doc.questions):
            raise ValueError("Review references evidence outside the supplied snapshot")
        review.report, review.status = report, "completed"
        session.status = "done"
        save_json(
            directory / "reviews" / f"{review.review_id}.json",
            review.model_dump(mode="json"),
        )
    event(
        doc,
        "submission",
        session.session_id,
        summary="Saved structured research submission",
    )
    return doc


def settle_failure(doc: Document, session: Session, error: str) -> None:
    session.status = "failed"
    if session.session_id in doc.assignments:
        assignment = doc.assignments[session.session_id]
        assignment.status, assignment.error = "failed", error
    if session.session_id in doc.reviews:
        doc.reviews[session.session_id].status = "failed"
        doc.reviews[session.session_id].error = error
    event(doc, "error", session.session_id, summary=error)


def consume_operation(
    doc: Document, state: Research, directory: Path, op: OperationRecord
) -> Document:
    receipt = read_json(directory / op.receipt_path)
    session = doc.sessions[op.session_id]
    identifier = f"{op.operation_id}_{op.attempt}"
    error = receipt.get("error")
    if error:
        op.status = "failed"
        # Unknown model charges remain reserved; a retry reserves a fresh allowance.
        if op.kind == "scrape_page":
            doc.source_attempts.setdefault(source_id(str(op.args["url"])), []).append(
                str(error)
            )
            doc.usage["reserved_pages"] = max(0, doc.usage.get("reserved_pages", 0) - 1)
        finish_trace(
            directory,
            identifier,
            status="failed",
            error=error,
            duration=receipt.get("duration"),
        )
        if receipt.get("transient") and op.attempt < 2:
            session.status = "ready"
            event(
                doc,
                "retry",
                session.session_id,
                summary="One bounded transient retry remains",
            )
        else:
            if op.kind in REMOTE_TOOLS and session.role == "investigator":
                tool_output(
                    directory, doc, session, {"error": error, "exhausted": True}
                )
                session.operation_id = None
                op.status = "consumed"
            else:
                settle_failure(doc, session, str(error))
            for waiter in op.waiters:
                tool_output(
                    directory,
                    doc,
                    doc.sessions[waiter],
                    {"error": error, "exhausted": True},
                )
                doc.sessions[waiter].operation_id = None
        return doc
    result = receipt.get("result")
    if not isinstance(result, dict):
        raise ValueError("Invalid operation receipt")
    op.status = "consumed"
    session.operation_id = None
    session.status = "ready"
    if op.kind == "count_input":
        session.counted_input = int(str(result["input_tokens"]))
        finish_trace(
            directory,
            identifier,
            response=read_json(directory / "calls" / identifier / "response.json")
            if (directory / "calls" / identifier / "response.json").exists()
            else result,
            status="completed",
            application_status="accepted",
            duration=receipt.get("duration"),
        )
    elif op.kind == "generate":
        response = Generation.model_validate(result)
        if response.input_tokens is not None and response.output_tokens is not None:
            doc.usage["reserved_total_tokens"] = max(
                0, doc.usage.get("reserved_total_tokens", 0) - op.tokens
            )
            bump(
                doc,
                session,
                "total_tokens",
                response.input_tokens + response.output_tokens,
            )
            bump(doc, session, "input_tokens_used", response.input_tokens)
            bump(doc, session, "output_tokens", response.output_tokens)
        else:
            bump(doc, session, "unknown_requests")
        session.counted_input = None
        finish_trace(
            directory,
            identifier,
            response=response.raw_response or result,
            status=response.status,
            duration=receipt.get("duration"),
            usage={
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "reasoning_tokens": response.reasoning_tokens,
            },
        )
        try:
            doc = consume_generation(doc, state, directory, session, response)
            finish_trace(directory, identifier, application_status="accepted")
        except ValueError as exc:
            finish_trace(
                directory,
                identifier,
                application_status="rejected",
                validation_issue=str(exc),
            )
            # Quality is a single-generation review; invalid output is a recorded failure.
            if session.role == "quality" or session.correction_used:
                settle_failure(doc, session, str(exc))
            else:
                session.correction_used = True
                items = messages(directory, session)
                calls = [v for v in response.output if v.get("type") == "function_call"]
                for call in calls:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.get("call_id"),
                            "output": json.dumps({"error": str(exc)}),
                        }
                    )
                if not calls:
                    items.append({"role": "user", "content": str(exc)})
                save_messages(directory, session, items, doc.revision + 1)
                session.status = "ready"
                event(doc, "validation_error", session.session_id, summary=str(exc))
    else:
        fetched = FetchResult.model_validate(result)
        if fetched.credits is not None:
            bump(doc, session, "firecrawl_credits", fetched.credits)
        consumers = [session.session_id, *op.waiters]
        if op.kind == "scrape_page":
            doc.usage["reserved_pages"] = max(0, doc.usage.get("reserved_pages", 0) - 1)
            try:
                sid = save_capture(doc, directory, op, fetched)
                bump(doc, session, "pages")
                for owner in consumers:
                    target = doc.sessions[owner]
                    target.operation_id = None
                    if target.role == "orientation":
                        tool_output(
                            directory, doc, target, {"source_id": sid, "saved": True}
                        )
                    else:
                        tool_output(
                            directory,
                            doc,
                            target,
                            saved_action(
                                doc,
                                directory,
                                target,
                                "read_source",
                                {"source_id": sid},
                            ),
                        )
            except ValueError as exc:
                doc.source_attempts.setdefault(
                    source_id(str(op.args["url"])), []
                ).append(str(exc))
                for owner in consumers:
                    target = doc.sessions[owner]
                    target.operation_id = None
                    if target.role == "orientation":
                        settle_failure(doc, target, str(exc))
                    else:
                        tool_output(directory, doc, target, {"error": str(exc)})
        else:
            doc.discovery = save_inventory(
                directory, fetched.candidates, op.operation_id, state.settings.map_limit
            )
            tool_output(
                directory,
                doc,
                session,
                {
                    "discovery": doc.discovery,
                    "candidates": fetched.model_dump(mode="json")["candidates"],
                    "evidence": "Leads only; fetch before citing",
                },
            )
        if session.role == "orientation" and doc.orientation_queue:
            doc.orientation_queue.pop(0)
    event(
        doc,
        "operation_completed",
        session.session_id,
        operation_id=op.operation_id,
        tool=op.kind,
        attempt=op.attempt,
        summary=f"Saved {op.kind} outcome",
    )
    return doc


def schedule_session(
    doc: Document, state: Research, directory: Path, session: Session
) -> OperationRecord | None:
    if session.operation_id:
        previous = doc.operations[session.operation_id]
        if previous.status == "reserved":
            return previous
        if previous.status in {"failed", "unknown"} and previous.attempt < 2:
            return reserve_operation(
                doc,
                state,
                directory,
                session,
                previous.kind,
                previous.args,
                retry=previous,
            )
        return None
    if session.role == "orientation" and doc.orientation_queue:
        entry = doc.orientation_queue[0]
        session.action = PendingAction(
            name=str(entry["tool"]),
            args={k: v for k, v in entry.items() if k != "tool"},
            call_id="orientation_setup",
            reason="Read required overview and discover company sources",
            remaining_question="Company/product boundaries and initial missing facts",
            expected_value="Ground orientation in complete saved evidence",
        )
    if session.action:
        action = session.action
        # Count each submitted action once, even when its dispatch later needs approval.
        if not action.args.get("_counted"):
            if needs(
                doc, session, requirements(doc, session, "tool_actions", 1), action.name
            ):
                return None
            bump(doc, session, "tool_actions")
            action.args["_counted"] = True
        args = {k: v for k, v in action.args.items() if k != "_counted"}
        if action.name in LOCAL_TOOLS:
            try:
                value = saved_action(doc, directory, session, action.name, args)
            except ValueError as exc:
                value = {"error": str(exc)}
            tool_output(directory, doc, session, value)
            return None
        if "url" in args:
            args["url"] = public_url(str(args["url"]))
        if action.name == "scrape_page":
            sid = source_id(str(args["url"]))
            if sid in doc.sources and doc.sources[sid].outcome == "success":
                value = (
                    saved_action(
                        doc, directory, session, "read_source", {"source_id": sid}
                    )
                    if session.role != "orientation"
                    else {"source_id": sid, "saved": True}
                )
                tool_output(directory, doc, session, value)
                if session.role == "orientation":
                    doc.orientation_queue.pop(0)
                return None
            shared = next(
                (
                    o
                    for o in doc.operations.values()
                    if o.kind == "scrape_page"
                    and o.args.get("url") == args["url"]
                    and o.status in {"reserved", "dispatched"}
                ),
                None,
            )
            if shared:
                shared.waiters.append(session.session_id)
                session.operation_id, session.status = shared.operation_id, "running"
                return None
            if (
                doc.source_attempts.get(sid)
                and not str(args.get("reopen_reason", "")).strip()
            ):
                tool_output(
                    directory,
                    doc,
                    session,
                    {
                        "error": "Source attempts exhausted; a new reason is required",
                        "attempts": list(doc.source_attempts[sid]),
                    },
                )
                return None
        return reserve_operation(doc, state, directory, session, action.name, args)
    if not session.transcript_path:
        start_context(doc, state, directory, session)
    return reserve_operation(
        doc,
        state,
        directory,
        session,
        "count_input" if session.counted_input is None else "generate",
        {},
    )


def advance(doc: Document, state: Research, directory: Path) -> None:
    if doc.finish_requested and doc.stage != "done":
        if any(o.status in {"reserved", "dispatched"} for o in doc.operations.values()):
            return
        for a in doc.assignments.values():
            if a.status in {"pending", "running", "paused"}:
                a.status = "partial"
        for q in doc.questions.values():
            if q.answer_status in {"unassessed", "partial"}:
                q.unresolved_reason = "Budget/user stop before resolution"
                q.work_status = "finished"
        for s in doc.sessions.values():
            if s.role != "quality":
                s.status = "done"
        for approval in doc.approvals:
            if approval.status == "pending":
                approval.status = "cancelled"
        doc.stage, doc.stop_reason = "done", "user_ended_partial"
        return
    if doc.stage == "batch":
        batch = doc.batches[-1]
        if all(
            doc.assignments[a].status in {"completed", "partial", "failed"}
            for a in batch.assignment_ids
        ):
            doc.stage = "coordinate"
        else:
            active = sum(
                a.launched and a.status in {"running", "paused"}
                for a in doc.assignments.values()
            )
            for aid in batch.assignment_ids:
                a = doc.assignments[aid]
                if (
                    a.launched
                    or a.status not in {"pending", "running"}
                    or active >= state.settings.concurrent_investigators
                ):
                    continue
                session = doc.sessions.setdefault(
                    aid,
                    Session(session_id=aid, role="investigator", transcript_path=""),
                )
                if needs(
                    doc, session, [("run", "investigations", 1)], f"Launch {a.topic}"
                ):
                    continue
                bump(doc, session, "investigations")
                a.status = "running"
                a.launched = True
                active += 1
                event(doc, "assignment_started", aid, summary=a.objective)
    if doc.stage in {"coordinate", "final_check"}:
        current = [
            s
            for s in doc.sessions.values()
            if s.role == "coordinator"
            and s.status in {"ready", "running", "paused", "failed"}
        ]
        if not current:
            sid = f"coordinator_{sum(s.role == 'coordinator' for s in doc.sessions.values()) + 1:04d}"
            doc.sessions[sid] = Session(
                session_id=sid, role="coordinator", transcript_path=""
            )


def quality_preview(
    doc: Document, state: Research, batch_id: str | None = None
) -> dict[str, JsonValue]:
    eligible = [
        b
        for b in doc.batches
        if all(
            doc.assignments[a].status in {"completed", "partial", "failed"}
            for a in b.assignment_ids
        )
        and any(doc.assignments[a].status == "completed" for a in b.assignment_ids)
    ]
    if doc.stage != "done" or not eligible:
        raise ValueError(
            "Quality review requires finalized research and a completed batch"
        )
    if batch_id:
        batch = next((b for b in eligible if b.batch_id == batch_id), None)
        if batch is None:
            raise ValueError("Selected batch is not eligible")
    else:
        batch = min(
            eligible,
            key=lambda b: (
                min(
                    doc.questions[q].importance
                    for a in b.assignment_ids
                    for q in doc.assignments[a].question_ids
                ),
                b.batch_id,
            ),
        )
    sources = sorted(
        {sid for aid in batch.assignment_ids for sid in doc.assignments[aid].sources}
    )
    return {
        "batch_id": batch.batch_id,
        "question_ids": list(
            sorted(
                {
                    q
                    for a in batch.assignment_ids
                    for q in doc.assignments[a].question_ids
                }
            )
        ),
        "source_ids": list(sources),
        "model": state.settings.quality_model,
        "effort": state.settings.quality_effort,
        "output_allowance": state.settings.quality_output_tokens,
        "input_allowance": doc.limits["input_tokens"],
        "generation_attempts": 1,
        "transient_retries": 1,
    }


def apply_commands(doc: Document, state: Research, directory: Path) -> None:
    with inbox(directory) as connection:
        rows = connection.execute(
            "SELECT id,payload FROM commands WHERE outcome IS NULL ORDER BY rowid"
        ).fetchall()
        for cid, payload in rows:
            if cid in doc.applied_commands:
                connection.execute(
                    "UPDATE commands SET outcome=? WHERE id=?",
                    (doc.applied_commands[cid], cid),
                )
                continue
            command = UserCommand.model_validate_json(payload)
            try:
                if command.job_id != state.job_id:
                    raise ValueError("Wrong job")
                if (
                    command.kind in {"finish", "quality"}
                    and command.expected_revision != doc.revision
                ):
                    raise ValueError("Stale run revision; refresh before submitting")
                if command.kind in {"approve", "decline"}:
                    decide(doc, command)
                elif command.kind == "finish":
                    if doc.stage == "done":
                        raise ValueError("Research already finalized")
                    doc.finish_requested = True
                else:
                    preview = quality_preview(doc, state, command.batch_id)
                    rid = f"quality_{len(doc.reviews) + 1:04d}"
                    path = f"reviews/{rid}_snapshot.json"
                    save_json(
                        directory / path,
                        {
                            "profile_version": doc.profile_version,
                            "profile": {
                                k: v.model_dump(mode="json")
                                for k, v in doc.profile.items()
                            },
                            "questions": {
                                k: q.model_dump(mode="json")
                                for k, q in doc.questions.items()
                            },
                            "batch": [
                                a.model_dump(mode="json")
                                for a in doc.assignments.values()
                                if a.batch_id == preview["batch_id"]
                            ],
                            "scope": preview,
                        },
                    )
                    doc.reviews[rid] = Review(
                        review_id=rid,
                        batch_id=str(preview["batch_id"]),
                        snapshot_path=path,
                        source_ids=[str(s) for s in preview["source_ids"]]
                        if isinstance(preview["source_ids"], list)
                        else [],
                    )
                    doc.sessions[rid] = Session(
                        session_id=rid, role="quality", transcript_path=""
                    )
                doc.applied_commands[cid] = "applied"
            except ValueError as exc:
                doc.applied_commands[cid] = f"rejected: {exc}"
            # Acknowledgements are copied to the inbox only after this decision is checkpointed.
            event(
                doc, "user_command", summary=doc.applied_commands[cid], command_id=cid
            )


def run(
    data_dir: Path,
    job_id: str,
    initial: Research | None,
    providers: Providers | None,
    *,
    max_steps: int | None = None,
) -> Research:
    from agent_system.company_intelligence.workflow import load_research

    directory = data_dir / "jobs" / job_id
    config: RunnableConfig = {"configurable": {"thread_id": job_id}}
    with (
        job_lock(directory),
        SqliteSaver.from_conn_string(str(data_dir / "checkpoints.sqlite")) as saver,
        ThreadPoolExecutor(max_workers=16) as pool,
    ):
        state = initial or load_research(saver, directory, job_id)
        if state is None:
            raise ValueError("Research job not found")
        doc = load_document(directory, state)
        futures: dict[str, Future[None]] = {}
        recovered = False
        for op in list(doc.operations.values()):
            if op.status in {"reserved", "dispatched"}:
                recovered = True
                if (directory / op.receipt_path).exists():
                    doc = consume_operation(doc, state, directory, op)
                elif op.status == "dispatched":
                    op.status = "unknown"
                    event(
                        doc,
                        "uncertain_call",
                        op.session_id,
                        summary="No receipt after restart; conservative reservation retained",
                    )
                    if op.attempt >= 2:
                        settle_failure(
                            doc,
                            doc.sessions[op.session_id],
                            "Uncertain operation exhausted its attempt allowance",
                        )
                    else:
                        doc.sessions[op.session_id].status = "ready"
                else:
                    doc.sessions[op.session_id].status = "ready"
        if recovered:
            persist(directory, state, doc, saver, config)
        steps = 0
        last_tick = time.monotonic()
        last_heartbeat = 0.0
        try:
            while True:
                before_document = doc.model_dump_json(exclude={"revision"})
                current = time.monotonic()
                active = bool(futures) or any(
                    s.status == "ready" for s in doc.sessions.values()
                )
                if active:
                    doc.usage["active_seconds"] = (
                        doc.usage.get("active_seconds", 0) + current - last_tick
                    )
                last_tick = current
                if current - last_heartbeat >= 1:
                    save_json(
                        directory / "worker.json",
                        {"heartbeat": now(), "active": active, "online": True},
                    )
                    last_heartbeat = current
                apply_commands(doc, state, directory)
                for oid, future in list(futures.items()):
                    if future.done():
                        future.result()
                        doc = consume_operation(
                            doc, state, directory, doc.operations[oid]
                        )
                        del futures[oid]
                advance(doc, state, directory)
                ready: list[OperationRecord] = []
                if not doc.finish_requested or doc.stage == "done":
                    for session in list(doc.sessions.values()):
                        if session.status != "ready":
                            continue
                        if (
                            session.role == "investigator"
                            and doc.assignments[session.session_id].status != "running"
                        ):
                            continue
                        try:
                            scheduled = schedule_session(doc, state, directory, session)
                            if scheduled:
                                ready.append(scheduled)
                        except ValueError as exc:
                            settle_failure(doc, session, str(exc))
                changed = doc.model_dump_json(exclude={"revision"}) != before_document
                if changed or ready:
                    persist(directory, state, doc, saver, config)
                # A dispatch marker is durable before the external call can begin.
                for op in ready:
                    op.status = "dispatched"
                if ready:
                    persist(directory, state, doc, saver, config)
                    for op in ready:
                        session = doc.sessions[op.session_id]
                        supplied = provider_state(doc, state, directory, session)
                        supplied.operation = Operation(
                            operation_id=op.operation_id,
                            kind=op.kind,
                            attempts=op.attempt,
                        )
                        if providers and op.request_path:
                            path = (
                                directory
                                / "calls"
                                / f"{op.operation_id}_{op.attempt}"
                                / "metadata.json"
                            )
                            meta = read_json(path)
                            meta["simulated"] = True
                            save_json(path, meta)
                        futures[op.operation_id] = pool.submit(
                            dispatch,
                            directory,
                            state,
                            supplied,
                            op.model_copy(deep=True),
                            providers,
                        )
                steps += 1
                if max_steps is not None and steps >= max_steps:
                    break
                actionable = any(
                    s.status in {"ready", "running"} for s in doc.sessions.values()
                )
                if not futures and not actionable:
                    if (
                        doc.stage == "done"
                        or any(s.status == "failed" for s in doc.sessions.values())
                        or providers is not None
                    ):
                        break
                    time.sleep(0.2)  # Local command wait; excluded from active runtime.
                else:
                    time.sleep(0.1)
        except KeyboardInterrupt:
            state.status = "interrupted"
            event(
                doc,
                "interrupted",
                summary="Worker stopped; completed receipts remain recoverable",
            )
            persist(directory, state, doc, saver, config)
        finally:
            save_json(
                directory / "worker.json",
                {"heartbeat": now(), "active": False, "online": False},
            )
        return state
