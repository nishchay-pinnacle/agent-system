"""Output projection reflects assessment authority without changing worker policy."""

from pathlib import Path

import pytest
import research_script
from research_script import SID, ScriptedProviders, orientation_submission
from test_orientation import new_state

from agent_system.company_intelligence.artifacts import project_gaps, read_json
from agent_system.company_intelligence.research_questions import (
    CATALOG_VERSION,
    QUESTIONS,
)
from agent_system.company_intelligence.schemas import (
    Gap,
    OrientationSubmission,
    QuestionAssessment,
)
from agent_system.company_intelligence.tools import tool_definitions
from agent_system.company_intelligence.workflow import run_research


def test_orientation_result_and_projection_purity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission = OrientationSubmission.model_validate(orientation_submission())
    for index, assessment in enumerate(submission.assessments):
        assessment.status = (
            "answered" if index < 10 else "partial" if index % 2 else "unassessed"
        )
    submission.assessments[
        10
    ].interview_question = "  Which deployment constraint requires approval?  "
    submission.assessments[
        11
    ].interview_question = "WHICH deployment  constraint requires approval?"
    submission.assessments[12].interview_question = " \n "
    submission.assessments[0].interview_question = "Already answered?"
    monkeypatch.setattr(
        research_script,
        "orientation_submission",
        lambda: submission.model_dump(mode="json"),
    )
    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, ScriptedProviders(tmp_path))
    assert done.stop_reason == "orientation_complete" and done.gaps == []
    result = read_json(tmp_path / "jobs" / done.job_id / "result.json")
    assert len(result["consequential_gaps"]) == 39
    assert result["interview_questions"] == [
        "Which deployment constraint requires approval?"
    ]
    snapshot = read_json(tmp_path / "jobs" / done.job_id / "orientation.json")
    assert snapshot["catalog_version"] == CATALOG_VERSION == 2
    assert len(QUESTIONS) == 49 and set(QUESTIONS) == {
        a.question_id for a in done.assessments
    }
    explicit = Gap(
        gap_id="g_1",
        question="Private prerequisites?",
        consequence="Changes deployment",
        state="open",
        critical_boundary=True,
        interview_question="What private prerequisites apply?",
    )
    done.gaps.append(explicit)
    done.investigation_plan[10].status = "completed"
    before = done.model_dump_json()
    first = project_gaps(done.assessments, done.investigation_plan, done.gaps)
    assert first == project_gaps(done.assessments, done.investigation_plan, done.gaps)
    assert done.model_dump_json() == before
    assert len(first[0]) == 40 and first[0][-1] == {
        **explicit.model_dump(mode="json"),
        "origin": "gap",
    }
    key = done.assessments[10].question_id
    gap = next(g for g in first[0] if g["gap_id"] == f"assessment:{key}")
    assert gap["source_ids"] == [SID] and gap["plan_ids"] == [
        done.investigation_plan[10].plan_id
    ]
    done.assessments[10].status = done.assessments[11].status = "answered"
    later, interviews = project_gaps(
        done.assessments, done.investigation_plan, done.gaps
    )
    assert len(later) == 38 and interviews == [explicit.interview_question]
    assert project_gaps([], [], done.gaps) == (
        [{**explicit.model_dump(mode="json"), "origin": "gap"}],
        [explicit.interview_question],
    )
    legacy = QuestionAssessment(
        question_id="S1",
        status="partial",
        answer="Need public documentation",
        source_ids=[],
    )
    assert legacy.interview_question is None
    public_gaps, questions = project_gaps([legacy], [], [])
    assert (
        questions == []
        and public_gaps[0]["consequence"]
        == "Required research question remains unresolved"
    )
    tools = tool_definitions(initial)
    schema = next(t for t in tools if t["name"] == "complete_orientation")["parameters"]
    # Actual strict schema supports null and requires the field; old persisted data defaults it.
    assessment_schema = schema["$defs"]["QuestionAssessment"]
    assert "interview_question" in assessment_schema["required"]
    assert {
        item["type"]
        for item in assessment_schema["properties"]["interview_question"]["anyOf"]
    } == {"string", "null"}
