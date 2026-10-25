"""Version-three research records and atomic coordinator proposal validation."""

from typing import Literal

from pydantic import Field, JsonValue

from agent_system.company_intelligence.research_questions import QUESTIONS
from agent_system.company_intelligence.schemas import (
    SECTION_KEYS,
    ResearchSettings,
    Section,
    SectionKey,
    SectionUpdate,
    Source,
    StrictModel,
)

AnswerStatus = Literal["unassessed", "partial", "answered", "not_applicable"]
WorkStatus = Literal["pending", "assigned", "blocked", "finished", "exhausted"]
Reason = Literal[
    "Company confirmation needed",
    "Public sources exhausted",
    "Prerequisite blocked",
    "Budget/user stop before resolution",
]


class Dependency(StrictModel):
    fact: str = Field(min_length=1)
    question_ids: list[str]
    assumption: str = ""
    limitations: str = ""


class Question(StrictModel):
    question_id: str
    text: str
    area: SectionKey
    answer: str = ""
    missing: str = ""
    importance: int = Field(default=3, ge=1, le=5)
    importance_reason: str = "Required product understanding"
    dependencies: list[Dependency] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    origin: str | None = None
    history: list[str] = Field(default_factory=list)
    answer_status: AnswerStatus = "unassessed"
    work_status: WorkStatus = "pending"
    unresolved_reason: Reason = "Company confirmation needed"
    merged_into: str | None = None


class Finding(StrictModel):
    question_id: str
    answer: str
    status: AnswerStatus
    missing: str
    source_ids: list[str]
    qualifications: str = ""
    explanation: str = ""
    reason: Reason = "Company confirmation needed"


class NewQuestion(StrictModel):
    text: str = Field(min_length=1)
    area: SectionKey
    missing: str = Field(min_length=1)
    importance: int = Field(ge=1, le=5)
    importance_reason: str
    origin: str | None = None


class AssignmentSpec(StrictModel):
    topic: str = Field(min_length=1)
    question_ids: list[str] = Field(min_length=1)
    objective: str = Field(min_length=1)
    prerequisites: list[Dependency]
    context_areas: list[SectionKey]
    source_leads: list[str]
    completion_criterion: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    reopen_reason: str = ""
    new_source: str = ""


class InvestigationResult(StrictModel):
    findings: list[Finding]
    assumptions: list[str]
    incidental_findings: list[str]
    new_questions: list[NewQuestion]
    attempted_approaches: list[str]
    next_sources: list[str]
    remaining_uncertainty: str
    outcome: Literal["progress", "completed", "partial", "exhausted", "blocked"]


class Assignment(AssignmentSpec):
    launched: bool = False
    assignment_id: str
    batch_id: str
    profile_version: int
    status: Literal[
        "pending", "running", "paused", "completed", "partial", "failed"
    ] = "pending"
    result: InvestigationResult | None = None
    sources: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class Batch(StrictModel):
    batch_id: str
    assignment_ids: list[str]
    incorporated: bool = False


class Merge(StrictModel):
    old_id: str
    target_id: str
    reason: str = Field(min_length=1)


class DependencyUpdate(StrictModel):
    question_id: str
    dependencies: list[Dependency]


class PriorityUpdate(StrictModel):
    question_id: str
    importance: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1)


class CoordinatorSubmission(StrictModel):
    sections: list[SectionUpdate]
    findings: list[Finding]
    new_questions: list[NewQuestion]
    merges: list[Merge]
    dependencies: list[DependencyUpdate]
    priorities: list[PriorityUpdate] = Field(default_factory=list)
    assumptions: list[str]
    assignments: list[AssignmentSpec]
    decision: Literal[
        "investigate", "sufficient_understanding", "public_sources_exhausted", "blocked"
    ]
    rationale: str = Field(min_length=1)


class ReviewReport(StrictModel):
    reviewed: str
    not_reviewed: str
    unsupported_claims: list[str]
    lost_qualifications: list[str]
    missed_facts: list[str]
    contradictions: list[str]
    inadequate_answers: list[str]
    suggested_corrections: list[str]
    source_ids: list[str]
    question_ids: list[str]


class Review(StrictModel):
    review_id: str
    batch_id: str
    snapshot_path: str
    source_ids: list[str]
    status: str = "pending"
    report: ReviewReport | None = None
    error: str | None = None


