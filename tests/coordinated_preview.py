"""Offline multi-batch preview built through the actual coordinated workflow."""

import json
from pathlib import Path

from research_script import URL
from test_coordinated_research import FakeProviders, proposal, spec

from agent_system.company_intelligence.agent import Generation
from agent_system.company_intelligence.approvals import UserCommand, enqueue
from agent_system.company_intelligence.execution import load_document
from agent_system.company_intelligence.schemas import Inputs, Research, ResearchSettings
from agent_system.company_intelligence.workflow import initial_research, run_research


def create_preview(root: Path, job_id: str, scenario: str) -> None:
    class Preview(FakeProviders):
        def generate(self, state: Research, timeout: float) -> Generation:
            generated = super().generate(state, timeout)
            payload = json.loads(state.messages[0]["content"])
            if (
                state.phase == "between"
                and payload["latest_batch"]
                and payload["latest_batch"][0]["assignment_id"] == "assignment_0001"
            ):
                generated.output[0]["arguments"] = json.dumps(
                    proposal([spec(["S3"]), spec(["C2"])])
                )
            return generated

        def count_input(self, state: Research, timeout: float) -> int:
            payload = json.loads(state.messages[0]["content"])
            if (
                scenario == "paused"
                and payload.get("assignment", {}).get("assignment_id")
                == "assignment_0003"
            ):
                return 100_000
            return super().count_input(state, timeout)

    initial = initial_research(
        job_id,
        Inputs(
            company="ExampleCo · synthetic preview",
            url=URL,
            product="Widget",
            product_url=URL,
        ),
        ResearchSettings(
            model="gpt-6-luna",
            generation_requests=120,
            total_tokens=2_000_000,
            input_tokens=96_000,
            active_seconds=3600,
        ),
        schema_version=3,
    )
    state = run_research(
        root, job_id, initial, Preview(), max_steps=10 if scenario == "active" else None
    )
    if scenario == "completed":
        directory = root / "jobs" / job_id
        doc = load_document(directory, state)
        enqueue(
            directory,
            UserCommand(
                command_id="preview-quality",
                job_id=job_id,
                kind="quality",
                expected_revision=doc.revision,
            ),
            doc,
        )
        run_research(root, job_id, providers=Preview())
