"""Local input estimates; provider usage is authoritative, opaque reasoning is reserved."""

import json
import math
from functools import lru_cache

import tiktoken
from pydantic import JsonValue

from research_agent.storage import Settings


@lru_cache(maxsize=8)
def encoding_for(model: str, explicit: str | None) -> tiktoken.Encoding:
    try:
        return (
            tiktoken.get_encoding(explicit)
            if explicit
            else tiktoken.encoding_for_model(model)
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "Unsupported tokenizer; set RESEARCH_ENCODING explicitly to a verified tiktoken encoding for this model"
        ) from exc


def estimate_input(request: dict[str, JsonValue], settings: Settings) -> dict[str, int]:
    opaque = 0

    def visible(value: JsonValue) -> JsonValue:
        nonlocal opaque
        if isinstance(value, dict):
            if value.get("encrypted_content"):
                # Ciphertext length is not a token measure. Reserve the configured
                # maximum output for each opaque prior response instead.
                opaque += settings.output_tokens
            return {k: visible(v) for k, v in value.items() if k != "encrypted_content"}
        if isinstance(value, list):
            return [visible(item) for item in value]
        return value

    serialized = json.dumps(visible(request), ensure_ascii=False)
    count = len(
        encoding_for(settings.model, settings.encoding).encode(
            serialized, disallowed_special=()
        )
    )
    return {
        "estimated_tokens": math.ceil(count * 1.1) + 512 + opaque,
        "opaque_reasoning_reserve": opaque,
    }
