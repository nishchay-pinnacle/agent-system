"""CLI entry point for starting, inspecting, and resuming local jobs."""

import argparse
import logging
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, StateSnapshot

from agent_system.company_intelligence.agent import validate_credentials
from agent_system.company_intelligence.schemas import Inputs, Research
from agent_system.company_intelligence.tools import public_url
from agent_system.company_intelligence.workflow import (
    initial_research,
    load_research,
    run_research,
)
from agent_system.config import data_directory, research_settings
from agent_system.state import snapshot_status
from agent_system.workflow import PAUSE_MESSAGE, build_workflow

logger = logging.getLogger(__name__)


def job_id_argument(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("job must be a UUID") from exc


def company_argument(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("company must not be blank")
    return value.strip()


def display(job_id: str, snapshot: StateSnapshot) -> None:
    print(f"Job ID: {job_id}")
    print(f"Company: {snapshot.values.get('company_name', 'unknown')}")
    print(f"Status: {snapshot_status(snapshot)}")
    for field in ("draft_path", "result_path"):
        if snapshot.values.get(field):
            print(f"{field}: {snapshot.values[field]}")


def expected_pause(snapshot: StateSnapshot) -> bool:
    pending = [task for task in snapshot.tasks if task.interrupts]
    return (
        snapshot.next == ("pause_for_resume",)
        and len(pending) == 1
        and pending[0].name == "pause_for_resume"
        and len(pending[0].interrupts) == 1
        and pending[0].interrupts[0].value == PAUSE_MESSAGE
    )


def display_research(state: "Research") -> int:
    print(f"Job ID: {state.job_id}")
    print(f"Company: {state.inputs.company}")
    print(f"Product: {state.inputs.product}")
    print(f"Status: {state.status}; phase: {state.phase}")
    print(f"Stop reason: {state.stop_reason or 'not stopped'}")
    if state.limiting_resource:
        print(f"Limit: {state.limiting_resource}; resume cannot reopen a terminal run")
    if state.error:
        print(f"Error: {state.error}")
    print(f"Run mode: {state.run_mode}")
    if state.stop_reason == "orientation_complete":
        print(
            "Orientation complete — profile is provisional; investigations have not run."
        )
    print(f"Readiness: {state.readiness}")
    print(f"Artifacts: {data_directory() / 'jobs' / state.job_id}")
    return 1 if state.status in {"blocked_by_error", "interrupted"} else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--company", required=True, type=company_argument)
    research = commands.add_parser("research")
    research.add_argument("--company", required=True, type=company_argument)
    research.add_argument("--url", required=True, type=public_url)
    research.add_argument("--product", required=True, type=company_argument)
    research.add_argument("--product-url", required=True, type=public_url)
    research.add_argument("--orientation-only", action="store_true")
    research.add_argument("--source-url", action="append", default=[], type=public_url)
    for name in ("status", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True, type=job_id_argument)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    job_id = str(uuid4()) if args.command in {"start", "research"} else args.job
    config: RunnableConfig = {"configurable": {"thread_id": job_id}}
    if args.command in {"start", "research"}:
        print(f"Job ID: {job_id}", flush=True)
    try:
        data_dir = data_directory()
        database = data_dir / "checkpoints.sqlite"
        directory = data_dir / "jobs" / job_id
        if args.command == "research":
            print(
                f"Dashboard: uv run python -m agent_system.dashboard --job {job_id} --port 8765",
                flush=True,
            )
            settings = research_settings()
            validate_credentials()
            initial = initial_research(
                job_id,
                Inputs(
                    company=args.company,
                    url=args.url,
                    product=args.product,
                    product_url=args.product_url,
                    source_urls=args.source_url,
                ),
                settings,
                run_mode="orientation_only" if args.orientation_only else "full",
            )
            data_dir.mkdir(parents=True, exist_ok=True)
            result = run_research(data_dir, job_id, initial)
            return display_research(result)
        if args.command in {"status", "resume"} and database.exists():
            with SqliteSaver.from_conn_string(str(database)) as saver:
                saved = load_research(saver, directory, job_id)
            if saved is not None:
                if args.command == "resume":
                    if saved.status != "completed":
                        validate_credentials()
                    saved = run_research(data_dir, job_id)
                return display_research(saved)
        if args.command != "start" and not database.exists():
            print(f"Job {job_id}: not found")
            return 1
        data_dir.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(database)) as checkpointer:
            graph = build_workflow(data_dir, checkpointer)
            if args.command == "start":
                logger.info("job=%s start", job_id)
                graph.invoke({"job_id": job_id, "company_name": args.company}, config)
            else:
                snapshot = graph.get_state(config)
                status = snapshot_status(snapshot)
                if status == "not found":
                    print(f"Job {job_id}: not found")
                    return 1
                if args.command == "resume" and status != "completed":
                    if not expected_pause(snapshot):
                        display(job_id, snapshot)
                        print(
                            "Job is incomplete without the expected interrupt; "
                            "no recovery attempted."
                        )
                        return 1
                    logger.info("job=%s resume start", job_id)
                    graph.invoke(Command(resume=True), config)
            snapshot = graph.get_state(config)
            display(job_id, snapshot)
            status = snapshot_status(snapshot)
            if args.command != "status":
                if status == "waiting_for_resume":
                    logger.info("job=%s stage=pause_for_resume pause", job_id)
                    print(
                        f"Resume: uv run python -m agent_system.worker resume "
                        f"--job {job_id}"
                    )
                elif status == "completed":
                    logger.info("job=%s finish", job_id)
                else:
                    logger.error("job=%s incomplete after execution", job_id)
                    return 1
        return 0
    except Exception:
        logger.exception("job=%s command=%s failed", job_id, args.command)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
