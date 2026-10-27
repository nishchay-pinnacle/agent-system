"""Stage-scoped research tools and the Firecrawl boundary."""

import hashlib
import re
import shlex
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from firecrawl import FirecrawlError
from firecrawl.v2.client import FirecrawlClient
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from requests.exceptions import RequestException

from research_agent.cleaning import clean_markdown, normalize_input, usable
from research_agent.html_cleaning import ConversionError, clean_html
from research_agent.storage import (
    Inputs,
    Run,
    Settings,
    local_path,
    public_url,
    read_json,
    record_firecrawl_call,
    save_json,
    save_run,
    source_id,
    write_text,
)


class ToolInputError(ValueError):
    """An argument or prerequisite the model can correct on its next turn."""


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


class QueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str
    path_filter: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1, le=50)


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=2000)


def tool_models(stage: str) -> dict[str, type[BaseModel]]:
    common: dict[str, type[BaseModel]] = {
        "read_page": ReadArgs,
        "update_profile": ProfileArgs,
    }
    if stage == "orientation":
        return {**common, "build_site_index": IndexArgs}
    if stage == "research":
        return {**common, "query_site_index": QueryArgs, "finish_research": FinishArgs}
    raise ValueError("Unsupported research stage")


def markdown_links(content: str) -> list[str]:
    links: list[str] = []
    for block in MarkdownIt("commonmark").parse(content):
        for token in [block, *(block.children or [])]:
            if token.type == "link_open":
                href = token.attrGet("href")
                if isinstance(href, str):
                    links.append(href)
            elif token.type in {"html_inline", "html_block"}:
                for link in BeautifulSoup(token.content, "html.parser").find_all(
                    "a", href=True
                ):
                    html_href = link.get("href")
                    if isinstance(html_href, str):
                        links.append(html_href)
    return links


