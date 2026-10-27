"""Full conversation retention replaces the obsolete orientation compaction contract."""

from pathlib import Path

import pytest
from test_orientation import FakeModel, FakePages, execute, setup_run

from research_agent.storage import Run, Settings, read_json
from research_agent.token_counting import estimate_input


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


def test_tokenizer_fails_explicitly_for_unknown_model_and_reserves_opaque_reasoning() -> (
    None
):
    with pytest.raises(ValueError, match="Unsupported tokenizer"):
        estimate_input({"input": []}, Settings(model="unknown-model"))
    settings = Settings(model="unknown-model", encoding="o200k_base")
    short = estimate_input({"input": [{"encrypted_content": "opaque"}]}, settings)
    long = estimate_input(
        {"input": [{"encrypted_content": "opaque" * 10000}]}, settings
    )
    assert short == long
    assert short["opaque_reasoning_reserve"] == settings.output_tokens


def test_full_history_does_not_fit_by_silently_compacting(tmp_path: Path) -> None:
    directory, run, model, pages = setup_run(tmp_path, max_model_calls=3)
    with pytest.raises(ValueError, match="limit exhausted"):
        execute(directory, run, model, pages)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 6
    reopened.settings.context_tokens = (
        8500  # Only 500 tokens remain after output reservation.
    )
    fresh_model = FakeModel(run.inputs)
    with pytest.raises(ValueError, match="Nothing truncated"):
        execute(directory, reopened, fresh_model, FakePages())
    assert not fresh_model.requests
    assert read_json(directory, "debug/turn-0000.tool.json")["items"]
