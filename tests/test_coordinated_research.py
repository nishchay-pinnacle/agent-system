"""Version-three outcome tests; all network boundaries are simulated."""

import json
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from research_script import SID, URL, orientation_submission

from agent_system.company_intelligence.agent import FetchResult, Generation
from agent_system.company_intelligence.approvals import UserCommand, enqueue
from agent_system.company_intelligence.coordination import (
    CoordinatorSubmission,
    Document,
    apply_proposal,
    catalog,
)
from agent_system.company_intelligence.execution import load_document
from agent_system.company_intelligence.schemas import Inputs, Research, ResearchSettings
from agent_system.company_intelligence.tools import source_id
from agent_system.company_intelligence.workflow import initial_research, run_research

PAGE = (
    "# Evidence\n"
    + "Substantive product capability with qualifications.\n" * 250
    + "FINAL COMPLETE FACT\n"
)
SHARED = "https://example.com/shared"


def spec(ids: list[str]) -> dict[str, object]:
    return dict(
        topic="Connected product understanding",
        question_ids=ids,
        objective="Resolve product boundaries",
        prerequisites=[],
        context_areas=["scope"],
        source_leads=[SHARED],
        completion_criterion="Evidence of product boundaries",
        rationale="Clarify what the campaign can claim",
    )


def proposal(assignments: list[dict[str, object]] | None = None) -> dict[str, object]:
    return dict(
        sections=[],
        findings=[],
        new_questions=[],
        merges=[],
        dependencies=[],
        assumptions=[],
        assignments=assignments or [],
        decision="investigate" if assignments else "public_sources_exhausted",
        rationale="Selected work addresses missing product boundaries"
        if assignments
        else "No promising untried public sources remain",
    )


class FakeProviders:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fetches: list[str] = []
        self.calls: list[Research] = []

    def count_input(self, state: Research, timeout: float) -> int:
        return 3000

    def retrieve(
        self, name: str, args: dict[str, object], timeout: float
    ) -> FetchResult:
        if name == "scrape_page":
            with self.lock:
                self.fetches.append(str(args["url"]))
            time.sleep(0.04)
            return FetchResult(markdown=PAGE, final_url=str(args["url"]))
        return FetchResult(
            candidates=[{"url": SHARED, "title": "Shared product evidence"}]
        )

    def generate(self, state: Research, timeout: float) -> Generation:
        with self.lock:
            self.calls.append(state.model_copy(deep=True))
        payload = json.loads(state.messages[0]["content"])
        name = "submit"
        if state.phase == "orientation":
            assert "FINAL COMPLETE FACT" in str(state.messages)
            output = orientation_submission()
        elif state.phase == "between":
            output = (
                proposal()
                if payload["latest_batch"]
                else proposal([spec(["S1", "S2"]), spec(["C1"])])
            )
        elif state.phase == "review":
            output = dict(
                reviewed="Selected batch with complete sources",
                not_reviewed="Other batches and external evidence",
                unsupported_claims=[],
                lost_qualifications=[],
                missed_facts=[],
                contradictions=[],
                inadequate_answers=[],
                suggested_corrections=[],
                source_ids=[source_id(SHARED)],
                question_ids=["S1"],
            )
        elif not any(m.get("type") == "function_call_output" for m in state.messages):
            name = "act"
            output = dict(
                tool="scrape_page",
                arguments={"url": SHARED},
                reason="Read product documentation",
                remaining_question="Which product boundaries apply?",
                expected_value="Support conditional product claims",
                findings=None,
            )
        else:
            assert "FINAL COMPLETE FACT" in str(state.messages)
            ids = payload["assignment"]["question_ids"]
            output = dict(
                findings=[
                    dict(
                        question_id=q,
                        answer="Product supports qualified routing",
                        status="partial",
                        missing="Confirm deployment conditions",
                        source_ids=[source_id(SHARED)],
                    )
                    for q in ids
                ],
                assumptions=[],
                incidental_findings=[],
                new_questions=[],
                attempted_approaches=["Read complete product documentation"],
                next_sources=[],
                remaining_uncertainty="Deployment conditions",
                outcome="completed",
            )
        revised = (
            "current_understanding" in payload
            or state.phase == "orientation"
            and "sources" in payload
        )
        if revised and state.phase == "orientation":
            for assessment in output["assessments"]:
                assessment["missing"] = (
                    "Specific deployment conditions remain unconfirmed"
                )
        if revised and state.phase == "between":
            output = dict(
                proposal=output,
                changes=[],
                profile_changes=[],
                dispositions=[
                    dict(
                        finding_id=fid,
                        disposition="deferred",
                        reason="Company confirmation is still needed",
                        reference_ids=[],
                    )
                    for fid in payload["pending_finding_ids"]
                ],
            )
        if revised and name == "act":
            name = output.pop("tool")
            output.update(output.pop("arguments"))
            previous = output.pop("findings", None)
            output["delta"] = {"findings": previous["findings"] if previous else []}
        return Generation(
            output=[
                {
                    "type": "function_call",
                    "name": name,
                    "call_id": str(uuid4()),
                    "arguments": json.dumps(output),
                }
            ],
            input_tokens=3000,
            output_tokens=500,
        )


