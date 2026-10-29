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
    assert snapshot["context"]["request_bytes"] > 0
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


def test_markdown_profile_renders_structure_without_executable_html(
    tmp_path: Path,
) -> None:
    from bs4 import BeautifulSoup

    from research_agent.dashboard import render_profile

    content = """# Profile

**Strong** and *emphasis* [S1].

- First
- Second

| Feature | Evidence |
| --- | --- |
| Export | [S2] |

[Vendor](https://example.com/) [Bad](javascript:alert(1))
<script>alert('x')</script><img src=x onerror=alert(1)>

![Remote image](https://example.com/tracker)
"""
    rendered = render_profile(content)
    soup = BeautifulSoup(rendered, "html.parser")
    assert soup.find("h1").text == "Profile"
    assert soup.find("strong").text == "Strong" and soup.find("em").text == "emphasis"
    assert len(soup.find_all("li")) == 2
    assert soup.find("table") and soup.find("td").text == "Export"
    assert soup.find("a")["href"] == "https://example.com/"
    assert "[S1]" in soup.text and "[S2]" in soup.text
    assert not soup.find_all(["script", "img"])
    assert all(
        not str(a.get("href", "")).startswith("javascript:") for a in soup.find_all("a")
    )
    directory, _, _, _ = setup_run(tmp_path)
    save_json(directory, "run.json", {"version": 2})
    (directory / "profile.md").write_text(content)
    assert state(directory)["profile_html"] == rendered


def test_continuation_endpoint_checks_origin_request_and_allowances(
    tmp_path: Path,
) -> None:
    from research_agent.storage import ContinuationRequest, read_json, save_run

    directory, run, _, _ = setup_run(tmp_path)
    run.continuation = ContinuationRequest(
        request_id="request-1",
        reason="Need evidence",
        unresolved_questions=["Q1"],
        next_steps="Read saved sources",
        additional_calls=2,
        additional_pages=0,
    )
    save_run(directory, run)
    with ThreadingHTTPServer(
        ("127.0.0.1", 0), handler(tmp_path, directory.name)
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port)
            origin = f"http://127.0.0.1:{server.server_port}"
            decision = {
                "request_id": "request-1",
                "approve": True,
                "additional_calls": 2,
                "additional_pages": 0,
            }
            for change, request_origin, expected in [
                ({}, "http://attacker.test", 403),
                ({"request_id": "stale"}, origin, 400),
                ({"additional_calls": -1}, origin, 400),
                ({"additional_calls": 1000}, origin, 400),
                ({"additional_pages": 1}, origin, 400),
                ({}, origin, 204),
                ({}, origin, 204),
            ]:
                conn.request(
                    "POST",
                    "/api/continuation",
                    json.dumps({**decision, **change}),
                    {"Content-Type": "application/json", "Origin": request_origin},
                )
                response = conn.getresponse()
                assert response.status == expected
                response.read()
            assert read_json(directory, "continuation-decision.json") == decision
            # Dashboard only records the decision; the runner owns allowances.
            assert (
                read_json(directory, "run.json")["settings"]["max_model_calls"]
                == run.settings.max_model_calls
            )
            conn.close()
        finally:
            server.shutdown()
            thread.join()
