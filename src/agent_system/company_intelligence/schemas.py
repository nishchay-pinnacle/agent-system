"""Validated research state and narrow model-owned tool arguments."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

SectionKey = Literal[
    "scope",
    "capabilities",
    "problems_and_use_cases",
    "customer_fit",
    "people_and_roles",
    "buying_decisions",
    "distinctive_expertise",
    "customer_language",
]
SECTION_KEYS: tuple[SectionKey, ...] = (
    "scope",
    "capabilities",
    "problems_and_use_cases",
    "customer_fit",
    "people_and_roles",
    "buying_decisions",
    "distinctive_expertise",
    "customer_language",
)
Coverage = Literal[
    "unassessed", "answered", "partial", "needs_internal_input", "not_applicable"
]
Phase = Literal["orientation", "between", "investigation", "review", "closing_review"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchSettings(StrictModel):
    model: str = "gpt-6.1-sol"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    generation_requests: int = Field(default=60, gt=0)
    firecrawl_requests: int = Field(default=40, gt=0)
    pages: int = Field(default=30, gt=0)
    investigations: int = Field(default=12, gt=0)
    investigation_retrievals: int = Field(default=6, gt=0)
    retrievals: int = Field(default=100, gt=0)
    active_seconds: float = Field(default=1800, gt=0)
    total_tokens: int = Field(default=500_000, gt=0)
    input_tokens: int = Field(default=48_000, gt=0)
    output_tokens: int = Field(default=6000, gt=0)
    request_seconds: float = Field(default=90, gt=0)


class Inputs(StrictModel):
    company: str
    url: str
    product: str
    product_url: str
    source_urls: list[str] = Field(default_factory=list)


class Section(StrictModel):
    coverage: Coverage = "unassessed"
    content: str = ""
    source_ids: list[str] = Field(default_factory=list)


class SectionUpdate(StrictModel):
    key: SectionKey
    coverage: Coverage
    content: str
    source_ids: list[str]


class GapUpdate(StrictModel):
    gap_id: str | None
    question: str = Field(min_length=1)
    consequence: str = Field(min_length=1)
    state: Literal["open", "answered", "partial", "needs_internal_input"]
    critical_boundary: bool
    interview_question: str


class Gap(GapUpdate):
    attempts: int = 0
    unsuccessful: int = 0
    parked: bool = False
    history: list[str] = Field(default_factory=list)


class Begin(StrictModel):
    gap_id: str | None
    question: str = Field(min_length=1)
    importance: str = Field(min_length=1)
    source_plan: str = Field(min_length=1)
    resolution_criterion: str = Field(min_length=1)
    new_source_id: str | None
    reopen_reason: str | None


class Complete(StrictModel):
    phase: Phase
    sections: list[SectionUpdate]
    gaps: list[GapUpdate]
    source_ids: list[str]
    findings: str = Field(min_length=1)
    contradictions: list[str]
    meaningful_change: bool
    next_step: Literal[
        "investigate", "review", "sufficient_understanding", "public_sources_exhausted"
    ]
    next_source: str | None
    return_question: str | None
    readiness: Literal["ready", "needs_review"]


class MapArgs(StrictModel):
    url: str
    query: str


class SearchArgs(StrictModel):
    query: str


class ScrapeArgs(StrictModel):
    url: str


class ReadArgs(StrictModel):
    source_id: str
    start_line: int = Field(ge=1)
    max_lines: int = Field(ge=1, le=200)


class Source(StrictModel):
    source_id: str
    requested_url: str
    final_url: str | None = None
    title: str = ""
    fetched_at: str | None = None
    filename: str | None = None
    outcome: Literal["discovered", "success", "failed"] = "discovered"


class Usage(StrictModel):
    generation_requests: int = 0
    counting_requests: int = 0
    firecrawl_requests: int = 0
    pages: int = 0
    retrievals: int = 0
    active_seconds: float = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int | None = None
    reserved_tokens: int = 0
    unknown_requests: list[str] = Field(default_factory=list)
    firecrawl_credits: float | None = None


class Operation(StrictModel):
    operation_id: str
    kind: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    call_id: str | None = None
    attempts: int = 0
    reserved_tokens: int = 0
    reserved_seconds: float = 0
    counted_input: int | None = None


class Research(StrictModel):
    job_id: str
    job_type: Literal["research"] = "research"
    inputs: Inputs
    settings: ResearchSettings
    instructions: str
    phase: Phase = "orientation"
    status: Literal["running", "completed", "blocked_by_error", "interrupted"] = (
        "running"
    )
    stop_reason: str | None = None
    limiting_resource: str | None = None
    error: str | None = None
    readiness: Literal["ready", "needs_review"] = "needs_review"
    profile: dict[SectionKey, Section] = Field(
        default_factory=lambda: {k: Section() for k in SECTION_KEYS}
    )
    sources: dict[str, Source] = Field(default_factory=dict)
    gaps: list[Gap] = Field(default_factory=list)
    investigations: list[dict[str, JsonValue]] = Field(default_factory=list)
    current: Begin | None = None
    investigation_retrievals: int = 0
    completed_investigations: int = 0
    no_progress: int = 0
    review_returned: bool = False
    return_question: str | None = None
    messages: list[dict[str, JsonValue]] = Field(default_factory=list)
    operation: Operation | None = None
    stage: Literal["plan", "reserve", "execute", "consume", "halt"] = "plan"
    operation_number: int = 0
    correction_used: bool = False
    usage: Usage = Field(default_factory=Usage)
    cache: dict[str, JsonValue] = Field(default_factory=dict)
    orientation_queue: list[Operation] = Field(default_factory=list)
    orientation_extra: bool = False
    last_activity: str = ""
    events: list[dict[str, JsonValue]] = Field(default_factory=list)
