"""Create an isolated, deterministic synthetic run for offline browser inspection."""

import argparse
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_script import URL, ScriptedProviders

from agent_system.company_intelligence.agent import (
    FetchResult,
    Generation,
    ProviderFailure,
)
from agent_system.company_intelligence.schemas import Inputs, Research, ResearchSettings
from agent_system.company_intelligence.workflow import initial_research, run_research

JOB_ID = "11111111-1111-4111-8111-111111111111"


class PreviewProviders(ScriptedProviders):
    def retrieve(
        self, name: str, args: dict[str, object], timeout: float
    ) -> FetchResult:
        if name == "scrape_page":
            return FetchResult(
                title="ExampleCo · Widget platform",
                final_url=URL,
                markdown="# Widget\n\nWidget routes support requests to the right team.\n\n<!-- CMS metadata -->\n<script>window.pwned = true</script>\n\n## Practical capabilities\n\n"
                + "Customers supply requests; Widget categorizes and routes them.\n"
                * 90
                + "\n| Plan | Restriction |\n| --- | --- |\n| Enterprise | Human approval required for exports |\n\n[Product documentation](https://example.com/docs)\n\n## Example\n\n```html\n<script>example only, never execute</script>\n```\n\n## Final caveat\n\nExports are Enterprise-only and require human approval.\n",
            )
        return FetchResult(
            candidates=[
                {
                    "url": f"https://example.com/docs/{i}",
                    "title": "Enterprise exports" if i == 42 else f"Product guide {i}",
                }
                for i in range(80)
            ]
        )

    def generate(self, state: Research, timeout: float) -> Generation:
        if state.usage.generation_requests == 1:
            raise ProviderFailure(
                "Synthetic temporary provider failure (preview only)", transient=True
            )
        return super().generate(state, timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/dashboard-preview"))
    args = parser.parse_args()
    root = args.output.resolve()
    if (root / "jobs" / JOB_ID).exists():
        parser.error(
            "Preview already exists; choose a new --output directory. No files were replaced."
        )
    root.mkdir(parents=True, exist_ok=True)
    ticks = count()
    timestamp = "2026-01-01T12:00:00+00:00"
    with (
        patch(
            "agent_system.company_intelligence.artifacts.now", return_value=timestamp
        ),
        patch("agent_system.company_intelligence.workflow.now", return_value=timestamp),
        patch(
            "agent_system.company_intelligence.call_traces.now", return_value=timestamp
        ),
        patch(
            "agent_system.company_intelligence.workflow.time",
            SimpleNamespace(monotonic=lambda: next(ticks) / 100),
        ),
    ):
        state = initial_research(
            JOB_ID,
            Inputs(company="ExampleCo", url=URL, product="Widget", product_url=URL),
            ResearchSettings(),
            run_mode="orientation_only",
        )
        result = run_research(root, JOB_ID, state, PreviewProviders(root))
    if result.stop_reason != "orientation_complete":
        raise RuntimeError(result.error)
    print(f"Synthetic preview saved: {root}")
    print(
        f"AGENT_SYSTEM_DATA_DIR='{root}' uv run python -m agent_system.dashboard --job {JOB_ID} --port 8765"
    )


if __name__ == "__main__":
    main()