def initial(**overrides: object) -> Research:
    settings = ResearchSettings.model_validate(
        dict(
            model="gpt-6-luna",
            generation_requests=120,
            total_tokens=2_000_000,
            input_tokens=96_000,
            active_seconds=3600,
            **overrides,
        )
    )
    return initial_research(
        str(uuid4()),
        Inputs(company="ExampleCo", url=URL, product="Widget", product_url=URL),
        settings,
        schema_version=3,
    )


def test_real_cycle_shared_source_and_quality(tmp_path: Path) -> None:
    fake = FakeProviders()
    state = initial()
    done = run_research(tmp_path, state.job_id, state, fake)
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, done)
    assert done.status == "completed", doc.model_dump_json()
    assert len(doc.questions) == 49
    assert len(doc.assignments) == 2
    assert doc.batches[0].incorporated
    assert fake.fetches.count(SHARED) == 1
    assert doc.usage["pages"] == 2
    assert all(source_id(SHARED) in a.sources for a in doc.assignments.values())
    result_bytes = (directory / "result.json").read_bytes()
    assert len(json.loads(result_bytes)["interview_backlog"]) == 49
    command = UserCommand(
        command_id="quality-command",
        job_id=state.job_id,
        kind="quality",
        expected_revision=doc.revision,
        batch_id=doc.batches[0].batch_id,
    )
    enqueue(directory, command, doc)
    reviewed = run_research(tmp_path, state.job_id, providers=fake)
    review_doc = load_document(directory, reviewed)
    assert next(iter(review_doc.reviews.values())).status == "completed"
    assert (directory / "result.json").read_bytes() == result_bytes
    assert fake.fetches.count(SHARED) == 1
    assert fake.calls[-1].settings.model == "gpt-6.1-sol"


def test_oversize_approval_is_idempotent_and_resume(tmp_path: Path) -> None:
    fake = FakeProviders()
    state = initial()
    state.settings.input_tokens = 2000
    paused = run_research(tmp_path, state.job_id, state, fake)
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, paused)
    assert paused.status == "paused"
    assert not fake.calls
    approval = doc.approvals[0]
    assert approval.remaining_question and approval.expected_value
    command = UserCommand(
        command_id="approve-input",
        job_id=state.job_id,
        kind="approve",
        expected_revision=approval.revision,
        request_id=approval.request_id,
        increases={"run:input_tokens": 10_000},
    )
    assert enqueue(directory, command, doc) == "queued"
    assert enqueue(directory, command, doc) == "queued"
    done = run_research(tmp_path, state.job_id, providers=fake)
    doc = load_document(directory, done)
    assert done.status == "completed"
    assert doc.limits["input_tokens"] == 12000
    assert len(doc.amendments) == 1
    with pytest.raises(ValueError, match="Stale"):
        enqueue(
            directory, command.model_copy(update={"command_id": "different-click"}), doc
        )


