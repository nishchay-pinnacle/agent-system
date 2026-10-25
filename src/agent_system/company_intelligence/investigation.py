"""Investigator submissions and complete saved-evidence retrieval."""

from pathlib import Path

from pydantic import JsonValue

from agent_system.company_intelligence.contexts import full_page
from agent_system.company_intelligence.coordination import (
    Document,
    InvestigationResult,
    Session,
    validate_finding,
)
from agent_system.company_intelligence.site_index import search_inventory


def record_findings(
    doc: Document, session: Session, result: InvestigationResult, *, final: bool
) -> None:
    assignment = doc.assignments[session.session_id]
    ids = [f.question_id for f in result.findings]
    if len(ids) != len(set(ids)) or set(ids) - set(assignment.question_ids):
        raise ValueError("Findings must refer to distinct assigned questions")
    if final and set(ids) != set(assignment.question_ids):
        raise ValueError("Final result must represent every assigned question")
    for finding in result.findings:
        validate_finding(doc, finding)
        if set(finding.source_ids) - set(assignment.sources):
            raise ValueError("Investigator must read cited sources")
    if assignment.result:
        existing = {f.question_id: f for f in assignment.result.findings}
        existing.update({f.question_id: f for f in result.findings})
        result = result.model_copy(
            update={
                "findings": list(existing.values()),
                "attempted_approaches": list(
                    dict.fromkeys(
                        assignment.result.attempted_approaches
                        + result.attempted_approaches
                    )
                ),
            }
        )
    for new in result.new_questions:
        new.origin = assignment.assignment_id
    assignment.result = result
    if final:
        if result.outcome == "progress":
            raise ValueError("Final submission must settle the objective")
        assignment.status = (
            "completed" if result.outcome in {"completed", "exhausted"} else "partial"
        )
        session.status = "done"


def saved_action(
    doc: Document,
    directory: Path,
    session: Session,
    tool: str,
    args: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    if tool == "read_source":
        sid = str(args.get("source_id", ""))
        page = full_page(doc, directory, sid)
        if sid in session.delivered:
            return {
                "source_id": sid,
                "already_in_context": True,
                "content_hash": page["content_hash"],
                "instruction": "Complete page remains in this active transcript.",
            }
        session.delivered.append(sid)
        if session.session_id in doc.assignments:
            doc.assignments[session.session_id].sources.append(sid)
        return page
    if tool == "search_site_index":
        return search_inventory(directory, str(args.get("query", "")), 20)
    if tool == "read_profile":
        area = str(args.get("area", ""))
        section = next((v for k, v in doc.profile.items() if k == area), None)
        if section is None:
            raise ValueError("Unknown profile area")
        return {
            "profile_version": doc.profile_version,
            "section": section.model_dump(mode="json"),
        }
    if tool == "read_findings":
        aid = str(args.get("assignment_id", ""))
        if aid not in doc.assignments:
            raise ValueError("Unknown assignment")
        return doc.assignments[aid].model_dump(mode="json")
    raise ValueError("Unknown saved-context tool")
