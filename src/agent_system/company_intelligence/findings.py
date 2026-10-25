"""Accepted evidence, explicit incorporation, and compact historical adapters."""

import hashlib
import json
from pathlib import Path

from pydantic import JsonValue

from agent_system.company_intelligence.artifacts import read_json, save_json
from agent_system.company_intelligence.call_traces import safe_path
from agent_system.company_intelligence.contracts import (
    CoordinationResult,
    EvidenceFinding,
    FindingsDelta,
)
from agent_system.company_intelligence.coordination import (
    Document,
    Finding,
    FindingRecord,
    Session,
    apply_proposal,
)
from agent_system.company_intelligence.schemas import ResearchSettings, Section


class SubmissionError(ValueError):
    def __init__(
        self,
        reason: str,
        *,
        question_ids: list[str] | None = None,
        finding_ids: list[str] | None = None,
        source_ids: list[str] | None = None,
    ) -> None:
        self.feedback: dict[str, JsonValue] = {
            "reason": reason,
            "question_ids": list(question_ids or []),
            "finding_ids": list(finding_ids or []),
            "source_ids": list(source_ids or []),
            "accepted": False,
        }
        super().__init__(json.dumps(self.feedback))


def immutable_json(
    directory: Path, relative: str, payload: dict[str, JsonValue]
) -> None:
    path = safe_path(directory, relative)
    if path.exists():
        if read_json(path) != payload:
            raise RuntimeError(f"Immutable artifact conflict: {relative}")
        return
    save_json(path, payload)


def lineage(doc: Document, source_ids: list[str]) -> list[dict[str, JsonValue]]:
    return [
        {
            "source_id": sid,
            "url": doc.sources[sid].final_url or doc.sources[sid].requested_url,
            "cleaned_hash": doc.sources[sid].cleaned_hash,
            "cleaner_version": doc.sources[sid].cleaner_version,
        }
        for sid in source_ids
    ]


def available_finding(doc: Document, fid: str) -> bool:
    record = doc.findings.get(fid)
    disposition = doc.dispositions.get(fid, {}).get("disposition")
    return (
        record is not None
        and disposition not in {"rejected", "superseded"}
        and (
            record.acceptance == "accepted"
            or disposition in {"incorporated", "already_represented"}
        )
    )


def validate_evidence(
    doc: Document, session: Session, candidate: EvidenceFinding
) -> list[dict[str, JsonValue]]:
    assignment = doc.assignments[session.session_id]
    if candidate.question_id not in assignment.question_ids:
        raise SubmissionError(
            "Question is outside this assignment", question_ids=[candidate.question_id]
        )
    invalid = [
        sid
        for sid in candidate.source_ids
        if sid not in assignment.sources
        or sid not in doc.sources
        or doc.sources[sid].outcome != "success"
    ]
    if invalid:
        raise SubmissionError(
            "Direct claims require evidence inspected by this assignment",
            question_ids=[candidate.question_id],
            source_ids=invalid,
        )
    inherited = [
        fid
        for fid in candidate.inherited_finding_ids
        if fid not in session.inherited_finding_ids
        or fid not in doc.findings
        or not available_finding(doc, fid)
        or doc.dispositions.get(fid, {}).get("disposition")
        in {"rejected", "superseded"}
    ]
    if inherited:
        raise SubmissionError(
            "Inherited findings must be available accepted context",
            question_ids=[candidate.question_id],
            finding_ids=inherited,
        )
    if candidate.status == "answered" and (
        not candidate.answer.strip()
        or not (candidate.source_ids or candidate.inherited_finding_ids)
    ):
        raise SubmissionError(
            "Answered finding needs substantive content and evidence",
            question_ids=[candidate.question_id],
        )
    if candidate.status in {"partial", "unassessed"} and not candidate.missing.strip():
        raise SubmissionError(
            "Unresolved finding needs specific missing information",
            question_ids=[candidate.question_id],
        )
    if candidate.status == "not_applicable" and not candidate.answer.strip():
        raise SubmissionError(
            "Not applicable needs an explanation", question_ids=[candidate.question_id]
        )
    for fid in candidate.supersedes:
        if (
            fid not in doc.findings
            or doc.findings[fid].content.question_id != candidate.question_id
        ):
            raise SubmissionError(
                "Supersession requires an existing finding for this question",
                finding_ids=[fid],
            )
    evidence = lineage(doc, candidate.source_ids)
    for fid in candidate.inherited_finding_ids:
        evidence.extend(doc.findings[fid].source_lineage)
    return list({json.dumps(item, sort_keys=True): item for item in evidence}.values())


