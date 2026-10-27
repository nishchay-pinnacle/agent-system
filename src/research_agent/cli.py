"""Terminal presentation and arguments; research modules supply data, not formatting."""

import argparse
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from research_agent.storage import Run, firecrawl_counts, local_path, read_json


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
        self.directory = directory
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown_calls = 0
        self.responses = 0
        if directory is not None:
            for path in sorted((directory / "debug").glob("call-*.response.json")):
                self.add_usage(read_json(directory, f"debug/{path.name}").get("usage"))

    def event(
        self, kind: str, name: str, details: Mapping[str, object] | None = None
    ) -> None:
        def sanitize(value: object) -> object:
            if isinstance(value, Mapping):
                return {
                    str(key): "[redacted]"
                    if any(
                        secret in str(key).lower()
                        for secret in ("api_key", "authorization", "password", "secret")
                    )
                    else sanitize(item)
                    for key, item in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [sanitize(item) for item in value]
            return value

        safe = {
            key: "[redacted]"
            if any(
                secret in key.lower()
                for secret in ("api_key", "authorization", "password", "secret")
            )
            else sanitize(value)
            for key, value in (details or {}).items()
        }
        print(f"\n{kind} · {name}", flush=True)
        for key, value in safe.items():
            print(f"  {key}: {preview(value)}", flush=True)
        if self.directory is not None:
            with local_path(self.directory, "events.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps(
                        {
                            "time": datetime.now(UTC).isoformat(),
                            "kind": kind,
                            "name": name,
                            "details": safe,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def skill(self, name: str, status: str) -> None:
        self.event("Skill", name, {"Status": status})

    def model_call(self, number: int, model: str) -> None:
        self.event(
            "Model call", f"{number} · {model}", {"Status": "Waiting for response…"}
        )

    def add_usage(self, usage: JsonValue) -> bool:
        self.responses += 1
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

    def summary(self, directory: Path, status: str) -> None:
        run = Run.model_validate(read_json(directory, "run.json"))
        counts = firecrawl_counts(directory)
        missing = self.unknown_calls + max(0, run.model_calls - self.responses)
        total_calls = run.model_calls + counts.scrape + counts.map
        details: dict[str, object] = {
            "Total tokens (reported)": f"{self.input_tokens + self.output_tokens:,}",
            "Input / output tokens": f"{self.input_tokens:,} / {self.output_tokens:,}",
            "Model requests": run.model_calls,
            "Firecrawl requests": f"{counts.scrape} scrape · {counts.map} map",
            "Total API requests": total_calls
            if counts.complete_history
            else f"{total_calls} recorded; earlier Firecrawl calls unavailable",
            "Counting": "Logical requests across all resumes; includes failed/reserved calls, excludes SDK retries and cached reuse",
        }
        details["Stage"] = run.stage
        if run.stage == "research":
            details["Research page attempts"] = (
                f"{run.research_page_attempts} / {run.research_page_budget}"
            )
        if missing:
            details["Token coverage"] = (
                f"{missing} model request(s) have no reported usage; excluded from token totals"
            )
        self.event("Run summary", status, details)

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
        description="Run orientation and research, or resume a saved run"
    )
    actions = result.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="Run orientation then research")
    for name in ("company", "product", "company-url", "product-url"):
        start.add_argument(f"--{name}", required=True)
    start.add_argument("--instructions", default="")
    start.add_argument(
        "--orientation-only", action="store_true", help="Stop after orientation"
    )
    resume = actions.add_parser("resume", help="Resume a new-format run")
    resume.add_argument("--run-id", required=True)
    resume.add_argument(
        "--max-model-calls", type=int, help="Explicit new total model-call allowance"
    )
    resume.add_argument(
        "--input-token-limit", type=int, help="Explicit new input-token limit"
    )
    resume.add_argument(
        "--page-budget",
        type=int,
        help="Explicit new total research page-fetch allowance",
    )
    resume.add_argument("--context-tokens", type=int)
    resume.add_argument("--encoding")
    dashboard = actions.add_parser("dashboard", help="Read-only localhost dashboard")
    dashboard.add_argument("--run-id")
    dashboard.add_argument("--port", type=int, default=8765)
    research = actions.add_parser(
        "research", help="Explicitly continue a completed orientation into research"
    )
    research.add_argument("--run-id", required=True)
    for command in (start, research):
        for flag, default, help_text in (
            (
                "model-calls",
                30,
                "Additional research model calls (cumulative ceiling 1000)",
            ),
            (
                "page-budget",
                20,
                "Research page-fetch attempts, including failures (1-10000)",
            ),
            (
                "input-token-limit",
                96000,
                "Full research input-token limit; no compaction",
            ),
            ("index-result-limit", 20, "Default research index query page size (1-50)"),
        ):
            command.add_argument(
                f"--{flag}",
                type=int,
                default=default
                if command is start or flag == "index-result-limit"
                else None,
                required=command is research and flag != "index-result-limit",
                help=help_text + (f"; default {default}" if command is start else ""),
            )
    for command in (start, resume, research, dashboard):
        command.add_argument(
            "--data-dir",
            type=Path,
            default=None,
            help="Data root (default RESEARCH_DATA_DIR or ./data)",
        )
    return result
