"""Provider boundary: Responses items are preserved locally, including reasoning."""

import json
import os
from typing import Protocol

import tiktoken
from firecrawl.v2.client import FirecrawlClient
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.responses import ResponseInputItemParam
from pydantic import BaseModel, Field, JsonValue, TypeAdapter
from requests.exceptions import RequestException

from agent_system.company_intelligence.schemas import Research
from agent_system.company_intelligence.tools import public_url, tool_definitions

INPUT_ITEMS = TypeAdapter(list[ResponseInputItemParam])
JSON_ITEMS = TypeAdapter(list[dict[str, JsonValue]])


class ProviderFailure(Exception):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class Generation(BaseModel):
    output: list[dict[str, JsonValue]]
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int = 0
    cached_tokens: int | None = None
    status: str = "completed"


class FetchResult(BaseModel):
    markdown: str | None = None
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


class LiveProviders:
    def __init__(self) -> None:
        validate_credentials()

    def count_input(self, state: Research, timeout: float) -> int:
        try:
            # Use only official model mappings. Unknown models (including the
            # current default) use the documented server counting endpoint.
            try:
                tiktoken.encoding_name_for_model(state.settings.model)
            except KeyError:
                pass
            else:
                encoding = tiktoken.encoding_for_model(state.settings.model)
                payload = json.dumps(
                    {
                        "instructions": state.instructions,
                        "input": state.messages,
                        "tools": tool_definitions(state),
                    }
                )
                # UTF-8 bytes provide a conservative BPE upper bound; request
                # framing receives another 1024 tokens in the workflow.
                return max(
                    len(encoding.encode(payload, disallowed_special=())),
                    len(payload.encode()),
                )
            with OpenAI(max_retries=0) as client:
                result = client.responses.input_tokens.count(
                    model=state.settings.model,
                    instructions=state.instructions,
                    input=INPUT_ITEMS.validate_python(state.messages),
                    tools=tool_definitions(state),
                    parallel_tool_calls=False,
                    reasoning={"effort": state.settings.effort},
                    tool_choice="required",
                    timeout=timeout,
                )
                return result.input_tokens
        except (APIConnectionError, APIStatusError) as exc:
            raise openai_failure(exc) from exc

    def generate(self, state: Research, timeout: float) -> Generation:
        try:
            with OpenAI(max_retries=0) as client:
                response = client.responses.create(
                    model=state.settings.model,
                    instructions=state.instructions,
                    input=INPUT_ITEMS.validate_python(state.messages),
                    tools=tool_definitions(state),
                    parallel_tool_calls=False,
                    tool_choice="required",
                    max_output_tokens=state.settings.output_tokens,
                    reasoning={"effort": state.settings.effort},
                    store=False,
                    include=["reasoning.encrypted_content"],
                    prompt_cache_key="company-intelligence-v1",
                    timeout=timeout,
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
                )
        except (APIConnectionError, APIStatusError) as exc:
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
                    formats=["markdown"],
                    only_main_content=True,
                    auto_resume=False,
                    timeout=milliseconds,
                )
                metadata = document.metadata
                final = metadata.source_url if metadata else None
                if final:
                    public_url(final, resolve=True)
                credits = metadata.credits_used if metadata else None
                return FetchResult(
                    markdown=document.markdown,
                    title=metadata.title or "" if metadata else "",
                    final_url=final,
                    credits=credits if isinstance(credits, (int, float)) else None,
                )
            if name == "find_site_pages":
                mapped = client.map(
                    public_url(str(args["url"]), resolve=True),
                    search=str(args["query"]),
                    include_subdomains=True,
                    limit=20,
                    timeout=milliseconds,
                )
                return FetchResult(
                    candidates=[
                        {
                            "url": link.url,
                            "title": link.title or "",
                            "description": link.description or "",
                        }
                        for link in mapped.links[:20]
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
    return ProviderFailure(
        f"OpenAI {type(exc).__name__} (status {code}); verify the configured model, credentials and API access",
        transient=code is None or code in {408, 409, 429} or code >= 500,
    )
