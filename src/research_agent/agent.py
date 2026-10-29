"""One tool-calling agent; LangGraph checkpoints contain only transcript references."""

import json
import time
from importlib.resources import files
from pathlib import Path
from typing import Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.responses.response_create_params import (
    ResponseCreateParamsNonStreaming,
)
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from research_agent.cli import CLI
from research_agent.storage import (
    JSON_OBJECT,
    RESEARCH_START,
    ContinuationArgs,
    ContinuationDecision,
    ContinuationRequest,
    FirecrawlCounts,
    ResearchStart,
    Run,
    activate_research,
    load_research_start,
    local_path,
    read_json,
    save_json,
    save_run,
    write_text,
)
from research_agent.tools import OrientationTools, ToolInputError, tool_models

CREATE_PARAMS = TypeAdapter(ResponseCreateParamsNonStreaming)
JSON_ITEMS = TypeAdapter(list[dict[str, JsonValue]])


class Model(Protocol):
    def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]: ...


class OpenAIModel:
    def __init__(self, api_key: str, timeout: float) -> None:
        self.api_key, self.timeout = api_key, timeout

    def generate(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        params = CREATE_PARAMS.validate_python(request)
        # Typed SDK parameters use Iterable; materialize before handing them to the SDK.
        params["tools"] = list(params.get("tools", []))
        items = params.get("input")
        if items is not None and not isinstance(items, str):
            params["input"] = list(items)
        try:
            with OpenAI(
                api_key=self.api_key, timeout=self.timeout, max_retries=1
            ) as client:
                return client.responses.create(**params).model_dump(mode="json")
        except (APIConnectionError, APIStatusError) as exc:
            code = exc.status_code if isinstance(exc, APIStatusError) else None
            # Do not log provider bodies/headers, which may echo private inputs.
            raise RuntimeError(
                f"OpenAI request failed ({type(exc).__name__}, status={code}); verify configured model, limits and credentials. Input was not truncated."
            ) from exc


class State(TypedDict):
    history: list[str]
    turn: int


def initialize_prompts(directory: Path, run: Run) -> None:
    package = files("research_agent")
    system = package.joinpath("prompts/system.md").read_text()
    skill = package.joinpath("skills/orientation/SKILL.md").read_text()
    user = (
        package.joinpath("prompts/user.md")
        .read_text()
        .format(**run.inputs.model_dump())
    )
    save_json(
        directory,
        "debug/firecrawl-counts.json",
        FirecrawlCounts(complete_history=True).model_dump(),
    )
    # Freeze loaded instructions so resume uses the same task and skill.
    write_text(directory, "system.md", system + "\n\n" + skill)
    save_json(directory, "user.json", {"items": [{"role": "user", "content": user}]})


def tool_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    schema = JSON_OBJECT.validate_python(model.model_json_schema())

    def strict(value: JsonValue) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties)
            value.pop("default", None)
            for child in value.values():
                strict(child)
        elif isinstance(value, list):
            for child in value:
                strict(child)

    strict(schema)
    return schema


def begin_research(
    directory: Path,
    run: Run,
    tools: OrientationTools,
    *,
    model_calls: int,
    page_budget: int,
    max_input_bytes: int,
    index_result_limit: int = 20,
) -> None:
    if run.version != 3:
        raise ValueError("Legacy runs cannot resume under new schemas; start a new run")
    if run.stage != "orientation" or run.status != "complete":
        raise ValueError(
            "Research requires a completed orientation; use resume for an existing research stage"
        )
    if not tools.complete():
        raise ValueError(
            "Orientation artifacts are incomplete; restore them before research"
        )
    if not local_path(directory, RESEARCH_START).exists():
        package = files("research_agent")
        with SqliteSaver.from_conn_string(
            str(local_path(directory, "checkpoints.sqlite"))
        ) as saver:
            checkpoint = saver.get({"configurable": {"thread_id": run.run_id}})
        if checkpoint is None:
            raise ValueError("Orientation checkpoint missing; restore run files")
        history = TypeAdapter(list[str]).validate_python(
            checkpoint["channel_values"]["history"]
        )
        start = ResearchStart(
            original_run=run.model_copy(deep=True),
            system=package.joinpath("prompts/system.md").read_text()
            + "\n\n"
            + package.joinpath("skills/research/SKILL.md").read_text(),
            history=history,
            model_calls=model_calls,
            page_budget=page_budget,
            max_input_bytes=max_input_bytes,
            index_result_limit=index_result_limit,
        )
        # Validate the cumulative limit before persisting the transition intent.
        run.settings.model_validate(
            {
                **run.settings.model_dump(),
                "max_model_calls": run.model_calls + model_calls,
            }
        )
        save_json(directory, RESEARCH_START, start.model_dump(mode="json"))
    else:
        start = load_research_start(directory, run)
        if (
            start.model_calls,
            start.page_budget,
            start.max_input_bytes,
            start.index_result_limit,
        ) != (model_calls, page_budget, max_input_bytes, index_result_limit):
            raise ValueError(
                "Research transition already prepared with different allowances; use resume"
            )
    activate_research(directory, run)


