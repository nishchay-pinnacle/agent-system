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
    assert "Orientation complete" in capsys.readouterr().out
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
