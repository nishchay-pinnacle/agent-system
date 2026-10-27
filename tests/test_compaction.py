"""The one initial-profile boundary, using real files and SQLite with fake providers."""

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import JsonValue
from test_orientation import FakeModel, FakePages, execute, setup_run

from research_agent import storage
from research_agent.agent import CREATE_PARAMS, run_agent
from research_agent.storage import (
    ORIENTATION_RECEIPT,
    ORIENTATION_SNAPSHOT,
    Run,
    read_json,
    save_json,
    source_id,
)
from research_agent.tools import Capture, OrientationTools


def checkpoint(directory: Path) -> dict[str, object]:
    with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
        saved = saver.get({"configurable": {"thread_id": directory.name}})
        assert saved is not None
        return saved["channel_values"]


@pytest.mark.parametrize("identical", [False, True])
def test_boundary_keeps_full_evidence_before_and_exact_snapshot_after(
    tmp_path: Path, identical: bool
) -> None:
    directory, run, _, _ = setup_run(tmp_path, identical=identical)
    archived: dict[Path, bytes] = {}

    class Pages(FakePages):
        def scrape(self, url: str) -> Capture:
            self.scraped.append(url)
            return Capture(
                markdown=f"# RAW_PAGE_BODY {url}\n"
                + "Evidence with conditions.\n" * 100
                + "END_OF_FULL_PAGE"
            )

    class Model(FakeModel):
        def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
            CREATE_PARAMS.validate_python(request)
            if ORIENTATION_RECEIPT in str(request["input"]):
                for folder in ("sources", "debug"):
                    archived.update(
                        {
                            path: path.read_bytes()
                            for path in (directory / folder).iterdir()
                            if path.is_file()
                            and (
                                folder == "sources"
                                or path.name.startswith(("turn-", "call-"))
                            )
                        }
                    )
            response = super().generate(request)
            response["output"].insert(
                0,
                {
                    "type": "reasoning",
                    "id": f"rs_{len(self.requests)}",
                    "summary": [],
                    "encrypted_content": "OLD_REASONING_MARKER",
                },
            )
            return response

    model, pages = Model(run.inputs), Pages()
    execute(directory, run, model, pages)
    assert run.status == "complete"
    assert pages.scraped == run.inputs.urls and pages.mapped == [run.inputs.site_url]
    assert len(model.requests) == len(run.inputs.urls) + 2
    profile_request = model.requests[-2]
    read_results = [
        json.loads(item["output"])
        for item in profile_request["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(read_results) == len(run.inputs.urls)
    for url, result in zip(run.inputs.urls, read_results, strict=True):
        source = f"sources/{source_id(url)}.md"
        assert result["source"] == source
        assert result["content"] == (directory / source).read_text()
        assert result["content"].endswith("END_OF_FULL_PAGE")
    assert "OLD_REASONING_MARKER" in str(profile_request)
    saved = read_json(directory, ORIENTATION_SNAPSHOT)
    assert saved["version"] == 1
    assert saved["profile"] == (directory / "profile.md").read_bytes().decode()
    assert saved["receipt"] == ORIENTATION_RECEIPT
    assert saved["sources"] == [result["source"] for result in read_results]
    request = model.requests[-1]
    assert request["input"][0] == read_json(directory, "user.json")["items"][0]
    assert len(request["input"]) == 2
    context = request["input"][1]
    assert context["role"] == "assistant"
    assert context["content"].count(saved["profile"]) == 1
    assert context["content"].count(ORIENTATION_RECEIPT) == 1
    assert "RAW_PAGE_BODY" not in str(request["input"])
    assert "OLD_REASONING_MARKER" not in str(request["input"])
    assert not any(
        item.get("type") in {"function_call", "function_call_output", "reasoning"}
        for item in request["input"]
    )
    assert {k: v for k, v in request.items() if k != "input"} == {
        k: v for k, v in profile_request.items() if k != "input"
    }
    assert all(path.read_bytes() == before for path, before in archived.items())
    state = checkpoint(directory)
    assert state["baseline"] == ORIENTATION_SNAPSHOT
    assert state["history"] == [
        f"debug/turn-{len(run.inputs.urls) + 1:04d}.response.json",
        f"debug/turn-{len(run.inputs.urls) + 1:04d}.tool.json",
    ]


@pytest.mark.parametrize(
    "interruption", ["before_snapshot", "after_snapshot", "after_checkpoint"]
)
def test_boundary_resume_reuses_work_and_freezes_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    directory, run, model, pages = setup_run(tmp_path, max_model_calls=3)
    original_save = storage.save_json

    def interrupted_save(directory: Path, name: str, value: object) -> None:
        if name == ORIENTATION_SNAPSHOT and interruption == "before_snapshot":
            raise OSError("Interrupted snapshot write")
        original_save(directory, name, value)
        if name == ORIENTATION_SNAPSHOT and interruption == "after_snapshot":
            raise OSError("Interrupted after snapshot write")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "save_json", interrupted_save)
        with pytest.raises((OSError, ValueError), match="Interrupted|limit exhausted"):
            execute(directory, run, model, pages)
    profile = (directory / "profile.md").read_bytes().decode()
    originals = {
        path: path.read_bytes() for path in (directory / "debug").glob("turn-*")
    }
    sources = {path: path.read_bytes() for path in (directory / "sources").iterdir()}
    state = checkpoint(directory)
    if interruption == "after_checkpoint":
        assert state["baseline"] == ORIENTATION_SNAPSHOT and state["history"] == []
    else:
        assert "baseline" not in state
    exists = interruption != "before_snapshot"
    assert (directory / ORIENTATION_SNAPSHOT).exists() == exists
    snapshot_bytes = (directory / ORIENTATION_SNAPSHOT).read_bytes() if exists else None
    if exists:
        (directory / "profile.md").write_text(
            "A later user edit must not change active context."
        )

    # Successful profile tool results must be reused, including before its checkpoint.
    def unexpected_profile(
        self: OrientationTools, content: str
    ) -> dict[str, JsonValue]:
        pytest.fail("Repeated a successful profile save")

    monkeypatch.setattr(OrientationTools, "update_profile", unexpected_profile)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 4
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    execute(directory, reopened, fresh_model, fresh_pages)
    assert reopened.status == "complete" and reopened.model_calls == 4
    assert len(fresh_model.requests) == 1 and not fresh_pages.scraped
    assert fresh_pages.mapped == [run.inputs.site_url]
    context = fresh_model.requests[0]["input"][1]["content"]
    assert context.endswith(profile) and context.count(profile) == 1
    assert "A later user edit" not in context
    CREATE_PARAMS.validate_python(fresh_model.requests[0])
    assert all(
        path.read_bytes() == before for path, before in {**originals, **sources}.items()
    )
    if snapshot_bytes is not None:
        assert (directory / ORIENTATION_SNAPSHOT).read_bytes() == snapshot_bytes


def test_failed_profile_write_does_not_compact(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path)

    class FailingTools(OrientationTools):
        def update_profile(self, content: str) -> dict[str, JsonValue]:
            raise OSError("Disk write failed")

    with pytest.raises(OSError, match="Disk write failed"):
        run_agent(
            directory,
            run,
            model,
            FailingTools(directory, run.inputs, run.settings, pages),
        )
    assert not (directory / ORIENTATION_SNAPSHOT).exists()
    assert not (directory / "profile.md").exists()
    assert "baseline" not in checkpoint(directory)
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    reopened = Run.model_validate(read_json(directory, "run.json"))
    execute(directory, reopened, fresh_model, fresh_pages)
    assert len(fresh_model.requests) == 1 and not fresh_pages.scraped
    assert (directory / ORIENTATION_SNAPSHOT).exists()


def test_snapshot_is_used_for_allowance_and_unknown_versions_are_rejected(
    tmp_path: Path,
) -> None:
    directory, run, model, pages = setup_run(tmp_path, max_model_calls=3)
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, model, pages)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 4
    reopened.settings.input_allowance = 1000
    fresh_model = FakeModel(run.inputs)
    with pytest.raises(ValueError, match="Complete model input"):
        execute(directory, reopened, fresh_model, FakePages())
    assert not fresh_model.requests and reopened.model_calls == 3
    snapshot = read_json(directory, ORIENTATION_SNAPSHOT)
    snapshot["version"] = 99
    save_json(directory, ORIENTATION_SNAPSHOT, snapshot)
    with pytest.raises(ValueError, match="Unsupported orientation snapshot version"):
        execute(directory, reopened, fresh_model, FakePages())
    assert not fresh_model.requests