def definitions(stage: str = "orientation") -> list[dict[str, JsonValue]]:
    return [
        {
            "type": "function",
            "name": name,
            "description": {
                "request_continuation": "Pause for dashboard approval of additional calls/pages. Explain why more research is worthwhile, unresolved questions, and next steps. Use before exhausting the allowance if completion is not justified.",
                "read_page": "Read a permitted HTML page; returns full cleaned evidence and its saved source reference.",
                "update_profile": "Replace the full Markdown profile with saved-source citations and remaining uncertainties.",
                "update_questions": "Save the full ordered question index and active question. Assess relevant successfully read sources for every affected question; useful evidence resets that question’s streak. Preserve every existing question ID and prior assessments. Explicit merges retain the original question with status merged, merged_into naming a non-merged target, and closure_reason.",
                "build_site_index": "Map the company root broadly after saving the profile; returns counts and limitations, then orientation stops.",
                "query_site_index": "Search any query term across URL/title/description; rank by matched terms then URL. Optional required_phrase and url_contains constrain results. Empty query browses. Paginated discovery only.",
                "finish_research": "Validate and save final profile, full question index and reason before completion. No open questions permitted.",
            }[name],
            "strict": True,
            "parameters": tool_schema(schema),
        }
        for name, schema in tool_models(stage).items()
    ]


