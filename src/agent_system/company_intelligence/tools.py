"""Narrow retrieval schemas, URL boundaries, and saved-source excerpts."""

import hashlib
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from openai.types.responses import ToolParam
from pydantic import BaseModel, JsonValue

from agent_system.company_intelligence.page_content import load_cleaned
from agent_system.company_intelligence.schemas import (
    Begin,
    Complete,
    IndexSearchArgs,
    MapArgs,
    OrientationSubmission,
    PlanUpdate,
    ReadArgs,
    Research,
    ScrapeArgs,
    SearchArgs,
    Source,
)

TOOL_MODELS: dict[str, type[BaseModel]] = {
    "complete_orientation": OrientationSubmission,
    "update_investigation_plan": PlanUpdate,
    "search_site_index": IndexSearchArgs,
    "find_site_pages": MapArgs,
    "search_web": SearchArgs,
    "scrape_page": ScrapeArgs,
    "read_source": ReadArgs,
    "begin_investigation": Begin,
    "complete_investigation": Complete,
}
RETRIEVALS = {
    "find_site_pages",
    "search_web",
    "scrape_page",
    "read_source",
    "search_site_index",
}


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
    if parts.path.lower().endswith(".pdf"):
        raise ValueError(
            "PDF ingestion is unsupported; record this source as unresolved"
        )
    return urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def source_id(url: str) -> str:
    return "s_" + hashlib.sha256(url.encode()).hexdigest()[:16]


def strict_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    from agent_system.company_intelligence.artifacts import JSON_OBJECT

    schema = JSON_OBJECT.validate_python(model.model_json_schema())

    def visit(value: JsonValue) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties)
                value["additionalProperties"] = False
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


def tool_definitions(state: Research) -> list[ToolParam]:
    names = ["complete_investigation"]
    if state.phase == "between":
        names = ["begin_investigation", "complete_investigation"]
        if state.schema_version >= 2:
            names.append("update_investigation_plan")
    elif state.phase == "orientation":
        names = (
            ["complete_orientation"]
            if state.schema_version >= 2
            else names + ["scrape_page", "read_source"]
        )
    elif state.phase in {"review", "closing_review"}:
        names += ["read_source"]
    else:
        names += sorted(RETRIEVALS)
    result: list[ToolParam] = []
    for name in names:
        result.append(
            {
                "type": "function",
                "name": name,
                "description": name.replace("_", " "),
                "strict": True,
                "parameters": {
                    k: v for k, v in strict_schema(TOOL_MODELS[name]).items()
                },
            }
        )
    return result


def excerpt(
    directory: Path, source: Source, start_line: int = 1, max_lines: int = 200
) -> dict[str, JsonValue]:
    if source.outcome != "success":
        raise ValueError("Source has no successful saved scrape")
    lines = load_cleaned(directory, source).splitlines()
    if start_line < 1 or max_lines < 1 or max_lines > 200:
        raise ValueError("Invalid line range (maximum 200 lines)")
    selected = lines[start_line - 1 : start_line - 1 + max_lines]
    content = "\n".join(selected)
    index: list[str] = []
    for number, line in enumerate(lines, 1):
        if not line.startswith("#"):
            continue
        heading = f"{number}: {line[:80]}"
        if len("\n".join([*index, heading]).encode()) > 500 or len(index) >= 20:
            break
        index.append(heading)
    # Include original heading text in the same conservative source-text budget.
    byte_limit = 3500 - len("\n".join(index).encode())
    while len(content.encode()) > byte_limit:
        content = content[: max(1, len(content) * 3 // 4)]
    delivered = len(content.splitlines())
    end = start_line + delivered - 1
    return {
        "source_id": source.source_id,
        "representation": "cleaned",
        "cleaner_version": source.cleaner_version,
        "cleaning_summary": source.cleaning_summary,
        "content_hash": source.cleaned_hash,
        "delivery": "excerpt",
        "url": source.requested_url,
        "title": source.title,
        "start_line": start_line,
        "end_line": end,
        "total_lines": len(lines),
        "more": end < len(lines) or content != "\n".join(selected),
        "partial_last_line": content != "\n".join(selected),
        "heading_index": [item for item in index],
        "content": content,
        "excerpt_limit": "3500 tokens maximum; conservative UTF-8 byte bound",
    }
