"""Single-agent research using fake providers and real saved runs/checkpoints."""

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import JsonValue
from test_orientation import FakePages, execute, setup_run

from research_agent.agent import CREATE_PARAMS, begin_research, run_agent
from research_agent.storage import RESEARCH_START, Run, read_json, source_id
from research_agent.tools import Capture, Entry, OrientationTools, ToolInputError


class CompanyPages(FakePages):
    def scrape(self, url: str) -> Capture:
        self.scraped.append(url)
        if self.fail_url == url:
            raise RuntimeError("Temporary provider outage")
        if url.endswith("/blocked"):
            return Capture(markdown="Access denied", status_code=403)
        if url.endswith("/media"):
            return Capture(markdown="Not HTML", content_type="application/pdf")
        if url.endswith("/redirect"):
            return Capture(
                markdown="Redirect evidence",
                final_url="https://unrelated.test/destination",
            )
        return Capture(
            markdown=f"# Full evidence for {url}\nExports require Pro; region constraints apply.\n"
            "[Linked company page](/linked)\n"
            "[External documentation](https://docs.vendor.test/api)\n"
            "[Not automatically authorized](https://docs.vendor.test/other)\n"
            if url == "https://docs.vendor.test/api"
            else f"# Full evidence for {url}\nExports require Pro; region constraints apply.\n"
            "[Linked company page](https://example.com/linked)\n"
            "[External documentation](https://docs.vendor.test/api)\n"
            "[Spec](https://example.com/spec.pdf)\n",
            final_url=url,
            content_type="text/html",
        )

    def map(self, url: str) -> list[Entry]:
        self.mapped.append(url)
        return [
            Entry(
                url="https://example.com/docs",
                title="Product API guide",
                description="Install securely",
            ),
            Entry(
                url="https://example.com/docs/install",
                title="Install guide",
                description="Product API steps",
            ),
            Entry(
                url="https://help.example.com/guide",
                title="Support",
                description="Product API help",
            ),
            Entry(url="https://example.com/blocked", title="Blocked"),
            Entry(url="https://example.com/media", title="Media"),
            Entry(url="https://example.com/redirect", title="Redirect"),
            Entry(url="https://unrelated.test/", title="External index result"),
        ]


class ResearchModel:
    def __init__(self, steps: list[tuple[str, dict[str, object]]]) -> None:
        self.steps = steps
        self.requests: list[dict[str, JsonValue]] = []

    def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        CREATE_PARAMS.validate_python(request)
        self.requests.append(request)
        items = request["input"]
        assert isinstance(items, list)
        completed = sum(
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and str(item.get("call_id", "")).startswith("research-")
            for item in items
        )
        assert completed < len(self.steps), "Unexpected extra model call"
        name, arguments = self.steps[completed]
        if name == "finish_research":
            latest = next(
                (
                    json.loads(item["arguments"])["content"]
                    for item in reversed(items)
                    if isinstance(item, dict)
                    and item.get("type") == "function_call"
                    and item.get("name") == "update_profile"
                ),
                "# Final profile [S1]",
            )
            arguments = {
                "profile": latest,
                "question_index": {
                    "active_question_id": None,
                    "questions": [
                        {
                            "id": "Q1",
                            "question": "Where is the product available?",
                            "status": "needs_interview",
                            "priority_reason": "Adoption feasibility",
                            "answer": None,
                            "answer_basis": None,
                            "source_assessments": [],
                            "closure_reason": "Unresolved through public research: plausible public leads exhausted.",
                        }
                    ],
                },
                **arguments,
            }
        return {
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "output": [
                {
                    "type": "function_call",
                    "call_id": f"research-{completed}",
                    "name": name,
                    "arguments": json.dumps(arguments),
                }
            ],
        }


def oriented(tmp_path: Path) -> tuple[Path, Run, CompanyPages]:
    directory, run, model, _ = setup_run(tmp_path, max_model_calls=5)
    pages = CompanyPages()
    execute(directory, run, model, pages)
    assert run.status == "complete" and run.model_calls == 5
    return directory, run, pages


def enter(
    directory: Path,
    run: Run,
    pages: CompanyPages,
    calls: int = 12,
    budget: int = 6,
    allowance: int = 96000,
) -> OrientationTools:
    tools = OrientationTools(directory, run.inputs, run.settings, pages, run)
    begin_research(
        directory,
        run,
        tools,
        model_calls=calls,
        page_budget=budget,
        input_token_limit=allowance,
    )
    tools.settings = run.settings
    return tools


