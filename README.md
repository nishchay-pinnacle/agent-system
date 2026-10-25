# Pinnacle / agent-system

A local, synchronous application for researching **one campaign product** from
public web pages. LangGraph and SQLite save progress between model/tool steps;
JSON and original Markdown stay on disk. A separate read-only dashboard displays
saved events, sources, coverage, findings, gaps, usage and results.

## Setup

```bash
uv sync
```

uv manages Python 3.13 and the project `.venv`. Optionally copy `.env.example` to
`.env` and add `OPENAI_API_KEY` and `FIRECRAWL_API_KEY`. Existing environment
variables take precedence. Never put real keys in tracked files.

Research defaults to `OPENAI_MODEL=gpt-6.1-sol` and
`OPENAI_REASONING_EFFORT=medium`. The same model handles every phase. The application
never substitutes a model; unavailable models/configuration fail visibly. API access
and credentials must be supplied by you; implementation tests do not establish live
account access. Model, effort, instructions and limits are persisted with each job;
changing environment settings affects new jobs, not resumed ones.

## Research and dashboard: two terminals

Terminal 1 (this command makes real, potentially paid provider requests):

```bash
uv run python -m agent_system.worker research \
  --company "ExampleCo" --url "https://example.com" \
  --product "Product name" --product-url "https://example.com/product"
```

Replace those example inputs with your company/product. Repeat
`--source-url "https://public.example/source"` for extra leads. A single-product
company may use the same company and product URL. Public HTTP(S) pages only;
local/private addresses, credentials in URLs and PDF ingestion are rejected.
The worker prints its UUID and an exact dashboard command immediately.

Terminal 2:

```bash
uv run python -m agent_system.dashboard --job <job_id> --port 8765
```

Open `http://127.0.0.1:8765`. The standard-library server binds only to loopback,
polls saved files every 2 seconds, exposes explicit job-scoped GET routes, and
never calls providers or changes research. It remains useful after the worker exits.
Select sources to inspect saved text, expand timeline entries for tool arguments,
delivered line ranges and usage, and copy profile/result JSON. Source content is
escaped text. A stale saved `running` status is labeled “no recent activity,” not
proof of a live worker. Reading saved runs requires no internet access.

```bash
uv run python -m agent_system.worker status --job <job_id>
uv run python -m agent_system.worker resume --job <job_id>
```

Ctrl+C preserves completed work and reports `interrupted`. Resume retains identity,
settings, attempts, history and budgets, reuses saved fetches/operation receipts,
and continues recoverable progress. A per-job OS process lock prevents concurrent
research writers; dashboard readers hold no worker lock. An operation gets at most
one explicit transient retry; authentication/configuration errors never retry
automatically. Manual resume can retry a failed/uncertain attempt once, within the
same limits. Exhausted attempts remain blocked. Unknown IDs fail clearly.

## Research behavior and stop meanings

Orientation scrapes company/product URLs (deduplicated), maps the site, and allows
one extra overview scrape. Targeted investigations name a question, consequence,
source strategy and resolution criterion. The eight profile sections cover scope,
capabilities, problems/use cases, customer fit, people/roles, buying decisions,
distinctive expertise and customer language. Claims cite successfully saved sources;
coverage and prose preserve uncertainty, explicit statements versus inference,
product boundaries, restrictions and contradictions. Search snippets are leads.

Three unsuccessful attempts park a gap. Reopening requires a new named source and
reason while retaining history. Three consecutive investigations without progress
trigger final review. Final review can return to one named investigation followed
by a closing review. Fresh investigation contexts preserve findings and history
without carrying the entire prior transcript. Within an investigation, full
Responses protocol items (including encrypted reasoning items) are replayed locally;
private reasoning is never shown on the dashboard.

- `sufficient_understanding`: final review considers understanding sufficient.
- `public_sources_exhausted`: final review identifies limits of accessible evidence.
- `budget_reached`: terminal partial result; `limiting_resource` names the cap.
- `blocked_by_error` / `interrupted`: execution failed/stopped; not successful research.

Readiness is independently `ready` or `needs_review`. A critical unresolved product
boundary always requires review. Completed jobs, including budget stops, cannot be
reopened by resume. Missing coverage is retained rather than filled with inventions.

## Limits and accounting

| Limit | Default | Environment override |
| --- | ---: | --- |
| Generation attempts | 60 | `AGENT_SYSTEM_GENERATION_REQUESTS` |
| Firecrawl attempts (discovery and retries included) | 40 | `AGENT_SYSTEM_FIRECRAWL_REQUESTS` |
| Distinct successful pages | 30 | `AGENT_SYSTEM_PAGES` |
| Completed targeted investigations | 12 | `AGENT_SYSTEM_INVESTIGATIONS` |
| Retrieval actions per investigation (reads/cache hits included) | 6 | `AGENT_SYSTEM_INVESTIGATION_RETRIEVALS` |
| Total retrieval actions | 100 | `AGENT_SYSTEM_RETRIEVALS` |
| Cumulative active execution, seconds | 1800 | `AGENT_SYSTEM_ACTIVE_SECONDS` |
| Total input + output tokens | 500000 | `AGENT_SYSTEM_TOTAL_TOKENS` |
| Input tokens per generation | 48000 | `AGENT_SYSTEM_INPUT_TOKENS` |
| Maximum output tokens per generation | 6000 | `AGENT_SYSTEM_OUTPUT_TOKENS` |
| Individual request timeout, seconds | 90 | `AGENT_SYSTEM_REQUEST_SECONDS` |

