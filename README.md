# Pinnacle / agent-system

A local, synchronous application for researching **one campaign product** from
public web pages. LangGraph and SQLite save progress between model/tool steps;
JSON, original and cleaned Markdown, discovery inventory, and exact call snapshots
stay on disk. A read-only workbench displays calls, sources, question coverage,
focused investigation plans, findings, gaps, usage and results.

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
Calls is the default view: select an attempt, then inspect Overview, Input, Output
and JSON. Full prompts load on demand; count operations are an optional filter.
Profile & Plan shows the eight sections, 49 questions and focused backlog. Sources
separates fetched evidence (Raw/Cleaned line coordinates) from paginated inventory
search. Activity retains saved events. Copy controls expose profile/result and
request/response inspection JSON. All page/provider text is escaped. A stale saved `running` status is labeled “no recent activity,” not
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

Orientation scrapes company/product URLs (deduplicated), cleans their full content,
and maps the site before one normally sufficient model submission. It has no
exploration tools. The full cleaned pages, fixed question catalog, small discovery
summary and limits form the input; the inventory stays on disk. If this complete
request exceeds the input cap, the run stops with `limiting_resource=input_tokens`
before generation; content is never silently shortened. Missing or empty required
pages block orientation visibly. Targeted investigations name a question, consequence,
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

- `orientation_complete`: orientation-only run completed; profile remains provisional.
- `sufficient_understanding`: final review considers understanding sufficient.
- `public_sources_exhausted`: final review identifies limits of accessible evidence.
- `budget_reached`: terminal partial result; `limiting_resource` names the cap.
- `blocked_by_error` / `interrupted`: execution failed/stopped; not successful research.

Readiness is independently `ready` or `needs_review`. A critical unresolved product
boundary always requires review. Completed jobs, including budget stops, cannot be
reopened by resume. Missing coverage is retained rather than filled with inventions.

## Cleaning and result interpretation

Each scrape requests Markdown, HTML and raw HTML in one call without main-content
filtering. Cleaner v2 prefers usable HTML, then raw HTML, then conservative Markdown.
Structural rules remove confirmed interface controls while preserving substantive
prose, code, tables, hidden answers/variants, qualifiers and accessible labels. Complex
tables retain semantic HTML, displayed as escaped text in the dashboard. Removed
navigation links become searchable discovery leads, never evidence by themselves.
Reports record fallbacks and uncertainty; model deliveries contain only a small summary.
Inline images/SVG retain source-local references and archive locators, without media
fetches, OCR or inferred visual meaning. Unicode normalization is limited to documented
prose whitespace, line endings, leading BOM removal and NUL replacement with U+FFFD.

Ambiguous DOM components survive; legacy Markdown lacks structural signals. Content
loaded only after JavaScript interaction, diagrams and media may require further
inspection. Empty/loading/error captures block successful source delivery. Existing
cleaned filenames and bytes are honored; missing v1 copies use the original v1
algorithm. No automatic recleaning, historical output migration or research rerun occurs.