def profile(url: str, title: str = "Supported profile") -> str:
    return f"# {title}\nExports require Pro. Regional availability remains unresolved.\n[S1]"


def test_research_continuation_preserves_orientation_and_retains_all_exchanges(
    tmp_path: Path,
) -> None:
    directory, run, pages = oriented(tmp_path)
    original = {
        path: path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
        and path.name
        not in {
            "run.json",
            "profile.md",
            "firecrawl-counts.json",
            "events.jsonl",
            "context.json",
            "questions.json",
            "source_index.json",
        }
    }
    current_profile = (
        profile(run.inputs.company_url, "User's exact current profile") + " [S2]"
    )
    (directory / "profile.md").write_text(current_profile)
    tools = enter(directory, run, pages)
    assert run.stage == "research" and run.status == "incomplete"
    assert run.settings.max_model_calls == 17
    steps = [
        ("query_site_index", {"query": '"product api"', "limit": 10}),
        ("read_page", {"url": "https://example.com/docs"}),
        ("update_profile", {"content": profile("https://example.com/docs")}),
        ("read_page", {"url": "https://docs.vendor.test/api"}),
        (
            "update_profile",
            {
                "content": profile(
                    "https://docs.vendor.test/api", "Revised product understanding"
                )
            },
        ),
        (
            "finish_research",
            {
                "reason": "Important areas are documented; remaining regional questions need a team interview."
            },
        ),
    ]
    model = ResearchModel(steps)
    run_agent(directory, run, model, tools)
    assert run.status == "complete" and run.stage == "research"
    assert run.model_calls == 11 and run.research_page_attempts == 2
    assert len(pages.mapped) == 1  # No repeated orientation or map.
    assert len(model.requests) == 6
    first = model.requests[0]
    assert (
        "name: research" in first["instructions"]
        and "name: orientation" not in first["instructions"]
    )
    assert "build the company site index, then stop" not in str(first)
    assert "research is now active" in first["input"][-1]["content"]
    assert "Full evidence for https://example.com/" in str(first["input"])
    assert "Target campaign product: Widget" in first["input"][0]["content"]
    assert {tool["name"] for tool in first["tools"]} == {
        "query_site_index",
        "read_page",
        "update_profile",
        "finish_research",
        "update_questions",
    }
    final_input = model.requests[-1]["input"]
    assert len(final_input) == 22
    assert "Full evidence for https://example.com/docs" in str(final_input)
    assert "Full evidence for https://docs.vendor.test/api" in str(final_input)
    assert "Supported profile" in str(
        final_input
    ) and "Revised product understanding" in str(final_input)
    assert (directory / "profile.md").read_text() == steps[4][1]["content"]
    assert (
        read_json(directory, "research/completion.json")["reason"]
        == steps[-1][1]["reason"]
    )
    assert read_json(directory, RESEARCH_START)["history"][0] == "user.json"
    assert all(path.read_bytes() == value for path, value in original.items())
    with SqliteSaver.from_conn_string(
        str(directory / "research/checkpoints.sqlite")
    ) as saver:
        state = saver.get({"configurable": {"thread_id": run.run_id}})["channel_values"]
    assert "baseline" not in state and len(state["history"]) == 24
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_pages, fresh_model = CompanyPages(), ResearchModel(steps)
    run_agent(
        directory,
        reopened,
        fresh_model,
        OrientationTools(directory, run.inputs, run.settings, fresh_pages),
    )
    assert (
        not fresh_model.requests and not fresh_pages.scraped and not fresh_pages.mapped
    )


def test_index_search_phrases_paths_pagination_and_cache_indicators(
    tmp_path: Path,
) -> None:
    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    first = tools.query_site_index('"PRODUCT api"', limit=2)
    assert first["total_matches"] == 3 and first["returned_matches"] == 2
    assert first["next_offset"] == 2 and first["has_more"] is True
    assert [entry["url"] for entry in first["matches"]] == [
        "https://example.com/docs",
        "https://example.com/docs/install",
    ]
    second = tools.query_site_index('"product api"', offset=2, limit=2)
    assert second["returned_matches"] == 1 and second["next_offset"] is None
    assert tools.query_site_index("missing") == {
        "matches": [],
        "total_matches": 0,
        "returned_matches": 0,
        "offset": 0,
        "limit": 20,
        "next_offset": None,
        "has_more": False,
        "evidence": "Discovery leads only; read selected HTML pages before citing",
    }
    filtered = tools.query_site_index("install guide", url_contains="/DOCS")
    assert filtered["returned_matches"] == 2
    before = len(pages.scraped)
    assert all(not entry["already_saved"] for entry in first["matches"])
    assert len(pages.scraped) == before
    tools.read_page("https://example.com/docs")
    assert (
        tools.query_site_index("", url_contains="/docs")["matches"][0]["already_saved"]
        is True
    )
    tools.read_page("https://example.com/docs")
    assert len(pages.scraped) == before + 1 and run.research_page_attempts == 1
    with pytest.raises(ValueError):
        tools.execute("query_site_index", '{"query":"", "limit":51}')
    with pytest.raises(ToolInputError, match="Unclosed quote"):
        tools.query_site_index('"unterminated')