def test_dependencies_and_grouping_transaction() -> None:
    doc = Document(questions=catalog())
    bad = proposal([spec(["S1", "S2"]), spec(["S2"])])
    with pytest.raises(ValueError, match="duplicate"):
        apply_proposal(
            doc, CoordinatorSubmission.model_validate(bad), ResearchSettings()
        )
    assert not doc.assignments
    cycle = proposal()
    cycle["dependencies"] = [
        {
            "question_id": "S1",
            "dependencies": [{"fact": "Product boundary", "question_ids": ["S2"]}],
        },
        {
            "question_id": "S2",
            "dependencies": [{"fact": "Platform dependency", "question_ids": ["S1"]}],
        },
    ]
    with pytest.raises(ValueError, match="cycle"):
        apply_proposal(
            doc, CoordinatorSubmission.model_validate(cycle), ResearchSettings()
        )


def test_local_decline_retains_incremental_findings(tmp_path: Path) -> None:
    class Incremental(FakeProviders):
        def generate(self, state: Research, timeout: float) -> Generation:
            response = super().generate(state, timeout)
            if (
                state.phase == "investigation"
                and response.output[0]["name"] == "submit"
            ):
                result = json.loads(response.output[0]["arguments"])
                result["outcome"] = "progress"
                response.output[0]["name"] = "read_profile"
                response.output[0]["arguments"] = json.dumps(
                    dict(
                        area="scope",
                        reason="Check saved boundary assumptions",
                        remaining_question="Deployment conditions",
                        expected_value="Resolve conditions against the profile",
                        delta={"findings": result["findings"]},
                    )
                )
            return response

    fake = Incremental()
    state = initial(investigator_generations=2)
    paused = run_research(tmp_path, state.job_id, state, fake)
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, paused)
    assert paused.status == "paused"
    assert all(a.accepted_finding_ids for a in doc.assignments.values())
    for approval in doc.approvals:
        enqueue(
            directory,
            UserCommand(
                command_id="decline-" + approval.request_id,
                job_id=state.job_id,
                kind="decline",
                request_id=approval.request_id,
                expected_revision=approval.revision,
            ),
            doc,
        )
    call_count = sum(c.phase == "investigation" for c in fake.calls)
    done = run_research(tmp_path, state.job_id, providers=fake)
    doc = load_document(directory, done)
    assert all(
        a.status == "partial" and a.accepted_finding_ids
        for a in doc.assignments.values()
    )
    assert sum(c.phase == "investigation" for c in fake.calls) == call_count
    assert set(doc.declined_questions) == {"S1", "S2", "C1"}


def test_generation_reservations_do_not_overspend(tmp_path: Path) -> None:
    state = initial()
    # Orientation + first coordinator fit; the next operation must request authorization.
    state.settings.total_tokens = 24_000
    fake = FakeProviders()
    paused = run_research(tmp_path, state.job_id, state, fake)
    doc = load_document(tmp_path / "jobs" / state.job_id, paused)
    assert paused.status == "paused"
    assert (
        doc.usage.get("total_tokens", 0) + doc.usage.get("reserved_total_tokens", 0)
        <= 24_000
    )
    assert doc.approvals
    assert all(
        a.action and a.remaining_question and a.expected_value for a in doc.approvals
    )


def test_launch_approval_counts_assignment_on_resume(tmp_path: Path) -> None:
    state = initial(investigations=1)
    fake = FakeProviders()
    paused = run_research(tmp_path, state.job_id, state, fake)
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, paused)
    assert doc.usage["investigations"] == 1
    approval = next(a for a in doc.approvals if a.status == "pending")
    enqueue(
        directory,
        UserCommand(
            command_id="approve-launch",
            job_id=state.job_id,
            kind="approve",
            request_id=approval.request_id,
            expected_revision=approval.revision,
        ),
        doc,
    )
    done = run_research(tmp_path, state.job_id, providers=fake)
    doc = load_document(directory, done)
    assert doc.usage["investigations"] == 2
    assert all(a.launched for a in doc.assignments.values())


