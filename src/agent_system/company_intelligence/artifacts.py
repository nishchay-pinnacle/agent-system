"""Job-scoped files, process ownership, events, and derived projections."""

import fcntl
import json
import logging
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from pydantic import JsonValue, TypeAdapter

from agent_system.company_intelligence.research_questions import QUESTIONS
from agent_system.company_intelligence.schemas import (
    Gap,
    PlanEntry,
    QuestionAssessment,
    Research,
    Source,
)
from agent_system.workflow import write_output

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
logger = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict[str, JsonValue]:
    return JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: object) -> None:
    # Reuse the foundation's atomic, idempotent writer.
    write_output(
        path, {k: v for k, v in JSON_OBJECT.validate_json(json.dumps(value)).items()}
    )


@contextmanager
def job_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another researcher owns this job") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def event(state: Research, kind: str, **details: JsonValue) -> None:
    logger.info("job=%s phase=%s event=%s", state.job_id, state.phase, kind)
    state.last_activity = now()
    state.events.append(
        {
            "sequence": len(state.events) + 1,
            "timestamp": state.last_activity,
            "kind": kind,
            "phase": state.phase,
            "job_id": state.job_id,
            **details,
        }
    )


def read_events(directory: Path, after: int = 0) -> list[dict[str, JsonValue]]:
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if not line.endswith("\n"):
            break
        item = JSON_OBJECT.validate_json(line)
        sequence = item.get("sequence")
        if isinstance(sequence, int) and sequence > after:
            events.append(item)
    return events[:100]


def source_path(directory: Path, source: Source) -> Path:
    expected = f"sources/{source.source_id}.md"
    if (
        not re.fullmatch(r"s_[0-9a-f]{16}", source.source_id)
        or source.filename != expected
    ):
        raise ValueError("Invalid saved source reference")
    path = directory / expected
    if (
        path.resolve().parent != (directory / "sources").resolve()
        or path.is_symlink()
        or not path.resolve().is_relative_to(directory.resolve())
    ):
        raise ValueError("Invalid source path")
    return path


def project_gaps(
    assessments: list[QuestionAssessment], plans: list[PlanEntry], gaps: list[Gap]
) -> tuple[list[dict[str, JsonValue]], list[str]]:
    """Derive reporting gaps without changing research state or resolution policy."""
    projected: list[dict[str, JsonValue]] = []
    for assessment in assessments:
        if assessment.status not in {"partial", "unassessed"}:
            continue
        linked = [
            p
            for p in plans
            if p.question_id == assessment.question_id and p.status != "superseded"
        ]
        importance = list(dict.fromkeys(p.importance for p in linked))
        projected.append(
            {
                "gap_id": f"assessment:{assessment.question_id}",
                "origin": "assessment",
                "question_id": assessment.question_id,
                "question": QUESTIONS[assessment.question_id],
                "status": assessment.status,
                "answer": assessment.answer,
                "state": "partial" if assessment.status == "partial" else "open",
                "consequence": "\n".join(importance)
                or "Required research question remains unresolved",
                "source_ids": list(assessment.source_ids),
                "plan_ids": [p.plan_id for p in linked],
                "interview_question": assessment.interview_question,
            }
        )
    projected.extend(
        {**gap.model_dump(mode="json"), "origin": "gap"}
        for gap in gaps
        if gap.state != "answered"
    )
    questions: list[str] = []
    seen: set[str] = set()
    for item in projected:
        question = item.get("interview_question")
        if isinstance(question, str) and question.strip():
            key = " ".join(question.split()).casefold()
            if key not in seen:
                questions.append(question.strip())
                seen.add(key)
    return projected, questions


def project(directory: Path, state: Research) -> None:
    """Called only with checkpointed state; files never drive the worker."""
    directory.mkdir(parents=True, exist_ok=True)
    events_path = directory / "events.jsonl"
    existing = events_path.read_bytes() if events_path.exists() else b""
    # Repair only an interrupted trailing line. Completed event lines are append-only.
    valid_length = existing.rfind(b"\n") + 1
    last = 0
    if valid_length:
        final = JSON_OBJECT.validate_json(existing[:valid_length].splitlines()[-1])
        last = int(str(final["sequence"]))
    with events_path.open("ab") as handle:
        if valid_length != len(existing):
            handle.truncate(valid_length)
        for item in state.events:
            if int(str(item["sequence"])) > last:
                handle.write((json.dumps(item) + "\n").encode())
    if state.status == "completed" and (directory / "result.json").exists():
        return  # Repair interrupted event tails, but never migrate completed artifacts.
    profile = {k: v.model_dump(mode="json") for k, v in state.profile.items()}
    sources = {k: v.model_dump(mode="json") for k, v in state.sources.items()}
    save_json(
        directory / "investigation_plan.json",
        {"plan": [p.model_dump(mode="json") for p in state.investigation_plan]},
    )
    save_json(directory / "profile.json", profile)
    save_json(directory / "sources.json", sources)
    for index, record in enumerate(state.investigations, 1):
        save_json(directory / "investigations" / f"{index:03d}.json", record)
    summary = state.model_dump(
        mode="json",
        exclude={
            "messages",
            "instructions",
            "cache",
            "events",
            "orientation_queue",
            "source_deliveries",
        },
    )
    save_json(directory / "dashboard.json", summary)
    if state.status == "completed" and not (directory / "result.json").exists():
        gaps, interviews = project_gaps(
            state.assessments, state.investigation_plan, state.gaps
        )
        save_json(
            directory / "result.json",
            {
                "job_id": state.job_id,
                "inputs": state.inputs.model_dump(mode="json"),
                "run_mode": state.run_mode,
                "stop_reason": state.stop_reason,
                "limiting_resource": state.limiting_resource,
                "readiness": state.readiness,
                "coverage": {k: v.coverage for k, v in state.profile.items()},
                "consequential_gaps": gaps,
                "interview_questions": interviews,
                "artifacts": {
                    "profile": "profile.json",
                    "orientation": "orientation.json",
                    "plan": "investigation_plan.json",
                    "site_index": "site_index.json",
                    "calls": "calls/",
                    "sources": "sources.json",
                    "investigations": "investigations/",
                },
                "usage": state.usage.model_dump(mode="json"),
            },
        )
