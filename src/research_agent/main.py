"""Start or resume one orientation run: python -m research_agent.main --help."""

import argparse
import logging
import os
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import ValidationError

from research_agent.agent import OpenAIModel, initialize_prompts, run_agent
from research_agent.storage import (
    Inputs,
    Run,
    Settings,
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
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


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    load_dotenv(Path.cwd() / ".env", override=False)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # SDK HTTP logs can contain request URLs; only application progress reaches terminal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    directory: Path | None = None
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
            if args.action == "resume":
                updates = {
                    key: getattr(args, key)
                    for key in ("max_model_calls", "input_allowance")
                    if getattr(args, key) is not None
                }
                run.settings = Settings.model_validate(
                    {**run.settings.model_dump(), **updates}
                )
            print(
                f"Run: {run.run_id}\nModel: {run.settings.model}\nOutputs: {directory.resolve()}",
                flush=True,
            )
            openai_key, firecrawl_key = (
                os.environ.get("OPENAI_API_KEY", ""),
                os.environ.get("FIRECRAWL_API_KEY", ""),
            )
            if not openai_key or not firecrawl_key:
                raise ValueError(
                    "Set OPENAI_API_KEY and FIRECRAWL_API_KEY; saved run is incomplete"
                )
            tools = OrientationTools(
                directory,
                run.inputs,
                run.settings,
                FirecrawlPages(firecrawl_key, run.settings),
            )
            run_agent(
                directory,
                run,
                OpenAIModel(openai_key, run.settings.timeout_seconds),
                tools,
            )
            print("Orientation complete. No additional pages explored.")
            for name in ("profile.md", "sources", "site_index.json"):
                print(directory.resolve() / name)
    except (ValueError, RuntimeError, OSError) as exc:
        detail = (
            "Input/configuration validation failed"
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        print(f"Orientation incomplete: {type(exc).__name__}: {detail}")
        if directory is not None:
            print(f"Preserved work: {directory.resolve()}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
