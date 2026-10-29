"""Exercise installed SDK serialization without network or credentials."""

import json

import httpx
import pytest
import requests
from openai import OpenAI

from research_agent import agent, tools
from research_agent.agent import OpenAIModel, definitions
from research_agent.storage import Settings
from research_agent.tools import FirecrawlPages


def test_openai_sdk_sends_exact_model_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_fake",
                "object": "response",
                "created_at": 1,
                "model": "explicit-test-model",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_fake",
                        "call_id": "call_fake",
                        "name": "read_page",
                        "arguments": '{"url":"https://example.com/"}',
                        "status": "completed",
                    }
                ],
            },
        )

    def client(**kwargs: object) -> OpenAI:
        assert kwargs == {"api_key": "fake-key", "timeout": 12, "max_retries": 1}
        return OpenAI(
            api_key="fake-key",
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
            max_retries=0,
        )

    monkeypatch.setattr(agent, "OpenAI", client)
    result = OpenAIModel("fake-key", 12).generate(
        {
            "model": "explicit-test-model",
            "instructions": "Orientation only",
            "input": [{"role": "user", "content": "Read the company page"}],
            "tools": list(definitions()),
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "truncation": "disabled",
            "max_output_tokens": 16000,
        }
    )
    assert result["status"] == "completed"
    assert sent[0]["model"] == "explicit-test-model"
    assert {tool["name"] for tool in sent[0]["tools"]} == {
        "request_continuation",
        "read_page",
        "update_profile",
        "build_site_index",
        "update_questions",
    }


def test_firecrawl_sdk_scrape_map_options_and_capture_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []

    def post(
        url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float
    ) -> requests.Response:
        assert timeout == 12
        sent.append(json)
        response = requests.Response()
        response.status_code = 200
        if url.endswith("/scrape"):
            payload = {
                "success": True,
                "data": {
                    "markdown": "Evidence",
                    "html": "<p>Evidence</p>",
                    "rawHtml": "<html><p>Evidence</p></html>",
                    "metadata": {
                        "sourceURL": "https://example.com/",
                        "title": "Example",
                        "statusCode": 200,
                    },
                },
            }
        else:
            payload = {
                "success": True,
                "links": [
                    {
                        "url": "https://example.com/docs",
                        "title": "Docs",
                        "description": "API",
                    }
                ],
            }
        response._content = __import__("json").dumps(payload).encode()
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(tools, "public_url", lambda url, resolve=False: url)
    provider = FirecrawlPages("fake-key", Settings(model="unused", timeout_seconds=12))
    capture = provider.scrape("https://example.com/")
    assert capture.raw_html == "<html><p>Evidence</p></html>"
    assert capture.status_code == 200 and capture.final_url == "https://example.com/"
    assert sent[0]["formats"] == ["markdown", "html", "rawHtml"]
    assert sent[0]["onlyMainContent"] is False
    assert sent[0]["timeout"] == 12000
    entries = provider.map("https://example.com/")
    assert entries[0].description == "API"
    assert sent[1]["sitemap"] == "include" and sent[1]["includeSubdomains"] is True
    assert sent[1]["limit"] == 5000


def test_provider_context_error_preserves_progress_and_resume(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_orientation import FakeModel, FakePages, execute, setup_run

    from research_agent.storage import Run, read_json

    directory, run, model, pages = setup_run(tmp_path, max_model_calls=3)
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, model, pages)
    before = (directory / "profile.md").read_bytes()
    run.settings.max_model_calls = 8
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Context window exceeded",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                }
            },
        )

    def client(**kwargs: object) -> OpenAI:
        return OpenAI(
            api_key="fake",
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
            max_retries=0,
        )

    monkeypatch.setattr(agent, "OpenAI", client)
    with pytest.raises(RuntimeError, match="Input was not truncated"):
        execute(directory, run, OpenAIModel("fake", 12), FakePages())
    assert run.status == "incomplete" and run.model_calls == 4
    assert (directory / "profile.md").read_bytes() == before
    assert read_json(directory, "debug/context.json")["request_bytes"] == len(sent[0])
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_pages = FakePages()
    execute(directory, reopened, FakeModel(run.inputs), fresh_pages)
    assert reopened.status == "complete" and not fresh_pages.scraped
