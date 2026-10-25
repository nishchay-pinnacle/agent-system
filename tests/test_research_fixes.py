"""Mechanical research regressions, using only fake providers and local evidence."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from research_script import URL, orientation_submission
from test_coordinated_research import FakeProviders, initial, proposal, spec

from agent_system.company_intelligence.agent import FetchResult, Generation
from agent_system.company_intelligence.artifacts import read_json
from agent_system.company_intelligence.contexts import (
    definitions,
    messages,
    start_context,
)
from agent_system.company_intelligence.contracts import (
    CoordinationResult,
    EvidenceFinding,
    FindingsDelta,
    OrientationResult,
    QuestionRevision,
    ReadSource,
)
from agent_system.company_intelligence.coordination import (
    Assignment,
    Batch,
    Document,
    Finding,
    Session,
    Source,
)
from agent_system.company_intelligence.execution import (
    consume_generation,
    new_document,
    quality_preview,
    save_capture,
    settle_failure,
)
from agent_system.company_intelligence.findings import (
    SubmissionError,
    accept_delta,
    archive_findings,
    hydrate,
    incorporate,
    reconcile_work,
)
from agent_system.company_intelligence.handoff import accept_orientation
from agent_system.company_intelligence.investigation import saved_action
from agent_system.company_intelligence.page_content import (
    CLEANER_VERSION,
    clean_new,
    cleaned_path,
    digest,
    load_cleaned,
)
from agent_system.company_intelligence.retrieval import (
    SourceLimitation,
    evidence_problem,
)
from agent_system.company_intelligence.schemas import (
    ResearchSettings,
)
from agent_system.company_intelligence.tools import public_url, source_id

SID = source_id(URL)
CONTENT = (
    "# Complete evidence\n"
    + "A qualification that must survive.\n" * 250
    + "LAST FACT\n"
)


def setup_doc(tmp_path: Path) -> tuple[Document, Session]:
    state = initial()
    doc = new_document(state)
    doc.orientation_queue = []
    source = Source(
        source_id=SID,
        requested_url=URL,
        outcome="success",
        filename=f"sources/{SID}.md",
        cleaner_version=CLEANER_VERSION,
    )
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / source.filename).write_text(CONTENT)
    clean_new(tmp_path, source, CONTENT)
    source.cleaned_filename = f"sources/{SID}.cleaned.v{CLEANER_VERSION}.md"
    cleaned_path(tmp_path, source).write_text(CONTENT)
    source.cleaned_hash = digest(CONTENT)
    doc.sources[SID] = source
    assignment = Assignment(
        **spec(["S1", "S2"]),
        assignment_id="assignment_0001",
        batch_id="batch_0001",
        profile_version=0,
        status="running",
        sources=[SID],
    )
    doc.assignments[assignment.assignment_id] = assignment
    session = Session(
        session_id=assignment.assignment_id,
        role="investigator",
        transcript_path="",
        contract_version=2,
    )
    doc.sessions[session.session_id] = session
    return doc, session


def candidate(qid: str = "S1", **changes: object) -> EvidenceFinding:
    return EvidenceFinding.model_validate(
        {
            "question_id": qid,
            "answer": "Conditional deployment capability",
            "status": "partial",
            "missing": "Which deployment conditions apply?",
            "source_ids": [SID],
            "qualifications": "Only with hardware attestation",
            **changes,
        }
    )


def coordination(doc: Document, disposition: str = "deferred") -> CoordinationResult:
    return CoordinationResult.model_validate(
        {
            "changes": [],
            "profile_changes": [],
            "dispositions": [
                {
                    "finding_id": fid,
                    "disposition": disposition,
                    "reason": "Keep the specific missing deployment condition visible",
                    "reference_ids": [],
                }
                for a in doc.assignments.values()
                for fid in a.accepted_finding_ids
            ],
            "proposal": proposal(),
        }
    )


def test_orientation_optional_proposals_and_immutable_provenance(
    tmp_path: Path,
) -> None:
    doc, _ = setup_doc(tmp_path)
    session = doc.sessions["orientation"]
    state = initial()
    start_context(doc, state, tmp_path, session)
    payload = orientation_submission()
    payload["plan"] = []
    for a in payload["assessments"]:
        a["missing"] = "The supported hardware/version boundary needs confirmation"
    result = OrientationResult.model_validate(payload)
    accept_orientation(doc, state, tmp_path, session, result, "op_00001_1")
    saved = (tmp_path / "orientation.json").read_bytes()
    snapshot = json.loads(saved)
    assert len(snapshot["assessments"]) == 49 and len(snapshot["profile"]) == 8
    assert snapshot["sources"][0]["content_hash"] == digest(CONTENT)
    assert snapshot["accepted_call"] == "op_00001_1"
    assert (
        doc.questions["S1"].missing
        == "The supported hardware/version boundary needs confirmation"
    )
    assert snapshot["plan"] == []
    with pytest.raises(RuntimeError, match="Immutable"):
        accept_orientation(doc, state, tmp_path, session, result, "op_99999_1")
    assert (tmp_path / "orientation.json").read_bytes() == saved
    invalid = result.model_copy(deep=True)
    invalid.assessments[0].missing = ""
    with pytest.raises(SubmissionError):
        accept_orientation(doc, state, tmp_path, session, invalid, "op_00001_1")


def test_every_finding_requires_disposition_and_revisions_preserve_support(
    tmp_path: Path,
) -> None:
    doc, session = setup_doc(tmp_path)
    ids = accept_delta(
        doc, session, FindingsDelta(findings=[candidate()]), "op_00002_1"
    )
    doc.assignments[session.session_id].status = "partial"
    doc.batches = [Batch(batch_id="batch_0001", assignment_ids=[session.session_id])]
    empty = coordination(doc).model_copy(update={"dispositions": []})
    with pytest.raises(SubmissionError, match="Every pending finding"):
        incorporate(doc, empty, ResearchSettings())
    assert not doc.batches[0].incorporated
    accepted = coordination(doc, "incorporated")
    accepted.changes = [
        QuestionRevision(
            **candidate().model_dump(),
            expected_revision=0,
            supporting_finding_ids=ids,
            revision_reason="Incorporate the qualified observation",
        )
    ]
    updated = incorporate(doc, accepted, ResearchSettings())
    assert updated.batches[0].incorporated
    assert updated.questions["S1"].qualifications == "Only with hardware attestation"
    assert updated.questions["S1"].finding_ids == ids
    assert len(updated.questions["S1"].history[0]) < 250
    archive_findings(tmp_path, updated)
    restored = hydrate(
        tmp_path,
        Document.model_validate(updated.model_dump(mode="json", exclude={"findings"})),
    )
    assert (
        restored.findings[ids[0]].content.qualifications
        == updated.questions["S1"].qualifications
    )
    deferred = incorporate(doc, coordination(doc), ResearchSettings())
    assert deferred.dispositions[ids[0]]["disposition"] == "deferred"
    assert deferred.questions["S1"].answer == doc.questions["S1"].answer


def test_inherited_evidence_is_not_a_direct_source_read(tmp_path: Path) -> None:
    doc, session = setup_doc(tmp_path)
    ids = accept_delta(
        doc, session, FindingsDelta(findings=[candidate()]), "op_00002_1"
    )
    other = Assignment(
        **spec(["S1"]),
        assignment_id="assignment_0002",
        batch_id="batch_0002",
        profile_version=0,
    )
    doc.assignments[other.assignment_id] = other
    inheritor = Session(
        session_id=other.assignment_id,
        role="investigator",
        transcript_path="",
        contract_version=2,
    )
    with pytest.raises(SubmissionError, match="Direct claims"):
        accept_delta(
            doc, inheritor, FindingsDelta(findings=[candidate()]), "op_00003_1"
        )
    saved_action(doc, tmp_path, inheritor, "read_findings", {"reference_id": ids[0]})
    inherited = candidate(source_ids=[], inherited_finding_ids=ids)
    accepted = accept_delta(
        doc, inheritor, FindingsDelta(findings=[inherited]), "op_00003_1"
    )
    assert not other.sources and not inheritor.delivered
    assert doc.findings[accepted[0]].source_lineage[0]["cleaned_hash"] == digest(
        CONTENT
    )
    assert (
        accept_delta(doc, inheritor, FindingsDelta(findings=[inherited]), "op_00003_1")
        == accepted
    )


def test_rejected_final_never_replaces_partial_output(tmp_path: Path) -> None:
    doc, session = setup_doc(tmp_path)
    start_context(doc, initial(), tmp_path, session)
    ids = accept_delta(
        doc, session, FindingsDelta(findings=[candidate()]), "op_00001_1"
    )
    response = Generation(
        output=[
            {
                "type": "function_call",
                "call_id": "test",
                "name": "submit",
                "arguments": json.dumps(
                    {
                        "findings": [candidate().model_dump()],
                        "assumptions": [],
                        "incidental_findings": [],
                        "new_questions": [],
                        "attempted_approaches": [],
                        "next_sources": [],
                        "remaining_uncertainty": "conditions",
                        "outcome": "partial",
                    }
                ),
            }
        ]
    )
    with pytest.raises(SubmissionError, match="every assigned question"):
        consume_generation(doc, initial(), tmp_path, session, response, "op_00002_1")
    assignment = doc.assignments[session.session_id]
    assert assignment.accepted_finding_ids == ids and assignment.result is None
    assert assignment.status == "running"
    settle_failure(doc, session, "Unexpected remote generation error")
    reconcile_work(doc)
    assert assignment.accepted_finding_ids == ids
    assert doc.questions["S1"].work_status == "pending"


def test_exact_role_tools_and_hash_sensitive_repeat_reads(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        ReadSource.model_validate(
            {
                "source_id": SID,
                "url": URL,
                "reason": "read",
                "remaining_question": "condition",
                "expected_value": "evidence",
            }
        )
    coordinator = {t["name"] for t in definitions("coordinator", 2)}
    assert "scrape_page" not in coordinator and "read_source" in coordinator
    doc, session = setup_doc(tmp_path)
    assert (
        saved_action(doc, tmp_path, session, "read_source", {"source_id": SID})[
            "content"
        ]
        == CONTENT
    )
    assert saved_action(doc, tmp_path, session, "read_source", {"source_id": SID})[
        "already_in_context"
    ]
    session.delivered_hashes[SID] = "a different historical capture"
    assert (
        saved_action(doc, tmp_path, session, "read_source", {"source_id": SID})[
            "content"
        ]
        == CONTENT
    )


def test_expected_error_captures_and_pdf_leads(tmp_path: Path) -> None:
    assert public_url("https://example.com/manual.pdf").endswith(".pdf")
    assert evidence_problem(
        FetchResult(markdown="# 404\nUseful site navigation", status_code=404)
    )
    assert evidence_problem(
        FetchResult(
            html="<title>404 Not Found</title><main><h1>404 Not Found</h1><nav>Products Docs</nav></main>"
        )
    )
    assert (
        evidence_problem(
            FetchResult(
                html="<article><h1>Understanding HTTP 404</h1><p>Keep this substantial error-handling example.</p></article>"
            )
        )
        is None
    )
    doc, _ = setup_doc(tmp_path)
    from agent_system.company_intelligence.coordination import OperationRecord

    op = OperationRecord(
        operation_id="op_00010",
        session_id="orientation",
        kind="scrape_page",
        args={"url": URL},
        receipt_path="operations/op_00010_1.json",
    )
    with pytest.raises(SourceLimitation):
        save_capture(
            doc, tmp_path, op, FetchResult(markdown="Access denied", status_code=403)
        )
    assert doc.sources[SID].outcome == "failed"
    assert doc.sources[SID].capture_metadata["receipt"] == op.receipt_path


def test_partial_batch_review_and_failure_continuation(tmp_path: Path) -> None:
    doc, session = setup_doc(tmp_path)
    accept_delta(doc, session, FindingsDelta(findings=[candidate()]), "op_00001_1")
    assignment = doc.assignments[session.session_id]
    assignment.status = "partial"
    doc.batches = [Batch(batch_id="batch_0001", assignment_ids=[session.session_id])]
    doc.stage = "done"
    assert quality_preview(doc, initial())["batch_id"] == "batch_0001"
    assignment.accepted_finding_ids = []
    assignment.status = "failed"
    with pytest.raises(ValueError, match="Quality review requires"):
        quality_preview(doc, initial())
    doc.stage = "batch"
    session.status = "failed"
    from agent_system.company_intelligence.execution import advance

    advance(doc, initial(), tmp_path)
    assert doc.stage == "coordinate" and any(
        s.role == "coordinator" and s.status == "ready" for s in doc.sessions.values()
    )


def test_initial_parallel_briefing_has_orientation_proposals(tmp_path: Path) -> None:
    doc, _ = setup_doc(tmp_path)
    doc.assignments.clear()
    doc.orientation_path = "orientation.json"
    from agent_system.company_intelligence.artifacts import save_json

    save_json(
        tmp_path / "orientation.json",
        {
            "plan": [
                {
                    "priority": 1,
                    "source_strategy": "Search hardware requirements",
                    "resolution_criterion": "Specific supported version",
                }
            ],
            "contradictions": [],
            "discovery": {"discovered_count": 30},
            "inventory": "site_index.json",
        },
    )
    session = Session(
        session_id="coordinator_0001",
        role="coordinator",
        transcript_path="",
        contract_version=2,
    )
    start_context(doc, initial(), tmp_path, session)
    payload = json.loads(messages(tmp_path, session)[0]["content"])
    assert payload["parallel_work"]["available_slots_at_next_boundary"] == 3
    assert payload["orientation_handoff"]["plan"][0]["priority"] == 1
    assert (
        "No minimum" not in payload["instruction"]
    )  # Existing rationale explains useful smaller batches.


def test_legacy_session_contract_and_pending_receipt_are_preserved(
    tmp_path: Path,
) -> None:
    from agent_system.company_intelligence.coordination import OperationRecord
    from agent_system.company_intelligence.findings import adapt_legacy

    doc, session = setup_doc(tmp_path)
    session.contract_version = 1
    doc.storage_version = 1
    session.operation_id = "op_00010"
    doc.operations["op_00010"] = OperationRecord(
        operation_id="op_00010",
        session_id=session.session_id,
        kind="generate",
        args={},
        receipt_path="operations/op_00010_1.json",
    )
    original = doc.sessions[session.session_id].model_dump()
    adapted = adapt_legacy(tmp_path, doc)
    assert adapted.sessions[session.session_id].model_dump() == original
    assert adapted.operations["op_00010"].contract_version == 1
    assert "act" in {
        tool["name"] for tool in definitions("investigator", session.contract_version)
    }
    assert "act" not in {tool["name"] for tool in definitions("investigator", 2)}


def test_known_pdf_never_dispatches_or_consumes_provider_allowance(
    tmp_path: Path,
) -> None:
    from agent_system.company_intelligence.coordination import PendingAction
    from agent_system.company_intelligence.execution import schedule_session

    doc, session = setup_doc(tmp_path)
    start_context(doc, initial(), tmp_path, session)
    session.action = PendingAction(
        name="scrape_page",
        args={"url": "https://example.com/manual.pdf"},
        call_id="pdf-call",
        reason="Read hardware prerequisites",
        remaining_question="Hardware requirements",
        expected_value="Resolve supported platforms",
    )
    assert schedule_session(doc, initial(), tmp_path, session) is None
    assert doc.usage.get("firecrawl_requests", 0) == 0
    assert doc.source_attempts[source_id("https://example.com/manual.pdf")]
    assert "unsupported_format" in str(messages(tmp_path, session))


def test_storage_failure_is_not_an_expected_source_limitation(tmp_path: Path) -> None:
    from agent_system.company_intelligence.coordination import PendingAction
    from agent_system.company_intelligence.execution import schedule_session

    doc, session = setup_doc(tmp_path)
    start_context(doc, initial(), tmp_path, session)
    cleaned_path(tmp_path, doc.sources[SID]).write_bytes(b"\xff")
    session.action = PendingAction(
        name="read_source",
        args={"source_id": SID},
        call_id="read-call",
        reason="Read source",
        remaining_question="Requirements",
        expected_value="Evidence",
    )
    with pytest.raises(UnicodeDecodeError):
        schedule_session(doc, initial(), tmp_path, session)


def test_compact_context_keeps_understanding_and_complete_pages(tmp_path: Path) -> None:
    from agent_system.company_intelligence.contexts import start_context_v1
    from agent_system.company_intelligence.coordination import InvestigationResult

    doc, session = setup_doc(tmp_path)
    assignment = doc.assignments[session.session_id]
    findings = [candidate("S1"), candidate("S2")]
    accept_delta(doc, session, FindingsDelta(findings=findings), "op_00001_1")
    old_result = InvestigationResult(
        findings=[
            Finding.model_validate(
                f.model_dump(exclude={"inherited_finding_ids", "supersedes"})
            )
            for f in findings
        ],
        assumptions=[],
        incidental_findings=["Historical report detail. " * 1500],
        new_questions=[],
        attempted_approaches=["Read complete documentation"],
        next_sources=[],
        remaining_uncertainty="Deployment prerequisites",
        outcome="partial",
    )
    assignment.result = old_result
    for qid in assignment.question_ids:
        doc.questions[qid].history = [old_result.model_dump_json()] * 3
    start_context_v1(doc, initial(), tmp_path, session)
    before = len(json.dumps(messages(tmp_path, session)).encode())
    # Different immutable context revisions, same explicit understanding/evidence.
    doc.revision += 1
    start_context(doc, initial(), tmp_path, session)
    after = len(json.dumps(messages(tmp_path, session)).encode())
    payload = json.loads(messages(tmp_path, session)[0]["content"])
    assert after < before / 2
    assert len(payload["findings_history"]) == 2
    page = saved_action(doc, tmp_path, session, "read_source", {"source_id": SID})
    assert page["content"] == CONTENT and digest(page["content"]) == digest(CONTENT)
    print(
        f"OFFLINE_CONTEXT_BYTES before={before} after={after} complete_page_bytes={len(CONTENT.encode())}"
    )


def test_long_running_call_does_not_checkpoint_each_timing_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time as real_time

    from agent_system.company_intelligence import execution, timing

    ticks = [0.0]
    entered = threading.Event()

    def sleep(seconds: float) -> None:
        ticks[0] += 1.0
        real_time.sleep(0.001)

    fake_time = SimpleNamespace(monotonic=lambda: ticks[0], sleep=sleep)

    class Slow(FakeProviders):
        def retrieve(
            self, name: str, args: dict[str, object], timeout: float
        ) -> FetchResult:
            entered.set()
            while ticks[0] < 59:
                real_time.sleep(0.001)
            return FetchResult(markdown=CONTENT, final_url=str(args.get("url", URL)))

    monkeypatch.setattr(execution, "time", fake_time)
    monkeypatch.setattr(timing, "time", fake_time)
    state = initial()
    execution.run(tmp_path, state.job_id, state, Slow(), max_steps=60)
    directory = tmp_path / "jobs" / state.job_id
    revisions = list((directory / "state").glob("*.json"))
    assert entered.is_set()
    assert len(revisions) <= 4
    assert read_json(directory / "timing.json")["total_seconds"] >= 59
    actual_bytes = sum(p.stat().st_size for p in revisions)
    # Prior loop checkpointed the same meaningful state on each timing tick.
    before_bytes = 60 * max(p.stat().st_size for p in revisions)
    assert actual_bytes < before_bytes / 5
    print(
        f"OFFLINE_STORAGE snapshots_before=60 snapshots_after={len(revisions)} bytes_before={before_bytes} bytes_after={actual_bytes}"
    )


def test_documentation_controls_removed_without_substantive_loss() -> None:
    from agent_system.company_intelligence.html_cleaning import clean_html

    fixture = Path(__file__).parent / "fixtures" / "documentation_controls.html"
    cleaned = clean_html(fixture.read_text(), URL, SID)
    assert "CTRL+K" not in cleaned.content and "Use ↑/↓" not in cleaned.content
    assert "System theme" not in cleaned.content and "Copy link" not in cleaned.content
    for expected in (
        "Intel TDX",
        "AMD SEV-SNP",
        "measurements must match",
        "curl",
        "Attestation required",
        "Theme options in an application",
        "Supported platform limitations",
    ):
        assert expected in cleaned.content
    assert any("compute-nodes" in d["url"] for d in cleaned.discovery)
    other = clean_html(
        (fixture.parent / "documentation_category.html").read_text(), URL, SID
    )
    for expected in (
        "404 errors",
        "Linux prerequisites",
        "Azure VM",
        "keyboard shortcuts",
        "Enterprise only",
    ):
        assert expected in other.content


def test_pending_legacy_action_is_consumed_by_legacy_validator(tmp_path: Path) -> None:
    from agent_system.company_intelligence.contexts import start_context_v1

    doc, session = setup_doc(tmp_path)
    session.contract_version = 1
    start_context_v1(doc, initial(), tmp_path, session)
    response = Generation(
        output=[
            {
                "type": "function_call",
                "call_id": "historical",
                "name": "act",
                "arguments": json.dumps(
                    {
                        "tool": "read_profile",
                        "arguments": {"area": "scope"},
                        "reason": "Inspect current scope",
                        "remaining_question": "Platform relationship",
                        "expected_value": "Resolve boundaries",
                        "findings": None,
                    }
                ),
            }
        ]
    )
    consume_generation(doc, initial(), tmp_path, session, response, "op_00010_1")
    assert session.action.name == "read_profile"
    assert session.contract_version == 1


def test_old_orientation_still_requires_plans(tmp_path: Path) -> None:
    from agent_system.company_intelligence.orientation import apply_submission
    from agent_system.company_intelligence.schemas import OrientationSubmission

    doc, _ = setup_doc(tmp_path)
    state = initial()
    state.sources = doc.sources
    payload = orientation_submission()
    payload["plan"] = []
    with pytest.raises(ValueError, match="focused plans"):
        apply_submission(state, OrientationSubmission.model_validate(payload), tmp_path)


def test_context_components_measure_complete_tool_page_separately(
    tmp_path: Path,
) -> None:
    from agent_system.company_intelligence.contexts import component_sizes, request

    doc, session = setup_doc(tmp_path)
    start_context(doc, initial(), tmp_path, session)
    page = saved_action(doc, tmp_path, session, "read_source", {"source_id": SID})
    from agent_system.company_intelligence.contexts import save_messages

    items = messages(tmp_path, session) + [
        {
            "type": "function_call_output",
            "call_id": "source",
            "output": json.dumps(page),
        }
    ]
    save_messages(tmp_path, session, items, 10)
    components = component_sizes(request(doc, initial(), tmp_path, session))
    assert components["source_content"] >= len(CONTENT.encode())
    assert "not exact token" in components["unit"]


def test_unusable_capture_can_be_followed_by_a_usable_capture(tmp_path: Path) -> None:
    from agent_system.company_intelligence.coordination import OperationRecord

    doc = new_document(initial())
    op = OperationRecord(
        operation_id="op_00001",
        session_id="orientation",
        kind="scrape_page",
        args={"url": URL},
        receipt_path="operations/op_00001_1.json",
    )
    with pytest.raises(SourceLimitation):
        save_capture(
            doc,
            tmp_path,
            op,
            FetchResult(html="<html><script>loading()</script></html>"),
        )
    assert not (tmp_path / "sources" / f"{SID}.md").exists()
    save_capture(doc, tmp_path, op, FetchResult(markdown=CONTENT))
    assert doc.sources[SID].outcome == "success"
    assert load_cleaned(tmp_path, doc.sources[SID]) == CONTENT


def test_unexpected_provider_failure_surfaces_and_dashboard_records_cause(
    tmp_path: Path,
) -> None:
    from agent_system.company_intelligence.execution import run
    from agent_system.dashboard import read_dashboard

    class Broken(FakeProviders):
        def retrieve(
            self, name: str, args: dict[str, object], timeout: float
        ) -> FetchResult:
            raise RuntimeError("Simulated programming failure")

    state = initial()
    with pytest.raises(RuntimeError, match="Simulated programming failure"):
        run(tmp_path, state.job_id, state, Broken())
    directory = tmp_path / "jobs" / state.job_id
    projected = read_dashboard(directory)
    assert projected["status"] == "blocked_by_error"
    assert "Simulated programming failure" in projected["error"]
    assert not list((directory / "operations").glob("*.json"))
