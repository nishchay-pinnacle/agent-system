"""Exercise real SQLite persistence across independent CLI processes."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_system.worker", *args],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_worker_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "isolated-data"
    monkeypatch.setenv("AGENT_SYSTEM_DATA_DIR", str(data_dir))
    company = "ExampleCo & Partners"
    started = run_cli("start", "--company", company)
    assert started.returncode == 0, started.stderr
    job_id = next(
        line.removeprefix("Job ID: ")
        for line in started.stdout.splitlines()
        if line.startswith("Job ID: ")
    )
    assert str(UUID(job_id)) == job_id
    assert "Status: waiting_for_resume" in started.stdout
    assert (
        f"uv run python -m agent_system.worker resume --job {job_id}" in started.stdout
    )
    draft = data_dir / "jobs" / job_id / "draft.json"
    result = draft.with_name("result.json")
    expected = {"job_id": job_id, "company_name": company, "placeholder": True}
    assert json.loads(draft.read_text()) == expected
    assert not result.exists()
    assert (data_dir / "checkpoints.sqlite").is_file()
    draft_before = (draft.read_bytes(), draft.stat().st_mtime_ns)

    paused = run_cli("status", "--job", job_id)
    assert paused.returncode == 0, paused.stderr
    assert "Status: waiting_for_resume" in paused.stdout
    assert f"Company: {company}" in paused.stdout
    assert f"draft_path: {draft}" in paused.stdout
    assert not result.exists()  # Inspection must not advance the workflow.

    resumed = run_cli("resume", "--job", job_id)
    assert resumed.returncode == 0, resumed.stderr
    assert "Status: completed" in resumed.stdout
    assert f"Job ID: {job_id}" in resumed.stdout
    assert json.loads(result.read_text()) == {**expected, "draft_path": str(draft)}
    assert (draft.read_bytes(), draft.stat().st_mtime_ns) == draft_before

    completed = run_cli("status", "--job", job_id)
    assert completed.returncode == 0, completed.stderr
    assert "Status: completed" in completed.stdout
    assert f"Company: {company}" in completed.stdout
    assert f"result_path: {result}" in completed.stdout
    result_before = (result.read_bytes(), result.stat().st_mtime_ns)
    repeated = run_cli("resume", "--job", job_id)
    assert repeated.returncode == 0, repeated.stderr
    assert "Status: completed" in repeated.stdout
    assert "stage=finish start" not in repeated.stderr
    assert (draft.read_bytes(), draft.stat().st_mtime_ns) == draft_before
    assert (result.read_bytes(), result.stat().st_mtime_ns) == result_before


def test_resume_unknown_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SYSTEM_DATA_DIR", str(tmp_path / "data"))
    # Exercise an unknown thread in an existing checkpoint database.
    assert run_cli("start", "--company", "ExistingCo").returncode == 0
    unknown = str(uuid4())
    resumed = run_cli("resume", "--job", unknown)
    assert resumed.returncode != 0
    assert f"Job {unknown}: not found" in resumed.stdout
    assert not (tmp_path / "data" / "jobs" / unknown).exists()
