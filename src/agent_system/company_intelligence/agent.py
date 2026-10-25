"""Provider boundary: Responses items are preserved locally, including reasoning."""

import json
import os
from pathlib import Path
from typing import Protocol

import tiktoken
from firecrawl.v2.client import FirecrawlClient
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.responses import ResponseInputItemParam
from openai.types.responses.input_token_count_params import InputTokenCountParams
from openai.types.responses.response_create_params import (
    ResponseCreateParamsNonStreaming,
)
from pydantic import BaseModel, Field, JsonValue, TypeAdapter
from requests.exceptions import RequestException

from agent_system.company_intelligence.artifacts import read_json
from agent_system.company_intelligence.call_traces import finish_trace
from agent_system.company_intelligence.schemas import Research
from agent_system.company_intelligence.tools import public_url, tool_definitions

INPUT_ITEMS = TypeAdapter(list[ResponseInputItemParam])
# Keep adapters alive while materializing SDK iterable validators.
CREATE_PARAMS = TypeAdapter(ResponseCreateParamsNonStreaming)
COUNT_PARAMS = TypeAdapter(InputTokenCountParams)
JSON_ITEMS = TypeAdapter(list[dict[str, JsonValue]])


class ProviderFailure(Exception):
    def __init__(
        self, message: str, *, transient: bool = False, capacity: bool = False
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.capacity = capacity


class Generation(BaseModel):
    output: list[dict[str, JsonValue]]
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int = 0
    cached_tokens: int | None = None
    status: str = "completed"
    raw_response: dict[str, JsonValue] | None = None


class FetchResult(BaseModel):
    status_code: int | None = None
    content_type: str | None = None
    provider_error: str | None = None
    markdown: str | None = None
    html: str | None = None
    raw_html: str | None = None
    title: str = ""
    final_url: str | None = None
    candidates: list[dict[str, str]] = Field(default_factory=list)
    credits: float | None = None


class Providers(Protocol):
    def count_input(self, state: Research, timeout: float) -> int: ...
    def generate(self, state: Research, timeout: float) -> Generation: ...
    def retrieve(
        self, name: str, args: dict[str, JsonValue], timeout: float
    ) -> FetchResult: ...


def validate_credentials() -> None:
    missing = [
        key
        for key in ("OPENAI_API_KEY", "FIRECRAWL_API_KEY")
        if not os.getenv(key, "").strip()
    ]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))


def output_limit(state: Research) -> int:
    return (
        state.settings.orientation_output_tokens
        if state.schema_version >= 2 and state.phase == "orientation"
        else state.settings.output_tokens
    )


def request_payload(state: Research) -> dict[str, JsonValue]:
    return {
        "model": state.settings.model,
        "instructions": state.instructions,
        "input": [m for m in state.messages],
        "tools": [t for t in JSON_ITEMS.validate_python(tool_definitions(state))],
        "parallel_tool_calls": False,
        "tool_choice": "required",
        "max_output_tokens": output_limit(state),
        "reasoning": {"effort": state.settings.effort},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "company-intelligence-v1",
        "truncation": "disabled",
    }


def counting_payload(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        key: value
        for key, value in request.items()
        if key
        in {
            "model",
            "instructions",
            "input",
            "tools",
            "parallel_tool_calls",
            "tool_choice",
            "reasoning",
            "truncation",
        }
    }


def counting_kind(model: str) -> str:
    try:
        tiktoken.encoding_name_for_model(model)
    except KeyError:
        return "remote_count"
    return "local_count"


