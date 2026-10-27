# Company research design

One agent runs orientation and research using the existing model → tool LangGraph
loop. `main.py` coordinates automatic/explicit handoff and resume; `cli.py` handles
terminal output and appends sanitized activity to `events.jsonl`. Skills hold
research judgment. Tools implement validation, persistence, and stopping mechanics.

## Evidence and questions

Successful captures retain hash-based internal filenames and existing integrity
checks. `source_index.json` assigns contiguous stable IDs S1, S2, … to canonical
requested URLs. The index is saved before returning evidence. A replay after a
partial write reuses existing capture/metadata and assigns or reuses the short ID.
Failed and blocked captures never receive source IDs. Structural blocked-page
checks combine title and short main-content signals, including pages without h1.

`QuestionIndex` stores a nullable active question and ordered questions. Only an
open question may be active. Full replacements permit reprioritizing, merging,
adding, and reopening; existing IDs retain source assessment history. Omitting an
existing ID is rejected before saving or finishing. A merged record stays in the
index with status `merged`, a `merged_into` target, and closure reason. Targets must
exist, be distinct, and not themselves be merged (no chains/cycles). Finishing
requires all surviving questions to be answered or deferred. The optional merge
field defaults to null when absent; no files are migrated. Assessments
are deduplicated by source ID. A correction from unproductive to useful moves that
assessment to the end, resetting the streak; useful evidence cannot be overwritten
as an unproductive reread. Unmentioned questions do not acquire assessments. The
model decides relevance, usefulness, inference, and exhausted public leads; code
does not add semantic scoring or attempt to prove claims.

Each question's consecutive false assessments derive its unproductive-page count.
Skills permit deferral after three, or earlier with an explanation of exhausted
leads. Every deferral needs a closure reason. The active question does not gate
updates or answers for other questions. Answers and profiles share one citation
validator that resolves short IDs and verifies source hashes. Legacy path citation
validation remains for defense against malformed local links; new prompts use IDs.

`finish_research` validates the final profile and index before writes, rejects open
questions, saves profile and questions, and writes the completion marker last.
Those writes are individually atomic, not one transaction. A crash between writes
leaves an incomplete run; replay idempotently writes the supplied final artifacts.
Completed-run validation rechecks citations, questions, source integrity, and index.

## History, limits, and recovery

Version-3 runs retain every tool call/result and model item. Checkpoints store only
transcript file references. Orientation's initial profile no longer compacts history.
The version-3 research transition snapshots the completed checkpoint's history and
research instructions, then activates research. Its initial history contains all
orientation references plus a short transition message. No full profile/question
snapshot is injected on each request. Research uses its existing separate SQLite
file and the same run ID; filenames retain stage prefixes and cumulative call IDs.
A prepared transition resumes idempotently. Active research does not reset budgets.

The complete assembled request is serialized with `json.dumps(ensure_ascii=False,
separators=(",", ":"))` and measured as UTF-8 bytes. This includes every instruction,
message, tool call/result, definition, and encrypted reasoning payload. No token
estimation, output-space reserve, or reasoning reserve is added. The default
`max_input_bytes` is 1,000,000. `debug/context.json` records `request_bytes` and
`max_input_bytes`; the dashboard displays “Request size / byte limit”. This is an
application payload-size limit, not a model context guarantee. API response usage
remains the only source of token metrics. Over-limit requests stop before reserving
a model call; provider context-limit failures follow the existing incomplete-run
error path with history and progress preserved.

Model and page reservations are durable before provider calls. Existing saved
responses/results/captures are reused after interruption. Crashes between a paid
call and its durable result can still repeat calls; SQLite cannot provide external
exactly-once execution. Existing SDK timeouts/retries remain bounded. Budget/context
exhaustion, corruption, and provider errors preserve incomplete work. Correctable
tool input errors return to the model within the remaining allowance.

Legacy run versions are rejected before parsing their old settings or executing
frozen prompts. No data migration, reset, or token-to-byte reinterpretation occurs.
The dashboard reads raw saved metadata so legacy artifacts remain viewable.

## Dashboard

`dashboard.py` serves one static HTML document and three JSON GET endpoints on
127.0.0.1 only: run listing, current state, and incremental events. Host validation
blocks DNS rebinding, no CORS is enabled, and no write endpoint exists. Run IDs and
artifact paths use the existing traversal/symlink protections. Only explicit saved
artifact types are exposed; credentials, captures, arbitrary debug files, and
SQLite files are not served. Scraped content is inserted as text. Profiles use the installed Markdown parser
with raw HTML disabled and tables enabled; images are replaced with alt text and
links are restricted to HTTP(S)/mailto. Only this rendered output is inserted as
HTML; citation buttons are attached to text nodes outside links and code.

The browser schedules its next poll one second after the prior cycle completes,
uses generation IDs to reject results after run switches (including A → B → A), and preserves tab/filter/expansion/scroll state, and
follows activity only near its bottom. Source citations select saved sources.
Metrics reuse response usage and Firecrawl counters; no secondary usage store is
introduced. Cached and reasoning tokens are displayed as subsets. Missing usage
and legacy counters remain unavailable, with response coverage shown separately.

## Existing scope

HTML cleaning, URL authorization, source hash validation, atomic writes, local run
locking, provider boundaries, and CLI accounting are reused. Search is local any-
term substring matching ranked by matched-term count then URL, with required phrase,
whole-URL filter, and pagination. It never fetches its results. Company scope and
linked-resource restrictions remain unchanged. No subagents, external search,
PDF/media support, services, queues, migrations, or new execution framework.
