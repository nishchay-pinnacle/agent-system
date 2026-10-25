"""Conservative deterministic Markdown cleaning and shared artifact loading."""

import hashlib
import re
from pathlib import Path

from agent_system.company_intelligence.artifacts import source_path
from agent_system.company_intelligence.schemas import Source

CLEANER_VERSION = "1"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_markdown(raw: str) -> str:
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


def cleaned_path(directory: Path, source: Source) -> Path:
    raw = source_path(directory, source)
    path = raw.with_name(f"{source.source_id}.cleaned.md")
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Invalid cleaned source path")
    return path


def load_cleaned(directory: Path, source: Source) -> str:
    """Worker-only migration: old raw artifacts are never overwritten or refetched."""
    raw = source_path(directory, source).read_bytes().decode("utf-8")
    cleaned = clean_markdown(raw)
    path = cleaned_path(directory, source)
    if not path.exists() or path.read_text(encoding="utf-8") != cleaned:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(cleaned, encoding="utf-8")
        temporary.replace(path)
    source.cleaned_filename = f"sources/{source.source_id}.cleaned.md"
    source.cleaner_version = CLEANER_VERSION
    source.raw_hash = digest(raw)
    source.cleaned_hash = digest(cleaned)
    source.cleaning_stats = {
        "raw_bytes": len(raw.encode()),
        "cleaned_bytes": len(cleaned.encode()),
        "raw_lines": len(raw.splitlines()),
        "cleaned_lines": len(cleaned.splitlines()),
        "removed_bytes": len(raw.encode()) - len(cleaned.encode()),
    }
    return cleaned