def accept_delta(
    doc: Document, session: Session, delta: FindingsDelta, operation_id: str
) -> list[str]:
    """All validation precedes mutation; replay allocates the same artifact IDs."""
    records: list[FindingRecord] = []
    ids = [f.question_id for f in delta.findings]
    if len(ids) != len(set(ids)):
        raise SubmissionError("Delta has duplicate question records", question_ids=ids)
    for candidate in delta.findings:
        evidence = validate_evidence(doc, session, candidate)
        digest = hashlib.sha256(
            (session.session_id + operation_id + candidate.model_dump_json()).encode()
        ).hexdigest()[:24]
        records.append(
            FindingRecord(
                finding_id=f"finding_{digest}",
                assignment_id=session.session_id,
                operation_id=operation_id,
                content=Finding.model_validate(
                    candidate.model_dump(
                        exclude={"inherited_finding_ids", "supersedes"}
                    )
                ),
                inherited_finding_ids=candidate.inherited_finding_ids,
                supersedes=candidate.supersedes,
                source_lineage=evidence,
            )
        )
    assignment = doc.assignments[session.session_id]
    for record in records:
        previous = doc.findings.get(record.finding_id)
        if previous and previous != record:
            raise RuntimeError("Finding identity conflict")
        doc.findings[record.finding_id] = record
        if record.finding_id not in assignment.accepted_finding_ids:
            assignment.accepted_finding_ids.append(record.finding_id)
    return [r.finding_id for r in records]


def reconcile_work(doc: Document) -> None:
    for qid, question in doc.questions.items():
        owners = [
            a
            for a in doc.assignments.values()
            if qid in a.question_ids and a.status in {"pending", "running", "paused"}
        ]
        if owners:
            question.work_status = (
                "blocked" if all(a.status == "paused" for a in owners) else "assigned"
            )
        elif question.answer_status in {"answered", "not_applicable"}:
            question.work_status = "finished"
        elif qid in doc.declined_questions or any(
            a.result and a.result.outcome == "exhausted"
            for a in doc.assignments.values()
            if qid in a.question_ids
        ):
            question.work_status = "exhausted"
        elif any(
            any(
                doc.questions[other].answer_status in {"partial", "unassessed"}
                for other in dep.question_ids
            )
            and not dep.assumption
            for dep in question.dependencies
        ):
            question.work_status = "blocked"
        elif doc.finish_requested or any(
            a.status in {"completed", "partial"}
            for a in doc.assignments.values()
            if qid in a.question_ids
        ):
            question.work_status = "finished"
        else:
            question.work_status = "pending"


