"""Full conversation retention replaces the obsolete orientation compaction contract."""

import json
from pathlib import Path

import pytest
from test_orientation import FakeModel, FakePages, execute, setup_run

from research_agent.storage import Run, read_json


def test_orientation_retains_source_profile_and_question_exchanges_on_resume(
    tmp_path: Path,
) -> None:
    directory, run, model, pages = setup_run(tmp_path, max_model_calls=4)
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, model, pages)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 5
    fresh_model, fresh_pages = FakeModel(run.inputs), FakePages()
    execute(directory, reopened, fresh_model, fresh_pages)
    items = fresh_model.requests[0]["input"]
    assert len(items) == 9
    assert "# Example" in str(items)
    assert "Provisional product profile" in str(items)
    assert "Where is the product available?" in str(items)
    assert not fresh_pages.scraped
    assert not (directory / "context").exists()


def test_full_history_does_not_fit_by_silently_compacting(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path, max_model_calls=3)
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, model, pages)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 6
    reopened.settings.max_input_bytes = 1000
    fresh_model = FakeModel(run.inputs)
    with pytest.raises(ValueError, match="Nothing truncated"):
        execute(directory, reopened, fresh_model, FakePages())
    assert not fresh_model.requests
    assert read_json(directory, "debug/turn-0000.tool.json")["items"]


def test_full_utf8_request_bytes_include_reasoning_without_token_reserve(
    tmp_path: Path,
) -> None:
    class ReasoningModel(FakeModel):
        def generate(self, request):
            result = super().generate(request)
            result["output"].insert(
                0,
                {
                    "id": "reasoning-" + result["output"][0]["call_id"],
                    "type": "reasoning",
                    "encrypted_content": "opaque",
                    "summary": [],
                },
            )
            result["usage"] = {"input_tokens": 50, "output_tokens": 10}
            return result

    directory, run, _, pages = setup_run(tmp_path, max_model_calls=4)
    run.settings.model = "gpt-6-luna"  # No tokenizer registry is needed.
    (directory / "system.md").write_text(
        "Research café 世界 🌍\n" + (directory / "system.md").read_text()
    )
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, ReasoningModel(run.inputs), pages)
    context = read_json(directory, "debug/context.json")
    size = context["request_bytes"]
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 5
    reopened.settings.max_input_bytes = size - 1
    blocked = ReasoningModel(run.inputs)
    with pytest.raises(ValueError, match="Nothing truncated"):
        execute(directory, reopened, blocked, FakePages())
    assert not blocked.requests and reopened.model_calls == 4
    assert reopened.status == "incomplete"
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_input_bytes = size  # Exact boundary is allowed.
    fresh = ReasoningModel(run.inputs)
    execute(directory, reopened, fresh, FakePages())
    request = fresh.requests[0]
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert len(encoded) == size
    assert len(encoded) > len(encoded.decode("utf-8"))
    assert str(request).count("encrypted_content") >= 4
    assert "Provisional product profile" in str(request)
    assert request["truncation"] == "disabled"
    assert read_json(directory, "debug/context.json") == {
        "request_bytes": size,
        "max_input_bytes": size,
    }
    assert reopened.status == "complete"
