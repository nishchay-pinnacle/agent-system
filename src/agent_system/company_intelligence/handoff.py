"""Immutable accepted orientation output with exact delivered-evidence provenance."""

from pathlib import Path

from agent_system.company_intelligence.contexts import messages
from agent_system.company_intelligence.contracts import OrientationResult
from agent_system.company_intelligence.coordination import (
    Document,
    Finding,
    FindingRecord,
    Session,
)
from agent_system.company_intelligence.findings import (
    SubmissionError,
    immutable_json,
    lineage,
)
from agent_system.company_intelligence.orientation import apply_submission
from agent_system.company_intelligence.schemas import OrientationSubmission, Research


def accept_orientation(
    doc: Document,
    state: Research,
    directory: Path,
    session: Session,
    result: OrientationResult,
    call_id: str,
) -> None:
    for assessment in result.assessments:
        if (
            assessment.status in {"partial", "unassessed"}
            and not assessment.missing.strip()
        ):
            raise SubmissionError(
                "Orientation requires specific remaining uncertainty",
                question_ids=[assessment.question_id],
            )
    payload = result.model_dump(mode="json")
    base = OrientationSubmission.model_validate(
        {
            **payload,
            "assessments": [
                a.model_dump(exclude={"missing"}) for a in result.assessments
            ],
        }
    )
    oriented = state.model_copy(deep=True)
    oriented.sources = doc.sources
    try:
        apply_submission(
            oriented, base, directory, require_plans=False, write_snapshot=False
        )
    except ValueError as exc:
        raise SubmissionError(str(exc)) from exc
    evidence = [
        {
            "source_id": sid,
            "canonical_url": doc.sources[sid].final_url
            or doc.sources[sid].requested_url,
            "content_hash": session.delivered_hashes.get(sid),
            "cleaner_version": doc.sources[sid].cleaner_version,
        }
        for sid in session.delivered
    ]
    snapshot = {
        **payload,
        "version": 3,
        "contract_version": 2,
        "profile": {k: v.model_dump(mode="json") for k, v in oriented.profile.items()},
        "sources": evidence,
        "discovery": doc.discovery,
        "inventory": "site_index.json",
        "accepted_call": call_id,
        "exact_context": f"calls/{call_id}/request.json",
        "briefing_context": session.transcript_path,
    }
    immutable_json(directory, "orientation.json", snapshot)
    doc.orientation_path = "orientation.json"
    doc.profile, doc.profile_version = oriented.profile, 1
    for assessment in result.assessments:
        q = doc.questions[assessment.question_id]
        q.answer, q.answer_status, q.source_ids = (
            assessment.answer,
            assessment.status,
            assessment.source_ids,
        )
        q.missing = assessment.missing
        q.work_status = (
            "finished"
            if assessment.status in {"answered", "not_applicable"}
            else "pending"
        )
        plans = [p for p in result.plan if p.question_id == q.question_id]
        if plans:
            q.importance, q.importance_reason = (
                min(p.priority for p in plans),
                "; ".join(p.importance for p in plans),
            )
        fid = f"finding_orientation_{q.question_id}"
        finding = Finding(
            question_id=q.question_id,
            answer=q.answer,
            status=q.answer_status,
            missing=q.missing,
            source_ids=q.source_ids,
        )
        doc.findings[fid] = FindingRecord(
            finding_id=fid,
            assignment_id="orientation",
            operation_id=call_id,
            content=finding,
            source_lineage=lineage(doc, q.source_ids),
        )
        q.finding_ids = [fid]
        doc.profile_findings.setdefault(q.area, []).append(fid)
    # Preserve exactly supplied protocol content; snapshot links refer to the full request.
    if not messages(directory, session):
        raise RuntimeError("Accepted orientation context is missing")
    session.status = "done"
    doc.stage = "done" if state.run_mode == "orientation_only" else "coordinate"
    doc.stop_reason = (
        "orientation_complete" if state.run_mode == "orientation_only" else None
    )
