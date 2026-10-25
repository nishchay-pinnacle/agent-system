"""One honest accounting projection for the saved dashboard and System view."""

from pydantic import JsonValue

from agent_system.company_intelligence.coordination import Document


def usage_projection(doc: Document) -> dict[str, JsonValue]:
    return {
        "generation_requests": doc.usage.get("generation_requests", 0),
        "counting_requests": doc.usage.get("counting_requests", 0),
        "firecrawl_requests": doc.usage.get("firecrawl_requests", 0),
        "firecrawl_credits": doc.usage.get("firecrawl_credits"),
        "pages": doc.usage.get("pages", 0),
        "input_tokens": doc.usage.get("input_tokens_used", 0),
        "output_tokens": doc.usage.get("output_tokens", 0),
        "cached_tokens": doc.usage.get("cached_tokens"),
        "reasoning_tokens": doc.usage.get("reasoning_tokens"),
        "reserved_tokens": doc.usage.get("reserved_total_tokens", 0),
        "active_seconds": doc.usage.get("active_seconds", 0),
        "unknown_requests": doc.usage.get("unknown_requests", 0),
        "note": "Cached input and reasoning output are subsets; never add them twice. Unknown historical detail remains unknown.",
    }
