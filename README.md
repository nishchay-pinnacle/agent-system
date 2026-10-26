# Orientation research agent

One local Python agent reads your company and product overview pages, writes a
provisional Markdown profile, maps the company site broadly, and stops. It cannot
read discovered pages. Orientation is a skill loaded by this same agent, not a
separate agent. There is no dashboard or deeper research workflow.

## Setup

Requires Python 3.13 and uv. Run from this repository:

```bash
uv sync --locked
# Create a template only if .env does not already exist:
test -e .env || cp .env.example .env
```

Edit `.env` to set `OPENAI_API_KEY`, `FIRECRAWL_API_KEY`, and `OPENAI_MODEL` to the
exact OpenAI Responses API model you intend to use. The model must support strict
function tools and `reasoning.encrypted_content`. No model is selected or silently
substituted. Existing shell environment variables take precedence over `.env`.
Existing `.env` and research data are never migrated or overwritten by setup.
Old `AGENT_SYSTEM_*` settings are not used.

## Start and resume

These commands make paid provider calls when valid credentials are configured:

```bash
uv run python -m research_agent.main start \
  --company "Example Company" \
  --product "Example Product" \
  --company-url "https://example.com/" \
  --product-url "https://example.com/product" \
  --instructions "Distinguish product claims from company-wide claims."

# Replace with the run ID printed by start:
uv run python -m research_agent.main resume --run-id orientation_YOUR_RUN_ID
```

Identical starting URLs are read once. A run completes only after both starting
pages have been read successfully, a nonempty profile with saved-source references
exists, and mapping succeeds. Missing information can remain unknown in the profile.
Failures exit with code 1 and preserve work. Resume opens the same SQLite thread and
uses saved captures, model responses, and tool results. It does not support legacy
`agent_system` jobs. Their data remains untouched.

Limits are persisted at start. Resume keeps the original model and configuration,
with two explicit limit overrides:

```bash
uv run python -m research_agent.main resume --run-id orientation_YOUR_RUN_ID \
  --max-model-calls 20 --input-allowance 160000
```

Increase the input allowance only if the configured model has enough context
capacity, including output tokens. Full evidence is never silently truncated.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | required | OpenAI credential; never stored in run metadata |
| `FIRECRAWL_API_KEY` | required | Firecrawl credential; never stored in run metadata |
| `OPENAI_MODEL` | required | Exact model name, persisted with the run |
| `RESEARCH_DATA_DIR` | `data` | Data root, relative to current directory; `--data-dir` overrides it |
| `RESEARCH_MAX_MODEL_CALLS` | `12` | Total logical model requests across start and resume |
| `RESEARCH_INPUT_ALLOWANCE` | `96000` | Conservative serialized UTF-8 input bytes plus 2048 framing reserve |
| `RESEARCH_OUTPUT_TOKENS` | `8000` | Maximum model output tokens |
| `RESEARCH_TIMEOUT_SECONDS` | `120` | Timeout for each provider attempt; Firecrawl server timeout also set |
| `RESEARCH_MAP_LIMIT` | `5000` | Maximum requested discovery entries |

OpenAI retries temporary failures once through its SDK. Firecrawl 4.54.0's
`max_retries=2` means two total attempts, retrying 502/transport errors. Other
Firecrawl failures stop for explicit resume. SDK retries may repeat paid calls;
the model-call limit counts logical requests, so at most two HTTP attempts per
request are possible. There is no outer retry loop or custom accounting framework.

## Outputs

Each run lives under `data/runs/<run_id>/`:

- `profile.md`: flexible, provisional company/product understanding and citations.
- `site_index.json`: deduplicated URLs, available titles/descriptions, counts and
  discovery limitations. Sitemap/subdomain mapping is broad, never exhaustive.
- `sources/`: original capture JSON (HTML, raw HTML, Markdown), complete cleaned
  Markdown, metadata, hashes, cleaning details, and retained failed captures.
- `run.json`: inputs, persisted configuration, request count and completion status.
- `checkpoints.sqlite`: framework-managed execution state with file references.
- `debug/`: model requests/responses, complete tool results and failure records.
- `system.md`, `user.json`: runtime-loaded prompt and skill snapshots for resume.

Run files contain research content and full debugging transcripts; keep them
private. `.env`, `.venv`, and `data/` are ignored by Git. Terminal output is centralized in `cli.py`: it shows skill loading, model calls,
tool parameters, compact results, and safe errors. HTTP request logs are suppressed.
Each model response shows input/output tokens and cumulative reported usage,
including saved responses on resume. Cached input and reasoning tokens are subsets,
not added again. Missing provider usage is marked unavailable; failed requests
without usage cannot be included. Long profile parameters show a short preview;
full content remains in the run files. Only one process
may operate on a run at a time (local Unix file lock).

## Offline verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/research_agent
uv run pytest
```

Tests use temporary directories, real SQLite checkpoints, fake model/page
responses, and fake HTTP transports. They do not call paid APIs. Live provider
availability and model permissions are not established by these tests.

Read [docs/design.md](docs/design.md) for the small implementation map, recovery
semantics, API references, and original commit provenance.