def run_agent(
    directory: Path,
    run: Run,
    model: Model,
    tools: OrientationTools,
    display: CLI | None = None,
) -> None:
    if run.version != 3:
        raise ValueError("Legacy runs cannot resume under new schemas; start a new run")
    display = display or CLI(directory)
    research = None
    if run.stage == "research" or local_path(directory, RESEARCH_START).exists():
        research = activate_research(directory, run)
        save_json(
            directory,
            "research/user.json",
            {"items": [{"role": "user", "content": research.receipt}]},
        )
    tools.run, tools.settings = run, run.settings
    prefix = "research-" if run.stage == "research" else ""

    def await_continuation(request_id: str, args: ContinuationArgs) -> None:
        pending = run.continuation
        if pending is None or pending.request_id != request_id:
            pending = ContinuationRequest(request_id=request_id, **args.model_dump())
            run.continuation = pending
        if pending.status == "applied":
            return
        run.status = "awaiting_continuation"
        save_run(directory, run)
        display.event("Paused", "Awaiting dashboard continuation approval")
        while True:
            path = local_path(directory, "continuation-decision.json")
            if path.exists():
                decision = ContinuationDecision.model_validate(
                    read_json(directory, "continuation-decision.json")
                )
                if decision.request_id == request_id:
                    if not decision.approve:
                        raise ValueError(
                            "Continuation declined; progress saved. Resume to reconsider."
                        )
                    settings = run.settings.model_copy(
                        update={
                            "max_model_calls": run.settings.max_model_calls
                            + decision.additional_calls
                        }
                    )
                    settings = type(settings).model_validate(settings.model_dump())
                    pages = run.research_page_budget + decision.additional_pages
                    if pages > 10000 or (
                        run.stage != "research" and decision.additional_pages
                    ):
                        raise ValueError("Invalid additional page allowance")
                    run.settings = settings
                    run.research_page_budget = pages
                    tools.settings = run.settings
                    pending.status = "applied"
                    run.status, run.error = "incomplete", None
                    # Allowances and consumption marker are committed together.
                    save_run(directory, run)
                    return
            time.sleep(1)

    def model_node(state: State) -> State:
        turn = state["turn"]
        history = list(state["history"])
        response_name = f"debug/{prefix}turn-{turn:04d}.response.json"
        request_name = f"debug/{prefix}turn-{turn:04d}.request.json"
        if not local_path(directory, response_name).exists():
            if run.continuation is not None and run.continuation.status == "pending":
                if (
                    run.continuation.origin == "runtime"
                    and run.model_calls < run.settings.max_model_calls
                ):
                    # Existing CLI resume overrides remain an explicit approval path.
                    run.continuation.status = "applied"
                    save_run(directory, run)
                elif (
                    run.continuation.origin == "model"
                    or local_path(directory, "continuation-decision.json").exists()
                ):
                    await_continuation(run.continuation.request_id, run.continuation)
            budget_name = f"debug/{prefix}turn-{turn:04d}.budget.json"
            budget = {
                "stage": run.stage,
                "model_calls_used": run.model_calls,
                "max_model_calls": run.settings.max_model_calls,
                "calls_remaining_including_this_response": max(
                    0, run.settings.max_model_calls - run.model_calls
                ),
                "research_pages_used": run.research_page_attempts,
                "research_page_budget": run.research_page_budget,
                "max_input_bytes": run.settings.max_input_bytes,
                "max_output_tokens_per_response": run.settings.output_tokens,
            }
            save_json(
                directory,
                budget_name,
                {
                    "items": [
                        {
                            "role": "developer",
                            "content": "Current application allowances: "
                            + json.dumps(budget)
                            + ". These are soft planning limits but enforced until the user approves more. "
                            "Use the last available call to finish if justified, or request_continuation with a reason and next steps. "
                            "You may request earlier. Exhaustion does not mean completion. The byte limit is not a model context guarantee.",
                        }
                    ]
                },
            )
            history.append(budget_name)
            items: list[dict[str, JsonValue]] = []
            for reference in history:
                saved_items = JSON_ITEMS.validate_python(
                    read_json(directory, reference)["items"]
                )
                items.extend(
                    {key: value for key, value in item.items() if value is not None}
                    for item in saved_items
                )
            request: dict[str, JsonValue] = {
                "model": run.settings.model,
                "instructions": research.system
                if research is not None
                else local_path(directory, "system.md").read_text(),
                "input": list(items),
                "tools": list(definitions(run.stage)),
                "tool_choice": "required",
                "parallel_tool_calls": False,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": run.settings.output_tokens,
                "truncation": "disabled",
            }
            # Count the complete application request, including opaque reasoning,
            # using compact UTF-8 JSON. This is not a model context estimate.
            request_bytes = len(
                json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            save_json(
                directory,
                "debug/context.json",
                {
                    "request_bytes": request_bytes,
                    "max_input_bytes": run.settings.max_input_bytes,
                },
            )
            if request_bytes > run.settings.max_input_bytes:
                raise ValueError(
                    f"Complete model input is {request_bytes} UTF-8 bytes; max_input_bytes is {run.settings.max_input_bytes}. Nothing truncated. Run incomplete; resume with --max-input-bytes to raise the application size limit. This does not guarantee the request fits the model context."
                )
            if run.model_calls >= run.settings.max_model_calls:
                if run.continuation is None or run.continuation.status == "applied":
                    run.continuation = ContinuationRequest(
                        request_id=f"{prefix}exhausted-{run.model_calls}",
                        origin="runtime",
                        reason="Model-call allowance exhausted before completion.",
                        unresolved_questions=[],
                        next_steps="Resume saved research after approval.",
                        additional_calls=10,
                        additional_pages=0,
                    )
                save_run(directory, run)
                raise ValueError(
                    f"Model-call limit exhausted; {run.stage} incomplete. Resume with a higher --max-model-calls to continue explicitly."
                )
            save_json(directory, request_name, request)
            run.model_calls += 1
            save_json(
                directory, f"debug/call-{run.model_calls:04d}.request.json", request
            )
            save_run(directory, run)
            display.model_call(run.model_calls, run.settings.model)
            response = model.generate(request)
            save_json(
                directory, f"debug/call-{run.model_calls:04d}.response.json", response
            )
            display.model_result(response.get("usage"))
            if response.get("status") != "completed":
                details = response.get("incomplete_details")
                if (
                    isinstance(details, dict)
                    and details.get("reason") == "max_output_tokens"
                ):
                    raise ValueError(
                        f"Model response exceeded the per-response output limit ({run.settings.output_tokens:,} tokens); "
                        "partial response saved, tool not executed. Increase this run's saved output_tokens "
                        "setting before resuming. This is not the model-call limit; "
                        f"{max(0, run.settings.max_model_calls - run.model_calls)} calls remain."
                    )
                raise ValueError(
                    "Model response incomplete; response retained, resume retries within the call limit"
                )
            output = JSON_ITEMS.validate_python(response.get("output"))
            calls = [item for item in output if item.get("type") == "function_call"]
            if len(calls) != 1:
                raise ValueError(
                    "Expected exactly one stage tool call; response retained"
                )
            save_json(directory, response_name, {"items": output})
        else:
            display.event("Model", "Reusing saved response")
            budget_name = f"debug/{prefix}turn-{turn:04d}.budget.json"
            if local_path(directory, budget_name).exists():
                history.append(budget_name)
        return {"history": history + [response_name], "turn": turn}

    def tool_node(state: State) -> State:
        output = JSON_ITEMS.validate_python(
            read_json(directory, state["history"][-1])["items"]
        )
        call = next(item for item in output if item.get("type") == "function_call")
        name, arguments, call_id = (
            call.get("name"),
            call.get("arguments"),
            call.get("call_id"),
        )
        if not all(isinstance(v, str) for v in (name, arguments, call_id)):
            raise ValueError("Malformed function call")
        result_name = f"debug/{prefix}turn-{state['turn']:04d}.tool.json"
        if not local_path(directory, result_name).exists():
            display.tool_call(str(name), str(arguments))
            try:
                result = tools.execute(str(name), str(arguments))
                if name == "request_continuation":
                    await_continuation(
                        str(call_id), ContinuationArgs.model_validate(result)
                    )
                    result = {
                        "approved": True,
                        "max_model_calls": run.settings.max_model_calls,
                        "research_page_budget": run.research_page_budget,
                    }
            except ToolInputError as exc:
                result = {
                    "error": str(exc),
                    "action": "Correct the arguments or fulfill the prerequisite, then retry.",
                }
            display.tool_result(str(name), result)
            save_json(
                directory,
                result_name,
                {
                    "items": [
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result, ensure_ascii=False),
                        }
                    ]
                },
            )
        else:
            display.event("Tool", str(name), {"Status": "Reusing saved result"})
            saved_items = JSON_ITEMS.validate_python(
                read_json(directory, result_name)["items"]
            )
            result = JSON_OBJECT.validate_json(str(saved_items[0]["output"]))
        return {"history": state["history"] + [result_name], "turn": state["turn"] + 1}

    def route(state: State) -> str:
        return END if tools.complete() else "model"

    graph = StateGraph(State)
    graph.add_node("model", model_node)
    graph.add_node("tool", tool_node)
    graph.add_edge(START, "model")
    graph.add_edge("model", "tool")
    graph.add_conditional_edges("tool", route, {END: END, "model": "model"})
    config: RunnableConfig = {
        "configurable": {"thread_id": run.run_id},
        "recursion_limit": 2010,  # Settings caps total model calls at 1000.,
    }
    run.status, run.error = "incomplete", None
    save_run(directory, run)
    try:
        tools.validate_artifacts()
        checkpoint_path = local_path(
            directory,
            "research/checkpoints.sqlite"
            if research is not None
            else "checkpoints.sqlite",
        )
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            compiled = graph.compile(checkpointer=saver)
            snapshot = compiled.get_state(config)
            if snapshot.values and not snapshot.next:
                if not tools.complete():
                    raise ValueError(
                        "Completed checkpoint has missing artifacts; restore run files"
                    )
            else:
                initial: State | None = (
                    None
                    if snapshot.values
                    else {
                        "history": research.history + ["research/user.json"]
                        if research is not None
                        else ["user.json"],
                        "turn": 0,
                    }
                )
                compiled.invoke(initial, config=config)
        if not tools.complete():
            raise ValueError(
                f"{run.stage.capitalize()} did not produce all required outputs"
            )
    except Exception as exc:
        # Our boundary errors carry safe context; unexpected error messages may contain data.
        run.error = (
            str(exc)
            if isinstance(exc, (ValueError, RuntimeError))
            and not isinstance(exc, ValidationError)
            else type(exc).__name__
        )
        save_json(
            directory,
            f"debug/{prefix}failure-{run.model_calls:04d}.json",
            {"error": run.error, "type": type(exc).__name__},
        )
        save_run(directory, run)
        raise
    run.status = "complete"
    save_run(directory, run)
