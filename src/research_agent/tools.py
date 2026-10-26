"""The three orientation tools and the Firecrawl boundary."""

import hashlib
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from bs4 import BeautifulSoup
from firecrawl import FirecrawlError
from firecrawl.v2.client import FirecrawlClient
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from requests.exceptions import RequestException

from research_agent.cleaning import clean_markdown, normalize_input, usable
from research_agent.html_cleaning import ConversionError, clean_html
from research_agent.storage import (
    Inputs,
    Settings,
    local_path,
    public_url,
    read_json,
    save_json,
    source_id,
    write_text,
)

logger = logging.getLogger(__name__)


class Capture(BaseModel):
    markdown: str = ""
    html: str = ""
    raw_html: str = ""
    final_url: str = ""
    title: str = ""
    status_code: int | None = None
    content_type: str = ""
    error: str = ""


class Entry(BaseModel):
    url: str
    title: str = ""
    description: str = ""


class Pages(Protocol):
    def scrape(self, url: str) -> Capture: ...
    def map(self, url: str) -> list[Entry]: ...


class FirecrawlPages:
    def __init__(self, api_key: str, settings: Settings) -> None:
        # This SDK counts total attempts: 2 means one retry for 502/transport failures.
        self.client = FirecrawlClient(
            api_key=api_key, timeout=settings.timeout_seconds, max_retries=2
        )
        self.settings = settings

    def scrape(self, url: str) -> Capture:
        try:
            document = self.client.scrape(
                public_url(url, resolve=True),
                formats=["markdown", "html", "rawHtml"],
                only_main_content=False,
                auto_resume=False,
                timeout=int(self.settings.timeout_seconds * 1000),
            )
            raw = document.model_dump(mode="json")
            metadata = raw.get("metadata") or {}
            final_url = metadata.get("source_url") or url
            public_url(final_url, resolve=True)
            return Capture.model_validate(
                {
                    "markdown": raw.get("markdown") or "",
                    "html": raw.get("html") or "",
                    "raw_html": raw.get("raw_html") or "",
                    "final_url": final_url,
                    "title": metadata.get("title") or "",
                    "status_code": metadata.get("status_code"),
                    "content_type": metadata.get("content_type") or "",
                    "error": metadata.get("error") or "",
                }
            )
        except (FirecrawlError, RequestException) as exc:
            raise RuntimeError(
                f"Firecrawl scrape failed ({type(exc).__name__}); capture incomplete"
            ) from exc

    def map(self, url: str) -> list[Entry]:
        try:
            mapped = self.client.map(
                public_url(url, resolve=True),
                sitemap="include",
                include_subdomains=True,
                limit=self.settings.map_limit,
                timeout=int(self.settings.timeout_seconds * 1000),
            )
            return [
                Entry(
                    url=link.url,
                    title=link.title or "",
                    description=link.description or "",
                )
                for link in mapped.links
            ]
        except (FirecrawlError, RequestException) as exc:
            raise RuntimeError(
                f"Firecrawl map failed ({type(exc).__name__}); index incomplete"
            ) from exc


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str


class ProfileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str = Field(min_length=1)


class IndexArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    site_url: str


TOOL_MODELS: dict[str, type[BaseModel]] = {
    "read_page": ReadArgs,
    "update_profile": ProfileArgs,
    "build_site_index": IndexArgs,
}


def evidence_problem(capture: Capture) -> str | None:
    if capture.status_code is not None and capture.status_code >= 400:
        return f"HTTP {capture.status_code}"
    if capture.error:
        return "Provider reported a source error"
    if any(
        kind in capture.content_type.lower()
        for kind in (
            "application/pdf",
            "application/octet-stream",
            "image/",
            "audio/",
            "video/",
        )
    ):
        return "Unsupported non-page content type"
    pattern = r"(?:404(?:[ :–-]+(?:page )?not found)?|(?:page )?not found|403(?:[ :–-]+forbidden)?|access denied|forbidden|verify (?:that )?you are human|just a moment)[.! ]*"
    for raw in (capture.html, capture.raw_html):
        if not raw:
            continue
        soup = BeautifulSoup(raw, "html.parser")
        main = soup.find("main") or soup.find(True, attrs={"role": "main"}) or soup
        heading, title = main.find("h1"), soup.find("title")
        if heading and re.fullmatch(pattern, heading.get_text(" ", strip=True), re.I):
            if not main.find("article") or (
                title and re.fullmatch(pattern, title.get_text(" ", strip=True), re.I)
            ):
                return "Structural error/access-blocked capture"
    return None


