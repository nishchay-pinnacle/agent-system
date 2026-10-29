# Company research agent

One local agent reads the starting company/product pages, saves a provisional
profile and a small question index, maps the site, then automatically continues
into HTML research. Questions are dynamically prioritized; a source can inform
any question, including inactive ones. All exchanges survive handoff and resume.

## Setup

Requires Python 3.13 and uv:

```bash
uv sync --locked
# Create a template only if .env does not already exist:
test -e .env || cp .env.example .env
```

Set `OPENAI_API_KEY`, `FIRECRAWL_API_KEY`, and `OPENAI_MODEL` in `.env`. The exact
model must support Responses API strict tools and encrypted reasoning replay.
There is no default model or fallback. Shell variables take precedence over `.env`.

## Start and resume

These commands use paid APIs when valid credentials are configured:

```bash
uv run python -m research_agent.main start \
  --company "Example Company" --product "Example Product" \
  --company-url "https://example.com/" \
  --product-url "https://example.com/product"

uv run python -m research_agent.main resume --run-id orientation_YOUR_RUN_ID
```

`start --orientation-only` stops after the initial profile, question index, and
site map. Identical starting URLs are read once. To explicitly continue a completed
version-3 orientation:

```bash
uv run python -m research_agent.main research --run-id orientation_YOUR_RUN_ID \
  --model-calls 30 --page-budget 20 --max-input-bytes 1000000
```

The model-call and page-budget flags are required for explicit research.
`--max-input-bytes` defaults to 1,000,000 on start and research. These flags
apply to research; orientation uses environment settings.
`--index-result-limit` defaults to 20. Model-call allowances are additional at
transition (maximum cumulative ceiling 1000); page budgets include failed fetches
but exclude cached reuse and SDK retries. Resume overrides are total ceilings:

```bash
uv run python -m research_agent.main resume --run-id orientation_YOUR_RUN_ID \
  --max-model-calls 60 --page-budget 30 --max-input-bytes 2000000
```

The complete assembled request—including instructions, all retained exchanges,
tool definitions, and encrypted reasoning—is serialized as compact JSON and
measured in UTF-8 bytes. `max_input_bytes` defaults to **1,000,000 bytes**; configure
orientation with `RESEARCH_MAX_INPUT_BYTES`, research with `--max-input-bytes`, and
resume with the same flag. This is an application size limit, **not a guarantee
that the request fits the model context**. There is no tokenizer or reasoning-token
reserve. Actual input/output token metrics still come from API responses.

All history is retained. Exceeding the byte limit or receiving a provider context-
limit error leaves the run incomplete with saved progress. No compaction, silent
truncation, or automatic deferral of questions occurs.

## Research behavior

`update_questions` saves the full ordered question index and active open question.
The active question guides source selection, not what may be learned. Record useful
evidence for every affected question, including clearly labeled, cited inference.
Question order expresses priority. Answers are documented, inferred, or mixed.
Updates cannot omit existing question IDs. To merge, retain the original question
with status `merged`, set `merged_into` to a distinct non-merged question, and give
a `closure_reason`. Its evidence remains saved; the surviving question must be
answered or deferred before completion.

Assess only relevant, successfully read sources. Three distinct consecutive pages
without progress permit `needs_interview`; useful evidence resets that question's
streak. Duplicate assessments and failed/blocked reads cannot inflate the count.
Earlier deferral requires explaining exhausted plausible public leads. Use
“unresolved through public research.” Later evidence can reopen or answer questions.
Interview questions are a subset of this index, not a separate artifact.

`query_site_index` matches any search term across URL/title/description, ranked by
number of matched terms then URL. `required_phrase` requires an exact substring;
`url_contains` filters the entire decoded URL. Empty search browses. Pagination
reports counts and saved-source status; querying never scrapes pages.

`read_page` returns complete cleaned HTML evidence and a stable run-local source ID
such as `S1`. Repeated reads reuse it. Cite claims as `[S1]` in profiles and answers.
Reads remain limited to indexed/linked company pages and resources directly linked
from company evidence. No external search, additional agents, or PDF/media work.

`finish_research` validates the supplied final profile, question index, and reason,
saves both artifacts, and then records completion. No open questions are allowed.
Reference checks establish source existence/integrity, not factual truth.

