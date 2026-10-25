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
    if final and result.outcome == "progress":
        raise ValueError("Final submission must settle the objective")
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
    from agent_system.company_intelligence.findings import (
        SubmissionError,
        available_finding,
    )

    if tool == "read_source":
        sid = str(args.get("source_id", ""))
        if sid not in doc.sources or doc.sources[sid].outcome != "success":
            raise SubmissionError(
                "No usable saved evidence for source", source_ids=[sid]
            )
        page = full_page(doc, directory, sid)
        if (session.contract_version == 1 and sid in session.delivered) or (
            session.contract_version >= 2
            and session.delivered_hashes.get(sid) == page["content_hash"]
        ):
            return {
                "source_id": sid,
                "already_in_context": True,
                "content_hash": page["content_hash"],
                "instruction": "Complete page remains in this active transcript.",
            }
        if sid not in session.delivered:
            session.delivered.append(sid)
        session.delivered_hashes[sid] = str(page["content_hash"])
        if session.session_id in doc.assignments:
            doc.assignments[session.session_id].sources.append(sid)
        return page
    if tool == "search_site_index":
        result = search_inventory(directory, str(args.get("query", "")), 20)
        entries = result.get("entries", [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    source = doc.sources.get(str(entry.get("source_id")))
                    entry["capture_status"] = (
                        source.outcome if source else "not_captured"
                    )
        return result
    if tool == "read_profile":
        area = str(args.get("area", ""))
        section = next((v for k, v in doc.profile.items() if k == area), None)
        if section is None:
            raise SubmissionError("Unknown profile area")
        return {
            "profile_version": doc.profile_version,
            "section": section.model_dump(mode="json"),
        }
    if tool == "read_findings":
        aid = str(args.get("reference_id", args.get("assignment_id", "")))
        if session.contract_version >= 2:
            ids = (
                [aid]
                if aid in doc.findings
                else doc.assignments[aid].accepted_finding_ids
                if aid in doc.assignments
                else []
            )
            if not ids:
                raise SubmissionError(
                    "Unknown finding or assignment reference", finding_ids=[aid]
                )
            records = [doc.findings[fid] for fid in ids]
            session.inherited_finding_ids = list(
                dict.fromkeys(
                    session.inherited_finding_ids
                    + [
                        r.finding_id
                        for r in records
                        if available_finding(doc, r.finding_id)
                    ]
                )
            )
            return {"findings": [r.model_dump(mode="json") for r in records]}
        if aid not in doc.assignments:
            raise SubmissionError("Unknown assignment")
        return doc.assignments[aid].model_dump(mode="json")
    raise SubmissionError("Unknown saved-context tool")
