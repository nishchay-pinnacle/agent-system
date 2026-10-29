"""Run-local files, validated inputs, and atomic writes (no legacy run migration)."""

import fcntl
import hashlib
import ipaddress
import json
import re
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def public_url(value: str, *, resolve: bool = False) -> str:
    parts = urlsplit(value)
    host = parts.hostname
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username
        or parts.password
        or parts.port not in {None, 80, 443}
        or host.lower() == "localhost"
        or host.lower().endswith((".local", ".localhost"))
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("Source must be a public HTTP(S) URL without credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("Source hostname must be public") from None
    else:
        if not address.is_global:
            raise ValueError("Private/local source addresses are not allowed")
    if resolve:
        addresses = socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(
            not ipaddress.ip_address(a[4][0]).is_global for a in addresses
        ):
            raise ValueError("Source resolves to a non-public address")
    return urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def source_id(url: str) -> str:
    return "s_" + hashlib.sha256(url.encode()).hexdigest()[:16]


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    company: str = Field(min_length=1)
    product: str = Field(min_length=1)
    company_url: str
    product_url: str
    instructions: str = ""

    @field_validator("company_url", "product_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return public_url(value)

    @property
    def urls(self) -> list[str]:
        return list(dict.fromkeys([self.company_url, self.product_url]))

    @property
    def site_url(self) -> str:
        parts = urlsplit(self.company_url)
        return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    max_model_calls: int = Field(default=12, ge=1, le=1000)
    max_input_bytes: int = Field(default=1_000_000, ge=1)
    output_tokens: int = Field(default=16000, ge=256)
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    map_limit: int = Field(default=5000, ge=1, le=100000)
    index_result_limit: int = Field(default=20, ge=1, le=50)


class WorkflowIntent(BaseModel):
    """Creation-time choice; absent on runs created before automatic continuation."""

    model_config = ConfigDict(extra="forbid")
    full_workflow: bool
    model_calls: int = Field(ge=1, le=1000)
    page_budget: int = Field(ge=1, le=10000)
    max_input_bytes: int = Field(default=1_000_000, ge=1)
    index_result_limit: int = Field(ge=1, le=50)


class ContinuationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=2000)
    unresolved_questions: list[str]
    next_steps: str = Field(min_length=1, max_length=2000)
    additional_calls: int = Field(ge=1, le=1000)
    additional_pages: int = Field(ge=0, le=10000)


class ContinuationRequest(ContinuationArgs):
    request_id: str
    origin: Literal["model", "runtime"] = "model"
    status: Literal["pending", "applied"] = "pending"


class ContinuationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str
    approve: bool
    additional_calls: int = Field(ge=1, le=1000)
    additional_pages: int = Field(ge=0, le=10000)


class Run(BaseModel):
    version: int = 3
    run_id: str
    inputs: Inputs
    settings: Settings
    status: str = "incomplete"
    model_calls: int = 0
    error: str | None = None
    stage: Literal["orientation", "research"] = "orientation"
    continuation: ContinuationRequest | None = None
    workflow_intent: WorkflowIntent | None = None
    research_page_budget: int = Field(default=0, ge=0, le=10000)
    research_page_attempts: int = Field(default=0, ge=0)


def run_directory(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid run ID")
    path = root / run_id
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Run directory escapes storage root")
    return path


def local_path(directory: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid run-local path")
    path = directory / relative
    if not path.resolve().is_relative_to(directory.resolve()) or any(
        p.is_symlink() for p in [path, *path.parents] if p != directory.parent
    ):
        raise ValueError("Run artifact must stay within the run (no symlinks)")
    return path


def write_text(directory: Path, name: str, content: str) -> None:
    path = local_path(directory, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(directory: Path, name: str, value: object) -> None:
    write_text(directory, name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(directory: Path, name: str) -> dict[str, JsonValue]:
    return JSON_OBJECT.validate_json(
        local_path(directory, name).read_text(encoding="utf-8")
    )


def save_run(directory: Path, run: Run) -> None:
    save_json(directory, "run.json", run.model_dump(mode="json"))


@contextmanager
def run_lock(directory: Path) -> Iterator[None]:
    with local_path(directory, ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another process owns this run") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class FirecrawlCounts(BaseModel):
    scrape: int = Field(default=0, ge=0)
    map: int = Field(default=0, ge=0)
    complete_history: bool = False


def firecrawl_counts(directory: Path) -> FirecrawlCounts:
    if not local_path(directory, "debug/firecrawl-counts.json").exists():
        return FirecrawlCounts()
    return FirecrawlCounts.model_validate(
        read_json(directory, "debug/firecrawl-counts.json")
    )


def record_firecrawl_call(directory: Path, operation: Literal["scrape", "map"]) -> None:
    counts = firecrawl_counts(directory)
    if operation == "scrape":
        counts.scrape += 1
    else:
        counts.map += 1
    # Reserve before calling so failed requests also count across resume.
    save_json(directory, "debug/firecrawl-counts.json", counts.model_dump())


RESEARCH_START = "research/start.json"
RESEARCH_RECEIPT = (
    "Orientation is complete and research is now active. The starting pages, initial "
    "profile and company site index are saved. Investigate material gaps using the "
    "saved index and linked HTML evidence, update the profile, then explicitly "
    "finish_research when further public research is unlikely to materially help."
)


class ResearchStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[3] = 3
    original_run: Run
    system: str = Field(min_length=1)
    history: list[str]
    receipt: str = RESEARCH_RECEIPT
    model_calls: int = Field(ge=1, le=1000)
    page_budget: int = Field(ge=1, le=10000)
    max_input_bytes: int = Field(default=1_000_000, ge=1)
    index_result_limit: int = Field(default=20, ge=1, le=50)


def load_research_start(directory: Path, run: Run) -> ResearchStart:
    saved = read_json(directory, RESEARCH_START)
    if saved.get("version") != 3:
        raise ValueError("Unsupported research context version; preserve run files")
    start = ResearchStart.model_validate(saved)
    if (
        start.original_run.run_id != run.run_id
        or start.original_run.inputs != run.inputs
        or start.original_run.stage != "orientation"
        or start.original_run.status != "complete"
        or start.receipt != RESEARCH_RECEIPT
    ):
        raise ValueError("Research context does not match this completed orientation")
    return start


def activate_research(directory: Path, run: Run) -> ResearchStart:
    start = load_research_start(directory, run)
    if run.stage == "orientation":
        if (
            run.status != "complete"
            or run.model_calls != start.original_run.model_calls
        ):
            raise ValueError(
                "Cannot activate research from changed/incomplete orientation"
            )
        run.settings = Settings.model_validate(
            {
                **start.original_run.settings.model_dump(),
                "max_model_calls": start.original_run.model_calls + start.model_calls,
                "max_input_bytes": start.max_input_bytes,
                "index_result_limit": start.index_result_limit,
            }
        )
        run.stage, run.status, run.error = "research", "incomplete", None
        run.research_page_budget = start.page_budget
        save_run(directory, run)
    return start


class SourceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(pattern=r"^S[1-9][0-9]*$")
    useful: bool


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1)
    status: Literal["open", "answered", "needs_interview", "merged"]
    priority_reason: str = Field(min_length=1)
    answer: str | None
    answer_basis: Literal["documented", "inferred", "mixed"] | None
    source_assessments: list[SourceAssessment]
    closure_reason: str | None
    merged_into: str | None = None

    @property
    def unproductive_pages(self) -> int:
        streak = 0
        for assessment in self.source_assessments:
            streak = 0 if assessment.useful else streak + 1
        return streak


class QuestionIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active_question_id: str | None
    questions: list[Question]