def test_research_url_scope_and_redirects(tmp_path: Path) -> None:
    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    for url in (
        "https://example.com/linked",
        "https://help.example.com/guide",
        "https://docs.vendor.test/api",
    ):
        assert tools.read_page(url)["source"] == f"sources/{source_id(url)}.md"
    for url in (
        "https://example.com/arbitrary",
        "https://unrelated.test/",
        "https://docs.vendor.test/other",
        "http://127.0.0.1/private",
    ):
        before = len(pages.scraped)
        with pytest.raises(ValueError):
            tools.execute("read_page", json.dumps({"url": url}))
        assert len(pages.scraped) == before
    result = tools.read_page("https://example.com/redirect")
    assert result["recoverable"] is True and "scope" in result["error"]
    assert not (
        directory / f"sources/{source_id('https://example.com/redirect')}.json"
    ).exists()


def test_unsupported_blocked_pages_and_persisted_fetch_budget(tmp_path: Path) -> None:
    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages, budget=2)
    before = len(pages.scraped)
    assert tools.read_page("https://example.com/spec.pdf")["recoverable"] is True
    assert len(pages.scraped) == before and run.research_page_attempts == 0
    for url in ("https://example.com/blocked", "https://example.com/media"):
        result = tools.read_page(url)
        assert result["recoverable"] is True and "source" not in result
        assert (directory / result["capture"]).exists()
    assert run.research_page_attempts == 2
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh = OrientationTools(
        directory, reopened.inputs, reopened.settings, pages, reopened
    )
    with pytest.raises(RuntimeError, match="page budget exhausted"):
        fresh.read_page("https://example.com/docs")
    assert len(pages.scraped) == before + 2
    # Already saved starting sources remain readable even at the page budget.
    assert fresh.read_page(run.inputs.company_url)["source"]
    assert reopened.research_page_attempts == 2


def test_research_integrity_and_citation_validation(tmp_path: Path) -> None:
    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    tools.read_page("https://example.com/docs")
    supported = profile("https://example.com/docs") + " [S3]"
    tools.update_profile(supported)
    with pytest.raises(ToolInputError, match="not saved evidence"):
        tools.update_profile("[Invented](sources/s_0000000000000000.md)")
    with pytest.raises(ToolInputError, match="Invalid local source citation"):
        tools.update_profile("[Escape](../sources/other.md)")
    assert (directory / "profile.md").read_text() == supported
    source = directory / f"sources/{source_id('https://example.com/docs')}.md"
    source.write_text("corrupted")
    before = len(pages.scraped)
    with pytest.raises(ValueError, match="invalid hash"):
        tools.read_page("https://example.com/docs")
    with pytest.raises(ValueError, match="invalid hash"):
        tools.update_profile(supported)
    assert len(pages.scraped) == before


@pytest.mark.parametrize("committed", [False, True])
def test_transition_resume_uses_frozen_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    from research_agent import storage

    directory, run, pages = oriented(tmp_path)
    initial = (directory / "profile.md").read_text()
    save = storage.save_run

    def interrupt(directory: Path, run: Run) -> None:
        if committed:
            save(directory, run)
        raise OSError("transition interruption")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "save_run", interrupt)
        with pytest.raises(OSError, match="transition interruption"):
            enter(directory, run, pages)
    saved_start = (directory / RESEARCH_START).read_bytes()
    (directory / "profile.md").write_text(profile(run.inputs.company_url, "Later edit"))
    reopened = Run.model_validate(read_json(directory, "run.json"))
    assert reopened.stage == ("research" if committed else "orientation")
    model = ResearchModel(
        [("finish_research", {"reason": "No useful public gaps remain"})]
    )
    fresh_pages = CompanyPages()
    run_agent(
        directory,
        reopened,
        model,
        OrientationTools(directory, reopened.inputs, reopened.settings, fresh_pages),
    )
    assert reopened.stage == "research" and reopened.status == "complete"
    assert reopened.model_calls == 6 and reopened.settings.max_model_calls == 17
    assert initial == next(
        json.loads(item["arguments"])["content"]
        for item in model.requests[0]["input"]
        if item.get("name") == "update_profile"
    )
    assert "Later edit" not in str(model.requests[0])
    assert (directory / RESEARCH_START).read_bytes() == saved_start
    assert not fresh_pages.scraped and not fresh_pages.mapped


