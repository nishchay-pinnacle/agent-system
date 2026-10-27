"""Start, research, or resume one company run: python -m research_agent.main --help."""

import os
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import ValidationError

from research_agent.agent import (
    OpenAIModel,
    begin_research,
    initialize_prompts,
    run_agent,
)
from research_agent.cli import CLI, configure_logging, parser
from research_agent.storage import (
    RESEARCH_START,
    Inputs,
    Run,
    Settings,
    activate_research,
    local_path,
    read_json,
    run_directory,
    run_lock,
    save_run,
)
from research_agent.tools import FirecrawlPages, OrientationTools


def environment_settings() -> Settings:
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        raise ValueError(
            "Set OPENAI_MODEL explicitly; no model is selected automatically"
        )
    return Settings.model_validate(
        {
            "model": model,
            **{
                field: os.environ[env]
                for field, env in {
                    "max_model_calls": "RESEARCH_MAX_MODEL_CALLS",
                    "input_allowance": "RESEARCH_INPUT_ALLOWANCE",
                    "output_tokens": "RESEARCH_OUTPUT_TOKENS",
                    "timeout_seconds": "RESEARCH_TIMEOUT_SECONDS",
                    "map_limit": "RESEARCH_MAP_LIMIT",
                }.items()
                if env in os.environ
            },
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    load_dotenv(Path.cwd() / ".env", override=False)
    configure_logging()
    display = CLI()
    directory: Path | None = None
    summary_ready = False
    stage = "Orientation"
    try:
        root = (
            args.data_dir or Path(os.environ.get("RESEARCH_DATA_DIR", "data"))
        ) / "runs"
        if args.action == "start":
            run = Run(
                run_id="orientation_" + uuid4().hex[:16],
                inputs=Inputs(
                    company=args.company,
                    product=args.product,
                    company_url=args.company_url,
                    product_url=args.product_url,
                    instructions=args.instructions,
                ),
                settings=environment_settings(),
            )
            directory = run_directory(root, run.run_id)
            directory.mkdir(parents=True, exist_ok=False)
            save_run(directory, run)
            initialize_prompts(directory, run)
        else:
            directory = run_directory(root, args.run_id)
        with run_lock(directory):
            run = Run.model_validate(read_json(directory, "run.json"))
            if run.version != 1 or run.run_id != directory.name:
                raise ValueError(
                    "Unsupported run format or mismatched ID; legacy runs are not migrated"
                )
            if (
                args.action == "resume"
                and local_path(directory, RESEARCH_START).exists()
            ):
                activate_research(directory, run)
            stage = run.stage.capitalize()
            if args.action == "resume":
                if args.page_budget is not None:
                    if run.stage != "research":
                        raise ValueError(
                            "--page-budget applies only to active research"
                        )
                    updated = Run.model_validate(
                        {**run.model_dump(), "research_page_budget": args.page_budget}
                    )
                    run.research_page_budget = updated.research_page_budget
                updates = {
                    key: getattr(args, key)
                    for key in ("max_model_calls", "input_allowance")
                    if getattr(args, key) is not None
                }
                run.settings = Settings.model_validate(
                    {**run.settings.model_dump(), **updates}
                )
            openai_key, firecrawl_key = (
                os.environ.get("OPENAI_API_KEY", ""),
                os.environ.get("FIRECRAWL_API_KEY", ""),
            )
            if not openai_key or not firecrawl_key:
                raise ValueError(
                    f"Set OPENAI_API_KEY and FIRECRAWL_API_KEY; {run.stage} is incomplete"
                )
            tools = OrientationTools(
                directory,
                run.inputs,
                run.settings,
                FirecrawlPages(firecrawl_key, run.settings),
                run,
            )
            if args.action == "research":
                # Prepare the durable context before selecting the research stage.
                begin_research(
                    directory,
                    run,
                    tools,
                    model_calls=args.model_calls,
                    page_budget=args.page_budget,
                    input_allowance=args.input_allowance,
                    index_result_limit=args.index_result_limit,
                )
                stage = "Research"
            display = CLI(directory)
            summary_ready = True
            display.event(
                "Research",
                f"{run.inputs.company} · {run.inputs.product}",
                {"Model": run.settings.model},
            )
            display.skill(
                run.stage,
                "Using saved instructions" if args.action == "resume" else "Loaded",
            )
            run_agent(
                directory,
                run,
                OpenAIModel(openai_key, run.settings.timeout_seconds),
                tools,
                display,
            )
            display.event(
                "Complete",
                f"{stage} complete",
                {
                    name: str(directory.resolve() / name)
                    for name in ("profile.md", "sources", "site_index.json")
                },
            )
            display.summary(directory, "Complete")
    except (ValueError, RuntimeError, OSError) as exc:
        detail = (
            "Input/configuration validation failed"
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        display.event("Error", f"{stage} incomplete: {detail}")
        if directory is not None:
            display.event(
                "Saved work",
                str(directory.resolve()),
                {
                    "Resume": f"uv run python -m research_agent.main resume --run-id {directory.name}"
                },
            )
            if summary_ready:
                display.summary(directory, "Incomplete")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
