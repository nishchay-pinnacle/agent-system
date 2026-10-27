"""Dashboard snapshots, incremental activity, and safe read-only artifact access."""

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from test_orientation import execute, setup_run

from research_agent.cli import CLI
from research_agent.dashboard import events, handler, state
from research_agent.storage import save_json


def test_state_events_and_legacy_fields(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path)
    execute(directory, run, model, pages)
    first = events(directory, 0)
    assert first["events"] and first["offset"] > 0
    CLI(directory).event(
        "Test", "New", {"nested": {"api_key": "sensitive", "text": "safe"}}
    )
    update = events(directory, first["offset"])
    assert len(update["events"]) == 1
    assert "sensitive" not in json.dumps(update)
    assert "[redacted]" in json.dumps(update)
    assert events(directory, update["offset"])["events"] == []
    snapshot = state(directory)
    assert snapshot["questions"]["questions"][0]["id"] == "Q1"
    assert snapshot["sources"][0]["content"]
    assert snapshot["context"]["estimated_tokens"] > 0
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    save_json(legacy, "run.json", {"version": 1, "settings": {"input_allowance": 1000}})
    old = state(legacy)
    assert (
        old["questions"] is None and old["context"] is None and old["firecrawl"] is None
    )
    assert old["calls"] == []


def test_http_allowlist_and_rebinding(tmp_path: Path) -> None:
    directory, _, _, _ = setup_run(tmp_path)
    save_json(directory, "run.json", {"version": 1})
    with ThreadingHTTPServer(
        ("127.0.0.1", 0), handler(tmp_path, directory.name)
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            for path, expected in [
                ("/", 200),
                ("/api/state?run=orientation_test", 200),
                ("/api/state?run=../outside", 400),
                ("/run.json", 404),
                ("/api/state?run=.env", 400),
            ]:
                connection.request("GET", path)
                response = connection.getresponse()
                assert response.status == expected
                response.read()
            connection.request("GET", "/api/runs", headers={"Host": "attacker.test"})
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            connection.request("POST", "/api/state")
            response = connection.getresponse()
            assert response.status == 501
            response.read()
            connection.close()
        finally:
            server.shutdown()
            thread.join()
    (directory / "profile.md").symlink_to(tmp_path / "private.txt")
    with pytest.raises(ValueError, match="no symlinks"):
        state(directory)
