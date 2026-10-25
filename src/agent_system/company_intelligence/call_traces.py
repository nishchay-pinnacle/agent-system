"""Durable attempt records; dashboard views mask opaque encrypted protocol fields."""

import hashlib
import re
from pathlib import Path

from pydantic import JsonValue

from agent_system.company_intelligence.artifacts import now, read_json, save_json
from agent_system.company_intelligence.schemas import Operation, Research


def safe_path(directory: Path, relative: str) -> Path:
    path = directory / relative
    if not path.resolve().is_relative_to(directory.resolve()) or any(
        p.is_symlink() for p in [path, *path.parents] if p != directory.parent
    ):
        raise ValueError("Invalid artifact path")
    return path


def trace_id(op: Operation) -> str:
    return f"{op.operation_id}_{op.attempts}"


def trace_directory(directory: Path, identifier: str) -> Path:
    if not re.fullmatch(r"op_[0-9]{5,}_[1-9][0-9]*", identifier):
        raise ValueError("Invalid call ID")
    return safe_path(directory, f"calls/{identifier}")


def start_trace(
    directory: Path,
    state: Research,
    request: dict[str, JsonValue],
    kind: str,
    simulated: bool,
) -> str:
    op = state.operation
    assert op is not None
    identifier = trace_id(op)
    target = trace_directory(directory, identifier)
    save_json(target / "request.json", request)
    save_json(
        target / "metadata.json",
        {
            "id": identifier,
            "operation_id": op.operation_id,
            "attempt": op.attempts,
            "kind": kind,
            "phase": state.phase,
            "model": state.settings.model,
            "question_id": state.current.question_id if state.current else None,
            "plan_id": state.current.plan_id if state.current else None,
            "started_at": now(),
            "ended_at": None,
            "status": "unknown",
            "application_status": "pending",
            "timeout_seconds": op.reserved_seconds,
            "configured_timeout_seconds": state.settings.request_timeout(op.kind),
            "counted_input": op.counted_input,
            "count_trace_id": op.count_trace_id,
            "reserved_tokens": op.reserved_tokens,
            "reserved_seconds": op.reserved_seconds,
            "retry_of": f"{op.operation_id}_{op.attempts - 1}"
            if op.attempts > 1
            else None,
            "sources": state.source_deliveries,
            "simulated": simulated,
        },
    )
    return identifier


def finish_trace(
    directory: Path,
    identifier: str,
    *,
    response: dict[str, JsonValue] | None = None,
    **updates: JsonValue,
) -> None:
    target = trace_directory(directory, identifier)
    if not (target / "metadata.json").exists():
        return  # Never invent historical prompts.
    if response is not None:
        save_json(target / "response.json", response)
    metadata = read_json(target / "metadata.json")
    metadata.update(updates)
    if response is not None or "status" in updates:
        metadata["ended_at"] = now()
    save_json(target / "metadata.json", metadata)


def inspection_view(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [inspection_view(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "[Opaque encrypted field omitted; sha256="
                + hashlib.sha256(str(item).encode()).hexdigest()
                + "]"
                if key == "encrypted_content"
                else inspection_view(item)
            )
            for key, item in value.items()
        }
    return value


def call_details(directory: Path, identifier: str) -> dict[str, JsonValue]:
    target = trace_directory(directory, identifier)
    metadata = read_json(safe_path(target, "metadata.json"))
    result: dict[str, JsonValue] = {
        "metadata": metadata,
        "inspection_notice": "Inspection view: opaque encrypted fields are replaced with hashes; this is not byte-for-byte exact.",
    }
    errors: list[JsonValue] = []
    for name in ("request", "response", "application"):
        path = safe_path(target, f"{name}.json")
        try:
            result[name] = inspection_view(read_json(path)) if path.exists() else None
        except (ValueError, OSError):
            result[name] = None
            errors.append(f"{name} artifact is unavailable or incomplete")
    result["artifact_errors"] = errors
    return result


def call_summaries(
    directory: Path,
    phase: str = "",
    status: str = "",
    counts: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, JsonValue]:
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid call pagination")
    root = safe_path(directory, "calls")
    rows: list[dict[str, JsonValue]] = []
    unavailable = 0
    for path in sorted(root.glob("*/metadata.json")):
        try:
            item = read_json(safe_path(directory, str(path.relative_to(directory))))
        except (OSError, ValueError):
            unavailable += 1
            continue
        if (phase and item.get("phase") != phase) or (
            status and item.get("status") != status
        ):
            continue
        if not counts and item.get("kind") != "generation":
            continue
        rows.append({k: v for k, v in item.items() if k != "sources"})
    return {
        "calls": [r for r in rows[offset : offset + limit]],
        "total": len(rows),
        "unavailable_count": unavailable,
        "offset": offset,
        "legacy_notice": None
        if root.exists()
        else "Exact call input was not recorded for this run",
    }
