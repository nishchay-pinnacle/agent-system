"""Saved discovery metadata, deliberately separate from fetched evidence."""

from pathlib import Path

from pydantic import JsonValue

from agent_system.company_intelligence.artifacts import read_json, save_json
from agent_system.company_intelligence.tools import public_url, source_id


def save_inventory(
    directory: Path, candidates: list[dict[str, str]], provenance: str, limit: int
) -> dict[str, JsonValue]:
    path = directory / "site_index.json"
    entries: dict[str, dict[str, JsonValue]] = {}
    if path.exists():
        saved_entries = read_json(path).get("entries", [])
        if not isinstance(saved_entries, list):
            raise ValueError("Invalid inventory entries")
        for entry in saved_entries:
            if isinstance(entry, dict):
                entries[str(entry["url"])] = entry
    rejected = 0
    for candidate in candidates:
        try:
            url = public_url(candidate.get("url", ""))
        except ValueError:
            rejected += 1
            continue
        item = entries.setdefault(
            url, {"source_id": source_id(url), "url": url, "provenance": []}
        )
        for field in ("title", "description"):
            if candidate.get(field):
                item[field] = candidate[field]
        origins = item["provenance"]
        if isinstance(origins, list) and provenance not in origins:
            origins.append(provenance)
    summary: dict[str, JsonValue] = {
        "artifact": "site_index.json",
        "discovered_count": len(entries),
        "returned_count": len(candidates),
        "rejected_count": rejected,
        "requested_limit": limit,
        "possibly_truncated": len(candidates) >= limit,
        "completeness": "unknown",
        "evidence": "Discovery leads only; scrape before citing.",
    }
    save_json(
        path,
        {
            "version": 1,
            "summary": summary,
            "entries": [entries[url] for url in sorted(entries)],
        },
    )
    return summary


def search_inventory(
    directory: Path, query: str, limit: int = 10, offset: int = 0
) -> dict[str, JsonValue]:
    if not 1 <= limit <= 20 or offset < 0:
        raise ValueError("Invalid inventory pagination")
    path = directory / "site_index.json"
    if not path.exists():
        return {"entries": [], "total_matches": 0, "available": False}
    terms = query.casefold().split()
    ranked: list[tuple[int, str, dict[str, JsonValue]]] = []
    entries = read_json(path).get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid site inventory")
    for item in entries:
        if not isinstance(item, dict):
            continue
        url, title, description = (
            str(item.get(k, "")).casefold() for k in ("url", "title", "description")
        )
        if not all(term in url + " " + title + " " + description for term in terms):
            continue
        score = sum(
            3 * (term in title) + 2 * (term in url) + (term in description)
            for term in terms
        )
        ranked.append((-score, url, item))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return {
        "entries": [row[2] for row in ranked[offset : offset + limit]],
        "total_matches": len(ranked),
        "offset": offset,
        "available": True,
        "evidence": "Leads, not evidence",
    }
