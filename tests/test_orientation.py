"""Offline outcomes for full-page orientation, inventories and durable call inspection."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from research_script import SID, URL, ScriptedProviders, orientation_submission

from agent_system.company_intelligence import agent
from agent_system.company_intelligence.agent import (
    FetchResult,
    Generation,
    ProviderFailure,
)
from agent_system.company_intelligence.artifacts import read_json
from agent_system.company_intelligence.call_traces import call_details, call_summaries
from agent_system.company_intelligence.orientation import apply_submission
from agent_system.company_intelligence.page_content import clean_markdown
from agent_system.company_intelligence.schemas import (
    Inputs,
    OrientationSubmission,
    Research,
    ResearchSettings,
)
from agent_system.company_intelligence.site_index import search_inventory
from agent_system.company_intelligence.tools import excerpt, tool_definitions
from agent_system.company_intelligence.workflow import initial_research, run_research
from agent_system.dashboard import make_server

LONG_PAGE = (
    "# Widget\r\n<!-- noise -->\n<script>bad()</script>\n"
    + ("A substantive product detail.\n" * 400)
    + "\nFINAL FACT: Enterprise-only exports require human approval.\n"
)


def new_state(**limits: object) -> Research:
    return initial_research(
        str(uuid4()),
        Inputs(company="ExampleCo", url=URL, product="Widget", product_url=URL),
        ResearchSettings.model_validate(limits),
        run_mode="orientation_only",
    )


class FullPages(ScriptedProviders):
    def retrieve(
        self, name: str, args: dict[str, object], timeout: float
    ) -> FetchResult:
        if name == "scrape_page":
            return FetchResult(markdown=LONG_PAGE, title="Widget", final_url=URL)
        return FetchResult(
            candidates=[
                {
                    "url": f"https://example.com/page-{i}",
                    **({"title": "Enterprise exports"} if i == 88 else {}),
                }
                for i in range(100)
            ]
            + [{"url": "http://localhost/private"}]
        )

    def generate(self, state: Research, timeout: float) -> Generation:
        text = json.dumps(state.messages)
        assert "FINAL FACT" in text
        assert "bad()" not in text
        assert text.count("FINAL FACT") == 1
        assert "page-88" not in text
        assert [t["name"] for t in tool_definitions(state)] == ["complete_orientation"]
        return super().generate(state, timeout)


def test_full_orientation_inventory_and_reopen(tmp_path: Path) -> None:
    initial = new_state(map_limit=101)
    boundary = FullPages(tmp_path)
    # Stop after setup and reopen from actual SQLite with a fresh provider/runtime.
    paused = run_research(tmp_path, initial.job_id, initial, boundary, max_steps=8)
    assert paused.status == "running"
    done = run_research(tmp_path, initial.job_id, providers=FullPages(tmp_path))
    assert done.stop_reason == "orientation_complete"
    assert done.run_mode == "orientation_only" and done.readiness == "needs_review"
    assert done.completed_investigations == 0 and done.usage.generation_requests == 1
    directory = tmp_path / "jobs" / done.job_id
    assert len(done.sources) == 1 and len(done.investigation_plan) == 49
    assert done.sources[SID].cleaning_stats["raw_bytes"] == len(LONG_PAGE.encode())
    assert done.discovery["discovered_count"] == 100
    assert (
        done.discovery["rejected_count"] == 1 and done.discovery["possibly_truncated"]
    )
    assert search_inventory(directory, "enterprise")["entries"][0]["url"].endswith(
        "page-88"
    )
    assert (
        "page-88" not in done.model_dump_json()
        and "FINAL FACT" not in done.model_dump_json()
    )
    assert (directory / "sources" / f"{SID}.md").read_bytes() == LONG_PAGE.encode()
    assert "FINAL FACT" in (directory / "sources" / f"{SID}.cleaned.v4.md").read_text()
    snapshot = (directory / "orientation.json").read_bytes()
    assert len(json.loads(snapshot)["assessments"]) == 49
    calls = call_summaries(directory)["calls"]
    assert len(calls) == 1
    detail = call_details(directory, calls[0]["id"])
    assert detail["request"]["max_output_tokens"] == 12000
    assert detail["metadata"]["sources"][0]["roles"] == ["company", "product"]
    assert detail["metadata"]["count_trace_id"]
    assert "scripted-encrypted-protocol-item" not in json.dumps(detail)
    again = run_research(tmp_path, initial.job_id, providers=FullPages(tmp_path))
    assert (
        again.usage == done.usage
        and (directory / "orientation.json").read_bytes() == snapshot
    )


def test_cleaner_and_legacy_source_delivery(tmp_path: Path) -> None:
    raw = "# Product\r\n\x00<!-- comment -->\n<style>bad</style>\n\n\n\n\n| Plan | Limit |\n| Pro | 10 |\n[Docs](https://example.com/docs) ![Diagram](image.png)\nEnterprise requires approval.\n```html\n<script>example()</script>\n<!-- keep -->\n\n\n\n\n```\n"
    clean = clean_markdown(raw)
    assert clean == clean_markdown(clean)
    assert "<!-- comment -->" not in clean and "<style>" not in clean
    assert "<script>example()</script>" in clean and "<!-- keep -->" in clean
    for fact in (
        "| Pro | 10 |",
        "[Docs]",
        "![Diagram]",
        "Enterprise requires approval.",
    ):
        assert fact in clean
    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, ScriptedProviders(tmp_path))
    source = done.sources[SID]
    directory = tmp_path / "jobs" / done.job_id
    before = (directory / source.filename).read_bytes()
    (directory / source.cleaned_filename).unlink()
    source.cleaned_filename = source.cleaner_version = None
    delivered = excerpt(directory, source)
    assert (
        delivered["representation"] == "cleaned"
        and "<script>" not in delivered["content"]
    )
    assert (
        source.cleaner_version == "1"
        and (directory / source.filename).read_bytes() == before
    )


def test_question_validation(tmp_path: Path) -> None:
    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, ScriptedProviders(tmp_path))
    directory = tmp_path / "jobs" / done.job_id
    for mutation in ("missing", "duplicate", "citation", "reason", "plan"):
        payload = orientation_submission()
        if mutation == "missing":
            payload["assessments"].pop()
        if mutation == "duplicate":
            payload["assessments"][-1]["question_id"] = "S1"
        if mutation == "citation":
            payload["assessments"][0]["source_ids"] = ["s_unknown"]
        if mutation == "reason":
            payload["assessments"][0].update(status="not_applicable", answer=" ")
        if mutation == "plan":
            payload["plan"] = payload["plan"][1:]
        with pytest.raises(ValueError):
            apply_submission(
                done, OrientationSubmission.model_validate(payload), directory
            )
    payload = orientation_submission()
    payload["plan"].append(payload["plan"][0].copy())
    apply_submission(done, OrientationSubmission.model_validate(payload), directory)
    assert len(done.investigation_plan) == 50
    assert len({p.plan_id for p in done.investigation_plan}) == 50


def test_oversize_and_missing_required_page(tmp_path: Path) -> None:
    class CountLarge(FullPages):
        def count_input(self, state: Research, timeout: float) -> int:
            assert "FINAL FACT" in json.dumps(state.messages)
            return 90000

        def generate(self, state: Research, timeout: float) -> Generation:
            pytest.fail("Oversized input must not be generated")

    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, CountLarge(tmp_path))
    assert (
        done.limiting_resource == "input_tokens" and done.usage.generation_requests == 0
    )
    assert (
        "FINAL FACT"
        in (
            tmp_path / "jobs" / initial.job_id / "sources" / f"{SID}.cleaned.v4.md"
        ).read_text()
    )

    class Empty(FullPages):
        def retrieve(
            self, name: str, args: dict[str, object], timeout: float
        ) -> FetchResult:
            return FetchResult(markdown="<script>nothing()</script>")

    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, Empty(tmp_path))
    assert done.status == "blocked_by_error" and done.stop_reason is None
    assert done.usage.generation_requests == 0


def test_retry_traces_and_http(tmp_path: Path) -> None:
    class Retry(ScriptedProviders):
        def generate(self, state: Research, timeout: float) -> Generation:
            if state.usage.generation_requests == 1:
                raise ProviderFailure("Temporary provider failure", transient=True)
            return super().generate(state, timeout)

    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, Retry(tmp_path))
    directory = tmp_path / "jobs" / done.job_id
    rows = call_summaries(directory)["calls"]
    assert [r["status"] for r in rows] == ["failed", "completed"]
    assert rows[1]["retry_of"] == rows[0]["id"]
    assert len(call_summaries(directory, counts=True)["calls"]) == 3
    before = {str(p): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    server = make_server(directory, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for route in [
            "/api/calls",
            "/api/call/" + rows[1]["id"],
            "/api/inventory?q=widget",
            "/api/orientation",
            "/api/source/" + SID + "?representation=cleaned",
            "/dashboard.js",
            "/dashboard.css",
        ]:
            with urlopen(base + route, timeout=5) as response:
                assert response.status == 200
        for route in [
            "/api/call/../../.env",
            "/api/call/%2e%2e%2f.env",
            "/api/inventory?offset=-1",
        ]:
            with pytest.raises(HTTPError):
                urlopen(base + route, timeout=5)
        # A named artifact may not escape through a symlink either.
        (directory / "calls" / "op_99999_1").symlink_to(
            tmp_path, target_is_directory=True
        )
        with pytest.raises(HTTPError):
            urlopen(base + "/api/call/op_99999_1", timeout=5)
        (directory / "calls" / "op_99999_1").unlink()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert before == {
        str(p): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    }


def test_exact_sdk_request_and_invalid_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    class FakeResponse:
        usage = None
        status = "completed"
        output = []

        def model_dump(self, **kwargs: object) -> dict[str, object]:
            return {
                "id": "resp_fake",
                "model": "fake-model",
                "status": "completed",
                "output": [],
                "error": None,
                "unexpected_detail": "preserve me",
            }

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.responses = SimpleNamespace(
                create=self.create, input_tokens=SimpleNamespace(count=self.count)
            )

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def create(self, **kwargs: object) -> FakeResponse:
            captured.append(kwargs)
            return FakeResponse()

        def count(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                input_tokens=500,
                model_dump=lambda **kw: {
                    "input_tokens": 500,
                    "object": "response.input_tokens",
                },
            )

    monkeypatch.setattr(agent, "OpenAI", FakeClient)
    monkeypatch.setattr(agent, "validate_credentials", lambda: None)
    initial = new_state()
    # Complete deterministic fetch setup with a fake; resume against the fake SDK.
    partial = run_research(
        tmp_path, initial.job_id, initial, ScriptedProviders(tmp_path), max_steps=8
    )
    assert not partial.orientation_queue
    done = run_research(tmp_path, initial.job_id)
    assert done.status == "blocked_by_error" and len(captured) == 2
    directory = tmp_path / "jobs" / initial.job_id
    for row, actual in zip(call_summaries(directory)["calls"], captured, strict=True):
        actual.pop("timeout")
        assert read_json(directory / "calls" / row["id"] / "request.json") == actual
        detail = call_details(directory, row["id"])
        assert detail["response"]["unexpected_detail"] == "preserve me"
        assert detail["metadata"]["application_status"] == "rejected"
    before = len(captured)
    # Replaying consume uses the existing receipt, never dispatching again.
    from agent_system.company_intelligence.schemas import Operation
    from agent_system.company_intelligence.workflow import execute

    first = call_summaries(directory)["calls"][0]
    done.operation = Operation(
        operation_id=first["operation_id"], kind="generate", attempts=1
    )
    done.stage = "execute"
    execute(done, directory, None)
    assert done.stage == "consume" and len(captured) == before


def test_interrupted_orientation_fresh_process_and_legacy(tmp_path: Path) -> None:
    from research_script import completion
    from test_company_intelligence import subprocess_run

    job_id = str(uuid4())
    paused = subprocess_run(tmp_path, job_id, "orientation_pause")
    assert paused.status == "interrupted" and paused.run_mode == "orientation_only"
    directory = tmp_path / "jobs" / job_id
    counts = call_summaries(directory, counts=True)["calls"]
    assert counts[0]["status"] == "unknown"
    finished = subprocess_run(tmp_path, job_id, "normal")
    assert finished.stop_reason == "orientation_complete"
    assert (
        finished.usage.counting_requests == 2
        and finished.usage.generation_requests == 1
    )
    assert finished.completed_investigations == 0
    assert (
        call_summaries(directory, counts=True)["calls"][1]["retry_of"]
        == counts[0]["id"]
    )
    assert subprocess_run(tmp_path, job_id, "normal").usage == finished.usage

    class Legacy(ScriptedProviders):
        def generate(self, state: Research, timeout: float) -> Generation:
            payload = completion(
                state.phase,
                "review"
                if state.phase == "orientation"
                else "public_sources_exhausted",
            )
            return Generation(
                output=[
                    {
                        "type": "function_call",
                        "call_id": "legacy_call",
                        "name": "complete_investigation",
                        "arguments": json.dumps(payload),
                    }
                ],
                input_tokens=500,
                output_tokens=80,
            )

    legacy = new_state()
    legacy.schema_version = 1
    legacy.run_mode = "full"
    legacy.instructions = "Persisted legacy instruction text, unchanged."
    done = run_research(tmp_path, legacy.job_id, legacy, Legacy(tmp_path))
    assert done.stop_reason == "public_sources_exhausted"
    assert done.instructions == legacy.instructions and not done.assessments
    assert not (tmp_path / "jobs" / legacy.job_id / "orientation.json").exists()
    # Truly old runs have no call directory: never fabricate prompts.
    old = tmp_path / "old"
    old.mkdir()
    assert (
        call_summaries(old)["legacy_notice"]
        == "Exact call input was not recorded for this run"
    )


def test_request_save_failure_and_local_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_system.company_intelligence import call_traces
    from agent_system.company_intelligence.agent import (
        LiveProviders,
        counting_payload,
        request_payload,
    )
    from agent_system.company_intelligence.schemas import Operation
    from agent_system.company_intelligence.workflow import execute

    initial = new_state(model="gpt-4o")
    initial.operation = Operation(
        operation_id="op_00001", kind="count_input", attempts=1, reserved_seconds=10
    )
    initial.messages = [{"role": "user", "content": "Local test input"}]
    identifier = call_traces.start_trace(
        tmp_path,
        initial,
        counting_payload(request_payload(initial)),
        "local_count",
        False,
    )
    monkeypatch.setattr(agent, "validate_credentials", lambda: None)

    def no_client(**kwargs: object) -> None:
        pytest.fail("Local counting must not call a provider")

    monkeypatch.setattr(agent, "OpenAI", no_client)
    monkeypatch.setattr(
        agent.tiktoken,
        "encoding_for_model",
        lambda model: SimpleNamespace(
            encode=lambda text, **kwargs: list(text.encode())
        ),
    )
    assert LiveProviders(tmp_path, identifier).count_input(initial, 10) > 0
    assert call_summaries(tmp_path)["total"] == 0
    assert call_summaries(tmp_path, counts=True)["calls"][0]["kind"] == "local_count"

    class NoDispatch(ScriptedProviders):
        def generate(self, state: Research, timeout: float) -> Generation:
            pytest.fail("Must persist the request before dispatch")

    def unwritable(*args: object, **kwargs: object) -> None:
        raise OSError("Synthetic unwritable trace")

    monkeypatch.setattr(call_traces, "save_json", unwritable)
    initial.operation = Operation(
        operation_id="op_00002", kind="generate", attempts=1, reserved_seconds=10
    )
    with pytest.raises(OSError, match="unwritable"):
        execute(initial, tmp_path, NoDispatch(tmp_path))


def test_later_phase_cleaning_and_local_search_allowance(tmp_path: Path) -> None:
    from agent_system.company_intelligence.workflow import (
        consume,
        execute,
        operation,
        reserve,
    )

    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, FullPages(tmp_path))
    directory = tmp_path / "jobs" / done.job_id
    done.phase = "investigation"
    done.status = "running"
    done.messages = []
    done.source_deliveries = []
    firecrawl_before = done.usage.firecrawl_requests
    retrievals_before = done.usage.retrievals
    operation(
        done, "search_site_index", {"query": "enterprise", "limit": 10}, "search_call"
    )
    reserve(done, directory)
    assert done.usage.firecrawl_requests == firecrawl_before
    assert done.usage.retrievals == retrievals_before + 1
    assert "page-88" in json.dumps(done.messages)
    operation(done, "scrape_page", {"url": "https://example.com/new-page"}, "new_call")
    reserve(done, directory)
    execute(done, directory, FullPages(tmp_path))
    consume(done, directory)
    delivered = json.loads(done.messages[-1]["output"])
    assert (
        delivered["representation"] == "cleaned" and "bad()" not in delivered["content"]
    )
    assert delivered["more"]  # Later investigations still use the existing excerpt cap.
    assert done.source_deliveries[-1]["delivery"] == "excerpt"


def test_redirect_dedup_and_explicit_backlog_updates(tmp_path: Path) -> None:
    from agent_system.company_intelligence.orientation import update_plan
    from agent_system.company_intelligence.schemas import PlanUpdate

    initial = new_state()
    initial.inputs.product_url = "https://example.com/widget"
    from agent_system.company_intelligence.schemas import Operation

    initial.orientation_queue.insert(
        1,
        Operation(
            operation_id="",
            kind="scrape_page",
            args={"url": initial.inputs.product_url},
        ),
    )
    done = run_research(tmp_path, initial.job_id, initial, FullPages(tmp_path))
    assert done.stop_reason == "orientation_complete"
    directory = tmp_path / "jobs" / done.job_id
    context = json.loads(
        read_json(directory / "orientation_context.json")["messages"][0]["content"]
    )
    assert len(context["pages"]) == 1
    assert context["pages"][0]["roles"] == ["company", "product"]
    original = (directory / "orientation.json").read_bytes()
    first = done.investigation_plan[0]
    update_plan(
        done,
        PlanUpdate.model_validate(
            {
                "assessments": [
                    {
                        "question_id": "S1",
                        "status": "answered",
                        "answer": "The company supplies the Widget request-routing product.",
                        "source_ids": [SID],
                    }
                ],
                "additions": [],
                "supersede": [
                    {
                        "plan_id": first.plan_id,
                        "reason": "Saved company evidence now resolves S1; the old scope investigation is redundant.",
                    }
                ],
            }
        ),
    )
    assert first.status == "superseded" and done.assessments[0].status == "answered"
    assert (directory / "orientation.json").read_bytes() == original


def test_partial_artifacts_and_strict_tool_schemas(tmp_path: Path) -> None:
    state = new_state()
    for phase in ("orientation", "between", "investigation"):
        state.phase = phase
        for tool in tool_definitions(state):

            def check_schema(value: object) -> None:
                if isinstance(value, dict):
                    if "properties" in value:
                        assert set(value["required"]) == set(value["properties"])
                        assert value["additionalProperties"] is False
                    for child in value.values():
                        check_schema(child)
                elif isinstance(value, list):
                    for child in value:
                        check_schema(child)

            check_schema(tool["parameters"])
    state.phase = "orientation"
    done = run_research(tmp_path, state.job_id, state, ScriptedProviders(tmp_path))
    directory = tmp_path / "jobs" / done.job_id
    (directory / "profile.json").unlink()
    row = call_summaries(directory)["calls"][0]
    (directory / "calls" / row["id"] / "response.json").write_text("{unfinished")
    detail = call_details(directory, row["id"])
    assert (
        detail["request"] and detail["response"] is None and detail["artifact_errors"]
    )
    server = make_server(directory, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/api/state", timeout=5
        ) as response:
            data = json.load(response)
        assert data["profile"]["scope"]["coverage"] == "answered"
        assert data["artifact_errors"] and not (directory / "profile.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
