"""Offline end-to-end orientation with real files and SQLite, fake providers."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from research_agent.agent import initialize_prompts, run_agent
from research_agent.storage import (
    Inputs,
    Run,
    Settings,
    local_path,
    read_json,
    run_directory,
    save_json,
    source_id,
)
from research_agent.tools import Capture, Entry, OrientationTools


class FakePages:
    def __init__(self) -> None:
        self.scraped: list[str] = []
        self.mapped: list[str] = []
        self.fail_url: str | None = None
        self.fail_map = False
        self.large = False

    def scrape(self, url: str) -> Capture:
        self.scraped.append(url)
        if self.fail_url == url:
            return Capture(markdown="Access denied", status_code=403)
        return Capture(
            markdown="# Example\nExports require Pro. Company claim: saves time.\n"
            + ("long evidence " * 10000 if self.large else ""),
            final_url=url,
        )

    def map(self, url: str) -> list[Entry]:
        self.mapped.append(url)
        if self.fail_map:
            raise RuntimeError("Simulated map failure")
        return [
            Entry(url=url + "docs#one", title="Documentation"),
            Entry(url=url + "docs#two", description="API reference"),
            Entry(url="https://help.example.com/", title="Support"),
        ]


class FakeModel:
    def __init__(self, inputs: Inputs) -> None:
        self.inputs = inputs
        self.requests: list[dict[str, JsonValue]] = []
        self.incomplete = False

    def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        self.requests.append(request)
        items = request["input"]
        assert isinstance(items, list)
        completed = sum(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in items
        )
        if completed < len(self.inputs.urls):
            name, arguments = "read_page", {"url": self.inputs.urls[completed]}
        elif completed == len(self.inputs.urls):
            name = "update_profile"
            arguments = {
                "content": "# Provisional product profile\nCompany claims exports require Pro. Pricing unknown.\n"
                + "\n".join(f"[S{i + 1}]" for i, url in enumerate(self.inputs.urls))
            }
        elif completed == len(self.inputs.urls) + 1:
            name, arguments = (
                "update_questions",
                {
                    "active_question_id": None,
                    "questions": [
                        {
                            "id": "Q1",
                            "question": "Where is the product available?",
                            "status": "open",
                            "priority_reason": "Adoption feasibility",
                            "answer": None,
                            "answer_basis": None,
                            "source_assessments": [],
                            "closure_reason": None,
                        }
                    ],
                },
            )
        elif completed == len(self.inputs.urls) + 2:
            name, arguments = "build_site_index", {"site_url": self.inputs.site_url}
        else:
            pytest.fail("Agent continued beyond orientation")
        return {
            "status": "incomplete" if self.incomplete else "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "call_id": f"call-{completed}",
                }
            ],
        }


def setup_run(
    tmp_path: Path, *, identical: bool = False, **settings: int
) -> tuple[Path, Run, FakeModel, FakePages]:
    inputs = Inputs(
        company="Example",
        product="Widget",
        company_url="https://example.com/",
        product_url="https://example.com/"
        if identical
        else "https://example.com/product",
    )
    run = Run(
        run_id="orientation_test",
        inputs=inputs,
        settings=Settings(
            model="explicit-test-model", encoding="o200k_base", **settings
        ),
    )
    directory = run_directory(tmp_path, run.run_id)
    directory.mkdir()
    initialize_prompts(directory, run)
    return directory, run, FakeModel(inputs), FakePages()


def execute(directory: Path, run: Run, model: FakeModel, pages: FakePages) -> None:
    run_agent(
        directory,
        run,
        model,
        OrientationTools(directory, run.inputs, run.settings, pages),
    )


@pytest.mark.parametrize("identical", [False, True])
def test_orientation_saves_artifacts_and_stops(tmp_path: Path, identical: bool) -> None:
    directory, run, model, pages = setup_run(tmp_path, identical=identical)
    execute(directory, run, model, pages)
    assert run.status == "complete"
    assert pages.scraped == run.inputs.urls
    assert len(model.requests) == len(run.inputs.urls) + 3
    assert pages.mapped == [run.inputs.site_url]
    assert (directory / "profile.md").read_text().startswith("# Provisional")
    index = read_json(directory, "site_index.json")
    assert index["summary"]["discovered_count"] == 2
    assert index["entries"][0]["description"] == "API reference"
    assert (
        "entries"
        not in read_json(
            directory, f"debug/turn-{len(run.inputs.urls) + 2:04d}.tool.json"
        )["items"][0]["output"]
    )
    for url in run.inputs.urls:
        assert (directory / f"sources/{source_id(url)}.capture.json").exists()
        assert (directory / f"sources/{source_id(url)}.md").exists()
    assert "name: orientation" in model.requests[0]["instructions"]
    assert "Target campaign product: Widget" in model.requests[0]["input"][0]["content"]
    assert "# Example" in str(model.requests[1]["input"])
    assert model.requests[0]["model"] == "explicit-test-model"
    # A fresh runtime opens SQLite; completed runs do no more external work.
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    execute(directory, reopened, fresh_model, fresh_pages)
    assert (
        not fresh_model.requests and not fresh_pages.scraped and not fresh_pages.mapped
    )


def test_failed_page_is_incomplete_then_resume_reuses_first_page(
    tmp_path: Path,
) -> None:
    directory, run, model, pages = setup_run(tmp_path)
    pages.fail_url = run.inputs.product_url
    with pytest.raises(ValueError, match="unusable"):
        execute(directory, run, model, pages)
    assert read_json(directory, "run.json")["status"] == "incomplete"
    assert not (directory / "profile.md").exists()
    assert list((directory / "sources").glob("*.failed-*.json"))
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    execute(directory, reopened, fresh_model, fresh_pages)
    assert fresh_pages.scraped == [run.inputs.product_url]
    assert (
        len(fresh_model.requests) == 3
    )  # cached failed tool request is not regenerated
    assert reopened.status == "complete"


def test_failed_map_keeps_profile_and_resumes_without_model(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path)
    pages.fail_map = True
    with pytest.raises(RuntimeError, match="map failure"):
        execute(directory, run, model, pages)
    assert (directory / "profile.md").exists()
    assert not (directory / "site_index.json").exists()
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    execute(directory, reopened, fresh_model, fresh_pages)
    assert not fresh_model.requests and not fresh_pages.scraped
    assert fresh_pages.mapped == [run.inputs.site_url]


def test_model_limit_persists_and_can_be_explicitly_increased(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path, max_model_calls=1)
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, model, pages)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, reopened, FakeModel(run.inputs), FakePages())
    reopened.settings.max_model_calls = 6
    fresh_pages = FakePages()
    execute(directory, reopened, FakeModel(run.inputs), fresh_pages)
    assert fresh_pages.scraped == [run.inputs.product_url]
    assert reopened.model_calls == 5


def test_oversized_page_preserved_whole_then_resume_without_refetch(
    tmp_path: Path,
) -> None:
    directory, run, model, pages = setup_run(tmp_path, input_token_limit=10000)
    pages.large = True
    with pytest.raises(ValueError, match="Nothing truncated"):
        execute(directory, run, model, pages)
    path = directory / f"sources/{source_id(run.inputs.company_url)}.md"
    assert path.read_text().count("long evidence") == 10000
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.input_token_limit = 1000000
    fresh_pages = FakePages()
    execute(directory, reopened, FakeModel(run.inputs), fresh_pages)
    assert fresh_pages.scraped == [run.inputs.product_url]


def test_total_context_allowance_stops_before_model(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path, input_token_limit=1000)
    with pytest.raises(ValueError, match="Complete model input"):
        execute(directory, run, model, pages)
    assert not model.requests and run.model_calls == 0


def test_incomplete_model_response_is_retained_and_retried(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path)
    model.incomplete = True
    with pytest.raises(ValueError, match="Model response incomplete"):
        execute(directory, run, model, pages)
    assert (directory / "debug/call-0001.response.json").exists()
    assert not pages.scraped
    reopened = Run.model_validate(read_json(directory, "run.json"))
    execute(directory, reopened, FakeModel(run.inputs), FakePages())
    assert reopened.model_calls == 6


def test_saved_capture_reused_after_cleaning_interruption(tmp_path: Path) -> None:
    directory, run, _, pages = setup_run(tmp_path)
    url = run.inputs.company_url
    save_json(
        directory,
        f"sources/{source_id(url)}.capture.json",
        Capture(markdown="Saved evidence, Pro required.").model_dump(),
    )
    result = OrientationTools(directory, run.inputs, run.settings, pages).read_page(url)
    assert result["content"] == "Saved evidence, Pro required."
    assert not pages.scraped


def test_tool_scope_arguments_and_paths(tmp_path: Path) -> None:
    directory, run, _, pages = setup_run(tmp_path)
    tools = OrientationTools(directory, run.inputs, run.settings, pages)
    with pytest.raises(ValueError, match="supplied"):
        tools.read_page("https://example.com/extra")
    with pytest.raises(ValueError):
        tools.execute(
            "read_page", '{"url":"https://example.com/", "destination":"../outside"}'
        )
    with pytest.raises(ValueError):
        tools.update_profile("premature profile")
    with pytest.raises(ValueError):
        run_directory(tmp_path, "../escape")
    with pytest.raises(ValueError):
        local_path(directory, "../escape")
    (directory / "sources").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        tools.read_page(run.inputs.company_url)
    assert not pages.scraped


@pytest.mark.parametrize(
    "capture",
    [
        Capture(markdown="Loading..."),
        Capture(html="<main><h1>Access denied</h1><p>Navigation</p></main>"),
        Capture(content_type="application/pdf", markdown="pdf"),
        Capture(error="provider problem", markdown="text"),
    ],
)
def test_unusable_captures_never_become_evidence(
    tmp_path: Path, capture: Capture
) -> None:
    directory, run, _, pages = setup_run(tmp_path)
    save_json(
        directory,
        f"sources/{source_id(run.inputs.company_url)}.capture.json",
        capture.model_dump(),
    )
    with pytest.raises(ValueError, match="unusable"):
        OrientationTools(directory, run.inputs, run.settings, pages).read_page(
            run.inputs.company_url
        )
    assert not (
        directory / f"sources/{source_id(run.inputs.company_url)}.json"
    ).exists()


def test_index_saved_before_checkpoint_is_not_remapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent import agent

    directory, run, model, pages = setup_run(tmp_path)
    original_save = agent.save_json

    def interrupted_save(directory: Path, name: str, value: object) -> None:
        if name == "debug/turn-0004.tool.json":
            raise OSError("Simulated interruption after successful map")
        original_save(directory, name, value)

    with monkeypatch.context() as patch:
        patch.setattr(agent, "save_json", interrupted_save)
        with pytest.raises(OSError):
            execute(directory, run, model, pages)
    assert (directory / "site_index.json").exists()
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_pages, fresh_model = FakePages(), FakeModel(run.inputs)
    execute(directory, reopened, fresh_model, fresh_pages)
    assert reopened.status == "complete"
    assert (
        not fresh_pages.mapped and not fresh_pages.scraped and not fresh_model.requests
    )


def test_conversion_failure_falls_back_to_saved_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent import html_cleaning

    directory, run, _, pages = setup_run(tmp_path)
    capture = Capture(
        markdown="Saved fallback evidence",
        html="<pre>  code\n</pre><table><tr><td rowspan=2>Limit</td></tr></table>",
    )
    save_json(
        directory,
        f"sources/{source_id(run.inputs.company_url)}.capture.json",
        capture.model_dump(),
    )
    monkeypatch.setattr(
        html_cleaning, "render_markdown", lambda text: "lost protected blocks"
    )
    result = OrientationTools(directory, run.inputs, run.settings, pages).read_page(
        run.inputs.company_url
    )
    assert result["content"] == "Saved fallback evidence"
    metadata = read_json(directory, f"sources/{source_id(run.inputs.company_url)}.json")
    assert "protected block" in str(metadata["fallback_reasons"])


def test_sdk_output_with_null_optionals_replays_after_resume(tmp_path: Path) -> None:
    from openai.types.responses import ResponseFunctionToolCall

    from research_agent.agent import CREATE_PARAMS

    class SDKModel(FakeModel):
        def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
            CREATE_PARAMS.validate_python(request)
            response = super().generate(request)
            call = response["output"][0]
            response["output"] = [
                ResponseFunctionToolCall.model_validate(
                    {**call, "id": "fc_test"}
                ).model_dump(mode="json")
            ]
            return response

    directory, run, _, pages = setup_run(tmp_path)
    model = SDKModel(run.inputs)
    pages.fail_url = run.inputs.company_url
    with pytest.raises(ValueError, match="unusable"):
        execute(directory, run, model, pages)
    saved = read_json(directory, "debug/turn-0000.response.json")
    assert saved["items"][0]["namespace"] is None
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model = SDKModel(run.inputs)
    execute(directory, reopened, fresh_model, FakePages())
    assert reopened.status == "complete"
    replay = fresh_model.requests[0]["input"][1]
    assert "namespace" not in replay
    assert replay["call_id"] == "call-0"
    assert read_json(directory, "debug/turn-0000.response.json") == saved


@pytest.mark.parametrize("budget", [3, 6, 7])
def test_rejected_profile_can_be_corrected_after_resume(
    tmp_path: Path, budget: int
) -> None:
    directory, run, _, pages = setup_run(tmp_path, max_model_calls=budget)

    class CorrectingModel(FakeModel):
        def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
            items = request["input"]
            assert isinstance(items, list)
            rejected = [
                item
                for item in items
                if isinstance(item, dict) and item.get("call_id") == "bad-profile"
            ]
            if rejected:
                error = json.loads(rejected[-1]["output"])
                assert "Profile must reference every starting source" in error["error"]
                request = {
                    **request,
                    "input": [item for item in items if item not in rejected],
                }
            response = super().generate(request)
            call = response["output"][0]
            if call["name"] == "update_profile" and not rejected:
                call["call_id"] = "bad-profile"
                call["arguments"] = '{"content":"Profile without citations"}'
            return response

    model = CorrectingModel(run.inputs)
    if budget == 7:

        class LegacyTools(OrientationTools):
            def update_profile(self, content: str) -> dict[str, JsonValue]:
                raise ValueError("Legacy citation rejection")

        with pytest.raises(ValueError, match="Legacy citation rejection"):
            run_agent(
                directory,
                run,
                model,
                LegacyTools(directory, run.inputs, run.settings, pages),
            )
        assert not (directory / "debug/turn-0002.tool.json").exists()
        run = Run.model_validate(read_json(directory, "run.json"))
        model, pages = CorrectingModel(run.inputs), FakePages()
    if budget == 3:
        with pytest.raises(ValueError, match="limit exhausted"):
            execute(directory, run, model, pages)
        assert not (directory / "context").exists()
        assert not (directory / "profile.md").exists()
        run = Run.model_validate(read_json(directory, "run.json"))
        run.settings.max_model_calls = 6
        model, pages = CorrectingModel(run.inputs), FakePages()
    execute(directory, run, model, pages)
    assert run.status == "complete" and run.model_calls == 6
    assert (directory / "profile.md").read_text().count("[S") == 2
    if budget in {3, 7}:
        assert len(model.requests) == 3 and not pages.scraped


@pytest.mark.parametrize(
    "damage",
    [
        "missing_source",
        "changed_source",
        "changed_capture",
        "broken_index",
        "invalid_index",
    ],
)
def test_resume_rejects_damaged_artifacts_without_provider_calls(
    tmp_path: Path, damage: str
) -> None:
    directory, run, model, pages = setup_run(tmp_path)
    execute(directory, run, model, pages)
    sid = source_id(run.inputs.company_url)
    source = directory / f"sources/{sid}.md"
    if damage == "missing_source":
        source.unlink()
    elif damage == "changed_source":
        source.write_text("Changed evidence")
    elif damage == "changed_capture":
        save_json(
            directory,
            f"sources/{sid}.capture.json",
            Capture(markdown="Changed original").model_dump(),
        )
    else:
        (directory / "site_index.json").write_text(
            "{" if damage == "broken_index" else "{}"
        )
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    with pytest.raises(ValueError):
        execute(directory, reopened, fresh_model, fresh_pages)
    assert read_json(directory, "run.json")["status"] == "incomplete"
    assert (
        not fresh_model.requests and not fresh_pages.scraped and not fresh_pages.mapped
    )
