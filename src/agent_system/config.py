"""Configuration for the local, uv-installed application."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.company_intelligence.schemas import ResearchSettings

from dotenv import load_dotenv


def data_directory() -> Path:
    # uv sync installs this src-layout package in editable mode.
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)
    configured = Path(os.environ.get("AGENT_SYSTEM_DATA_DIR", "data")).expanduser()
    return (root / configured).resolve()


def research_settings() -> "ResearchSettings":
    """Load non-secret settings; callers persist this resolved definition."""
    from agent_system.company_intelligence.schemas import ResearchSettings

    data_directory()  # Load the optional root .env, preserving exported variables.
    defaults = ResearchSettings(
        model="gpt-6-luna",
        generation_requests=120,
        firecrawl_requests=80,
        pages=60,
        active_seconds=3600,
        total_tokens=2_000_000,
        input_tokens=96_000,
    ).model_dump()
    values = {
        key: os.environ.get(f"AGENT_SYSTEM_{key.upper()}", value)
        for key, value in defaults.items()
    }
    values["model"] = os.environ.get("OPENAI_MODEL", defaults["model"])
    values["effort"] = os.environ.get("OPENAI_REASONING_EFFORT", defaults["effort"])
    for role in ("orientation", "coordinator", "investigator"):
        for field, fallback in (("model", "model"), ("effort", "effort")):
            key = f"{role}_{field}"
            values[key] = os.environ.get(
                f"AGENT_SYSTEM_{key.upper()}", values[fallback]
            )
    return ResearchSettings.model_validate(values)
