# Pinnacle / agent-system

A local Python application for researching **one campaign product**. New version-three
runs use full-page orientation, a coordinator, and up to three concurrent investigators.
One worker owns canonical state; LangGraph/SQLite checkpoints reference durable local
revision files, complete cleaned evidence, findings, and exact Responses call traces.
The System dashboard displays execution and accepts narrow worker-applied commands.

## Setup

```bash
uv sync
```

uv manages Python 3.13 and the project `.venv`. Optionally copy `.env.example` to
`.env` and add `OPENAI_API_KEY` and `FIRECRAWL_API_KEY`. Existing environment
variables take precedence. Never put real keys in tracked files.

New research roles default to `gpt-6-luna` with `medium` reasoning. The explicitly
requested quality review defaults to `gpt-6.1-sol` / `medium`. Model IDs are never
silently substituted. API/model access is not established by the fake-provider tests.

`OPENAI_MODEL` and `OPENAI_REASONING_EFFORT` explicitly override the three research
roles. Per-role `AGENT_SYSTEM_ORIENTATION_MODEL`, `AGENT_SYSTEM_COORDINATOR_MODEL`,
`AGENT_SYSTEM_INVESTIGATOR_MODEL` (and corresponding `_EFFORT` settings) take precedence.
Quality uses `AGENT_SYSTEM_QUALITY_MODEL` / `AGENT_SYSTEM_QUALITY_EFFORT` separately.
The worker prints resolved research models before execution. Settings are saved once;
changing environment values never changes a resumed job.

If an existing `.env` contains `OPENAI_MODEL=gpt-6.1-sol`, change it to
`OPENAI_MODEL=gpt-6-luna` for normal testing, keeping `OPENAI_REASONING_EFFORT=medium`.
Alternatively prefix the research command with those two environment assignments.
The implementation does not rewrite your `.env`.

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

Open `http://127.0.0.1:8765`. System is the initial view for active new runs. It shows
orientation, a logical coordinator, batches of investigators, separate answer/work
coverage, used/reserved allowances, and human decisions. Select an agent or question
for findings, missing information, sources, attempts, and exact-call links. Completed
batches collapse; selection and filters persist during polling. Calls, Sources,
Profile & Plan, and Activity remain available. All research content is escaped.

```bash
uv run python -m agent_system.worker status --job <job_id>
uv run python -m agent_system.worker resume --job <job_id>
```

The worker remains available while awaiting approval. When it is offline, dashboard
decisions are saved and labelled **Approved — waiting for worker** until resume applies
them. The dashboard never starts a worker or calls a provider. Writes require the
same loopback origin and CSRF token and enter a job-local command inbox; only the
locked worker applies them. Repeated submissions cannot apply an amendment twice.

## New research execution and limits

Orientation supplies complete cleaned overview pages, campaign boundaries, all 49
stable questions, and compact discovery metadata. Its original snapshot is preserved.
`--orientation-only` stops there. Otherwise the coordinator groups related questions
into assignments, validates prerequisites, runs an explicit batch, and incorporates
settled results before creating another batch. Investigators save incremental findings
through normal tool submissions, with no summarizer calls. They cannot edit canonical
profile/questions. Newly discovered questions receive stable IDs; merged questions keep
old IDs and history. Completing an investigation never automatically answers a question.

Contexts start fresh from relevant saved state for each investigator/coordinator cycle.
Complete cleaned pages are retrieved on demand; active Responses protocol items remain
intact. No automatic chunking, excerpting, compression, or truncation is used for v3.
Shared successful sources are reused; simultaneous fetches share one provider request.
Failed/uncertain attempts remain recorded. Checkpoints and receipts prevent replaying
known completed work, but do not guarantee exactly-once remote execution.

| New-run resource | Default |
| --- | ---: |
| Concurrent investigators / launched investigations | 3 / 12 |
| Generation attempts, all roles | 120 |
| Input + output tokens / input per call | 2,000,000 / 96,000 |
| Firecrawl attempts / successful captures | 80 / 60 |
| Active execution seconds | 3,600 |
| Per-investigator generations / new pages / tool actions | 12 / 6 / 40 |
| Run-wide tool actions | 500 |
| Orientation / coordinator / investigator / quality output ceilings | 12,000 / 16,000 / 8,000 / 12,000 |

Existing retrieval/generation timeouts remain 90/300 seconds. Appropriate transient
failures get at most one retry, charged to the same assignment. Token counts have a
separate `2 × generation limit + 2` bound. Usage includes conservative reservations and
unknown charges; it is not an exact invoice or dollar cap. Active time measures elapsed
execution once across concurrent calls, excluding approval waiting and stopped workers.