Limits are starting safeguards, not quality guarantees or exact billing. SDK retries
are disabled (including Firecrawl's scrape auto-resume polling). Requests reserve
attempts, input + maximum output tokens and runtime **in a checkpoint before
sending**. Known usage reconciles the reservation. Reasoning tokens are already
included in output usage and are not added twice; cache tokens are reported when
returned. Firecrawl credits are shown only when returned by the SDK; otherwise
“not reported.” No dollar estimate or purported dollar cap is calculated.

The installed tiktoken version has no mapping for `gpt-6.1-sol`. For that model,
the documented Responses input-token counting endpoint counts the full request,
including tools and replayed items, with 1024 additional tokens reserved for
conservative overhead. If counting is unavailable, research blocks before the next
generation; the cap is never skipped. Known tiktoken model mappings may use a
conservative local tokenizer/UTF-8 byte upper bound plus the same overhead. Counting
attempts have a separate bound of `2 * generation_limit + 2` and are displayed.
Source excerpts use a conservative 3500-byte ceiling (therefore at most 3500
byte-BPE tokens), at most 200 lines, with explicit omissions and heading/line index.
Extremely long individual lines may be partially delivered; original text is saved.

**Crash uncertainty:** remote requests are not exactly-once. After a crash, an
in-flight attempt without a durable receipt retains its token/runtime reservation
and is marked uncertain; a retry may incur another charge. Provider usage, network
timeouts and a conservative reservation cannot guarantee exact invoice accounting.
Crashes can overcharge the local allowance, intentionally preventing a reset to
zero. Requests use timeouts bounded by remaining active allowance; waiting between
processes is not charged. DNS/remote redirect behavior also depends on the provider:
requested URLs are validated before fetching and returned final URLs are checked,
but Firecrawl performs the actual remote fetch/redirect handling.

## Saved files

Default root: repository `data/`. `AGENT_SYSTEM_DATA_DIR` can be absolute or relative
to the repository root. Use the same setting between commands. No directories or
connections are created during imports. This is the editable local `uv sync` setup.

- `checkpoints.sqlite`: authoritative LangGraph workflow state.
- `jobs/<job_id>/sources/<source_id>.md`: original fetched Markdown.
- `sources.json`, `profile.json`: source manifest and current eight-section profile.
- `investigations/*.json`: concise phase findings and investigation history.
- `events.jsonl`: append-only, increasing event sequences (partial trailing lines recover).
- `dashboard.json`: atomic projection derived from checkpointed state.
- `result.json`: terminal inputs, reason, readiness, coverage, gaps, interview questions,
  artifact references and usage; absent during errors/interruption.
- `operations/*.json`, `discoveries/*.json`: reusable provider receipts/discovery results.

Projections are rebuilt from saved workflow state when the worker resumes. Source
files and completed receipts can be reused after a crash; dashboard JSON does not
drive execution. Data, `.env`, `.venv` and tool caches are Git-ignored.

## Original placeholder demo

No API key is needed. Its behavior and persisted jobs are preserved:

```bash
uv run python -m agent_system.worker start --company "ExampleCo"
uv run python -m agent_system.worker status --job <job_id>
uv run python -m agent_system.worker resume --job <job_id>
```

Start writes `draft.json` marked `placeholder: true`, interrupts normally, and prints
a resume command. A new process resumes that same UUID and writes `result.json`.
Completed placeholder jobs do not rewrite output on repeated resume. Old jobs without
a research type use this workflow. The research dashboard requires a research job.

## Development and verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/agent_system
uv run pytest
```

Default tests use temporary directories and explicitly scripted provider boundaries;
they make no paid/provider network calls and never enable a production fake mode.
They exercise fresh-process resume, context boundaries/protocol continuity, caching,
limits, parked gaps, honest failures and real local dashboard HTTP routes. These
are simulated evidence tests, not proof of real research quality or live API access.

Firecrawl 4.54.0 has annotations but no `py.typed` marker. A narrowly scoped mypy
import exception for `firecrawl`/`firecrawl.*` isolates that SDK; application/provider
responses are validated with Pydantic. No global typing errors are disabled.

Official API references checked for this implementation:
[Responses tools](https://developers.openai.com/api/docs/guides/function-calling),
[reasoning item continuity](https://developers.openai.com/api/docs/guides/reasoning),
[token counting](https://developers.openai.com/api/docs/guides/token-counting),
[requested model](https://developers.openai.com/api/docs/models/gpt-6.1-sol), and
[Firecrawl Python SDK](https://docs.firecrawl.dev/sdks/python).

File uploads, private/internal sources, PDFs, transcription, browser automation,
interviews and other future research modules remain unsupported. Those needs belong
in unresolved gaps/interview follow-ups. No deployment or paid run is part of setup.