class PendingAction(StrictModel):
    name: str
    args: dict[str, JsonValue]
    call_id: str
    reason: str
    remaining_question: str
    expected_value: str


class Session(StrictModel):
    session_id: str
    role: Literal["orientation", "coordinator", "investigator", "quality"]
    transcript_path: str
    status: Literal["ready", "running", "paused", "done", "failed"] = "ready"
    action: PendingAction | None = None
    delivered: list[str] = Field(default_factory=list)
    operation_id: str | None = None
    counted_input: int | None = None
    next_generation: bool = True
    correction_used: bool = False


class OperationRecord(StrictModel):
    operation_id: str
    session_id: str
    kind: str
    args: dict[str, JsonValue]
    attempt: int = 1
    status: Literal[
        "reserved", "dispatched", "completed", "consumed", "unknown", "failed"
    ] = "reserved"
    tokens: int = 0
    input_tokens: int | None = None
    page_reserved: bool = False
    request_path: str | None = None
    receipt_path: str
    waiters: list[str] = Field(default_factory=list)


class LimitItem(StrictModel):
    scope: str
    resource: str
    current: float
    used: float
    reserved: float
    increase: float


class Approval(StrictModel):
    request_id: str
    session_id: str
    revision: int = 1
    items: list[LimitItem]
    action: str
    reason: str
    remaining_question: str
    expected_value: str
    created_at: str
    status: Literal["pending", "approved", "declined", "cancelled"] = "pending"
    decided_at: str | None = None


class Document(StrictModel):
    revision: int = 0
    profile_version: int = 0
    profile: dict[SectionKey, Section] = Field(
        default_factory=lambda: {k: Section() for k in SECTION_KEYS}
    )
    assumptions: list[str] = Field(default_factory=list)
    questions: dict[str, Question] = Field(default_factory=dict)
    assignments: dict[str, Assignment] = Field(default_factory=dict)
    batches: list[Batch] = Field(default_factory=list)
    sessions: dict[str, Session] = Field(default_factory=dict)
    operations: dict[str, OperationRecord] = Field(default_factory=dict)
    sources: dict[str, Source] = Field(default_factory=dict)
    source_attempts: dict[str, list[str]] = Field(default_factory=dict)
    approvals: list[Approval] = Field(default_factory=list)
    amendments: list[dict[str, JsonValue]] = Field(default_factory=list)
    applied_commands: dict[str, str] = Field(default_factory=dict)
    reviews: dict[str, Review] = Field(default_factory=dict)
    usage: dict[str, float] = Field(default_factory=dict)
    limits: dict[str, float] = Field(default_factory=dict)
    original_limits: dict[str, float] = Field(default_factory=dict)
    stage: Literal["orientation", "coordinate", "batch", "final_check", "done"] = (
        "orientation"
    )
    final_check_done: bool = False
    finish_requested: bool = False
    stop_reason: str | None = None
    events: list[dict[str, JsonValue]] = Field(default_factory=list)
    declined_questions: list[str] = Field(default_factory=list)
    discovery: dict[str, JsonValue] = Field(default_factory=dict)
    orientation_queue: list[dict[str, JsonValue]] = Field(default_factory=list)


def catalog() -> dict[str, Question]:
    areas = dict(zip("SCUFPBEL", SECTION_KEYS, strict=True))
    return {
        qid: Question(question_id=qid, text=text, area=areas[qid[0]], missing=text)
        for qid, text in QUESTIONS.items()
    }


def validate_finding(doc: Document, finding: Finding) -> None:
    if finding.question_id not in doc.questions:
        raise ValueError("Unknown question")
    if finding.status == "answered" and (
        not finding.answer.strip() or not finding.source_ids
    ):
        raise ValueError("Answered questions require substantive saved evidence")
    if finding.status in {"partial", "unassessed"} and not finding.missing.strip():
        raise ValueError("Unresolved questions require specific missing information")
    if finding.status == "not_applicable" and not finding.answer.strip():
        raise ValueError("Not applicable requires an explanation")
    if any(
        s not in doc.sources or doc.sources[s].outcome != "success"
        for s in finding.source_ids
    ):
        raise ValueError("Citation must reference successful saved evidence")


