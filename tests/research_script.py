"""Explicit subprocess-only fake provider; never imported by production code."""

import json
import sys
from pathlib import Path

from agent_system.company_intelligence.agent import FetchResult, Generation
from agent_system.company_intelligence.research_questions import QUESTIONS
from agent_system.company_intelligence.schemas import (
    SECTION_KEYS,
    Inputs,
    Research,
    ResearchSettings,
)
from agent_system.company_intelligence.tools import source_id
from agent_system.company_intelligence.workflow import initial_research, run_research

URL = "https://example.com/"
SID = source_id(URL)


def completion(
    phase: str, next_step: str, *, changed: bool = True
) -> dict[str, object]:
    return {
        "phase": phase,
        "sections": [
            {
                "key": "scope",
                "coverage": "answered",
                "content": "Explicit source statement: ExampleCo's Widget routes support requests. Inference: useful for support teams.",
                "source_ids": [SID],
            }
        ]
        if phase == "orientation"
        else [],
        "gaps": [
            {
                "gap_id": None,
                "question": "Which teams use Widget?",
                "consequence": "Changes campaign audience",
                "state": "open",
                "critical_boundary": False,
                "interview_question": "Which teams have adopted it?",
            }
        ]
        if phase == "orientation"
        else [],
        "source_ids": [SID],
        "findings": "Confirmed Widget routes support requests; team fit remains uncertain.",
        "contradictions": [],
        "meaningful_change": changed,
        "next_step": next_step,
        "next_source": URL,
        "return_question": None,
        "readiness": "needs_review",
    }


def orientation_submission() -> dict[str, object]:
    return {
        "sections": [
            {
                "key": key,
                "coverage": "answered" if key == "scope" else "partial",
                "content": "ExampleCo Widget routes support requests."
                if key == "scope"
                else "Overview evidence is incomplete; targeted reading is required.",
                "source_ids": [SID],
            }
            for key in SECTION_KEYS
        ],
        "assessments": [
            {
                "question_id": key,
                "status": "partial",
                "answer": "Overview describes routing; this question needs targeted evidence.",
                "source_ids": [SID],
            }
            for key in QUESTIONS
        ],
        "plan": [
            {
                "question_id": key,
                "question": question,
                "importance": "Clarifies downstream campaign claims",
                "known_evidence": "Overview provides only general routing evidence",
                "source_strategy": "Search saved inventory for product documentation and customer examples",
                "resolution_criterion": "Specific evidence or documented source exhaustion",
                "priority": 1 if key.startswith(("S", "C")) else 3,
            }
            for key, question in QUESTIONS.items()
        ],
        "contradictions": [],
    }


def begin_args() -> dict[str, object]:
    return {
        "gap_id": "g_001",
        "question": "Which teams use Widget?",
        "importance": "Changes audience",
        "source_plan": "Read saved product evidence",
        "resolution_criterion": "Named demonstrated customer",
        "new_source_id": None,
        "reopen_reason": None,
    }


class ScriptedProviders:
    def __init__(self, directory: Path, mode: str = "normal") -> None:
        self.directory = directory
        self.mode = mode
        self.fetches = 0

    def count_input(self, state: Research, timeout: float) -> int:
        if self.mode == "orientation_pause" and state.phase == "orientation":
            raise KeyboardInterrupt
        if (
            self.mode == "pause"
            and state.completed_investigations == 1
            and state.phase == "between"
        ):
            raise KeyboardInterrupt
        return 500

    def generate(self, state: Research, timeout: float) -> Generation:
        with (self.directory / "test_contexts.jsonl").open("a") as handle:
            handle.write(
                json.dumps({"phase": state.phase, "messages": state.messages}) + "\n"
            )
        if self.mode == "schema" and state.phase != "orientation":
            name, args = "complete_investigation", {"invalid": True}
        elif state.phase == "orientation":
            name, args = (
                "complete_orientation",
                orientation_submission(),
            )
        elif state.phase == "between":
            name, args = "begin_investigation", begin_args()
            args["gap_id"] = state.gaps[0].gap_id if state.gaps else None
            plan = next(p for p in state.investigation_plan if p.status == "pending")
            args["plan_id"], args["question_id"] = plan.plan_id, plan.question_id
        elif state.phase == "investigation":
            has_tool = any(
                m.get("type") == "function_call_output" for m in state.messages
            )
            if not has_tool:
                if state.completed_investigations == 0:
                    name, args = "scrape_page", {"url": URL}
                else:
                    name, args = (
                        "read_source",
                        {"source_id": SID, "start_line": 1, "max_lines": 200},
                    )
            else:
                name, args = (
                    "complete_investigation",
                    completion(
                        "investigation",
                        "investigate"
                        if state.completed_investigations == 0
                        else "review",
                    ),
                )
        else:
            name, args = (
                "complete_investigation",
                completion(state.phase, "public_sources_exhausted"),
            )
        return Generation(
            output=[
                {
                    "type": "reasoning",
                    "id": "r1",
                    "summary": [],
                    "encrypted_content": "scripted-encrypted-protocol-item",
                },
                {
                    "type": "function_call",
                    "call_id": f"call_{state.usage.generation_requests}",
                    "name": name,
                    "arguments": json.dumps(args),
                },
            ],
            input_tokens=500,
            output_tokens=80,
            reasoning_tokens=20,
            cached_tokens=0,
        )

    def retrieve(
        self, name: str, args: dict[str, object], timeout: float
    ) -> FetchResult:
        self.fetches += 1
        with (self.directory / "test_fetches.jsonl").open("a") as handle:
            handle.write(json.dumps({"name": name, "args": args}) + "\n")
        if name == "scrape_page":
            return FetchResult(
                markdown="# Widget\nWidget routes support requests.\n<script>window.pwned=true</script>\n",
                title="ExampleCo Widget",
                final_url=URL,
            )
        return FetchResult(candidates=[{"url": URL, "title": "Widget"}])


def main() -> None:
    root, job_id, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    root.mkdir(parents=True, exist_ok=True)
    initial = (
        initial_research(
            job_id,
            Inputs(company="ExampleCo", url=URL, product="Widget", product_url=URL),
            ResearchSettings(),
            run_mode="orientation_only" if mode == "orientation_pause" else "full",
        )
        if mode in {"pause", "orientation_pause"}
        else None
    )
    result = run_research(root, job_id, initial, ScriptedProviders(root, mode))
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
