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
Set `RESEARCH_CONTEXT_TOKENS` to your model's supported context capacity; the
128000 default is a configured ceiling, not model metadata.

Token counting uses tiktoken 0.14.0. Known models use its registered encoding;
unknown models fail explicitly. Set `RESEARCH_ENCODING` to a verified encoding
name if needed (for example `o200k_base` for supported GPT-5 models). The first
encoding load may download its public vocabulary; subsequent counting is local.
See the [official model mapping](https://github.com/openai/tiktoken/blob/main/tiktoken/model.py).

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
version-2 orientation:

```bash
uv run python -m research_agent.main research --run-id orientation_YOUR_RUN_ID \
  --model-calls 30 --page-budget 20 --input-token-limit 96000
```

Those three flags are required for explicit research and have the shown defaults
on `start`. They apply to research; orientation uses environment settings.
`--index-result-limit` defaults to 20. Model-call allowances are additional at
transition (maximum cumulative ceiling 1000); page budgets include failed fetches
but exclude cached reuse and SDK retries. Resume overrides are total ceilings:

```bash
uv run python -m research_agent.main resume --run-id orientation_YOUR_RUN_ID \
  --max-model-calls 60 --page-budget 30 --input-token-limit 110000 \
  --context-tokens 128000
```

Input estimates include instructions, retained exchanges, tool schemas, and framing
reserve. Output space is reserved inside context capacity. Encrypted reasoning is
opaque: each payload reserves the configured output maximum rather than tokenizing
ciphertext. These estimates are not provider usage. No compaction or silent
truncation occurs. Budget/context exhaustion leaves work incomplete and never
changes open questions to interview questions automatically.

## Research behavior

`update_questions` saves the full ordered question index and active open question.
The active question guides source selection, not what may be learned. Record useful
evidence for every affected question, including clearly labeled, cited inference.
Question order expresses priority. Answers are documented, inferred, or mixed.

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

Open [localhost dashboard](http://127.0.0.1:8765). It is read-only and uses no API
credentials. Start/resume remains in the CLI. The left pane follows activity only
while you are at the bottom. The right pane shows saved metrics, per-call usage,
questions in saved priority order, the profile, and full cleaned sources. Clicking
`[S1]` opens that source. Both panes scroll independently; polling preserves tab,
expansion, and scroll state. Unknown usage is shown as unavailable; cached input
and reasoning output are subsets, never added twice. Profile rendering supports
headings, paragraphs, and citations; arbitrary HTML is displayed as text.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_MODEL` | required | Exact provider model |
| `OPENAI_API_KEY`, `FIRECRAWL_API_KEY` | required for research | Provider credentials |
| `RESEARCH_DATA_DIR` | `data` | Data root; CLI `--data-dir` overrides |
| `RESEARCH_MAX_MODEL_CALLS` | `12` | Orientation model-call ceiling |
| `RESEARCH_INPUT_TOKEN_LIMIT` | `96000` | Estimated input-token ceiling |
| `RESEARCH_CONTEXT_TOKENS` | `128000` | Configured context capacity, including output |
| `RESEARCH_ENCODING` | model mapping | Explicit verified tiktoken encoding |
| `RESEARCH_OUTPUT_TOKENS` | `8000` | Maximum output tokens |
| `RESEARCH_TIMEOUT_SECONDS` | `120` | Provider attempt timeout |
| `RESEARCH_MAP_LIMIT` | `5000` | Discovery entry limit |

Old byte allowance options/settings are not interpreted as token values. OpenAI
retries once; Firecrawl permits two total attempts. No outer retry loop is added.
Logical reservations count failures, while SDK retries may incur extra charges.

## Saved artifacts and compatibility

Runs live in `data/runs/<run_id>/`:

- `run.json` (version 2), `profile.md`, `questions.json`, `source_index.json`, and
  `site_index.json`: state, findings, priorities, source IDs, and discovery leads.
- `sources/`: complete cleaned Markdown, captures, hashes, and failed captures.
- `events.jsonl`: sanitized CLI events used for incremental dashboard polling.
- `debug/`: requests, responses, full tool results, usage, context estimate, failures.
- `checkpoints.sqlite`, `research/checkpoints.sqlite`: same run ID, transcript
  references rather than large payloads. Research starts with orientation history.
- `system.md`, `user.json`, `research/start.json`, `research/user.json`: frozen
  instructions and durable handoff references. `research/completion.json` records
  successful completion after final artifact writes.

Legacy runs remain untouched and viewable with unavailable fields labeled, but
cannot resume under the new schemas or frozen prompts. Start a new run; there is
no automatic migration. `.env`, `.venv`, and `data/` are ignored. Run artifacts
contain private research/transcripts; only one writer may hold a run's local lock.

## Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/research_agent
uv run pytest
```

Tests use fake providers and temporary files with real SQLite persistence. They
make no paid calls. See [docs/design.md](docs/design.md) for recovery details.