def test_exchanges_accumulate_after_baseline_without_recompacting(
    tmp_path: Path,
) -> None:
    directory, run, _, pages = setup_run(tmp_path)
    selected: list[bytes] = []

    class Model(FakeModel):
        def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
            CREATE_PARAMS.validate_python(request)
            compacted = ORIENTATION_RECEIPT in str(request["input"])
            later = [
                item
                for item in request["input"]
                if item.get("type") == "function_call_output"
            ]
            if compacted:
                selected.append((directory / ORIENTATION_SNAPSHOT).read_bytes())
            response = super().generate(request)
            if compacted and not later:
                response["output"][0].update(
                    name="update_profile",
                    arguments=json.dumps(
                        {
                            "content": "# Revised profile\n"
                            + "\n".join(
                                f"[Source](sources/{source_id(url)}.md)"
                                for url in run.inputs.urls
                            )
                        }
                    ),
                )
            return response

    model = Model(run.inputs)
    execute(directory, run, model, pages)
    assert run.status == "complete" and run.model_calls == 5
    assert len(selected) == 2 and selected[0] == selected[1]
    snapshot = read_json(directory, ORIENTATION_SNAPSHOT)
    final_input = model.requests[-1]["input"]
    assert len(final_input) == 4
    assert final_input[1]["content"].count(snapshot["profile"]) == 1
    assert final_input[2]["type"] == "function_call"
    assert final_input[2]["name"] == "update_profile"
    assert final_input[3]["type"] == "function_call_output"
    assert final_input[3]["call_id"] == final_input[2]["call_id"]
    assert (directory / "profile.md").read_text().startswith("# Revised")
    assert snapshot["profile"].startswith("# Provisional")
    assert "# Example" not in str(final_input)