class LiveProviders:
    def __init__(
        self, directory: Path | None = None, identifier: str | None = None
    ) -> None:
        validate_credentials()
        self.directory = directory
        self.identifier = identifier

    def payload(self) -> dict[str, JsonValue]:
        if self.directory is None or self.identifier is None:
            raise ValueError("Live model requests require a durable request snapshot")
        return read_json(self.directory / "calls" / self.identifier / "request.json")

    def count_input(self, state: Research, timeout: float) -> int:
        request = self.payload()
        try:
            if counting_kind(state.settings.model) == "local_count":
                encoding = tiktoken.encoding_for_model(state.settings.model)
                payload = json.dumps(request)
                return max(
                    len(encoding.encode(payload, disallowed_special=())),
                    len(payload.encode()),
                )
            with OpenAI(max_retries=0) as client:
                counted_request = COUNT_PARAMS.validate_python(request)
                if counted_request.get("tools") is not None:
                    counted_request["tools"] = list(counted_request["tools"] or [])
                items = counted_request.get("input")
                if items is not None and not isinstance(items, str):
                    counted_request["input"] = list(items)
                result = client.responses.input_tokens.count(
                    **counted_request, timeout=timeout
                )
                if self.directory is not None and self.identifier is not None:
                    finish_trace(
                        self.directory,
                        self.identifier,
                        response=result.model_dump(mode="json"),
                        status="completed",
                    )
                return result.input_tokens
        except (APIConnectionError, APIStatusError) as exc:
            if (
                isinstance(exc, APIStatusError)
                and self.directory is not None
                and self.identifier is not None
            ):
                finish_trace(
                    self.directory,
                    self.identifier,
                    response={
                        "status_code": exc.status_code,
                        "error": TypeAdapter(JsonValue).validate_python(exc.body),
                    },
                    status="failed",
                )
            raise openai_failure(exc) from exc

    def generate(self, state: Research, timeout: float) -> Generation:
        request = CREATE_PARAMS.validate_python(self.payload())
        request["tools"] = list(request.get("tools", []))
        items = request.get("input")
        if items is not None and not isinstance(items, str):
            request["input"] = list(items)
        try:
            with OpenAI(max_retries=0) as client:
                response = client.responses.create(**request, timeout=timeout)
                raw = response.model_dump(mode="json")
                if self.directory is not None and self.identifier is not None:
                    finish_trace(
                        self.directory,
                        self.identifier,
                        response=raw,
                        status=response.status or "unknown",
                    )
                usage = response.usage
                return Generation(
                    output=JSON_ITEMS.validate_python(
                        [
                            item.model_dump(mode="json", exclude_none=True)
                            for item in response.output
                        ]
                    ),
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    reasoning_tokens=usage.output_tokens_details.reasoning_tokens
                    if usage and usage.output_tokens_details
                    else 0,
                    cached_tokens=usage.input_tokens_details.cached_tokens
                    if usage and usage.input_tokens_details
                    else None,
                    status=response.status or "unknown",
                    raw_response=raw,
                )
        except (APIConnectionError, APIStatusError) as exc:
            if (
                isinstance(exc, APIStatusError)
                and self.directory is not None
                and self.identifier is not None
            ):
                finish_trace(
                    self.directory,
                    self.identifier,
                    response={
                        "status_code": exc.status_code,
                        "error": TypeAdapter(JsonValue).validate_python(exc.body),
                    },
                    status="failed",
                )
            raise openai_failure(exc) from exc

    def retrieve(
        self, name: str, args: dict[str, JsonValue], timeout: float
    ) -> FetchResult:
        from firecrawl import FirecrawlError

        # v2 is used directly for its typed methods and explicit retry controls.
        client = FirecrawlClient(
            api_key=os.environ["FIRECRAWL_API_KEY"], timeout=timeout, max_retries=0
        )
        try:
            milliseconds = max(1, int(timeout * 1000))
            if name == "scrape_page":
                url = public_url(str(args["url"]), resolve=True)
                document = client.scrape(
                    url,
                    formats=["markdown", "html", "rawHtml"],
                    only_main_content=False,
                    auto_resume=False,
                    timeout=milliseconds,
                )
                metadata = document.metadata
                final = metadata.source_url if metadata else None
                if final:
                    public_url(final, resolve=True)
                credits = metadata.credits_used if metadata else None
                return FetchResult(
                    status_code=getattr(metadata, "status_code", None),
                    content_type=getattr(metadata, "content_type", None),
                    provider_error=getattr(metadata, "error", None),
                    markdown=document.markdown,
                    html=document.html,
                    raw_html=document.raw_html,
                    title=metadata.title or "" if metadata else "",
                    final_url=final,
                    credits=credits if isinstance(credits, (int, float)) else None,
                )
            if name == "find_site_pages":
                mapped = client.map(
                    public_url(str(args["url"]), resolve=True),
                    search=str(args.get("query") or "") or None,
                    sitemap="include",
                    include_subdomains=True,
                    limit=int(str(args.get("limit", 5000))),
                    timeout=milliseconds,
                )
                return FetchResult(
                    candidates=[
                        {
                            "url": link.url,
                            "title": link.title or "",
                            "description": link.description or "",
                        }
                        for link in mapped.links
                    ]
                )
            searched = client.search(str(args["query"]), limit=5, timeout=milliseconds)
            candidates = []
            for item in (searched.web or [])[:5]:
                data = item.model_dump()
                if isinstance(data.get("url"), str):
                    candidates.append(
                        {
                            "url": str(data["url"]),
                            "title": str(data.get("title") or ""),
                            "description": str(data.get("description") or ""),
                        }
                    )
            return FetchResult(candidates=candidates)
        except FirecrawlError as exc:
            code = exc.status_code
            raise ProviderFailure(
                f"Firecrawl {type(exc).__name__} (status {code}); check provider configuration or source availability",
                transient=code in {408, 429} or (code is not None and code >= 500),
            ) from exc
        except RequestException as exc:
            raise ProviderFailure(
                "Firecrawl transport failure", transient=True
            ) from exc


def openai_failure(exc: APIConnectionError | APIStatusError) -> ProviderFailure:
    code = exc.status_code if isinstance(exc, APIStatusError) else None
    body = exc.body if isinstance(exc, APIStatusError) else None
    error = body.get("error", body) if isinstance(body, dict) else None
    capacity = isinstance(error, dict) and error.get("code") in {
        "context_length_exceeded",
        "max_tokens_exceeded",
    }
    if capacity:
        return ProviderFailure(
            "Complete input exceeds provider capacity; increasing the application allowance cannot fix this. Select an explicitly smaller review scope or a compatible configured model; content was not truncated.",
            capacity=True,
        )
    return ProviderFailure(
        f"OpenAI {type(exc).__name__} (status {code}); verify the configured model, credentials and API access",
        transient=code is None or code in {408, 409, 429} or code >= 500,
    )