def incorporate(
    original: Document, submission: CoordinationResult, settings: ResearchSettings
) -> Document:
    doc = original.model_copy(deep=True)
    if submission.proposal.findings or submission.proposal.sections:
        raise SubmissionError(
            "Use explicit question/profile revisions, not legacy narrative replacements"
        )
    required: set[str] = {
        fid for fid, d in doc.dispositions.items() if d.get("disposition") == "deferred"
    }
    if doc.batches and not doc.batches[-1].incorporated:
        required.update(
            fid
            for aid in doc.batches[-1].assignment_ids
            for fid in doc.assignments[aid].accepted_finding_ids
        )
    supplied = [d.finding_id for d in submission.dispositions]
    if len(supplied) != len(set(supplied)) or set(supplied) != required:
        raise SubmissionError(
            "Every pending finding needs exactly one explicit disposition",
            finding_ids=sorted(required.symmetric_difference(supplied)),
        )
    for disposition in submission.dispositions:
        if disposition.finding_id not in doc.findings:
            raise SubmissionError(
                "Unknown disposition finding", finding_ids=[disposition.finding_id]
            )
        if any(fid not in doc.findings for fid in disposition.reference_ids):
            raise SubmissionError(
                "Disposition references an unknown finding",
                finding_ids=disposition.reference_ids,
            )
        if (
            disposition.disposition in {"already_represented", "superseded"}
            and not disposition.reference_ids
        ):
            raise SubmissionError(
                "This disposition requires a supporting/replacement finding",
                finding_ids=[disposition.finding_id],
            )
        doc.dispositions[disposition.finding_id] = disposition.model_dump(mode="json")
    for change in submission.changes:
        q = doc.questions.get(change.question_id)
        if q is None or q.revision != change.expected_revision:
            raise SubmissionError(
                "Stale question revision", question_ids=[change.question_id]
            )
        if any(
            fid not in doc.findings
            or doc.findings[fid].content.question_id != change.question_id
            for fid in change.supporting_finding_ids
        ):
            raise SubmissionError(
                "Question revision requires matching finding references",
                question_ids=[change.question_id],
                finding_ids=change.supporting_finding_ids,
            )
        if set(change.retire_finding_ids) - set(q.finding_ids):
            raise SubmissionError(
                "Cannot retire unknown question support",
                finding_ids=change.retire_finding_ids,
            )
        q.history.append(
            json.dumps(
                {
                    "revision": q.revision,
                    "finding_ids": q.finding_ids,
                    "reason": change.revision_reason,
                }
            )
        )
        q.finding_ids = list(
            dict.fromkeys(
                [fid for fid in q.finding_ids if fid not in change.retire_finding_ids]
                + change.supporting_finding_ids
            )
        )
        if change.status == "answered" and not q.finding_ids:
            raise SubmissionError(
                "Answered revision requires accepted support",
                question_ids=[q.question_id],
            )
        q.answer, q.missing, q.qualifications = (
            change.answer,
            change.missing,
            change.qualifications,
        )
        q.answer_status, q.unresolved_reason = change.status, change.reason
        q.source_ids = list(
            dict.fromkeys(
                str(item["source_id"])
                for fid in q.finding_ids
                for item in doc.findings[fid].source_lineage
            )
        )
        q.revision += 1
    for section in submission.profile_changes:
        if section.expected_profile_version != doc.profile_version or any(
            fid not in doc.findings for fid in section.supporting_finding_ids
        ):
            raise SubmissionError(
                "Profile revision has stale version or unknown support",
                finding_ids=section.supporting_finding_ids,
            )
        retained = doc.profile_findings.get(section.key, [])
        if set(section.retire_finding_ids) - set(retained):
            raise SubmissionError(
                "Cannot retire unknown profile support",
                finding_ids=section.retire_finding_ids,
            )
        supports = list(
            dict.fromkeys(
                [fid for fid in retained if fid not in section.retire_finding_ids]
                + section.supporting_finding_ids
            )
        )
        if not supports:
            raise SubmissionError("Profile revision must reference supporting findings")
        doc.profile_findings[section.key] = supports
        doc.profile[section.key] = Section(
            coverage=section.coverage,
            content=section.content,
            source_ids=list(
                dict.fromkeys(
                    str(item["source_id"])
                    for fid in supports
                    for item in doc.findings[fid].source_lineage
                )
            ),
        )
    represented = {fid for q in doc.questions.values() for fid in q.finding_ids} | {
        fid for ids in doc.profile_findings.values() for fid in ids
    }
    for disposition in submission.dispositions:
        if (
            disposition.disposition == "incorporated"
            and disposition.finding_id not in represented
        ):
            raise SubmissionError(
                "Incorporated finding needs current answer/profile support",
                finding_ids=[disposition.finding_id],
            )
        if disposition.disposition == "already_represented" and not set(
            disposition.reference_ids
        ).issubset(represented):
            raise SubmissionError(
                "Already represented must reference current support",
                finding_ids=disposition.reference_ids,
            )
        if (
            disposition.disposition in {"rejected", "superseded"}
            and disposition.finding_id in represented
        ):
            raise SubmissionError(
                "Remove rejected/superseded support explicitly",
                finding_ids=[disposition.finding_id],
            )
    try:
        updated = apply_proposal(doc, submission.proposal, settings)
    except ValueError as exc:
        raise SubmissionError(str(exc)) from exc
    reconcile_work(updated)
    return updated


def archive_findings(directory: Path, doc: Document) -> None:
    for fid, record in doc.findings.items():
        relative = f"findings/{fid}.json"
        immutable_json(directory, relative, record.model_dump(mode="json"))
        doc.finding_paths[fid] = relative


def hydrate(directory: Path, doc: Document) -> Document:
    for fid, path in doc.finding_paths.items():
        record = FindingRecord.model_validate(read_json(safe_path(directory, path)))
        if record.finding_id != fid:
            raise RuntimeError("Finding reference mismatch")
        doc.findings[fid] = record
    return doc


def adapt_legacy(directory: Path, doc: Document) -> Document:
    if doc.storage_version >= 2:
        return doc
    for aid, assignment in doc.assignments.items():
        for index, candidate in enumerate(
            assignment.result.findings if assignment.result else []
        ):
            fid = f"finding_legacy_{aid}_{index:04d}"
            doc.findings[fid] = FindingRecord(
                finding_id=fid,
                assignment_id=aid,
                operation_id="legacy",
                content=candidate,
                source_lineage=lineage(
                    doc, [sid for sid in candidate.source_ids if sid in doc.sources]
                ),
                acceptance="legacy_unverified",
            )
            assignment.accepted_finding_ids.append(fid)
        # Keep the old result as a historical record, not inherited accepted evidence.
    for qid, question in doc.questions.items():
        if question.history:
            relative = f"history/legacy_{qid}.json"
            immutable_json(directory, relative, {"history": list(question.history)})
            doc.history_paths[qid] = relative
            question.history = [f"Historical transitions: {relative}"]
        question.finding_ids = [
            fid for fid in question.finding_ids if fid in doc.findings
        ]
    doc.storage_version = 2
    return doc
