"""CLI workflow intent and handoff recovery with fake providers and real checkpoints."""

from pathlib import Path

import pytest
from pydantic import JsonValue
from test_orientation import FakeModel, FakePages
from test_research import ResearchModel

from research_agent import agent, main
from research_agent.storage import RESEARCH_START, Inputs, Run, read_json

INPUTS = Inputs(
    company="Example",
    product="Widget",
    company_url="https://example.com/",
    product_url="https://example.com/product",
)
START = [
    "start",
    "--company",
    INPUTS.company,
    "--product",
    INPUTS.product,
    "--company-url",
    INPUTS.company_url,
    "--product-url",
    INPUTS.product_url,
]


class WorkflowModel:
    def __init__(self, *, two_reads: bool = False) -> None:
        self.orientation = FakeModel(INPUTS)
        steps = [("read_page", {"url": "https://example.com/docs"})]
        if two_reads:
            steps.append(("read_page", {"url": "https://help.example.com/"}))
        steps.append(
            ("finish_research", {"reason": "Remaining questions need an interview"})
        )
        self.research = ResearchModel(steps)

    def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if "name: research" in str(request["instructions"]):
            return self.research.generate(request)
        return self.orientation.generate(request)


@pytest.fixture
def providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WorkflowModel, FakePages]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_MODEL", "explicit-test-model")
    monkeypatch.setenv("RESEARCH_DATA_DIR", "data")
    monkeypatch.setenv("RESEARCH_MAX_MODEL_CALLS", "4")
    monkeypatch.setenv("RESEARCH_INPUT_ALLOWANCE", "96000")
    model, pages = WorkflowModel(), FakePages()
    monkeypatch.setattr(main, "OpenAIModel", lambda key, timeout: model)
    monkeypatch.setattr(main, "FirecrawlPages", lambda key, settings: pages)
    return model, pages


