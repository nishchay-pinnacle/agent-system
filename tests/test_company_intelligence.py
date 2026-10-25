"""Behavior tests use explicit scripted boundaries and isolated local data."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from research_script import ScriptedProviders, begin_args, completion

from agent_system.company_intelligence.agent import Generation, ProviderFailure
from agent_system.company_intelligence.artifacts import project, read_events
from agent_system.company_intelligence.schemas import (
    Begin,
    Complete,
    Inputs,
    Research,
    ResearchSettings,
)
from agent_system.company_intelligence.tools import public_url
from agent_system.company_intelligence.workflow import (
    begin,
    complete,
    initial_research,
    load_research,
    run_research,
)
from agent_system.dashboard import job_directory, make_server


def initial(job_id: str, **limits: object) -> Research:
    return initial_research(
        job_id,
        Inputs(
            company="ExampleCo",
            url="https://example.com/",
            product="Widget",
            product_url="https://example.com/",
        ),
        ResearchSettings.model_validate(limits),
    )


def subprocess_run(root: Path, job_id: str, mode: str) -> Research:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("research_script.py")),
            str(root),
            job_id,
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=40,
        env={**os.environ, "AGENT_SYSTEM_DATA_DIR": str(root)},
    )
    assert result.returncode == 0, result.stderr
    return Research.model_validate_json(result.stdout)


def test_lifecycle_and_context(tmp_path: Path) -> None:
    job_id = str(uuid4())
    paused = subprocess_run(tmp_path, job_id, "pause")
    assert paused.status == "interrupted"
    assert paused.completed_investigations == 1
    used = paused.usage.generation_requests
    finished = subprocess_run(tmp_path, job_id, "normal")
    assert finished.status == "completed"
    assert finished.job_id == job_id
    assert finished.stop_reason == "public_sources_exhausted"
    assert finished.usage.generation_requests > used
    assert finished.gaps[0].attempts == 2
    assert finished.usage.pages == 1
    assert finished.usage.firecrawl_requests == 2  # One scrape and one map.
    assert (
        finished.usage.input_tokens + finished.usage.output_tokens
        == finished.usage.generation_requests * 580
    )
    directory = tmp_path / "jobs" / job_id
    assert "routes support" in (directory / "profile.json").read_text()
    source = next(iter(finished.sources.values()))
    assert source.filename and (directory / source.filename).is_file()
    fetches = [
        json.loads(line)
        for line in (tmp_path / "test_fetches.jsonl").read_text().splitlines()
    ]
    assert sum(f["name"] == "scrape_page" for f in fetches) == 1
    contexts = [
        json.loads(line)
        for line in (tmp_path / "test_contexts.jsonl").read_text().splitlines()
    ]
    investigation_contexts = [
        c["messages"] for c in contexts if c["phase"] == "investigation"
    ]
    fresh = [m for m in investigation_contexts if len(m) == 1]
    assert len(fresh) == 2
    assert "Confirmed Widget" in json.dumps(fresh[1])
    assert "function_call_output" not in json.dumps(fresh[1])
    assert any(
        "scripted-encrypted-protocol-item" in json.dumps(c)
        for c in investigation_contexts
        if len(c) > 1
    )
    result_before = (directory / "result.json").stat().st_mtime_ns
    again = run_research(tmp_path, job_id, providers=ScriptedProviders(tmp_path))
    assert again.status == "completed"
    assert (directory / "result.json").stat().st_mtime_ns == result_before


def test_termination_and_parked_gaps(tmp_path: Path) -> None:
    job_id = str(uuid4())
    boundary = ScriptedProviders(tmp_path)
    limited = run_research(
        tmp_path, job_id, initial(job_id, firecrawl_requests=1), boundary
    )
    assert limited.stop_reason == "budget_reached"
    assert limited.limiting_resource == "firecrawl_requests"
    assert boundary.fetches == 1
    assert limited.usage.pages == 1
    assert (tmp_path / "jobs" / job_id / "result.json").is_file()
    resumed = run_research(tmp_path, job_id, providers=boundary)
    assert resumed.usage == limited.usage
    assert boundary.fetches == 1
    # Same real control transitions as the worker, with small deterministic submissions.
    state = initial(str(uuid4()))
    state.phase = "between"
    for _ in range(3):
        args = begin_args()
        args["gap_id"] = state.gaps[0].gap_id if state.gaps else None
        begin(state, Begin.model_validate(args))
        payload = completion("investigation", "investigate", changed=False)
        payload["source_ids"] = []
        complete(state, Complete.model_validate(payload))
    assert state.gaps[0].parked
    assert state.gaps[0].attempts == 3
    assert state.phase == "review"
    state.phase = "between"
    with pytest.raises(ValueError, match="new source"):
        begin(state, Begin.model_validate(begin_args()))
    token_id = str(uuid4())
    token_limited = run_research(
        tmp_path,
        token_id,
        initial(token_id, total_tokens=100),
        ScriptedProviders(tmp_path),
    )
    assert token_limited.limiting_resource == "total_tokens"
    assert token_limited.usage.generation_requests == 0

    class Uncertain(ScriptedProviders):
        def generate(self, state: Research, timeout: float) -> Generation:
            raise ProviderFailure("Uncertain transport failure", transient=True)

    uncertain_id = str(uuid4())
    uncertain = run_research(
        tmp_path,
        uncertain_id,
        initial(uncertain_id, generation_requests=1),
        Uncertain(tmp_path),
    )
    assert uncertain.limiting_resource == "generation_requests"
    assert uncertain.usage.generation_requests == 1
    assert uncertain.usage.reserved_tokens > 0
    assert len(uncertain.usage.unknown_requests) == 1
    assert uncertain.usage.input_tokens == 0


def test_honest_failure(tmp_path: Path) -> None:
    job_id = str(uuid4())
    failed = run_research(
        tmp_path, job_id, initial(job_id), ScriptedProviders(tmp_path, "schema")
    )
    assert failed.status == "blocked_by_error"
    assert failed.stop_reason is None
    assert "correction failed" in str(failed.error)
    assert failed.profile["scope"].coverage == "answered"
    assert (
        failed.usage.generation_requests == 3
    )  # Orientation plus two invalid attempts.
    assert failed.operation is None  # Reconciled receipt cannot be applied twice.
    assert not (tmp_path / "jobs" / job_id / "result.json").exists()

    class Broken(ScriptedProviders):
        def count_input(self, state: Research, timeout: float) -> int:
            raise ProviderFailure("Authentication rejected", transient=False)

    other = str(uuid4())
    blocked = run_research(tmp_path, other, initial(other), Broken(tmp_path))
    assert blocked.status == "blocked_by_error"
    assert blocked.usage.counting_requests == 1
    assert blocked.usage.pages == 1
    assert "Authentication" in str(blocked.error)
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        saved = load_research(saver, tmp_path / "jobs" / other, other)
    assert saved and saved.status == "blocked_by_error"


def test_dashboard_and_boundaries(tmp_path: Path) -> None:
    job_id = str(uuid4())
    saved = run_research(tmp_path, job_id, initial(job_id), ScriptedProviders(tmp_path))
    directory = job_directory(tmp_path, job_id)
    before = (directory / "profile.json").read_bytes()
    with pytest.raises(ValueError, match="not found"):
        job_directory(tmp_path, str(uuid4()))
    assert len(list((tmp_path / "jobs").iterdir())) == 1
    for url in (
        "http://127.0.0.1",
        "http://[::1]",
        "http://169.254.169.254/",
        "file:///etc/passwd",
        "http://user:secret@example.com",
    ):
        with pytest.raises(ValueError):
            public_url(url)
    server = make_server(directory, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path: str) -> dict[str, object]:
        with urlopen(base + path, timeout=5) as response:
            return json.load(response)

    try:
        assert get("/api/state")["dashboard"]["job_id"] == job_id
        events = get("/api/events?after=0")["events"]
        assert events and events[0]["sequence"] == 1
        after = events[-1]["sequence"]
        assert all(
            e["sequence"] > after for e in get(f"/api/events?after={after}")["events"]
        )
        sid = next(iter(saved.sources))
        source = get(f"/api/source/{sid}?max_lines=200")
        assert "<script>window.pwned=true</script>" in source["content"]
        assert get("/api/result")["stop_reason"] == "public_sources_exhausted"
        with urlopen(base, timeout=5) as response:
            html = response.read().decode()
        assert "textContent" in html and "innerHTML" not in html
        for path in (
            "/api/source/../../.env",
            "/api/source/%2e%2e%2f.env",
            "/.env",
            "/api/source/unknown",
        ):
            with pytest.raises(HTTPError):
                get(path)
        with (directory / "events.jsonl").open("ab") as handle:
            handle.write(b'{"unfinished":')
        assert read_events(directory)
        project(directory, saved)
        assert (directory / "events.jsonl").read_bytes().endswith(b"\n")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert (directory / "profile.json").read_bytes() == before
