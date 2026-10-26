"""Terminal presentation and arguments; research modules supply data, not formatting."""

import argparse
import json
import logging
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from research_agent.storage import read_json


def configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    for name in ("httpx", "httpcore", "openai", "firecrawl", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def preview(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Keep page/profile bodies readable without dumping full documents into terminal.
    text = " ".join(text.split())
    return (
        text
        if len(text) <= 240
        else text[:240] + f"… ({len(text):,} characters; full content saved)"
    )


class CLI:
    def __init__(self, directory: Path | None = None) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown_calls = 0
        if directory is not None:
            for path in sorted((directory / "debug").glob("call-*.response.json")):
                self.add_usage(read_json(directory, f"debug/{path.name}").get("usage"))

    def event(
        self, kind: str, name: str, details: Mapping[str, object] | None = None
    ) -> None:
        print(f"\n{kind} · {name}", flush=True)
        for key, value in (details or {}).items():
            if any(
                secret in key.lower()
                for secret in ("api_key", "authorization", "password", "secret")
            ):
                value = "[redacted]"
            print(f"  {key}: {preview(value)}", flush=True)

    def skill(self, name: str, status: str) -> None:
        self.event("Skill", name, {"Status": status})

    def model_call(self, number: int, model: str) -> None:
        self.event(
            "Model call", f"{number} · {model}", {"Status": "Waiting for response…"}
        )

    def add_usage(self, usage: JsonValue) -> bool:
        if not isinstance(usage, dict):
            self.unknown_calls += 1
            return False
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        if (
            type(incoming) is not int
            or type(outgoing) is not int
            or incoming < 0
            or outgoing < 0
        ):
            self.unknown_calls += 1
            return False
        self.input_tokens += incoming
        self.output_tokens += outgoing
        return True

    def model_result(self, usage: JsonValue) -> None:
        known = self.add_usage(usage)
        details: dict[str, object] = {"Tokens": "Unavailable from provider"}
        if known and isinstance(usage, dict):
            details["Tokens"] = (
                f"{usage['input_tokens']:,} input · {usage['output_tokens']:,} output"
            )
            for field, key, label in (
                ("input_tokens_details", "cached_tokens", "Cached input"),
                ("output_tokens_details", "reasoning_tokens", "Reasoning output"),
            ):
                extra = usage.get(field)
                if isinstance(extra, dict) and type(extra.get(key)) is int:
                    details[label] = f"{extra[key]:,} (included above)"
        details["Running total (reported)"] = (
            f"{self.input_tokens:,} input · {self.output_tokens:,} output · {self.input_tokens + self.output_tokens:,} total"
        )
        if self.unknown_calls:
            details["Usage unavailable"] = (
                f"{self.unknown_calls} response(s); excluded from totals"
            )
        self.event("Model result", "Response received", details)

    def tool_call(self, name: str, arguments: str) -> None:
        try:
            values = json.loads(arguments)
        except ValueError:
            values = {"Arguments": "Invalid JSON"}
        self.event(
            "Tool",
            name,
            values if isinstance(values, dict) else {"Arguments": "Invalid object"},
        )

    def tool_result(self, name: str, result: Mapping[str, JsonValue]) -> None:
        details: dict[str, object] = dict(result)
        if isinstance(details.get("content"), str):
            details["content"] = (
                f"{len(str(details['content'])):,} characters; complete text saved in source"
            )
        self.event("Tool result", name, details)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Start or resume an orientation research run"
    )
    actions = result.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="Create a new orientation run")
    for name in ("company", "product", "company-url", "product-url"):
        start.add_argument(f"--{name}", required=True)
    start.add_argument("--instructions", default="")
    resume = actions.add_parser("resume", help="Resume a new-format run")
    resume.add_argument("--run-id", required=True)
    resume.add_argument(
        "--max-model-calls", type=int, help="Explicit new total model-call allowance"
    )
    resume.add_argument(
        "--input-allowance", type=int, help="Explicit new conservative input allowance"
    )
    for command in (start, resume):
        command.add_argument(
            "--data-dir",
            type=Path,
            default=None,
            help="Data root (default RESEARCH_DATA_DIR or ./data)",
        )
    return result