@pytest.mark.parametrize("turn", [0, 1, 2])
@pytest.mark.parametrize("saved_result", [False, True])
def test_research_reuses_successful_work_across_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turn: int, saved_result: bool
) -> None:
    from research_agent import agent

    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    steps = [
        ("read_page", {"url": "https://example.com/docs"}),
        ("update_profile", {"content": profile("https://example.com/docs")}),
        ("finish_research", {"reason": "Remaining questions need an interview"}),
    ]
    model = ResearchModel(steps)
    save = agent.save_json

    def interrupt(directory: Path, name: str, value: object) -> None:
        if name == f"debug/research-turn-{turn:04d}.tool.json":
            if saved_result:
                save(directory, name, value)
            raise OSError("tool checkpoint interruption")
        save(directory, name, value)

    with monkeypatch.context() as patch:
        patch.setattr(agent, "save_json", interrupt)
        with pytest.raises(OSError, match="tool checkpoint interruption"):
            run_agent(directory, run, model, tools)
    assert run.status == "incomplete" and len(model.requests) == turn + 1
    assert run.research_page_attempts == 1
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model, fresh_pages = ResearchModel(steps), CompanyPages()
    run_agent(
        directory,
        reopened,
        fresh_model,
        OrientationTools(directory, reopened.inputs, reopened.settings, fresh_pages),
    )
    assert reopened.status == "complete" and reopened.model_calls == 8
    assert len(fresh_model.requests) == 2 - turn
    assert reopened.research_page_attempts == 1 and not fresh_pages.scraped
    assert (directory / "profile.md").read_text() == steps[1][1]["content"]
    assert (directory / "research/completion.json").is_file()


def test_failed_profile_save_replays_without_advancing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent import tools as tools_module

    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    initial = (directory / "profile.md").read_bytes()
    steps = [
        (
            "update_profile",
            {"content": profile(run.inputs.company_url, "New revision")},
        ),
        ("finish_research", {"reason": "Public evidence exhausted"}),
    ]
    model = ResearchModel(steps)
    write = tools_module.write_text

    def fail(directory: Path, name: str, content: str) -> None:
        if name == "profile.md":
            raise OSError("disk unavailable")
        write(directory, name, content)

    with monkeypatch.context() as patch:
        patch.setattr(tools_module, "write_text", fail)
        with pytest.raises(OSError, match="disk unavailable"):
            run_agent(directory, run, model, tools)
    assert (directory / "profile.md").read_bytes() == initial
    assert not (directory / "debug/research-turn-0000.tool.json").exists()
    assert not (directory / "research/completion.json").exists()
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model = ResearchModel(steps)
    run_agent(
        directory,
        reopened,
        fresh_model,
        OrientationTools(directory, reopened.inputs, reopened.settings, CompanyPages()),
    )
    assert len(fresh_model.requests) == 1 and reopened.status == "complete"
    assert (directory / "profile.md").read_text() == steps[0][1]["content"]


@pytest.mark.parametrize("limit", ["calls", "pages", "context"])
def test_exhaustion_preserves_incomplete_research(tmp_path: Path, limit: str) -> None:
    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages, calls=1 if limit == "calls" else 12, budget=1)
    steps = [
        ("read_page", {"url": "https://example.com/docs"}),
        ("read_page", {"url": "https://example.com/linked"}),
        ("finish_research", {"reason": "Done"}),
    ]
    model = ResearchModel(steps)
    if limit == "context":
        run.settings.input_token_limit = 1000
    with pytest.raises(
        (ValueError, RuntimeError),
        match={
            "calls": "Model-call limit exhausted",
            "pages": "page budget exhausted",
            "context": "Nothing truncated",
        }[limit],
    ):
        run_agent(directory, run, model, tools)
    assert run.status == "incomplete"
    assert not (directory / "research/completion.json").exists()
    assert len(model.requests) == {"calls": 1, "pages": 2, "context": 0}[limit]
    assert all(request["truncation"] == "disabled" for request in model.requests)
    reopened = Run.model_validate(read_json(directory, "run.json"))
    reopened.settings.max_model_calls = 20
    reopened.settings.input_token_limit = 96000
    reopened.research_page_budget = 3
    fresh_model = ResearchModel(steps)
    run_agent(
        directory,
        reopened,
        fresh_model,
        OrientationTools(directory, reopened.inputs, reopened.settings, CompanyPages()),
    )
    assert reopened.status == "complete" and reopened.research_page_attempts == 2


