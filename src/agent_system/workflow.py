"""Three synchronous placeholder stages backed by LangGraph persistence."""

import json
import logging
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from agent_system.state import JobState

logger = logging.getLogger(__name__)
PAUSE_MESSAGE = (
    "Checkpoint demonstration: resume this job to finish placeholder output."
)


def write_output(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    # Replace atomically so a process interruption cannot leave partial JSON.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def build_workflow(
    data_dir: Path, checkpointer: SqliteSaver
) -> CompiledStateGraph[JobState, None, JobState, JobState]:
    def create_draft(state: JobState) -> dict[str, str]:
        logger.info("job=%s stage=create_draft start", state["job_id"])
        path = data_dir / "jobs" / state["job_id"] / "draft.json"
        write_output(
            path,
            {
                "job_id": state["job_id"],
                "company_name": state["company_name"],
                "placeholder": True,
            },
        )
        logger.info("job=%s stage=create_draft finish", state["job_id"])
        return {"draft_path": str(path)}

    def pause_for_resume(state: JobState) -> dict[str, str]:
        interrupt(PAUSE_MESSAGE)
        logger.info("job=%s stage=pause_for_resume finish", state["job_id"])
        return {}

    def finish(state: JobState) -> dict[str, str]:
        logger.info("job=%s stage=finish start", state["job_id"])
        path = data_dir / "jobs" / state["job_id"] / "result.json"
        write_output(
            path,
            {
                "job_id": state["job_id"],
                "company_name": state["company_name"],
                "placeholder": True,
                "draft_path": state["draft_path"],
            },
        )
        logger.info("job=%s stage=finish finish", state["job_id"])
        return {"result_path": str(path)}

    graph = StateGraph(JobState)
    graph.add_node("create_draft", create_draft)
    graph.add_node("pause_for_resume", pause_for_resume)
    graph.add_node("finish", finish)
    graph.add_edge(START, "create_draft")
    graph.add_edge("create_draft", "pause_for_resume")
    graph.add_edge("pause_for_resume", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)