A limit pauses a justified blocked action, rather than completing the run. Requests
state the action, missing information, expected value, and used/reserved allowances.
A 25% increase is only a suggestion (or the immediate shortfall if larger). Approve it,
enter another positive increase, decline, or finish with current findings. Limits change;
usage/history never reset. Local declines retain incremental findings and prevent
immediate equivalent reassignment. Global declines leave affected work paused.
Provider-capacity errors cannot be fixed by approving more application budget.

Finalization has one bounded coverage check and no recurring return-investigation loop.
Every unresolved original/added question enters the interview backlog, including partial
answers, with explicit missing information and a reason. User-finalized results require
no closing model call and retain unincorporated findings separately.

## Optional Sol quality review

After finalization, select a completed batch in System and choose **Preview Sol quality
check**. The default is the earliest completed batch with the highest-priority work.
The preview shows the batch, questions, complete sources, model, and allowances. Only
**Run Sol quality check** queues execution. Resume the worker if it has exited.

The review freezes the profile/question index and selected batch, uses complete saved
supporting sources, and makes one generation with at most one transient retry. It uses
normal accounting/approvals without scraping, discovery, or changing finalized research.
Its separate report identifies evidence-linked corrections and explicitly limits its
claims to the supplied scope. Applying corrections or rerunning research is outside v1.

## Historical compatibility

Versions 1 and 2 retain their original sequential workflow, settings, excerpt delivery,
review/return policy, and terminal budget stops. Resume does not upgrade or reopen them.
Historical traces and results are unchanged. New CLI research uses version 3; the internal
legacy initializer remains available for compatibility tests. Missing System fields are
handled without inventing historical execution.

## Cleaning and result interpretation

Each scrape requests Markdown, HTML and raw HTML in one call without main-content
filtering. Cleaner v3 prefers usable HTML, then raw HTML, then conservative Markdown.
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
algorithm. Existing v2 copies are preserved; a missing v2 copy must be restored
from backup rather than regenerated under new rules. No automatic recleaning, historical output migration or research rerun occurs.

For legacy runs, `result.json` projects every partial/unassessed assessment into `consequential_gaps`,
alongside unresolved explicit gaps. Completed plans do not resolve assessments.
`interview_questions` contains only explicit company-input questions on unresolved
items, deduplicated without fabricating questions. It can be empty while public-research
gaps remain. Readiness and stop meanings are unchanged. Research seeks substantive
product understanding: customer stories can supply relevant facts, but testimonials,
logos, named customers and quote collections are not investigation objectives.

## Legacy limits and accounting

The following table and excerpt/runtime rules describe versions 1–2 only. New-run
limits and approval behavior are documented above; saved historical limits win.

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
- `sources/<source_id>.cleaned.v3.md`: canonical cleaned evidence; legacy `.cleaned.md` stays readable.
- `sources/<source_id>.cleaning.v3.json`: representation choice, hashes/counts, structural
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
one question. Application-assigned plan IDs remain stable. Legacy full runs select
pending plans when starting investigations; version-three coordinators regroup these suggestions. A separate explicit, evidence-validated
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
In versions 1–2, only orientation bypasses the excerpt limits. Version 3 delivers complete cleaned pages for all research roles. Statistics are bytes/lines,
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

## Coordinated synthetic dashboard previews

These commands use fake providers only and refuse to overwrite an existing preview:

```bash
uv run python tests/preview_dashboard.py --scenario paused --output /tmp/pinnacle-paused
uv run python tests/preview_dashboard.py --scenario completed --output /tmp/pinnacle-completed
uv run python tests/preview_dashboard.py --scenario active --output /tmp/pinnacle-active
```

Each prints its dashboard command. Completed includes a separate quality review;
paused includes multiple batches and a pending decision. Active captures an execution
boundary with an offline worker, so it must not pretend execution is still live.

Version-three artifacts include immutable `state/revision_*.json` references from
SQLite, `contexts/`, `operations/`, `question_index.json`, `reviews/`, the local
`commands.sqlite` inbox, and a `worker.json` liveness projection. Preserve these together
when backing up a job. The original result stays frozen when a quality review runs.

Future explicitly authorized live orientation trial (not part of verification):

```bash
OPENAI_MODEL=gpt-6-luna OPENAI_REASONING_EFFORT=medium \
uv run python -m agent_system.worker research --orientation-only \
  --company "ExampleCo" --url "https://example.com" \
  --product "Product name" --product-url "https://example.com/product"
```

Replace example inputs before use. Omit `--orientation-only` only when authorizing the
full research budget. A quality check additionally requires its explicit dashboard action.