## Local dashboard

```bash
uv run python -m research_agent.main dashboard
# Optional: --run-id orientation_YOUR_RUN_ID --port 8765 --data-dir data
```

Open [localhost dashboard](http://127.0.0.1:8765). It displays saved artifacts and uses no API
credentials. Start/resume remains in the CLI. The left pane follows activity only
while you are at the bottom. The right pane shows saved metrics, per-call usage,
questions in saved priority order, the profile, and full cleaned sources. Clicking
`[S1]` opens that source. Both panes scroll independently; polling preserves tab,
expansion, and scroll state. “Request size / byte limit” shows the application request size. Unknown usage is shown as unavailable; cached input
and reasoning output are subsets, never added twice. Profile rendering supports
headings, lists, tables, emphasis, links, and clickable citations using the existing
Markdown parser with raw HTML disabled. Images are rendered as alt text. Polling
waits for each cycle to complete before scheduling the next; run switches invalidate
in-flight results.

The model receives remaining call/page allowances and the application byte limit
on each turn. It can use `request_continuation` before exhaustion to explain why
more research is worthwhile and request additional calls/pages. The dashboard
shows this request with editable allowances and **Approve and continue** / **Keep
paused** buttons. Only the research process applies approved allowances, once;
all exchanges and budget messages remain in history. Existing completion checks
still apply. Approval does not change the byte limit or provider context limits.

Keep the research command running while awaiting dashboard approval. Closing the
browser does not lose the request. If the research process exits (including after
**Keep paused**), save approval in the dashboard and restart with the usual
`resume --run-id ...` command. If the model exhausts its calls without requesting
continuation, the run saves a runtime exhaustion notice and exits incomplete;
approve that notice and resume. This fallback does not invent an AI explanation.
No dashboard worker, background service, or automatic process launcher is added.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_MODEL` | required | Exact provider model |
| `OPENAI_API_KEY`, `FIRECRAWL_API_KEY` | required for research | Provider credentials |
| `RESEARCH_DATA_DIR` | `data` | Data root; CLI `--data-dir` overrides |
| `RESEARCH_MAX_MODEL_CALLS` | `12` | Orientation model-call ceiling |
| `RESEARCH_MAX_INPUT_BYTES` | `1000000` | Complete serialized-request UTF-8 byte limit |
| `RESEARCH_OUTPUT_TOKENS` | `16000` | Maximum output tokens |
| `RESEARCH_TIMEOUT_SECONDS` | `120` | Provider attempt timeout |
| `RESEARCH_MAP_LIMIT` | `5000` | Discovery entry limit |

Old token-limit and encoding settings are removed, not reinterpreted as bytes. OpenAI
retries once; Firecrawl permits two total attempts. No outer retry loop is added.
Logical reservations count failures, while SDK retries may incur extra charges.

## Saved artifacts and compatibility

Runs live in `data/runs/<run_id>/`:

- `run.json` (version 3), `profile.md`, `questions.json`, `source_index.json`, and
  `site_index.json`: state, findings, priorities, source IDs, and discovery leads.
- `sources/`: complete cleaned Markdown, captures, hashes, and failed captures.
- `events.jsonl`: sanitized CLI events used for incremental dashboard polling.
- `debug/`: requests, responses, full tool results, usage, request byte size, failures.
- `checkpoints.sqlite`, `research/checkpoints.sqlite`: same run ID, transcript
  references rather than large payloads. Research starts with orientation history.
- `system.md`, `user.json`, `research/start.json`, `research/user.json`: frozen
  instructions and durable handoff references. `research/completion.json` records
  successful completion after final artifact writes.

Version-1/2 runs remain untouched and viewable with unavailable fields labeled, but
cannot resume under the new schemas or frozen prompts. Start a new run; there is
no automatic migration. `.env`, `.venv`, and `data/` are ignored. Run artifacts
contain private research/transcripts; only one writer may hold a run's local lock.

## Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/research_agent
uv run pytest
# Optional standalone scheduler regression (Node.js; no packages):
node tests/dashboard_polling.cjs
```

Tests use fake providers and temporary files with real SQLite persistence. They
make no paid calls. See [docs/design.md](docs/design.md) for recovery details.
