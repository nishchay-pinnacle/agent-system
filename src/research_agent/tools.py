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
    QuestionIndex,
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
    url_contains: str = ""
    required_phrase: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1, le=50)


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=2000)
    profile: str = Field(min_length=1)
    question_index: QuestionIndex


def tool_models(stage: str) -> dict[str, type[BaseModel]]:
    common: dict[str, type[BaseModel]] = {
        "read_page": ReadArgs,
        "update_profile": ProfileArgs,
        "update_questions": QuestionIndex,
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
        if title and re.fullmatch(pattern, title.get_text(" ", strip=True), re.I):
            text = main.get_text(" ", strip=True)
            if len(text) < 2000 and re.search(pattern, text, re.I):
                return "Structural error/access-blocked capture"
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
            reason = read_json(self.directory, "research/completion.json").get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Invalid saved research completion reason")
        if profile.exists():
            self.validate_profile(profile.read_text())
        questions = local_path(self.directory, "questions.json")
        if questions.exists():
            self.validate_questions(
                QuestionIndex.model_validate(
                    read_json(self.directory, "questions.json")
                ),
                finishing=self.stage == "research",
            )
        return (
            questions.exists()
            and ready
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
        sources = self.source_index()
        short = next((key for key, value in sources.items() if value == url), None)
        if short is None:
            short = f"S{len(sources) + 1}"
            sources[short] = url
            save_json(self.directory, "source_index.json", sources)
        return {"source_id": short, "source": reference, "url": url, "content": content}

    def source_index(self) -> dict[str, str]:
        path = local_path(self.directory, "source_index.json")
        if not path.exists():
            return {}
        saved = read_json(self.directory, "source_index.json")
        sources: dict[str, str] = {}
        for key, value in saved.items():
            if (
                not re.fullmatch(r"S[1-9][0-9]*", key)
                or not isinstance(value, str)
                or not self.validate_source(value)
            ):
                raise ValueError("Invalid source index; restore run files")
            sources[key] = value
        if set(sources) != {f"S{i + 1}" for i in range(len(sources))} or len(
            set(sources.values())
        ) != len(sources):
            raise ValueError("Invalid source index IDs; restore run files")
        return sources

    def validate_questions(
        self, index: QuestionIndex, *, finishing: bool = False
    ) -> QuestionIndex:
        index = index.model_copy(deep=True)
        ids = [q.id for q in index.questions]
        if len(ids) != len(set(ids)):
            raise ToolInputError("Question IDs must be unique")
        if index.active_question_id is not None and not any(
            q.id == index.active_question_id and q.status == "open"
            for q in index.questions
        ):
            raise ToolInputError("Active question must exist and be open")
        sources = self.source_index()
        previous = {}
        if local_path(self.directory, "questions.json").exists():
            previous = {
                q.id: q
                for q in QuestionIndex.model_validate(
                    read_json(self.directory, "questions.json")
                ).questions
            }
        missing = set(previous) - set(ids)
        if missing:
            raise ToolInputError(
                "Existing questions cannot be removed: "
                + ", ".join(sorted(missing))
                + ". Retain each question; for a merge use status merged, merged_into, and closure_reason."
            )
        by_id = {q.id: q for q in index.questions}
        for question in index.questions:
            if question.status == "merged":
                target = by_id.get(question.merged_into or "")
                if (
                    target is None
                    or target.id == question.id
                    or target.status == "merged"
                    or not question.closure_reason
                ):
                    raise ToolInputError(
                        "Merged questions require a distinct, existing non-merged target and closure_reason"
                    )
            elif question.merged_into is not None:
                raise ToolInputError("Only merged questions may have merged_into")
            assessments = (
                {a.source_id: a for a in previous[question.id].source_assessments}
                if question.id in previous
                else {}
            )
            submitted: dict[str, bool] = {}
            for assessment in question.source_assessments:
                if assessment.source_id not in sources:
                    raise ToolInputError(
                        f"Source {assessment.source_id} is not saved evidence"
                    )
                if (
                    assessment.source_id in submitted
                    and submitted[assessment.source_id] != assessment.useful
                ):
                    raise ToolInputError("Conflicting duplicate source assessments")
                submitted[assessment.source_id] = assessment.useful
                old = assessments.get(assessment.source_id)
                if old and old.useful and not assessment.useful:
                    raise ToolInputError(
                        "Useful evidence cannot be changed into an unproductive reread"
                    )
                if old and not old.useful and assessment.useful:
                    del assessments[assessment.source_id]
                assessments[assessment.source_id] = assessment
            question.source_assessments = list(assessments.values())
            self.validate_citations(question.answer or "")
            if question.status == "answered" and (
                not question.answer or not question.answer_basis
            ):
                raise ToolInputError(
                    "Answered questions require an answer and answer_basis"
                )
            if question.status == "needs_interview" and not question.closure_reason:
                raise ToolInputError(
                    "Deferred questions require a closure reason, including exhausted public leads when fewer than three unproductive pages"
                )
            if finishing and question.status == "open":
                raise ToolInputError("Cannot finish with open questions")
        return index

    def update_questions(self, index: QuestionIndex) -> dict[str, JsonValue]:
        saved = self.validate_questions(index)
        save_json(self.directory, "questions.json", saved.model_dump(mode="json"))
        return {
            "index": saved.model_dump(mode="json"),
            "progress": {q.id: q.unproductive_pages for q in saved.questions},
        }

    def validate_citations(self, content: str) -> None:
        sources = self.source_index()
        for sid in re.findall(r"\[(S[0-9]+)\]", content):
            if sid not in sources:
                raise ToolInputError(f"Citation {sid} is not saved evidence")
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

    def validate_profile(self, content: str) -> None:
        if not content.strip():
            raise ToolInputError("Profile must contain nonempty Markdown")
        if self.stage == "orientation":
            if not self.pages_read():
                raise ToolInputError(
                    "Read both starting pages before writing the profile"
                )
            for url in self.inputs.urls:
                if not any(
                    value == url and f"[{key}]" in content
                    for key, value in self.source_index().items()
                ):
                    raise ToolInputError(
                        "Profile must reference every starting source using its short source ID"
                    )
        self.validate_citations(content)

    def update_profile(self, content: str) -> dict[str, JsonValue]:
        self.validate_profile(content)
        write_text(self.directory, "profile.md", content)
        return {"artifact": "profile.md", "saved": True}

    def query_site_index(
        self,
        query: str,
        url_contains: str = "",
        offset: int = 0,
        limit: int | None = None,
        required_phrase: str = "",
    ) -> dict[str, JsonValue]:
        args = QueryArgs(
            query=query,
            url_contains=url_contains,
            required_phrase=required_phrase,
            offset=offset,
            limit=limit,
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
                (not terms or any(term in haystack for term in terms))
                and url_contains.casefold() in unquote(entry.url).casefold()
                and required_phrase.casefold() in haystack
            ):
                matches[entry.url] = entry
        urls = sorted(
            matches,
            key=lambda url: (
                -sum(
                    term
                    in f"{url} {matches[url].title} {matches[url].description}".casefold()
                    for term in set(terms)
                ),
                url,
            ),
        )
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

    def finish_research(
        self, reason: str, profile: str, question_index: QuestionIndex
    ) -> dict[str, JsonValue]:
        if self.stage != "research":
            raise ToolInputError("Research is not active")
        if not reason.strip() or not profile.strip():
            raise ToolInputError("Provide a nonempty profile and completion reason")
        self.validate_artifacts()
        self.validate_citations(profile)
        index = self.validate_questions(question_index, finishing=True)
        write_text(self.directory, "profile.md", profile)
        save_json(self.directory, "questions.json", index.model_dump(mode="json"))
        save_json(self.directory, "research/completion.json", {"reason": reason})
        return {"artifact": "research/completion.json", "finished": True}

    def build_site_index(self, site_url: str) -> dict[str, JsonValue]:
        if public_url(site_url) != self.inputs.site_url:
            raise ToolInputError("Map the company site's root URL")
        if (
            not self.pages_read()
            or not local_path(self.directory, "profile.md").exists()
            or not local_path(self.directory, "questions.json").exists()
        ):
            raise ToolInputError(
                "Read starting pages and save the profile and questions before mapping"
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
        if isinstance(args, QuestionIndex):
            return self.update_questions(args)
        if isinstance(args, ReadArgs):
            return self.read_page(args.url)
        if isinstance(args, ProfileArgs):
            return self.update_profile(args.content)
        if isinstance(args, IndexArgs):
            return self.build_site_index(args.site_url)
        if isinstance(args, QueryArgs):
            return self.query_site_index(
                args.query,
                args.url_contains,
                args.offset,
                args.limit,
                args.required_phrase,
            )
        if isinstance(args, FinishArgs):
            return self.finish_research(args.reason, args.profile, args.question_index)
        raise ToolInputError("Unsupported tool arguments")
