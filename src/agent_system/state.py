"""Serializable state and status derived from LangGraph snapshots."""

from typing import Literal, NotRequired, TypedDict

from langgraph.types import StateSnapshot


class JobState(TypedDict):
    job_id: str
    company_name: str
    draft_path: NotRequired[str]
    result_path: NotRequired[str]


JobStatus = Literal["not found", "waiting_for_resume", "completed", "incomplete"]


def snapshot_status(snapshot: StateSnapshot) -> JobStatus:
    if not snapshot.values:
        return "not found"
    if any(task.interrupts for task in snapshot.tasks):
        return "waiting_for_resume"
    if snapshot.values.get("result_path") and not snapshot.next:
        return "completed"
    return "incomplete"