@pytest.mark.parametrize("pending", ["tool", "model"])
def test_previous_checkpoint_schema_resumes_without_reset(
    tmp_path: Path, pending: str
) -> None:
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    directory, run, _, pages = setup_run(tmp_path)
    tools = OrientationTools(directory, run.inputs, run.settings, pages)
    model = FakeModel(run.inputs)
    history = ["user.json"]
    # Seed real files in the old format: no baseline channel or snapshot.
    for turn in range(3):
        request = {
            "input": [
                item for ref in history for item in read_json(directory, ref)["items"]
            ]
        }
        response = model.generate(request)
        response_ref = f"debug/turn-{turn:04d}.response.json"
        save_json(directory, response_ref, {"items": response["output"]})
        history.append(response_ref)
        if turn == 2 and pending == "tool":
            break
        call = response["output"][0]
        result = tools.execute(call["name"], call["arguments"])
        result_ref = f"debug/turn-{turn:04d}.tool.json"
        save_json(
            directory,
            result_ref,
            {
                "items": [
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(result),
                    }
                ]
            },
        )
        history.append(result_ref)
    original_files = {
        path: path.read_bytes() for path in (directory / "debug").glob("turn-*")
    }

    class LegacyState(TypedDict):
        history: list[str]
        turn: int

    def unchanged(state: LegacyState) -> LegacyState:
        return state

    graph = StateGraph(LegacyState)
    graph.add_node("model", unchanged)
    graph.add_node("tool", unchanged)
    graph.add_edge(START, pending)
    graph.add_edge("model", END)
    graph.add_edge("tool", END)
    with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
        graph.compile(checkpointer=saver, interrupt_before=[pending]).invoke(
            {"history": history, "turn": 2 if pending == "tool" else 3},
            config={"configurable": {"thread_id": run.run_id}},
        )
    assert "baseline" not in checkpoint(directory)
    run.model_calls = 3
    storage.save_run(directory, run)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    execute(directory, reopened, fresh_model, fresh_pages)
    assert reopened.status == "complete" and reopened.model_calls == 4
    assert len(fresh_model.requests) == 1 and not fresh_pages.scraped
    assert fresh_pages.mapped == [run.inputs.site_url]
    assert all(path.read_bytes() == content for path, content in original_files.items())
    assert (directory / ORIENTATION_SNAPSHOT).exists() == (pending == "tool")
    CREATE_PARAMS.validate_python(fresh_model.requests[0])


def test_compacted_request_fits_allowance_that_full_transcript_exceeds(
    tmp_path: Path,
) -> None:
    directory, run, model, _ = setup_run(tmp_path, max_model_calls=3)

    class Pages(FakePages):
        def scrape(self, url: str) -> Capture:
            self.scraped.append(url)
            return Capture(markdown="FULL_PAGE_DETAIL " * 1000)

    pages = Pages()
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, model, pages)
    assert (
        len(json.dumps(model.requests[-1], ensure_ascii=False).encode()) + 2048 > 10000
    )
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 4
    reopened.settings.input_allowance = 10000
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    execute(directory, reopened, fresh_model, fresh_pages)
    request = fresh_model.requests[0]
    assert len(json.dumps(request, ensure_ascii=False).encode()) + 2048 <= 10000
    assert "FULL_PAGE_DETAIL" not in str(request["input"])
    assert reopened.status == "complete" and reopened.model_calls == 4
    assert not fresh_pages.scraped
