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
                "--orientation-only",
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
    assert "Run summary · Complete" in output
    assert "Model requests: 5" in output
    assert "Firecrawl requests: 2 scrape · 1 map" in output
    assert "Total API requests: 8" in output
    requests = len(model.requests)
    assert main.main(["resume", "--run-id", directory.name]) == 0
    assert len(model.requests) == requests
    assert "Total API requests: 8" in capsys.readouterr().out
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


def test_summary_keeps_failed_requests_and_usage_across_resume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_orientation import execute, setup_run

    from research_agent.cli import CLI
    from research_agent.storage import Run, save_json

    directory, run, model, pages = setup_run(tmp_path)
    pages.fail_map = True
    with pytest.raises(RuntimeError, match="map failure"):
        execute(directory, run, model, pages)
    # Supply synthetic reported usage; no provider calls are made.
    for path in (directory / "debug").glob("call-*.response.json"):
        response = read_json(directory, f"debug/{path.name}")
        response["usage"] = {"input_tokens": 100, "output_tokens": 20}
        save_json(directory, f"debug/{path.name}", response)
    capsys.readouterr()
    CLI(directory).summary(directory, "Incomplete")
    output = capsys.readouterr().out
    assert "Run summary · Incomplete" in output
    assert "Total tokens (reported): 600" in output
    assert "Input / output tokens: 500 / 100" in output
    assert "Total API requests: 8" in output
    reopened = Run.model_validate(read_json(directory, "run.json"))
    execute(directory, reopened, FakeModel(run.inputs), FakePages())
    CLI(directory).summary(directory, "Complete")
    output = capsys.readouterr().out
    assert "Total tokens (reported): 600" in output
    assert "Firecrawl requests: 2 scrape · 2 map" in output
    assert "Total API requests: 9" in output


def test_summary_marks_old_firecrawl_counts_as_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_orientation import setup_run

    from research_agent.cli import CLI
    from research_agent.storage import save_run

    directory, run, _, _ = setup_run(tmp_path)
    save_run(directory, run)
    (directory / "debug/firecrawl-counts.json").unlink()
    CLI(directory).summary(directory, "Incomplete")
    assert "earlier Firecrawl calls unavailable" in capsys.readouterr().out


def test_byte_limit_settings_and_cli_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from research_agent.cli import parser
    from research_agent.storage import Settings

    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-astra")
    monkeypatch.setenv("RESEARCH_MAX_INPUT_BYTES", "1234567")
    assert main.environment_settings().max_input_bytes == 1234567
    assert Settings(model="gpt-6.1-sol").max_input_bytes == 1_000_000
    args = parser().parse_args(
        ["research", "--run-id", "test", "--model-calls", "30", "--page-budget", "20"]
    )
    assert args.max_input_bytes == 1_000_000
    assert (
        parser()
        .parse_args(["resume", "--run-id", "test", "--max-input-bytes", "2000000"])
        .max_input_bytes
        == 2_000_000
    )
    with pytest.raises(ValueError):
        Settings(model="test", max_input_bytes=0)


def test_old_token_settings_are_not_reinterpreted_as_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from research_agent.storage import save_json

    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "data/runs/old_tokens"
    save_json(
        directory,
        "run.json",
        {
            "version": 2,
            "settings": {"input_token_limit": 96000, "encoding": "o200k_base"},
        },
    )
    before = (directory / "run.json").read_bytes()
    assert main.main(["resume", "--run-id", "old_tokens"]) == 1
    assert "Legacy runs" in capsys.readouterr().out
    assert (directory / "run.json").read_bytes() == before