@pytest.mark.parametrize("custom", [False, True])
def test_start_runs_both_stages_and_persists_research_allowances(
    tmp_path: Path,
    providers: tuple[WorkflowModel, FakePages],
    custom: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model, pages = providers
    args = (
        [
            "--model-calls",
            "8",
            "--page-budget",
            "3",
            "--input-allowance",
            "180000",
            "--index-result-limit",
            "7",
        ]
        if custom
        else []
    )
    assert main.main([*START, *args]) == 0
    directory = next((tmp_path / "data/runs").iterdir())
    run = Run.model_validate(read_json(directory, "run.json"))
    assert run.stage == "research" and run.status == "complete"
    assert run.workflow_intent is not None and run.workflow_intent.full_workflow
    assert run.workflow_intent.model_calls == (8 if custom else 30)
    assert run.settings.max_model_calls == (12 if custom else 34)
    assert run.settings.input_allowance == (180000 if custom else 160000)
    assert run.settings.index_result_limit == (7 if custom else 20)
    assert run.research_page_budget == (3 if custom else 20)
    assert run.research_page_attempts == 1 and run.model_calls == 6
    original = read_json(directory, RESEARCH_START)["original_run"]
    assert original["settings"]["max_model_calls"] == 4
    assert original["settings"]["input_allowance"] == 96000
    assert len(model.orientation.requests) == 4 and len(model.research.requests) == 2
    assert len(pages.scraped) == 3 and pages.mapped == [INPUTS.site_url]
    output = capsys.readouterr().out
    assert "Orientation complete — continuing to research" in output
    assert "Complete · Orientation complete" not in output
    assert "Complete · Research complete" in output
    assert output.count("Run summary · Complete") == 1
    assert "Model requests: 6" in output and "Total API requests: 10" in output
    assert "Total tokens (reported): 240" in output
    # A completed full workflow may be reopened without any additional calls.
    assert main.main(["resume", "--run-id", directory.name]) == 0
    assert len(model.orientation.requests) == 4 and len(model.research.requests) == 2
    assert len(pages.scraped) == 3 and len(pages.mapped) == 1


def test_orientation_only_remains_orientation_on_resume(
    tmp_path: Path,
    providers: tuple[WorkflowModel, FakePages],
) -> None:
    model, _ = providers
    assert main.main([*START, "--orientation-only"]) == 0
    directory = next((tmp_path / "data/runs").iterdir())
    run = Run.model_validate(read_json(directory, "run.json"))
    assert run.workflow_intent is not None and not run.workflow_intent.full_workflow
    assert run.workflow_intent.model_calls == 30
    assert run.stage == "orientation" and run.status == "complete"
    assert not (directory / RESEARCH_START).exists()
    assert main.main(["resume", "--run-id", directory.name]) == 0
    assert not model.research.requests and len(model.orientation.requests) == 4
    # Explicit extension is still available for these runs.
    assert (
        main.main(
            [
                "research",
                "--run-id",
                directory.name,
                "--model-calls",
                "5",
                "--page-budget",
                "2",
                "--input-allowance",
                "160000",
            ]
        )
        == 0
    )
    assert len(model.research.requests) == 2


@pytest.mark.parametrize("failure", ["map", "limit"])
def test_orientation_failure_stops_then_fresh_resume_continues_workflow(
    tmp_path: Path,
    providers: tuple[WorkflowModel, FakePages],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    model, pages = providers
    if failure == "map":
        pages.fail_map = True
    else:
        monkeypatch.setenv("RESEARCH_MAX_MODEL_CALLS", "1")
    assert main.main(START) == 1
    directory = next((tmp_path / "data/runs").iterdir())
    assert not (directory / RESEARCH_START).exists() and not model.research.requests
    output = capsys.readouterr().out
    assert "Orientation incomplete" in output and "resume --run-id" in output
    assert "Run summary · Complete" not in output
    assert "continuing to research" not in output
    fresh_model, fresh_pages = WorkflowModel(), FakePages()
    monkeypatch.setattr(main, "OpenAIModel", lambda key, timeout: fresh_model)
    monkeypatch.setattr(main, "FirecrawlPages", lambda key, settings: fresh_pages)
    assert (
        main.main(["resume", "--run-id", directory.name, "--max-model-calls", "4"]) == 0
    )
    run = Run.model_validate(read_json(directory, "run.json"))
    assert run.stage == "research" and run.status == "complete"
    assert run.settings.max_model_calls == 34 and run.model_calls == 6
    assert len(fresh_model.orientation.requests) == (0 if failure == "map" else 3)
    assert len(fresh_pages.scraped) == (1 if failure == "map" else 2)


@pytest.mark.parametrize(
    "boundary", ["before_snapshot", "after_snapshot", "after_activation"]
)
def test_fresh_resume_across_automatic_handoff(
    tmp_path: Path,
    providers: tuple[WorkflowModel, FakePages],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    boundary: str,
) -> None:
    model, _ = providers
    begin, save = main.begin_research, agent.save_json

    def interrupted_begin(*args: object, **kwargs: object) -> None:
        if boundary == "after_activation":
            begin(*args, **kwargs)
        raise OSError("handoff interruption")

    def interrupted_snapshot(directory: Path, name: str, value: object) -> None:
        save(directory, name, value)
        if name == RESEARCH_START:
            raise OSError("handoff interruption")

    with monkeypatch.context() as patch:
        if boundary == "after_snapshot":
            patch.setattr(agent, "save_json", interrupted_snapshot)
        else:
            patch.setattr(main, "begin_research", interrupted_begin)
        assert main.main(START) == 1
    directory = next((tmp_path / "data/runs").iterdir())
    assert len(model.orientation.requests) == 4 and not model.research.requests
    output = capsys.readouterr().out
    assert "Research incomplete" in output and "resume --run-id" in output
    assert "Run summary · Complete" not in output
    preserved = {
        path: path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and path.name not in {"run.json", "firecrawl-counts.json"}
    }
    fresh_model, fresh_pages = WorkflowModel(), FakePages()
    monkeypatch.setattr(main, "OpenAIModel", lambda key, timeout: fresh_model)
    monkeypatch.setattr(main, "FirecrawlPages", lambda key, settings: fresh_pages)
    assert main.main(["resume", "--run-id", directory.name]) == 0
    run = Run.model_validate(read_json(directory, "run.json"))
    assert run.stage == "research" and run.status == "complete"
    assert run.model_calls == 6 and run.settings.max_model_calls == 34
    assert run.research_page_budget == 20 and run.research_page_attempts == 1
    assert not fresh_model.orientation.requests and not fresh_pages.mapped
    assert len(fresh_model.research.requests) == 2 and len(fresh_pages.scraped) == 1
    assert all(path.read_bytes() == data for path, data in preserved.items())


def test_active_research_resume_does_not_reset_allowances(
    tmp_path: Path,
    providers: tuple[WorkflowModel, FakePages],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = WorkflowModel(two_reads=True)
    monkeypatch.setattr(main, "OpenAIModel", lambda key, timeout: model)
    assert main.main([*START, "--page-budget", "1"]) == 1
    directory = next((tmp_path / "data/runs").iterdir())
    snapshot = (directory / RESEARCH_START).read_bytes()
    output = capsys.readouterr().out
    assert "Research incomplete" in output and "page budget exhausted" in output
    assert "Run summary · Complete" not in output
    fresh_model, fresh_pages = WorkflowModel(two_reads=True), FakePages()
    monkeypatch.setattr(main, "OpenAIModel", lambda key, timeout: fresh_model)
    monkeypatch.setattr(main, "FirecrawlPages", lambda key, settings: fresh_pages)
    assert (
        main.main(
            [
                "resume",
                "--run-id",
                directory.name,
                "--page-budget",
                "3",
                "--max-model-calls",
                "40",
            ]
        )
        == 0
    )
    run = Run.model_validate(read_json(directory, "run.json"))
    assert run.model_calls == 7 and run.settings.max_model_calls == 40
    assert run.research_page_budget == 3 and run.research_page_attempts == 2
    assert not fresh_model.orientation.requests and not fresh_pages.mapped
    assert len(fresh_model.research.requests) == 1 and len(fresh_pages.scraped) == 1
    assert (directory / RESEARCH_START).read_bytes() == snapshot
