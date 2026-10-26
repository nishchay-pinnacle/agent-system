from pathlib import Path

import pytest
from test_orientation import FakeModel, FakePages

from research_agent import main
from research_agent.storage import Inputs, read_json


def test_start_and_resume_cli_with_fake_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-firecrawl")
    monkeypatch.setenv("OPENAI_MODEL", "explicit-test-model")
    inputs = Inputs(
        company="Example",
        product="Widget",
        company_url="https://example.com/",
        product_url="https://example.com/product",
    )
    model, pages = FakeModel(inputs), FakePages()
    monkeypatch.setattr(main, "OpenAIModel", lambda key, timeout: model)
    monkeypatch.setattr(main, "FirecrawlPages", lambda key, settings: pages)
    assert (
        main.main(
            [
                "start",
                "--company",
                inputs.company,
                "--product",
                inputs.product,
                "--company-url",
                inputs.company_url,
                "--product-url",
                inputs.product_url,
            ]
        )
        == 0
    )
    directory = next((tmp_path / "data/runs").iterdir())
    output = capsys.readouterr().out
    assert "Orientation complete" in output
    assert "Skill · orientation" in output
    assert "Model call · 1 · explicit-test-model" in output
    assert "Tool · read_page" in output
    assert "url: https://example.com/product" in output
    assert "Tool result · build_site_index" in output
    assert "discovered_count: 2" in output
    assert "Unavailable from provider" in output
    assert "action=" not in output
    requests = len(model.requests)
    assert main.main(["resume", "--run-id", directory.name]) == 0
    assert len(model.requests) == requests
    assert (
        read_json(directory, "run.json")["settings"]["model"] == "explicit-test-model"
    )


def test_cli_requires_explicit_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert (
        main.main(
            [
                "start",
                "--company",
                "Example",
                "--product",
                "Widget",
                "--company-url",
                "https://example.com/",
                "--product-url",
                "https://example.com/",
            ]
        )
        == 1
    )
    assert "Set OPENAI_MODEL explicitly" in capsys.readouterr().out
    assert not (tmp_path / "data").exists()


def test_token_totals_restore_without_double_counting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from research_agent.cli import CLI
    from research_agent.storage import save_json

    save_json(
        tmp_path,
        "debug/call-0001.response.json",
        {"usage": {"input_tokens": 100, "output_tokens": 20}},
    )
    save_json(tmp_path, "debug/call-0002.response.json", {"usage": None})
    display = CLI(tmp_path)
    display.model_result(
        {
            "input_tokens": 50,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 5},
        }
    )
    output = capsys.readouterr().out
    assert "50 input · 10 output" in output
    assert "150 input · 30 output · 180 total" in output
    assert "30 (included above)" in output and "5 (included above)" in output
    assert "1 response(s); excluded from totals" in output
    display.model_result(None)
    output = capsys.readouterr().out
    assert "Unavailable from provider" in output
    assert "150 input · 30 output · 180 total" in output


def test_tool_presentation_is_readable_and_http_logs_are_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import logging

    from research_agent.cli import CLI, configure_logging

    configure_logging()
    display = CLI()
    display.tool_call("read_page", '{"url":"https://example.com/product"}')
    display.tool_result(
        "read_page", {"source": "sources/example.md", "content": "evidence " * 1000}
    )
    display.tool_call(
        "update_profile", __import__("json").dumps({"content": "Profile " * 1000})
    )
    logging.getLogger("httpx").info("HTTP Request: should not appear")
    output = capsys.readouterr().out
    assert "Tool · read_page" in output
    assert "url: https://example.com/product" in output
    assert "9,000 characters; complete text saved in source" in output
    assert "full content saved" in output
    assert len(output) < 1000
    assert "HTTP Request" not in output
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
