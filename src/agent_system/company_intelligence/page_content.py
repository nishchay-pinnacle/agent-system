"""Shared, version-aware source cleaning and immutable capture loading."""

import hashlib
import re
from pathlib import Path

from markdown_it import MarkdownIt
from pydantic import JsonValue

from agent_system.company_intelligence.artifacts import (
    read_json,
    save_json,
    source_path,
)
from agent_system.company_intelligence.html_cleaning import (
    ConversionError,
    HTMLResult,
    clean_html,
)
from agent_system.company_intelligence.schemas import Source

CLEANER_VERSION = "4"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_markdown_v1(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    output: list[str] = []
    prose: list[str] = []
    fence: str | None = None

    def flush() -> None:
        block = "".join(prose)
        block = re.sub(r"<!--.*?-->", "", block, flags=re.S)
        block = re.sub(
            r"<(script|style)\b[^>]*>.*?</\1\s*>", "", block, flags=re.S | re.I
        )
        block = re.sub(r"\n(?:[ \t]*\n){3,}", "\n\n\n", block)
        output.append(block)
        prose.clear()

    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\n"))
        if fence:
            output.append(line)
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
        elif marker:
            flush()
            fence = marker[1]
            output.append(line)
        else:
            prose.append(line)
    flush()
    return "".join(output)


def normalize_input(raw: str) -> str:
    return (
        raw.removeprefix("\ufeff")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x00", "\ufffd")
    )


def clean_markdown(raw: str, removals: list[dict[str, JsonValue]] | None = None) -> str:
    """Normalize prose in-place using parser block boundaries, never an HTML roundtrip.

    Code, tables, semantic HTML, link destinations and inline code stay verbatim.
    Indentation, hard breaks and Unicode formatting characters are not prose clutter.
    """
    text = normalize_input(raw)
    lines = text.splitlines(keepends=True)
    protected: set[int] = set()
    removed: set[int] = set()
    parser = MarkdownIt("commonmark").enable("table")
    for token in parser.parse(text):
        if token.map is None:
            continue
        start, end = token.map
        if token.type in {"fence", "code_block", "table_open", "html_block"}:
            protected.update(range(start, end))
        if token.type == "html_block" and re.fullmatch(
            r"\s*(?:<!--.*?-->|<(script|style)\b[^>]*>.*?</\1\s*>)\s*",
            "".join(lines[start:end]),
            re.S | re.I,
        ):
            removed.update(range(start, end))
            if removals is not None:
                removals.append(
                    {
                        "rule": "markdown_executable_or_comment",
                        "start_line": start + 1,
                        "end_line": end,
                    }
                )
    output: list[str] = []
    blanks = 0
    # Only outside protected blocks. Keep destinations/entities exactly as captured.
    inline = re.compile(r"(`+).*?\1|!?\[[^\n]*?\]\([^\n]*?\)|<[^>]*>|\\.")
    for number, line in enumerate(lines):
        if number in removed:
            continue
        if number in protected:
            output.append(line)
            blanks = 0
            continue
        if not line.strip():
            blanks += 1
            if blanks <= 2:
                output.append("\n")
            continue
        blanks = 0
        indent = re.match(r"[ \t]*", line)
        assert indent is not None
        prefix = indent[0]
        body = line[len(prefix) :]
        hard_break = body.endswith("  \n")
        suffix = "  \n" if hard_break else ("\n" if body.endswith("\n") else "")
        body = body[: -len(suffix)] if suffix else body
        normalized: list[str] = []
        offset = 0
        for match in inline.finditer(body):
            normalized.append(
                re.sub(r"[ \t\u00a0\u202f]+", " ", body[offset : match.start()])
            )
            if not re.fullmatch(r"<!--.*?-->", match[0], re.S):
                normalized.append(match[0])
            elif removals is not None:
                removals.append(
                    {
                        "rule": "markdown_comment",
                        "start_line": number + 1,
                        "end_line": number + 1,
                    }
                )
            offset = match.end()
        normalized.append(re.sub(r"[ \t\u00a0\u202f]+", " ", body[offset:]))
        output.append(prefix + "".join(normalized) + suffix)
    return "".join(output)


def artifact_path(
    directory: Path, source: Source, filename: str, suffixes: set[str]
) -> Path:
    """Narrow source-local filenames only, with no symlink or directory escape."""
    source_path(directory, source)
    if filename not in {f"sources/{source.source_id}{suffix}" for suffix in suffixes}:
        raise ValueError("Invalid source artifact reference")
    path = directory / filename
    if (
        path.is_symlink()
        or (directory / "sources").is_symlink()
        or not path.resolve().is_relative_to(directory.resolve())
    ):
        raise ValueError("Invalid source artifact path")
    return path


def cleaned_path(directory: Path, source: Source) -> Path:
    suffix = (
        f".cleaned.v{source.cleaner_version}.md"
        if source.cleaner_version in {"2", "3", "4"}
        else ".cleaned.md"
    )
    filename = source.cleaned_filename or f"sources/{source.source_id}{suffix}"
    return artifact_path(
        directory,
        source,
        filename,
        {".cleaned.md", ".cleaned.v2.md", ".cleaned.v3.md", ".cleaned.v4.md"},
    )


def usable(content: str) -> bool:
    """Reject explicit empty/loading/error captures, not short substantive pages."""
    words = re.sub(r"[#*_`\s]+", " ", content).strip().casefold()
    return bool(words) and words.rstrip(". !…") not in {
        "loading",
        "loading please wait",
        "please wait",
        "access denied",
        "forbidden",
        "403 forbidden",
        "404 not found",
        "not found",
        "internal server error",
        "enable javascript",
        "please enable javascript",
        "javascript is required",
        "you need to enable javascript to run this app",
    }


def write_capture(path: Path, content: str) -> None:
    """Never replace an existing capture or delivered representation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content.encode("utf-8"))
        temporary.replace(path)


def load_cleaned(directory: Path, source: Source) -> str:
    """Honor saved versions/bytes; missing old files use exactly the v1 algorithm."""
    path = cleaned_path(directory, source)
    if path.exists():
        content = path.read_bytes().decode("utf-8")
        source.cleaned_hash = digest(content)
        if source.cleaner_version is None:
            source.cleaner_version = (
                "4"
                if path.name.endswith(".v4.md")
                else "3"
                if path.name.endswith(".v3.md")
                else "2"
                if path.name.endswith(".v2.md")
                else "1"
            )
        source.cleaned_filename = f"sources/{path.name}"
        if source.cleaner_version in {"2", "3", "4"}:
            report_name = (
                f"sources/{source.source_id}.cleaning.v{source.cleaner_version}.json"
            )
            report_path = artifact_path(
                directory,
                source,
                report_name,
                {".cleaning.v2.json", ".cleaning.v3.json", ".cleaning.v4.json"},
            )
            if report_path.exists():
                report = read_json(report_path)
                source.cleaning_report_filename = report_name
                source.cleaning_summary = report_summary(report, report_name)
        raw = source_path(directory, source).read_bytes().decode("utf-8")
        source.raw_hash = digest(raw)
        source.cleaning_stats = cleaning_stats(raw, content)
        return content
    raw = source_path(directory, source).read_bytes().decode("utf-8")
    if source.cleaner_version in {None, "1"}:
        cleaned = clean_markdown_v1(raw)
        source.cleaner_version = "1"
    elif source.cleaner_version in {"2", "3"}:
        raise ValueError(
            f"Missing historical v{source.cleaner_version} cleaned file; restore it from backup rather than recleaning with v4"
        )
    elif source.cleaner_version == CLEANER_VERSION:
        cleaned = clean_new(directory, source, raw)
    else:
        raise ValueError("Cannot reconstruct unknown saved cleaner version")
    write_capture(path, cleaned)
    source.cleaned_filename = f"sources/{path.name}"
    source.raw_hash = digest(raw)
    source.cleaned_hash = digest(cleaned)
    source.cleaning_stats = cleaning_stats(raw, cleaned)
    return cleaned


def cleaning_stats(raw: str, cleaned: str) -> dict[str, int]:
    return {
        "raw_bytes": len(raw.encode()),
        "cleaned_bytes": len(cleaned.encode()),
        "raw_lines": len(raw.splitlines()),
        "cleaned_lines": len(cleaned.splitlines()),
        "removed_bytes": len(raw.encode()) - len(cleaned.encode()),
    }


def report_summary(report: dict[str, JsonValue], filename: str) -> dict[str, JsonValue]:
    warnings = report.get("warnings", [])
    links = report.get("discovery_links", [])
    return {
        "selected_representation": report.get("selected_representation"),
        "fallback_reasons": report.get("fallback_reasons", []),
        "warning_count": len(warnings) if isinstance(warnings, list) else 0,
        "warnings": warnings[:5] if isinstance(warnings, list) else [],
        "report": filename,
        "discovery_links": len(links) if isinstance(links, list) else 0,
    }


def clean_new(directory: Path, source: Source, markdown: str) -> str:
    reasons: list[str] = []
    hashes: dict[str, JsonValue] = {"markdown": digest(markdown)}
    counts: dict[str, JsonValue] = {
        "markdown": {
            "bytes": len(markdown.encode()),
            "lines": len(markdown.splitlines()),
        }
    }
    result = HTMLResult()
    selected = "markdown"
    selected_raw = markdown
    cleaned = ""
    for representation, filename, suffix in (
        ("html", source.html_filename, ".html"),
        ("raw_html", source.raw_html_filename, ".raw.html"),
    ):
        if not filename:
            reasons.append(f"{representation}: not captured")
            continue
        raw = (
            artifact_path(directory, source, filename, {suffix})
            .read_bytes()
            .decode("utf-8")
        )
        hashes[representation] = digest(raw)
        counts[representation] = {
            "bytes": len(raw.encode()),
            "lines": len(raw.splitlines()),
        }
        if cleaned:
            continue  # Hash all captures, but select only one canonical representation.
        try:
            candidate = clean_html(
                normalize_input(raw),
                source.final_url or source.requested_url,
                source.source_id,
            )
        except ConversionError as exc:
            reasons.append(f"{representation}: {exc}")
            continue
        normalized = clean_markdown(candidate.content)
        if not usable(normalized):
            reasons.append(f"{representation}: empty/loading/error capture")
            continue
        result = candidate
        cleaned = normalized
        selected, selected_raw = representation, raw
    if not cleaned:
        cleaned = clean_markdown(markdown, result.removals)
        result.warnings.append(
            "Markdown fallback lacks DOM structure; ambiguous interface content is retained"
        )
    nul_count = selected_raw.count("\x00")
    if nul_count:
        result.warnings.append(f"Replaced {nul_count} NUL characters with U+FFFD")
    if not usable(cleaned):
        result.warnings.append(
            "No usable page content; an incomplete capture does not prove absence"
        )
    rule_counts: dict[str, JsonValue] = {}
    for removal in result.removals:
        rule = str(removal["rule"])
        count = rule_counts.get(rule, 0)
        rule_counts[rule] = int(str(count)) + 1
    report_name = f"sources/{source.source_id}.cleaning.v{CLEANER_VERSION}.json"
    report: dict[str, JsonValue] = {
        "cleaner_version": CLEANER_VERSION,
        "input_representation_version": 1,
        "selected_representation": selected,
        "fallback_reasons": list(reasons),
        "raw_hashes": hashes,
        "input_counts": counts,
        "cleaned_hash": digest(cleaned),
        "cleaned_bytes": len(cleaned.encode()),
        "cleaned_lines": len(cleaned.splitlines()),
        "nul_replacements": nul_count,
        "rule_counts": rule_counts,
        "removals": list(result.removals),
        "warnings": list(result.warnings),
        "discovery_links": [dict(link) for link in result.discovery],
        "assets": list(result.assets),
        "json_ld_metadata": result.json_ld,
        "breadcrumbs": list(result.breadcrumbs),
        "link_provenance": [dict(link) for link in result.links],
    }
    report_path = artifact_path(
        directory,
        source,
        report_name,
        {".cleaning.v2.json", ".cleaning.v3.json", ".cleaning.v4.json"},
    )
    if not report_path.exists():
        save_json(report_path, report)
    source.cleaning_report_filename = report_name
    source.cleaning_summary = report_summary(report, report_name)
    if not usable(cleaned):
        raise ValueError(
            "Source capture is empty/loading/error content; this does not prove absence"
        )
    return cleaned


def discovery_links(directory: Path, source: Source) -> list[dict[str, str]]:
    if not source.cleaning_report_filename:
        return []
    report = read_json(
        artifact_path(
            directory,
            source,
            source.cleaning_report_filename,
            {".cleaning.v2.json", ".cleaning.v3.json", ".cleaning.v4.json"},
        )
    )
    links = report.get("discovery_links", [])
    return (
        [
            {k: v for k, v in link.items() if isinstance(v, str)}
            for link in links
            if isinstance(link, dict)
        ]
        if isinstance(links, list)
        else []
    )