def unsupported_page(url: str) -> bool:
    return (
        urlsplit(url)
        .path.lower()
        .endswith(
            (
                ".pdf",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".svg",
                ".webp",
                ".mp3",
                ".mp4",
                ".wav",
                ".zip",
                ".doc",
                ".docx",
                ".pptx",
                ".xlsx",
            )
        )
    )


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
        self,
        directory: Path,
        inputs: Inputs,
        settings: Settings,
        pages: Pages,
        run: Run | None = None,
    ) -> None:
        self.run = run
        self.directory, self.inputs, self.settings, self.pages = (
            directory,
            inputs,
            settings,
            pages,
        )

    @property
    def stage(self) -> str:
        return self.run.stage if self.run is not None else "orientation"

    def validate_source(self, url: str) -> bool:
        sid = source_id(url)
        metadata_name = f"sources/{sid}.json"
        if not local_path(self.directory, metadata_name).exists():
            return False
        metadata = read_json(self.directory, metadata_name)
        reference = f"sources/{sid}.md"
        evidence = local_path(self.directory, reference)
        if (
            metadata.get("reference") != reference
            or metadata.get("requested_url") != url
            or not evidence.is_file()
            or hashlib.sha256(evidence.read_bytes()).hexdigest()
            != metadata.get("cleaned_sha256")
        ):
            raise ValueError(
                f"Saved source {sid} is missing or has an invalid hash; restore run files"
            )
        capture = Capture.model_validate(
            read_json(self.directory, f"sources/{sid}.capture.json")
        )
        hashes = {
            kind: hashlib.sha256(getattr(capture, kind).encode()).hexdigest()
            for kind in ("html", "raw_html", "markdown")
        }
        if hashes != metadata.get("raw_hashes"):
            raise ValueError(
                f"Saved capture {sid} has invalid hashes; restore run files"
            )
        return True

    def saved_sources(self) -> list[str]:
        references: list[str] = []
        for path in sorted(local_path(self.directory, "sources").glob("s_*.json")):
            if not re.fullmatch(r"s_[0-9a-f]{16}\.json", path.name):
                continue
            metadata = read_json(self.directory, f"sources/{path.name}")
            url = metadata.get("requested_url")
            if (
                not isinstance(url, str)
                or source_id(url) != path.stem
                or not self.validate_source(url)
            ):
                raise ValueError("Invalid saved source metadata; restore run files")
            references.append(f"sources/{path.stem}.md")
        return references

    def pages_read(self) -> bool:
        # Validate every present source, even when another starting page is missing.
        return all([self.validate_source(url) for url in self.inputs.urls])

    def company_url(self, url: str) -> bool:
        root = (urlsplit(self.inputs.company_url).hostname or "").removeprefix("www.")
        host = urlsplit(url).hostname or ""
        return host == root or host.endswith("." + root)

    def authorize_url(self, url: str) -> None:
        public_url(url)
        if url in self.inputs.urls:
            return
        if self.stage == "orientation":
            raise ToolInputError(
                "Orientation may read only the supplied starting pages"
            )
        company = self.company_url(url)
        if company:
            self.saved_index()
            entries = read_json(self.directory, "site_index.json")["entries"]
            if isinstance(entries, list) and any(
                isinstance(entry, dict) and public_url(str(entry["url"])) == url
                for entry in entries
            ):
                return
        for reference in self.saved_sources():
            metadata = read_json(
                self.directory, reference.removesuffix(".md") + ".json"
            )
            origin = str(metadata["requested_url"])
            # External resources must be linked from company evidence, not another external page.
            if not company and not (
                self.company_url(origin) or origin in self.inputs.urls
            ):
                continue
            base = str(metadata.get("final_url") or origin)
            links = markdown_links(local_path(self.directory, reference).read_text())
            cleaning = metadata.get("cleaning")
            details = cleaning.get("details") if isinstance(cleaning, dict) else None
            if isinstance(details, dict):
                discovery = details.get("discovery")
                if isinstance(discovery, list):
                    links.extend(
                        str(link["url"])
                        for link in discovery
                        if isinstance(link, dict) and isinstance(link.get("url"), str)
                    )
            for link in links:
                try:
                    target = public_url(urljoin(base, link))
                except ValueError:
                    continue
                if target == url:
                    return
        raise ToolInputError(
            "URL is outside the saved index/company-evidence link scope; choose an indexed company page or an explicitly linked resource"
        )

    def saved_index(self) -> dict[str, JsonValue]:
        saved = read_json(self.directory, "site_index.json")
        entries, summary = saved.get("entries"), saved.get("summary")
        if (
            saved.get("site_url") != self.inputs.site_url
            or not isinstance(entries, list)
            or not isinstance(summary, dict)
            or summary.get("discovered_count") != len(entries)
        ):
            raise ValueError("Invalid saved index; restore run files")
        for entry in entries:
            public_url(Entry.model_validate(entry).url)
        return summary

    def validate_artifacts(self) -> bool:
        ready = self.pages_read()
        self.saved_sources()
        if local_path(self.directory, "site_index.json").exists():
            self.saved_index()
        return ready

    def complete(self) -> bool:
        ready = self.validate_artifacts()
        profile = local_path(self.directory, "profile.md")
        index = local_path(self.directory, "site_index.json")
        if self.stage == "research":
            completion = local_path(self.directory, "research/completion.json")
            if not completion.exists():
                return False
            reason = FinishArgs.model_validate(
                read_json(self.directory, "research/completion.json")
            )
            if not reason.reason.strip():
                raise ValueError("Invalid saved research completion reason")
        return (
            ready
            and profile.exists()
            and bool(profile.read_text().strip())
            and index.exists()
        )

    def read_page(self, url: str) -> dict[str, JsonValue]:
        url = public_url(url)
        self.authorize_url(url)
        if self.stage == "research" and unsupported_page(url):
            return {
                "url": url,
                "error": "Unsupported format; research reads HTML pages only",
                "recoverable": True,
            }
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
                if self.run is not None and self.run.stage == "research":
                    if self.run.research_page_attempts >= self.run.research_page_budget:
                        raise RuntimeError(
                            "Research page budget exhausted; research incomplete. Resume with an explicit higher --page-budget"
                        )
                    self.run.research_page_attempts += 1
                    save_run(self.directory, self.run)
                record_firecrawl_call(self.directory, "scrape")
                capture = self.pages.scrape(url)
                # Original representations persist before cleaning or validation.
                save_json(self.directory, capture_name, capture.model_dump(mode="json"))
            problem = evidence_problem(capture)
            if self.stage == "research":
                final = public_url(capture.final_url or url)
                try:
                    self.authorize_url(final)
                except ToolInputError as exc:
                    problem = str(exc)
                if unsupported_page(final) or (
                    capture.content_type
                    and capture.content_type.split(";")[0].strip().lower()
                    not in {"text/html", "application/xhtml+xml"}
                ):
                    problem = "Unsupported format; research reads HTML pages only"
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
                if self.stage == "research":
                    return {
                        "url": url,
                        "error": problem,
                        "recoverable": True,
                        "capture": attempt,
                    }
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
        self.validate_source(url)
        content = local_path(self.directory, reference).read_text(encoding="utf-8")
        if len(content.encode()) + 2048 > self.settings.input_allowance:
            raise ValueError(
                "Complete page exceeds configured input allowance; saved without truncation. Increase --input-allowance on resume only if the configured model supports it."
            )
        return {"source": reference, "url": url, "content": content}

    def validate_citations(self, content: str) -> None:
        for link in markdown_links(content):
            parts = urlsplit(link)
            if parts.scheme or parts.netloc:
                continue
            path = unquote(parts.path).removeprefix("./")
            if "sources" not in path.split("/"):
                continue
            if not re.fullmatch(r"sources/s_[0-9a-f]{16}\.md", path):
                raise ToolInputError(
                    "Invalid local source citation; use the exact saved source reference"
                )
            metadata_path = path.removesuffix(".md") + ".json"
            if not local_path(self.directory, metadata_path).exists():
                raise ToolInputError(f"Citation {path} is not saved evidence")
            metadata = read_json(self.directory, metadata_path)
            url = metadata.get("requested_url")
            if (
                not isinstance(url, str)
                or f"sources/{source_id(url)}.md" != path
                or not self.validate_source(url)
            ):
                raise ValueError("Invalid citation source metadata; restore run files")

    def update_profile(self, content: str) -> dict[str, JsonValue]:
        if not content.strip():
            raise ToolInputError("Profile must contain nonempty Markdown")
        if self.stage == "orientation":
            if not self.pages_read():
                raise ToolInputError(
                    "Read both starting pages before writing the profile"
                )
            for url in self.inputs.urls:
                if f"sources/{source_id(url)}.md" not in content:
                    raise ToolInputError(
                        "Profile must reference every starting source using its saved path"
                    )
        self.validate_citations(content)
        write_text(self.directory, "profile.md", content)
        return {"artifact": "profile.md", "saved": True}

    def query_site_index(
        self,
        query: str,
        path_filter: str = "",
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, JsonValue]:
        args = QueryArgs(
            query=query, path_filter=path_filter, offset=offset, limit=limit
        )
        limit = args.limit or self.settings.index_result_limit
        try:
            terms = [term.casefold() for term in shlex.split(query)]
        except ValueError as exc:
            raise ToolInputError("Unclosed quote in index query") from exc
        self.saved_index()
        raw = read_json(self.directory, "site_index.json")["entries"]
        if not isinstance(raw, list):
            raise ValueError("Invalid saved index entries")
        matches: dict[str, Entry] = {}
        for item in raw:
            entry = Entry.model_validate(item)
            entry.url = public_url(entry.url)
            haystack = f"{entry.url} {entry.title} {entry.description}".casefold()
            if (
                all(term in haystack for term in terms)
                and path_filter.casefold()
                in unquote(urlsplit(entry.url).path).casefold()
            ):
                matches[entry.url] = entry
        urls = sorted(matches)
        results: list[JsonValue] = []
        for url in urls[offset : offset + limit]:
            entry = matches[url]
            results.append(
                {
                    "url": url,
                    "title": entry.title,
                    "description": entry.description,
                    "already_saved": self.validate_source(url),
                }
            )
        next_offset = offset + len(results)
        return {
            "matches": results,
            "total_matches": len(urls),
            "returned_matches": len(results),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < len(urls) else None,
            "has_more": next_offset < len(urls),
            "evidence": "Discovery leads only; read selected HTML pages before citing",
        }

    def finish_research(self, reason: str) -> dict[str, JsonValue]:
        if self.stage != "research":
            raise ToolInputError("Research is not active")
        if not reason.strip():
            raise ToolInputError("Provide a short reason for finishing research")
        self.validate_artifacts()
        profile = local_path(self.directory, "profile.md").read_text()
        if not profile.strip():
            raise ToolInputError("Save a nonempty profile before finishing")
        self.validate_citations(profile)
        completion = local_path(self.directory, "research/completion.json")
        if not completion.exists():
            save_json(
                self.directory,
                "research/completion.json",
                FinishArgs(reason=reason).model_dump(),
            )
        return {"artifact": "research/completion.json", "finished": True}

    def build_site_index(self, site_url: str) -> dict[str, JsonValue]:
        if public_url(site_url) != self.inputs.site_url:
            raise ToolInputError("Map the company site's root URL")
        if (
            not self.pages_read()
            or not local_path(self.directory, "profile.md").exists()
        ):
            raise ToolInputError(
                "Read starting pages and save the profile before mapping"
            )
        if local_path(self.directory, "site_index.json").exists():
            return self.saved_index()
        record_firecrawl_call(self.directory, "map")
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
        models = tool_models(self.stage)
        if name not in models:
            raise ToolInputError(f"Tool is unavailable during {self.stage}")
        try:
            args = models[name].model_validate_json(arguments)
            if isinstance(args, ReadArgs):
                args.url = public_url(args.url)
            elif isinstance(args, IndexArgs):
                args.site_url = public_url(args.site_url)
        except ValidationError as exc:
            errors = "; ".join(
                error["msg"] for error in exc.errors(include_input=False)
            )
            raise ToolInputError(f"Invalid {name} arguments: {errors}") from exc
        except ValueError as exc:
            raise ToolInputError(str(exc)) from exc
        if isinstance(args, ReadArgs):
            return self.read_page(args.url)
        if isinstance(args, ProfileArgs):
            return self.update_profile(args.content)
        if isinstance(args, IndexArgs):
            return self.build_site_index(args.site_url)
        if isinstance(args, QueryArgs):
            return self.query_site_index(
                args.query, args.path_filter, args.offset, args.limit
            )
        if isinstance(args, FinishArgs):
            return self.finish_research(args.reason)
        raise ToolInputError("Unsupported tool arguments")
