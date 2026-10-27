"""One tool-calling agent; LangGraph checkpoints contain only transcript references."""

import json
from importlib.resources import files
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.responses.response_create_params import (
    ResponseCreateParamsNonStreaming,
)
from pydantic import JsonValue, TypeAdapter, ValidationError

from research_agent.cli import CLI
from research_agent.storage import (
    JSON_OBJECT,
    FirecrawlCounts,
    Run,
    load_orientation_snapshot,
    local_path,
    read_json,
    save_json,
    save_orientation_snapshot,
    save_run,
    write_text,
)
from research_agent.tools import TOOL_MODELS, OrientationTools, ToolInputError

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
    baseline: NotRequired[str]


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


def definitions() -> list[dict[str, JsonValue]]:
    return [
        {
            "type": "function",
            "name": name,
            "description": {
                "read_page": "Read a supplied starting URL; returns full cleaned evidence and its saved source reference.",
                "update_profile": "Save flexible Markdown with source references after reading both starting pages.",
                "build_site_index": "Map the company root broadly after saving the profile; returns counts and limitations, then orientation stops.",
            }[name],
            "strict": True,
            "parameters": schema.model_json_schema(),
        }
        for name, schema in TOOL_MODELS.items()
    ]


def run_agent(
    directory: Path,
    run: Run,
    model: Model,
    tools: OrientationTools,
    display: CLI | None = None,
) -> None:
    display = display or CLI(directory)

    def model_node(state: State) -> State:
        turn = state["turn"]
        response_name = f"debug/turn-{turn:04d}.response.json"
        request_name = f"debug/turn-{turn:04d}.request.json"
        if not local_path(directory, response_name).exists():
            items: list[dict[str, JsonValue]] = []
            if baseline := state.get("baseline"):
                snapshot = load_orientation_snapshot(directory, baseline, run.inputs)
                items.extend(
                    JSON_ITEMS.validate_python(
                        read_json(directory, "user.json")["items"]
                    )
                )
                items.append(
                    {
                        "role": "assistant",
                        "content": snapshot.receipt
                        + "\n\nStarting sources:\n"
                        + "\n".join(snapshot.sources)
                        + "\n\nSaved initial profile:\n"
                        + snapshot.profile,
                    }
                )
            for reference in state["history"]:
                saved_items = JSON_ITEMS.validate_python(
                    read_json(directory, reference)["items"]
                )
                # SDK response objects serialize absent optional fields as null,
                # while input TypedDicts require those fields to be omitted.
                # Normalize replay only; retain the original response on disk.
                items.extend(
                    {key: value for key, value in item.items() if value is not None}
                    for item in saved_items
                )
            request: dict[str, JsonValue] = {
                "model": run.settings.model,
                "instructions": local_path(directory, "system.md").read_text(),
                "input": list(items),
                "tools": list(definitions()),
                "tool_choice": "required",
                "parallel_tool_calls": False,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": run.settings.output_tokens,
                "truncation": "disabled",
            }
            # UTF-8 bytes plus framing reserve is deliberately conservative for text
            # BPE inputs. This is an allowance guard, not provider token accounting.
            allowance_used = (
                len(json.dumps(request, ensure_ascii=False).encode()) + 2048
            )
            if allowance_used > run.settings.input_allowance:
                raise ValueError(
                    f"Complete model input requires a conservative allowance of {allowance_used}; configured {run.settings.input_allowance}. Nothing truncated. Resume with --input-allowance only within the model's capacity."
                )
            if run.model_calls >= run.settings.max_model_calls:
                raise ValueError(
                    "Model-call limit exhausted; orientation incomplete. Resume with a higher --max-model-calls to continue explicitly."
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
                raise ValueError(
                    "Model response incomplete; response retained, resume retries within the call limit"
                )
            output = JSON_ITEMS.validate_python(response.get("output"))
            calls = [item for item in output if item.get("type") == "function_call"]
            if len(calls) != 1:
                raise ValueError(
                    "Expected exactly one orientation tool call; response retained"
                )
            save_json(directory, response_name, {"items": output})
        else:
            display.event("Model", "Reusing saved response")
        return {"history": state["history"] + [response_name], "turn": turn}

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
        result_name = f"debug/turn-{state['turn']:04d}.tool.json"
        if not local_path(directory, result_name).exists():
            display.tool_call(str(name), str(arguments))
            try:
                result = tools.execute(str(name), str(arguments))
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
        if (
            name == "update_profile"
            and result.get("saved") is True
            and not state.get("baseline")
        ):
            if not tools.pages_read():
                raise ValueError(
                    "Cannot snapshot orientation without both starting sources"
                )
            baseline = save_orientation_snapshot(directory, run.inputs)
            return {"history": [], "turn": state["turn"] + 1, "baseline": baseline}
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
        "recursion_limit": run.settings.max_model_calls * 2 + 10,
    }
    run.status, run.error = "incomplete", None
    save_run(directory, run)
    try:
        tools.validate_artifacts()
        checkpoint_path = local_path(directory, "checkpoints.sqlite")
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            compiled = graph.compile(checkpointer=saver)
            snapshot = compiled.get_state(config)
            if baseline := snapshot.values.get("baseline"):
                load_orientation_snapshot(directory, baseline, run.inputs)
            if snapshot.values and not snapshot.next:
                if not tools.complete():
                    raise ValueError(
                        "Completed checkpoint has missing artifacts; restore run files"
                    )
            else:
                initial: State | None = (
                    None if snapshot.values else {"history": ["user.json"], "turn": 0}
                )
                compiled.invoke(initial, config=config)
        if not tools.complete():
            raise ValueError("Orientation did not produce all required outputs")
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
            f"debug/failure-{run.model_calls:04d}.json",
            {"error": run.error, "type": type(exc).__name__},
        )
        save_run(directory, run)
        raise
    run.status = "complete"
    save_run(directory, run)