def test_completed_receipt_resume_in_fresh_process(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    fake = FakeProviders()
    state = initial()
    stopped = run_research(tmp_path, state.job_id, state, fake, max_steps=1)
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, stopped)
    assert len(doc.operations) == 1
    script = "from pathlib import Path; from test_coordinated_research import FakeProviders; from agent_system.company_intelligence.workflow import run_research; import sys; s=run_research(Path(sys.argv[1]),sys.argv[2],providers=FakeProviders()); print(s.status)"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), state.job_id],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.resolve())},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "completed"
    assert (directory / "sources" / f"{SID}.cleaned.v4.md").exists()


def test_uncertain_call_keeps_reservation(tmp_path: Path) -> None:
    from agent_system.company_intelligence.artifacts import save_json

    fake = FakeProviders()
    state = initial()
    # Reach a dispatch boundary with a completed receipt not yet checkpoint-consumed.
    stopped = run_research(tmp_path, state.job_id, state, fake, max_steps=4)
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, stopped)
    dispatched = [o for o in doc.operations.values() if o.status == "dispatched"]
    assert dispatched
    op = dispatched[-1]
    (directory / op.receipt_path).unlink()
    before = doc.usage.get("reserved_total_tokens", 0)
    save_json(directory / stopped.runtime_path, doc.model_dump(mode="json"))
    done = run_research(tmp_path, state.job_id, providers=fake)
    after = load_document(directory, done)
    assert any(e["kind"] == "uncertain_call" for e in after.events)
    assert after.operations[op.operation_id].attempt == 2
    assert after.usage.get("reserved_total_tokens", 0) >= before


def test_strict_provider_tool_schemas() -> None:
    from agent_system.company_intelligence.contexts import definitions

    def check(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                assert set(value.get("required", [])) == set(
                    value.get("properties", {})
                )
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    for role in ("orientation", "coordinator", "investigator", "quality"):
        check(definitions(role))


def test_new_questions_and_merge_history() -> None:
    doc = Document(questions=catalog())
    update = proposal()
    update["new_questions"] = [
        dict(
            text="Which region hosts the data?",
            area="capabilities",
            missing="Hosting region",
            importance=1,
            importance_reason="Deployment boundary",
        )
    ]
    doc = apply_proposal(
        doc, CoordinatorSubmission.model_validate(update), ResearchSettings()
    )
    assert "Q0001" in doc.questions
    merge = proposal()
    merge["merges"] = [
        dict(old_id="S1", target_id="S2", reason="Overlapping product scope question")
    ]
    doc = apply_proposal(
        doc, CoordinatorSubmission.model_validate(merge), ResearchSettings()
    )
    assert doc.questions["S1"].merged_into == "S2"
    assert doc.questions["S1"].history
    assert len(doc.questions) == 50


def test_role_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_system import config

    monkeypatch.setattr(config, "data_directory", lambda: Path("/tmp"))
    for key in list(__import__("os").environ):
        if key.startswith("AGENT_SYSTEM_") or key in {
            "OPENAI_MODEL",
            "OPENAI_REASONING_EFFORT",
        }:
            monkeypatch.delenv(key)
    defaults = config.research_settings()
    assert (
        defaults.orientation_model
        == defaults.coordinator_model
        == defaults.investigator_model
        == "gpt-6-luna"
    )
    assert (
        defaults.orientation_effort
        == defaults.coordinator_effort
        == defaults.investigator_effort
        == "medium"
    )
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6.1-sol")
    assert config.research_settings().investigator_model == "gpt-6.1-sol"
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-luna")
    assert config.research_settings().investigator_model == "gpt-6-luna"
    assert defaults.quality_model == "gpt-6.1-sol"


def test_command_endpoint_security_and_offline_queue(tmp_path: Path) -> None:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from agent_system.dashboard import make_server

    state = initial()
    state.settings.input_tokens = 100
    paused = run_research(tmp_path, state.job_id, state, FakeProviders())
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, paused)
    server = make_server(directory, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(origin + "/api/system") as response:
            data = json.load(response)
        approval = doc.approvals[0]
        command = UserCommand(
            command_id="http-approval",
            job_id=state.job_id,
            kind="approve",
            request_id=approval.request_id,
            expected_revision=approval.revision,
        )
        url = origin + f"/api/jobs/{state.job_id}/commands"
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://attacker.invalid",
            "X-CSRF-Token": data["csrf_token"],
        }
        with pytest.raises(HTTPError) as rejected:
            urlopen(Request(url, command.model_dump_json().encode(), headers=headers))
        assert rejected.value.code == 403
        headers["Origin"] = origin
        with urlopen(
            Request(url, command.model_dump_json().encode(), headers=headers)
        ) as response:
            assert response.status == 202
        with urlopen(origin + "/api/system") as response:
            data = json.load(response)
        assert data["commands"][0]["outcome"] is None
        assert data["worker"]["online"] is False
        assert load_document(directory, paused).approvals[0].status == "pending"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_user_finish_without_hidden_model_call(tmp_path: Path) -> None:
    state = initial()
    state.settings.input_tokens = 100
    fake = FakeProviders()
    paused = run_research(tmp_path, state.job_id, state, fake)
    directory = tmp_path / "jobs" / state.job_id
    doc = load_document(directory, paused)
    enqueue(
        directory,
        UserCommand(
            command_id="finish-partial",
            job_id=state.job_id,
            kind="finish",
            expected_revision=doc.revision,
        ),
        doc,
    )
    done = run_research(tmp_path, state.job_id, providers=fake)
    assert done.status == "completed" and not fake.calls
    result = json.loads((directory / "result.json").read_text())
    assert len(result["interview_backlog"]) == 49
    assert all(
        q["reason"] == "Budget/user stop before resolution"
        for q in result["interview_backlog"]
    )