class OrientationTools:
    def __init__(
        self, directory: Path, inputs: Inputs, settings: Settings, pages: Pages
    ) -> None:
        self.directory, self.inputs, self.settings, self.pages = (
            directory,
            inputs,
            settings,
            pages,
        )

    def pages_read(self) -> bool:
        return all(
            local_path(self.directory, f"sources/{source_id(url)}.json").exists()
            for url in self.inputs.urls
        )

    def complete(self) -> bool:
        profile = local_path(self.directory, "profile.md")
        index = local_path(self.directory, "site_index.json")
        return (
            self.pages_read()
            and profile.exists()
            and bool(profile.read_text().strip())
            and index.exists()
        )

    def read_page(self, url: str) -> dict[str, JsonValue]:
        url = public_url(url)
        if url not in self.inputs.urls:
            raise ValueError("Orientation may read only the supplied starting pages")
        sid = source_id(url)
        reference = f"sources/{sid}.md"
        metadata_name = f"sources/{sid}.json"
        if not local_path(self.directory, metadata_name).exists():
            capture_name = f"sources/{sid}.capture.json"
            if local_path(self.directory, capture_name).exists():
                capture = Capture.model_validate(
                    read_json(self.directory, capture_name)
                )
            else:
                capture = self.pages.scrape(url)
                # Original representations persist before cleaning or validation.
                save_json(self.directory, capture_name, capture.model_dump(mode="json"))
            problem = evidence_problem(capture)
            cleaned = ""
            report: dict[str, JsonValue] = {}
            reasons: list[str] = []
            if not problem:
                for kind, raw in (
                    ("html", capture.html),
                    ("raw_html", capture.raw_html),
                ):
                    if not raw:
                        continue
                    try:
                        result = clean_html(
                            normalize_input(raw), capture.final_url or url, sid
                        )
                    except ConversionError as exc:
                        reasons.append(str(exc))
                        continue
                    candidate = clean_markdown(result.content)
                    if usable(candidate):
                        cleaned = candidate
                        report = {"representation": kind, "details": asdict(result)}
                        break
                if not cleaned:
                    cleaned = clean_markdown(capture.markdown)
                    report = {
                        "representation": "markdown",
                        "warning": "DOM unavailable; ambiguous interface content retained",
                    }
                if not usable(cleaned):
                    problem = "No usable cleaned evidence"
            if problem:
                # Keep failed attempts, but allow a fresh retrieval on explicit resume.
                attempt = f"sources/{sid}.failed-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.json"
                local_path(self.directory, capture_name).replace(
                    local_path(self.directory, attempt)
                )
                raise ValueError(
                    f"Starting page {sid} unusable: {problem}; original capture saved"
                )
            write_text(self.directory, reference, cleaned)
            save_json(
                self.directory,
                metadata_name,
                {
                    "source_id": sid,
                    "requested_url": url,
                    "final_url": capture.final_url or url,
                    "title": capture.title,
                    "reference": reference,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "cleaned_sha256": hashlib.sha256(cleaned.encode()).hexdigest(),
                    "cleaner_version": 1,
                    "cleaning": report,
                    "fallback_reasons": reasons,
                    "raw_hashes": {
                        kind: hashlib.sha256(raw.encode()).hexdigest()
                        for kind, raw in (
                            ("html", capture.html),
                            ("raw_html", capture.raw_html),
                            ("markdown", capture.markdown),
                        )
                    },
                },
            )
        content = local_path(self.directory, reference).read_text(encoding="utf-8")
        if len(content.encode()) + 2048 > self.settings.input_allowance:
            raise ValueError(
                "Complete page exceeds configured input allowance; saved without truncation. Increase --input-allowance on resume only if the configured model supports it."
            )
        return {"source": reference, "url": url, "content": content}

    def update_profile(self, content: str) -> dict[str, JsonValue]:
        if not self.pages_read():
            raise ValueError("Read both starting pages before writing the profile")
        if not content.strip():
            raise ValueError("Profile must contain nonempty Markdown")
        for url in self.inputs.urls:
            if f"sources/{source_id(url)}.md" not in content:
                raise ValueError(
                    "Profile must reference every starting source using its saved path"
                )
        write_text(self.directory, "profile.md", content)
        return {"artifact": "profile.md", "saved": True}

    def build_site_index(self, site_url: str) -> dict[str, JsonValue]:
        if public_url(site_url) != self.inputs.site_url:
            raise ValueError("Map the company site's root URL")
        if (
            not self.pages_read()
            or not local_path(self.directory, "profile.md").exists()
        ):
            raise ValueError("Read starting pages and save the profile before mapping")
        if local_path(self.directory, "site_index.json").exists():
            saved = read_json(self.directory, "site_index.json")
            saved_summary = saved["summary"]
            if not isinstance(saved_summary, dict):
                raise ValueError("Invalid saved index")
            return saved_summary
        candidates = self.pages.map(site_url)
        entries: dict[str, Entry] = {}
        rejected = 0
        for candidate in candidates:
            try:
                url = public_url(candidate.url)
            except ValueError:
                rejected += 1
                continue
            entry = entries.setdefault(url, Entry(url=url))
            if candidate.title:
                entry.title = candidate.title
            if candidate.description:
                entry.description = candidate.description
        summary: dict[str, JsonValue] = {
            "artifact": "site_index.json",
            "discovered_count": len(entries),
            "returned_count": len(candidates),
            "rejected_count": rejected,
            "requested_limit": self.settings.map_limit,
            "possibly_truncated": len(candidates) >= self.settings.map_limit,
            "limitations": "Sitemap and subdomain mapping; coverage unknown, not exhaustive. Discovery leads only, not scraped evidence.",
        }
        save_json(
            self.directory,
            "site_index.json",
            {
                "site_url": site_url,
                "summary": summary,
                "entries": [entries[url].model_dump() for url in sorted(entries)],
            },
        )
        return summary

    def execute(self, name: str, arguments: str) -> dict[str, JsonValue]:
        logger.info("run=%s action=%s", self.directory.name, name)
        if name not in TOOL_MODELS:
            raise ValueError("Unknown orientation tool")
        args = TOOL_MODELS[name].model_validate_json(arguments)
        if isinstance(args, ReadArgs):
            return self.read_page(args.url)
        if isinstance(args, ProfileArgs):
            return self.update_profile(args.content)
        if isinstance(args, IndexArgs):
            return self.build_site_index(args.site_url)
        raise ValueError("Unsupported tool arguments")
