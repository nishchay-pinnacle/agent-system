"""Budget pauses and a local, idempotent user-command inbox."""

import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from pydantic import Field

from agent_system.company_intelligence.artifacts import now
from agent_system.company_intelligence.coordination import (
    Approval,
    Document,
    LimitItem,
    Session,
)
from agent_system.company_intelligence.schemas import StrictModel


class UserCommand(StrictModel):
    command_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    job_id: str
    kind: Literal["approve", "decline", "finish", "quality"]
    request_id: str | None = None
    expected_revision: int = Field(ge=0)
    increases: dict[str, float] = Field(default_factory=dict)
    batch_id: str | None = None
    note: str = Field(default="", max_length=2000)


@contextmanager
def inbox(directory: Path) -> Iterator[sqlite3.Connection]:
    with sqlite3.connect(directory / "commands.sqlite", timeout=5) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, payload TEXT NOT NULL, outcome TEXT)"
        )
        yield connection


def enqueue(directory: Path, command: UserCommand, doc: Document) -> str:
    with inbox(directory) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT payload,outcome FROM commands WHERE id=?", (command.command_id,)
        ).fetchone()
        payload = command.model_dump_json()
        if previous:
            if previous[0] != payload:
                raise ValueError("Conflicting duplicate command")
            return previous[1] or "queued"
        if command.kind in {"approve", "decline"}:
            approval = next(
                (a for a in doc.approvals if a.request_id == command.request_id), None
            )
            if (
                approval is None
                or approval.status != "pending"
                or approval.revision != command.expected_revision
            ):
                raise ValueError("Stale approval request")
            validate_increases(command, approval)
            queued = connection.execute(
                "SELECT payload FROM commands WHERE outcome IS NULL"
            ).fetchall()
            if any(
                UserCommand.model_validate_json(row[0]).request_id == command.request_id
                for row in queued
            ):
                raise ValueError("Decision already queued")
        elif command.expected_revision != doc.revision:
            raise ValueError("Stale run revision; refresh before submitting")
        connection.execute(
            "INSERT INTO commands(id,payload) VALUES (?,?)",
            (command.command_id, payload),
        )
    return "queued"


def validate_increases(command: UserCommand, approval: Approval) -> None:
    allowed = {f"{i.scope}:{i.resource}" for i in approval.items}
    if set(command.increases) - allowed:
        raise ValueError("Unknown allowance")
    if any(
        not math.isfinite(v)
        or v <= 0
        or (not k.endswith(":active_seconds") and not v.is_integer())
        for k, v in command.increases.items()
    ):
        raise ValueError("Increases must be positive finite values in resource units")


def blocked(
    doc: Document,
    session: Session,
    requirements: list[tuple[str, str, float]],
    *,
    action: str,
    reason: str,
    remaining: str,
    value: str,
) -> bool:
    """Request only resources needed by a concrete proposed action."""
    items: list[LimitItem] = []
    for scope, resource, amount in requirements:
        limits = doc.limits if scope == "run" else doc.assignments[scope].limits
        usage = doc.usage if scope == "run" else doc.assignments[scope].usage
        used = usage.get(resource, 0)
        reserved = usage.get(f"reserved_{resource}", 0)
        limit = limits[resource]
        if resource == "input_tokens":
            used, reserved = 0, 0
        if used + reserved + amount > limit:
            items.append(
                LimitItem(
                    scope=scope,
                    resource=resource,
                    current=limit,
                    used=used,
                    reserved=reserved,
                    increase=max(
                        math.ceil(limit * 0.25),
                        math.ceil(used + reserved + amount - limit),
                    ),
                )
            )
    if not items:
        return False
    if not reason.strip() or not remaining.strip() or not value.strip():
        raise ValueError(
            "A budget request needs an action, remaining question and expected value"
        )
    if not any(
        a.session_id == session.session_id and a.status == "pending"
        for a in doc.approvals
    ):
        doc.approvals.append(
            Approval(
                request_id=f"approval_{len(doc.approvals) + 1:04d}",
                session_id=session.session_id,
                items=items,
                action=action,
                reason=reason,
                remaining_question=remaining,
                expected_value=value,
                created_at=now(),
            )
        )
    session.status = "paused"
    if session.session_id in doc.reviews:
        doc.reviews[session.session_id].status = "paused"
    if session.session_id in doc.assignments:
        doc.assignments[session.session_id].status = "paused"
    return True


def decide(doc: Document, command: UserCommand) -> None:
    approval = next(
        (a for a in doc.approvals if a.request_id == command.request_id), None
    )
    if (
        approval is None
        or approval.status != "pending"
        or approval.revision != command.expected_revision
    ):
        raise ValueError("Stale approval request")
    validate_increases(command, approval)
    session = doc.sessions[approval.session_id]
    approval.status = "approved" if command.kind == "approve" else "declined"
    approval.decided_at = now()
    approval.revision += 1
    if command.kind == "approve":
        for item in approval.items:
            key = f"{item.scope}:{item.resource}"
            increase = command.increases.get(key, item.increase)
            limits = (
                doc.limits
                if item.scope == "run"
                else doc.assignments[item.scope].limits
            )
            old = limits[item.resource]
            if item.scope == "run":
                doc.limits[item.resource] += increase
            else:
                doc.assignments[item.scope].limits[item.resource] += int(increase)
            doc.amendments.append(
                {
                    "request_id": approval.request_id,
                    "command_id": command.command_id,
                    "scope": item.scope,
                    "resource": item.resource,
                    "previous": old,
                    "increase": increase,
                    "resulting": limits[item.resource],
                    "at": now(),
                    "note": command.note,
                }
            )
            if item.scope == "run" and item.resource == "generation_requests":
                doc.limits["counting_requests"] = (
                    doc.limits["generation_requests"] * 2 + 2
                )
        session.status = "ready"
        if session.session_id in doc.assignments:
            doc.assignments[session.session_id].status = "running"
    elif session.session_id in doc.assignments and any(
        item.scope != "run" for item in approval.items
    ):
        assignment = doc.assignments[session.session_id]
        assignment.status = "partial"
        assignment.error = "Allowance declined; saved partial findings retained"
        session.status = "done"
        doc.declined_questions.extend(
            q for q in assignment.question_ids if q not in doc.declined_questions
        )
        for qid in assignment.question_ids:
            doc.questions[qid].unresolved_reason = "Budget/user stop before resolution"
            doc.questions[qid].work_status = "exhausted"
    else:
        session.status = "paused"