def validate_dependencies(doc: Document) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(qid: str) -> None:
        if qid in visiting:
            raise ValueError("Question dependency cycle")
        if qid in visited:
            return
        visiting.add(qid)
        for dep in doc.questions[qid].dependencies:
            if not dep.question_ids:
                raise ValueError("Dependencies need linked questions")
            for linked in dep.question_ids:
                if linked not in doc.questions:
                    raise ValueError("Unknown dependency question")
                visit(linked)
        visiting.remove(qid)
        visited.add(qid)

    for qid in doc.questions:
        visit(qid)


def apply_proposal(
    original: Document, proposal: CoordinatorSubmission, settings: ResearchSettings
) -> Document:
    """Validate on a copy; return a complete transaction or raise without mutation."""
    doc = original.model_copy(deep=True)
    if doc.batches and not doc.batches[-1].incorporated:
        batch = doc.batches[-1]
        if any(
            doc.assignments[a].status not in {"completed", "partial", "failed"}
            for a in batch.assignment_ids
        ):
            raise ValueError("Batch is not settled")
        for aid in batch.assignment_ids:
            assignment = doc.assignments[aid]
            for qid in assignment.question_ids:
                question = doc.questions[qid]
                question.history.append(
                    f"{aid}: {assignment.result.model_dump_json() if assignment.result else assignment.error}"
                )
                if assignment.result:
                    question.finding_ids.append(aid)
                if question.answer_status in {"unassessed", "partial"}:
                    question.work_status = (
                        "exhausted"
                        if qid in doc.declined_questions
                        or (
                            assignment.result
                            and assignment.result.outcome == "exhausted"
                        )
                        else "pending"
                    )
            for new in assignment.result.new_questions if assignment.result else []:
                if not any(
                    q.text == new.text for q in doc.questions.values()
                ) and not any(q.text == new.text for q in proposal.new_questions):
                    raise ValueError(
                        "Coordinator must represent newly discovered questions"
                    )
        batch.incorporated = True
    for new in proposal.new_questions:
        if new.origin and new.origin not in doc.assignments:
            raise ValueError("Unknown originating investigation")
        qid = f"Q{len([q for q in doc.questions if q.startswith('Q')]) + 1:04d}"
        doc.questions[qid] = Question(question_id=qid, **new.model_dump())
    seen: set[str] = set()
    for finding in proposal.findings:
        validate_finding(doc, finding)
        if finding.question_id in seen:
            raise ValueError("Duplicate question update")
        seen.add(finding.question_id)
        q = doc.questions[finding.question_id]
        q.history.append(q.model_dump_json(exclude={"history"}))
        q.answer, q.missing, q.answer_status = (
            finding.answer,
            finding.missing,
            finding.status,
        )
        q.source_ids, q.unresolved_reason = finding.source_ids, finding.reason
        q.work_status = (
            "finished"
            if finding.status in {"answered", "not_applicable"}
            else "pending"
        )
    for merge in proposal.merges:
        if (
            merge.old_id not in doc.questions
            or merge.target_id not in doc.questions
            or merge.old_id == merge.target_id
        ):
            raise ValueError("Invalid merge")
        cursor: str | None = merge.target_id
        while cursor:
            if cursor == merge.old_id:
                raise ValueError("Merge cycle")
            cursor = doc.questions[cursor].merged_into
        doc.questions[merge.old_id].merged_into = merge.target_id
        doc.questions[merge.old_id].history.append(merge.reason)
    for update in proposal.dependencies:
        qid, dependencies = update.question_id, update.dependencies
        if qid not in doc.questions:
            raise ValueError("Unknown dependency owner")
        doc.questions[qid].dependencies = dependencies
    for priority in proposal.priorities:
        if priority.question_id not in doc.questions:
            raise ValueError("Unknown priority question")
        doc.questions[priority.question_id].importance = priority.importance
        doc.questions[priority.question_id].importance_reason = priority.reason
    validate_dependencies(doc)
    for q in doc.questions.values():
        if q.answer_status in {"unassessed", "partial"} and any(
            not dep.assumption
            and any(
                doc.questions[other].answer_status in {"unassessed", "partial"}
                for other in dep.question_ids
            )
            for dep in q.dependencies
        ):
            q.work_status = "blocked"
            q.unresolved_reason = "Prerequisite blocked"
    for section in proposal.sections:
        if any(
            s not in doc.sources or doc.sources[s].outcome != "success"
            for s in section.source_ids
        ):
            raise ValueError("Unsupported profile citation")
        if not section.content.strip() or (
            section.coverage == "answered" and not section.source_ids
        ):
            raise ValueError("Profile requires content and evidence")
        doc.profile[section.key] = Section(**section.model_dump(exclude={"key"}))
    doc.assumptions = proposal.assumptions
    doc.profile_version += 1
    assigned: set[str] = set()
    for spec in proposal.assignments:
        if len(spec.question_ids) != len(set(spec.question_ids)):
            raise ValueError("Duplicate question within assignment")
        for qid in spec.question_ids:
            if qid not in doc.questions or qid in assigned:
                raise ValueError("Unknown or duplicate assigned question")
            if doc.questions[qid].merged_into:
                raise ValueError("Assign the surviving merged question")
            if (
                qid in doc.declined_questions
                or doc.questions[qid].work_status == "exhausted"
            ):
                if not spec.reopen_reason.strip() or not spec.new_source.strip():
                    raise ValueError(
                        "Reopening requires changed evidence or a new source and reason"
                    )
                if any(
                    spec.new_source in a.sources or spec.new_source in a.source_leads
                    for a in doc.assignments.values()
                    if qid in a.question_ids
                ):
                    raise ValueError("Reopening source was already attempted")
            assigned.add(qid)
    for spec in proposal.assignments:
        dependencies = spec.prerequisites + [
            d for q in spec.question_ids for d in doc.questions[q].dependencies
        ]
        for dep in dependencies:
            if not dep.question_ids or any(
                q not in doc.questions for q in dep.question_ids
            ):
                raise ValueError("Prerequisites need known question links")
            unresolved = [
                q
                for q in dep.question_ids
                if doc.questions[q].answer_status not in {"answered", "not_applicable"}
            ]
            if unresolved and (
                not dep.assumption.strip() or not dep.limitations.strip()
            ):
                raise ValueError(
                    "Missing prerequisite needs an explicit qualified assumption"
                )
            if any(q in assigned and q not in spec.question_ids for q in unresolved):
                raise ValueError("Same-batch unfinished dependency")
    if proposal.decision != "investigate" and proposal.assignments:
        raise ValueError("Stop decision cannot launch assignments")
    if proposal.decision == "investigate" and not proposal.assignments:
        raise ValueError("Investigation decision needs assignments")
    if proposal.assignments:
        bid = f"batch_{len(doc.batches) + 1:04d}"
        batch = Batch(batch_id=bid, assignment_ids=[])
        for spec in proposal.assignments:
            aid = f"assignment_{len(doc.assignments) + 1:04d}"
            doc.assignments[aid] = Assignment(
                **spec.model_dump(),
                assignment_id=aid,
                batch_id=bid,
                profile_version=doc.profile_version,
                limits={
                    "generation_requests": settings.investigator_generations,
                    "pages": settings.investigator_pages,
                    "tool_actions": settings.investigator_actions,
                },
            )
            batch.assignment_ids.append(aid)
            for qid in spec.question_ids:
                doc.questions[qid].work_status = "assigned"
        doc.batches.append(batch)
        doc.stage = "batch"
    elif proposal.decision == "blocked":
        doc.stop_reason = "prerequisite_blocked"
    else:
        doc.stop_reason = proposal.decision
        doc.stage = "done" if doc.final_check_done else "final_check"
    return doc


def interview_backlog(doc: Document) -> list[dict[str, JsonValue]]:
    if not set(QUESTIONS).issubset(doc.questions):
        raise ValueError("Original question IDs must remain represented")
    validate_dependencies(doc)
    return [
        {
            "question_ids": [q.question_id],
            "question": q.missing.strip() or q.text,
            "reason": q.unresolved_reason,
            "priority": q.importance,
            "answer": q.answer,
            "dependencies": [d.model_dump(mode="json") for d in q.dependencies],
        }
        for q in sorted(
            doc.questions.values(), key=lambda q: (q.importance, q.question_id)
        )
        if q.answer_status in {"unassessed", "partial"}
    ]
