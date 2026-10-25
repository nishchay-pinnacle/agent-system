"""Revised role contracts; historical sessions keep their original validators."""

from typing import Literal

from pydantic import Field

from agent_system.company_intelligence.coordination import (
    CoordinatorSubmission,
    Finding,
    InvestigationResult,
    NewQuestion,
)
from agent_system.company_intelligence.schemas import (
    PlannedInvestigation,
    QuestionAssessment,
    SectionKey,
    SectionUpdate,
    StrictModel,
)


class OrientationAssessment(QuestionAssessment):
    missing: str


class OrientationResult(StrictModel):
    sections: list[SectionUpdate]
    assessments: list[OrientationAssessment]
    plan: list[PlannedInvestigation]
    contradictions: list[str]


class EvidenceFinding(Finding):
    inherited_finding_ids: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)


class FindingsDelta(StrictModel):
    findings: list[EvidenceFinding] = Field(default_factory=list)


class FinalResult(StrictModel):
    findings: list[EvidenceFinding]
    assumptions: list[str]
    incidental_findings: list[str]
    new_questions: list[NewQuestion] = Field(default_factory=list)
    attempted_approaches: list[str]
    next_sources: list[str]
    remaining_uncertainty: str
    outcome: Literal["completed", "partial", "exhausted", "blocked"]


class QuestionRevision(EvidenceFinding):
    expected_revision: int
    supporting_finding_ids: list[str]
    revision_reason: str = Field(min_length=1)
    retire_finding_ids: list[str] = Field(default_factory=list)


class ProfileRevision(SectionUpdate):
    supporting_finding_ids: list[str]
    expected_profile_version: int
    revision_reason: str = Field(min_length=1)
    retire_finding_ids: list[str] = Field(default_factory=list)


class Disposition(StrictModel):
    finding_id: str
    disposition: Literal[
        "incorporated", "already_represented", "superseded", "rejected", "deferred"
    ]
    reason: str = Field(min_length=1)
    reference_ids: list[str]


class CoordinationResult(StrictModel):
    changes: list[QuestionRevision]
    profile_changes: list[ProfileRevision]
    dispositions: list[Disposition]
    # Routing/dependency fields retain their established representation.
    proposal: CoordinatorSubmission


class JustifiedAction(StrictModel):
    reason: str = Field(min_length=1)
    remaining_question: str = Field(min_length=1)
    expected_value: str = Field(min_length=1)
    delta: FindingsDelta = Field(default_factory=FindingsDelta)


class ReadSource(JustifiedAction):
    source_id: str


class ScrapePage(JustifiedAction):
    url: str
    reopen_reason: str | None = None


class SearchInventory(JustifiedAction):
    query: str


class ReadFindings(JustifiedAction):
    reference_id: str


class ReadProfile(JustifiedAction):
    area: SectionKey


class FindPages(JustifiedAction):
    url: str
    query: str


TOOL_MODELS: dict[str, type[JustifiedAction]] = {
    "read_source": ReadSource,
    "read_findings": ReadFindings,
    "read_profile": ReadProfile,
    "search_site_index": SearchInventory,
    "scrape_page": ScrapePage,
    "find_site_pages": FindPages,
    "search_web": SearchInventory,
}


def legacy_result(result: FinalResult) -> InvestigationResult:
    """Routing metadata only; accepted finding payloads live in their own artifacts."""
    return InvestigationResult.model_validate(
        {**result.model_dump(exclude={"findings"}), "findings": []}
    )
