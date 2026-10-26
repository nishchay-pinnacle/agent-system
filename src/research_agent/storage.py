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
    input_allowance: int = Field(default=96000, ge=1000)
    output_tokens: int = Field(default=8000, ge=256)
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    map_limit: int = Field(default=5000, ge=1, le=100000)


class Run(BaseModel):
    version: int = 1
    run_id: str
    inputs: Inputs
    settings: Settings
    status: str = "incomplete"
    model_calls: int = 0
    error: str | None = None


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