def test_resume_terminal_receipt_commits_recovered_result(tmp_path: Path) -> None:
    from langgraph.checkpoint.sqlite import SqliteSaver

    from agent_system.company_intelligence.artifacts import read_json
    from agent_system.company_intelligence.workflow import build_research_workflow

    fake = FakeProviders()
    state = initial()
    finished = run_research(tmp_path, state.job_id, state, fake)
    directory = tmp_path / "jobs" / state.job_id
    # Reopen the last real dispatch checkpoint, as if the worker exited after its receipt.
    revisions = sorted((directory / "state").glob("*.json"))
    candidates = [(p, Document.model_validate(read_json(p))) for p in revisions]
    path, prior = next(
        (p, d)
        for p, d in reversed(candidates)
        if d.stage == "final_check"
        and any(
            o.status == "dispatched" and o.kind == "generate"
            for o in d.operations.values()
        )
    )
    finished.runtime_path = str(path.relative_to(directory))
    finished.status = "running"
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        build_research_workflow(directory, saver).update_state(
            {"configurable": {"thread_id": state.job_id}},
            {"research": finished.model_dump(mode="json")},
        )
    calls = len(fake.calls)
    resumed = run_research(tmp_path, state.job_id, providers=fake)
    assert resumed.status == "completed"
    assert load_document(directory, resumed).stage == "done"
    assert len(fake.calls) == calls


def test_quality_preview_http_uses_saved_projection(tmp_path: Path) -> None:
    from urllib.request import urlopen

    from agent_system.dashboard import make_server

    fake = FakeProviders()
    state = initial()
    run_research(tmp_path, state.job_id, state, fake)
    server = make_server(tmp_path / "jobs" / state.job_id, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/api/quality-preview"
        ) as response:
            preview = json.load(response)
        assert preview["batch_id"] == "batch_0001"
        assert preview["source_ids"] == [source_id(SHARED)]
        assert preview["model"] == "gpt-6.1-sol"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
