"""Timeout dispatch and persisted-setting compatibility, without provider calls."""

from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from agent_system.company_intelligence.agent import FetchResult, Generation
from agent_system.company_intelligence.call_traces import call_details
from agent_system.company_intelligence.schemas import Inputs, Research, ResearchSettings
from agent_system.company_intelligence.workflow import (
    LimitReached,
    build_research_workflow,
    execute,
    load_research,
    operation,
    reserve,
)


@pytest.mark.parametrize(
    ("kind", "configured"),
    [
        ("generate", 300),
        ("count_input", 90),
        ("scrape_page", 90),
        ("find_site_pages", 90),
    ],
)
@pytest.mark.parametrize("remaining", [1000, 37.5])
def test_request_timeout_at_provider_boundary(
    tmp_path: Path, kind: str, configured: int, remaining: float
) -> None:
    state = Research(
        job_id=str(uuid4()),
        inputs=Inputs(
            company="Example",
            url="https://example.com/",
            product="Widget",
            product_url="https://example.com/",
        ),
        settings=ResearchSettings(),
        instructions="Synthetic test",
    )
    state.usage.active_seconds = state.settings.active_seconds - remaining
    operation(
        state,
        kind,
        {"url": "https://example.com/", "query": ""}
        if kind in {"scrape_page", "find_site_pages"}
        else {},
    )
    assert state.operation is not None
    state.operation.counted_input = 100
    expected = min(configured, remaining)
    dispatched: list[float] = []

    class Provider:
        def count_input(self, state: Research, timeout: float) -> int:
            dispatched.append(timeout)
            return 100

        def generate(self, state: Research, timeout: float) -> Generation:
            dispatched.append(timeout)
            return Generation(output=[], input_tokens=100, output_tokens=10)

        def retrieve(
            self, name: str, args: dict[str, object], timeout: float
        ) -> FetchResult:
            dispatched.append(timeout)
            return FetchResult(markdown="# Saved page")

    reserve(state, tmp_path)
    assert state.operation.reserved_seconds == expected
    assert state.usage.active_seconds <= state.settings.active_seconds
    execute(state, tmp_path, Provider())
    assert dispatched == [expected]
    if kind in {"generate", "count_input"}:
        metadata = call_details(tmp_path, "op_00001_1")["metadata"]
        assert metadata["timeout_seconds"] == expected
        assert metadata["configured_timeout_seconds"] == configured


def test_retry_and_exhausted_time_budget(tmp_path: Path) -> None:
    state = Research(
        job_id=str(uuid4()),
        inputs=Inputs(
            company="Example",
            url="https://example.com/",
            product="Widget",
            product_url="https://example.com/",
        ),
        settings=ResearchSettings(active_seconds=350),
        instructions="Synthetic test",
    )
    operation(state, "generate")
    assert state.operation is not None
    state.operation.counted_input = 100
    reserve(state, tmp_path)
    assert state.operation.reserved_seconds == 300
    # An uncertain first attempt retains its full reservation on recovery.
    reserve(state, tmp_path)
    assert state.operation.attempts == 2 and state.operation.reserved_seconds == 50
    with pytest.raises(LimitReached, match="active_seconds"):
        reserve(state, tmp_path)


def test_reopen_preserves_new_and_legacy_settings(tmp_path: Path) -> None:
    database = str(tmp_path / "checkpoints.sqlite")
    state = Research(
        job_id=str(uuid4()),
        inputs=Inputs(
            company="Example",
            url="https://example.com/",
            product="Widget",
            product_url="https://example.com/",
        ),
        settings=ResearchSettings(generation_request_seconds=240, request_seconds=80),
        instructions="Synthetic test",
    )
    raw = state.model_dump(mode="json")
    legacy = state.model_dump(mode="json")
    legacy["settings"].pop("generation_request_seconds")
    with SqliteSaver.from_conn_string(database) as saver:
        graph = build_research_workflow(tmp_path, saver)
        graph.update_state({"configurable": {"thread_id": "new"}}, {"research": raw})
        graph.update_state(
            {"configurable": {"thread_id": "legacy"}}, {"research": legacy}
        )
    with SqliteSaver.from_conn_string(database) as saver:
        new = load_research(saver, tmp_path, "new")
        old = load_research(saver, tmp_path, "legacy")
    assert new and new.settings.request_timeout("generate") == 240
    assert new.settings.request_timeout("count_input") == 80
    assert old and old.settings.request_timeout("generate") == 80
    assert old.settings.request_timeout("scrape_page") == 80