def test_provider_failure_consumes_attempt_and_reuses_model_response(
    tmp_path: Path,
) -> None:
    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    pages.fail_url = "https://example.com/docs"
    steps = [
        ("read_page", {"url": pages.fail_url}),
        ("finish_research", {"reason": "Public evidence sufficient"}),
    ]
    with pytest.raises(RuntimeError, match="provider outage"):
        run_agent(directory, run, ResearchModel(steps), tools)
    assert run.research_page_attempts == 1 and run.status == "incomplete"
    assert not (directory / "debug/research-turn-0000.tool.json").exists()
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh_model = ResearchModel(steps)
    run_agent(
        directory,
        reopened,
        fresh_model,
        OrientationTools(directory, reopened.inputs, reopened.settings, CompanyPages()),
    )
    assert len(fresh_model.requests) == 1 and reopened.model_calls == 7
    assert reopened.research_page_attempts == 2 and reopened.status == "complete"


def test_old_orientation_run_defaults_and_explicit_research_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from research_agent import main
    from research_agent.storage import save_json

    (tmp_path / "runs").mkdir()
    directory, run, pages = oriented(tmp_path / "runs")
    saved = read_json(directory, "run.json")
    for key in (
        "stage",
        "research_page_budget",
        "research_page_attempts",
        "workflow_intent",
    ):
        saved.pop(key)
    saved["settings"].pop("index_result_limit")
    save_json(directory, "run.json", saved)
    model = ResearchModel(
        [("finish_research", {"reason": "No additional useful public evidence"})]
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake")
    monkeypatch.setattr(main, "OpenAIModel", lambda key, timeout: model)
    monkeypatch.setattr(main, "FirecrawlPages", lambda key, settings: pages)
    common = ["--run-id", run.run_id, "--data-dir", str(directory.parent.parent)]
    assert main.main(["resume", *common]) == 0
    assert not model.requests and not (directory / RESEARCH_START).exists()
    assert (
        main.main(
            [
                "research",
                *common,
                "--model-calls",
                "3",
                "--page-budget",
                "2",
                "--input-token-limit",
                "96000",
                "--index-result-limit",
                "5",
            ]
        )
        == 0
    )
    assert len(model.requests) == 1
    final = Run.model_validate(read_json(directory, "run.json"))
    assert final.stage == "research" and final.settings.max_model_calls == 8
    assert final.settings.index_result_limit == 5
    output = capsys.readouterr().out
    assert "Research complete" in output and "Model requests: 6" in output
    assert main.main(["resume", *common]) == 0
    assert len(model.requests) == 1


def question(
    qid: str, assessments: list[dict[str, object]] | None = None, **updates: object
) -> dict[str, object]:
    return {
        "id": qid,
        "question": f"What informs {qid}?",
        "status": "open",
        "priority_reason": "Material product gap",
        "answer": None,
        "answer_basis": None,
        "source_assessments": assessments or [],
        "closure_reason": None,
        **updates,
    }


def test_questions_cross_question_learning_streaks_and_fresh_resume(
    tmp_path: Path,
) -> None:
    from research_agent.storage import QuestionIndex

    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    ids = [
        tools.read_page(url)["source_id"]
        for url in (
            "https://example.com/docs",
            "https://example.com/docs/install",
            "https://help.example.com/guide",
        )
    ]
    assert ids == ["S3", "S4", "S5"]
    assessments = [{"source_id": sid, "useful": False} for sid in ids]
    saved = tools.update_questions(
        QuestionIndex.model_validate(
            {
                "active_question_id": "Q1",
                "questions": [question("Q1", assessments), question("Q2", assessments)],
            }
        )
    )
    assert saved["progress"] == {"Q1": 3, "Q2": 3}
    assert tools.read_page("https://example.com/docs")["source_id"] == "S3"
    assert tools.read_page("https://example.com/blocked")["recoverable"]
    # Rereads and failures cannot create new assessments. Duplicate S5 adds no streak.
    saved = tools.update_questions(
        QuestionIndex.model_validate(
            {
                "active_question_id": "Q1",
                "questions": [
                    question(
                        "Q2",
                        [{"source_id": "S3", "useful": True}],
                        status="answered",
                        answer="Inferred: linked workflow implies adoption constraint [S3].",
                        answer_basis="inferred",
                    ),
                    question("Q1", [assessments[-1], assessments[-1]]),
                ],
            }
        )
    )
    assert saved["progress"] == {"Q2": 0, "Q1": 3}
    reopened = Run.model_validate(read_json(directory, "run.json"))
    fresh = OrientationTools(
        directory, reopened.inputs, reopened.settings, CompanyPages(), reopened
    )
    index = QuestionIndex.model_validate(read_json(directory, "questions.json"))
    assert [q.id for q in index.questions] == ["Q2", "Q1"]
    assert (
        index.active_question_id == "Q1"
        and index.questions[0].answer_basis == "inferred"
    )
    assert fresh.read_page("https://example.com/docs")["source_id"] == "S3"
    assert fresh.validate_questions(index).questions[1].unproductive_pages == 3
    with pytest.raises(ToolInputError, match="not saved evidence"):
        fresh.update_questions(
            QuestionIndex.model_validate(
                {
                    "active_question_id": None,
                    "questions": [
                        question("Q3", [{"source_id": "S99", "useful": False}])
                    ],
                }
            )
        )
    with pytest.raises(ToolInputError, match="Active question"):
        fresh.update_questions(index.model_copy(update={"active_question_id": "Q2"}))
    with pytest.raises(ToolInputError, match="unique"):
        fresh.update_questions(
            QuestionIndex.model_validate(
                {
                    "active_question_id": None,
                    "questions": [question("Q1"), question("Q1")],
                }
            )
        )


def test_finish_validates_before_writes_and_completion_follows_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_agent import tools as module
    from research_agent.storage import QuestionIndex

    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    old = (directory / "profile.md").read_text()
    index = QuestionIndex.model_validate(read_json(directory, "questions.json"))
    with pytest.raises(ToolInputError, match="open questions"):
        tools.finish_research("Done", "Final [S1]", index)
    assert (directory / "profile.md").read_text() == old
    index.active_question_id = None
    index.questions[0].status = "needs_interview"
    index.questions[
        0
    ].closure_reason = "Unresolved through public research: public leads exhausted."
    with pytest.raises(ToolInputError, match="not saved evidence"):
        tools.finish_research("Done", "Final [S99]", index)
    save = module.save_json

    def fail(directory: Path, name: str, value: object) -> None:
        if name == "questions.json":
            raise OSError("disk failure")
        save(directory, name, value)

    with monkeypatch.context() as patch:
        patch.setattr(module, "save_json", fail)
        with pytest.raises(OSError):
            tools.finish_research("Done", "Final [S1]", index)
    assert not (directory / "research/completion.json").exists()
    tools.finish_research("Done", "Final [S1]", index)
    assert tools.complete()
    (directory / "profile.md").write_text("Damaged citation [S99]")
    with pytest.raises(ToolInputError, match="not saved evidence"):
        tools.complete()


def test_search_any_terms_ranking_phrase_and_full_url(tmp_path: Path) -> None:
    directory, run, pages = oriented(tmp_path)
    tools = enter(directory, run, pages)
    result = tools.query_site_index("support install absent")
    assert result["total_matches"] == 3
    assert (
        tools.query_site_index("support missing", url_contains="help.example.com")[
            "total_matches"
        ]
        == 1
    )
    assert (
        tools.query_site_index("product install", required_phrase="install securely")[
            "total_matches"
        ]
        == 1
    )
    ranked = tools.query_site_index("install guide securely")
    assert ranked["matches"][0]["url"] == "https://example.com/docs"


def test_blocked_layout_without_heading_and_legitimate_mentions() -> None:
    from research_agent.tools import evidence_problem

    assert evidence_problem(
        Capture(
            html="<title>Access denied</title><main><p>Access denied. Verify you are human.</p></main>"
        )
    )
    assert (
        evidence_problem(
            Capture(
                html="<title>Permissions guide</title><main><article>When access denied appears, contact your administrator.</article></main>"
            )
        )
        is None
    )
