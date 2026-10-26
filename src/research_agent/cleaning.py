"""Evidence-preserving Markdown normalization, extracted from the original cleaner."""

import re

from markdown_it import MarkdownIt
from pydantic import JsonValue


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