`result.json` projects every partial/unassessed assessment into `consequential_gaps`,
alongside unresolved explicit gaps. Completed plans do not resolve assessments.
`interview_questions` contains only explicit company-input questions on unresolved
items, deduplicated without fabricating questions. It can be empty while public-research
gaps remain. Readiness and stop meanings are unchanged. Research seeks substantive
product understanding: customer stories can supply relevant facts, but testimonials,
logos, named customers and quote collections are not investigation objectives.

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
| Orientation maximum output tokens | 12000 | `AGENT_SYSTEM_ORIENTATION_OUTPUT_TOKENS` |
| Site map candidate limit | 5000 | `AGENT_SYSTEM_MAP_LIMIT` |
| Firecrawl and token-count timeout, seconds | 90 | `AGENT_SYSTEM_REQUEST_SECONDS` |
| Model generation timeout, seconds | 300 | `AGENT_SYSTEM_GENERATION_REQUEST_SECONDS` |

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
Investigation/review excerpts use cleaned content with a conservative 3500-byte ceiling (therefore at most 3500
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
- `sources/<source_id>.html` and `.raw.html`: unmodified HTML captures, when returned.
- `sources/<source_id>.cleaned.v2.md`: canonical cleaned evidence; legacy `.cleaned.md` stays readable.
- `sources/<source_id>.cleaning.v2.json`: representation choice, hashes/counts, structural
  rule removals, warnings, navigation leads, asset locators and labelled JSON-LD metadata.
- `sources.json`, `profile.json`: source manifest with representation, cleaner version,
  SHA-256 hashes and byte/line statistics; current eight-section profile.
- `site_index.json`: normalized discovery leads, provenance and completeness summary.
- `orientation_context.json`: full orientation context, referenced rather than embedded
  in checkpoints; reused through corrections/retries.
- `orientation.json`: original provisional profile, 49 assessments and focused plan.
- `investigation_plan.json`: current backlog statuses, projected from checkpoints.
- `calls/<operation>_<attempt>/`: durable request, response, metadata and parsed
  application outcome. Full trace payloads never enter subsequent model context.
- `investigations/*.json`: concise phase findings and investigation history.
- `events.jsonl`: append-only, increasing event sequences (partial trailing lines recover).
- `dashboard.json`: atomic projection derived from checkpointed state.
- `result.json`: terminal inputs, reason, readiness, coverage, gaps, interview questions,
  artifact references and usage; absent during errors/interruption.
- `operations/*.json`, `discoveries/*.json`: reusable provider receipts/discovery results.

Active-run projections are rebuilt from saved workflow state when the worker resumes.
Completed outputs and historical source/call bytes are preserved. Source
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

## Orientation-only mode and plans

To stop after orientation (this command uses real, potentially paid providers):

```bash
uv run python -m agent_system.worker research \
  --company "ExampleCo" --url "https://example.com" \
  --product "Product name" --product-url "https://example.com/product" \
  --orientation-only
uv run python -m agent_system.dashboard --job <printed_job_id> --port 8765
```

Omit `--orientation-only` for a full research run. Mode is saved with the job;
resuming retains it. Completed orientation-only jobs do not run investigations or
convert to full runs. Their banner explicitly identifies a provisional profile.

All 49 question IDs must appear exactly once. Partial/unassessed questions require
at least one focused plan, with one question ID per plan; multiple plans may address
one question. Application-assigned plan IDs remain stable. Full runs must select
pending plans when starting investigations. A separate explicit, evidence-validated
assessment update changes question status; finishing an investigation does not.
Newly revealed gaps can add plans; superseding requires an explanation. The original
orientation snapshot stays unchanged while checkpointed assessment/backlog statuses
advance. Planned count is independent of the execution allowance, which remains 12
by default. Unfinished plans remain visible when the run stops.

Mapping includes subdomains and sitemap discovery supported by Firecrawl 4.54.0.
It discovers leads without scraping them. Reaching the requested cap marks the
inventory possibly truncated; fewer results still do not establish completeness.
`search_site_index(query, limit)` searches saved URL/title/description deterministically
(limit 1–20, default 10); it consumes retrieval allowance, not Firecrawl allowance.
User-supplied extra URLs remain leads during orientation. Web/map fallback remains
available in investigations.

Cleaning is versioned and conservative: normalize line endings/NULs, remove complete
script/style blocks and HTML comments outside fenced code, and collapse excessive
blank lines outside code. It preserves substantive prose, links, image alt text,
tables, caveats and examples. It does not promise universal cookie/navigation removal.
Only orientation bypasses the normal excerpt limits. Statistics are bytes/lines,
not per-block token estimates.

## Call traces and compatibility

Each attempt saves its request before dispatch, then its provider response and a
separate application outcome. Invalid function arguments do not erase the provider
output. Token-count calls and local counting are distinguished from generations,
and generations link to the count used for their reservation. Trace metadata uses
existing accounting; it is not another billing system. Transport timeout is recorded
separately. Request bodies include actual instructions, ordered inputs, schemas,
model/effort, output limit, cache/include/store settings and disabled truncation.
Credentials, headers and environment values are excluded. Research content is
preserved in these local debug artifacts, so handle them like the source material.

An interrupted attempt without a durable response remains unknown; retry attempts
have separate records and retain conservative reservations. Receipt reuse does not
create another call. The dashboard replaces opaque encrypted protocol fields with
labeled hashes; copied inspection JSON is therefore **not byte-for-byte exact**.
Original fields remain local for protocol replay. No private reasoning is decoded.

Legacy research defaults to `run_mode=full` and retains its persisted instructions,
conversation, budgets and original orientation-completion protocol. In-flight legacy
orientation is not restarted or upgraded to the new 49-question schema. Such runs
will not acquire a retrospective orientation plan. Historical raw excerpts and
receipts are unchanged. When the worker next delivers an old saved source, it writes
a cleaned companion without refetching; old cache keys cannot supply raw excerpts as
new cleaned deliveries. Dashboard reads never create cleaned files. Historical calls
without snapshots display “Exact call input was not recorded for this run.” New calls
made while resuming an old run are traced, but missing historical prompts remain missing.

## Synthetic dashboard preview

No API keys or network/provider calls are needed to create the preview:

```bash
uv run python tests/preview_dashboard.py
AGENT_SYSTEM_DATA_DIR=data/dashboard-preview uv run python -m agent_system.dashboard \
  --job 11111111-1111-4111-8111-111111111111 --port 8765
```

Open `http://127.0.0.1:8765`. The development fixture contains full cleaned evidence,
80 discovery leads, all 49 assessments/plans, token counting, and a failed generation
followed by a successful retry. It lives under ignored data storage and never enables
a production fake mode. Generation refuses to overwrite an existing preview; use
`--output /path/to/a/new/preview` and the printed dashboard command for another copy.
The synthetic fixed clock can intentionally appear stale. Simulated checks and this
preview do not establish live account access or real research quality.

Generation requests default to 300 seconds; Firecrawl and remote token counting
retain 90 seconds. Every attempt (including retries) is capped by the remaining
active-time allowance. The dashboard updates elapsed time and timeout every two
seconds while an attempt has no recorded outcome, showing configured and effective
budget-capped timeouts separately. An overdue unknown attempt is not proof that a
worker is still running. Finished attempts retain their recorded duration.

Both settings are persisted for new jobs. Existing jobs saved before the timeout
split retain their original shared timeout on resume; no saved research is rewritten.
The new default does not reopen a job that already exhausted its retry allowance.
