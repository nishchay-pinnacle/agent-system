# Company research design

Original branch: `main`. Original commit:
`d9eaad6b1be4963f202757e9eba2aeba07516bfb`.
Rebuild branch: `rebuild/orientation`. The original history is preserved and the
rebuild history is retained in main. Old run data is neither deleted nor migrated.
Retrieve an original file with `git show <original-sha>:<path>`.

## Reading the implementation

1. `main.py` validates environment settings and starts/resumes one run or explicitly activates research.
   `cli.py` owns argument parsing and all terminal presentation: model calls and
   reported token totals, tool arguments/results, skills, completion and errors.
   Usage totals are reconstructed from existing response records, not new state.
2. `agent.py` loads the system prompt, user template, and stage skill;
   snapshots them; and builds a two-node LangGraph loop: model → tool → model/end.
   These nodes belong to one agent. There is no separate orientation agent.
3. `tools.py` implements shared `read_page`/`update_profile`, orientation-only
   `build_site_index`, and research-only `query_site_index`/`finish_research`, with
   strict argument validation and narrowly scoped Firecrawl calls.
4. `storage.py` owns run-local path validation, atomic writes and a process lock.
5. `cleaning.py` and `html_cleaning.py` preserve evidence while removing known UI
   controls. The larger HTML cleaner is deliberately reused rather than replaced
   with a smaller but lossy regex cleaner.

The model selects tool calls following the loaded skill. During orientation the runtime restricts
reads to the two canonical input URLs, requires their source references before
saving a profile, and requires a profile before indexing. Research reads indexed
or linked company pages and external URLs linked directly from company evidence.
Tools cannot choose output destinations. Research stops immediately after explicit
`finish_research`; orientation still stops immediately after mapping.

## Reuse and removal

Reused the original structural HTML cleaner, Markdown block-aware normalization,
URL validation/source hashes, structural error-page detection, representation
fallback approach, and URL inventory merging. Preserved cleaning regression
cases and fixtures. Atomic writes and local run locking follow the original
approach, now isolated from the old schemas.

Removed tracked `agent_system` application code and legacy workflow tests. The
coordinator, investigators, 49-question contract, fixed profile schema, approvals,
dashboard, and legacy compatibility code are absent. The new agent, CLI, storage
contract and integration tests were rebuilt for this smaller scope.

## Checkpoint and failure semantics

SQLite uses the run ID as LangGraph's thread ID. Graph state contains transcript
file references and a turn counter, not entire research outputs. Before the first
successful profile save, active context contains the complete starting-page tool
results. In orientation, that save is the sole compaction boundary: after validating both sources,
the runtime atomically writes `context/initial-profile.v1.json` with version 1,
the exact saved profile, source references and a deterministic progress receipt.
It then checkpoints this snapshot reference as `baseline` and clears active
`history`. No model call is used for compaction.

The next request contains the unchanged system/skill, original user request,
tool definitions/settings, and one assistant message containing the snapshot.
Superseded tool exchanges and reasoning items are absent; new exchanges accumulate
after the baseline. The full original requests, responses, tool results and sources
remain archived unchanged. The existing input allowance checks the rebuilt request;
there is no truncation.

The snapshot has a stable run-local name and is never regenerated from a later edit
to `profile.md`. It is saved before its checkpoint reference, so boundary replay
reuses the same snapshot and saved successful tool result. Rejected or failed
profile saves do not compact. Missing/invalid selected snapshots pause recovery;
unsupported snapshot versions are rejected explicitly.

Older orientation checkpoints lacking `baseline` keep their original history.
A pending successful profile tool can establish the new boundary; checkpoints
already past that save continue unmodified without retroactive compaction. There
is no reset or migration of original transcripts. Pre-rebuild legacy jobs remain
unsupported as before.
Model requests and raw responses are saved before tools execute; successful tool
results are saved before their checkpoint. A cached model response prevents
regenerating a pending tool request on resume. Captures and the index are reused
when a tool must reexecute after a checkpoint interruption. Profile replacement
is atomic and idempotent. Failed captures are archived so explicit resume may
fetch a new capture; successful captures remain reusable.

A model-call reservation is saved before the external call. An interrupted call
may consume a reservation without a saved response. A crash between a successful
external action and its local save can still repeat that paid action on resume;
SQLite cannot make external APIs exactly-once. SDK retries are also bounded but
may incur additional charges. A completed run opens without external calls.

Input allowance is a conservative byte-based guard, not exact provider token
accounting: UTF-8 serialized request bytes plus a 2048 framing reserve. It checks
both each full page and the combined prompt/transcript/tool schemas. Exceeding it
stops visibly without dropping evidence. A provider can still reject its own
context/output limits. Incomplete model outputs never execute partial tool calls.

Missing/failed sources, index failure, exhausted limits and provider errors leave
the run incomplete. Correctable tool argument/prerequisite errors are returned as
tool results so the model can retry within the existing call budget. Reopening
validates saved source files and recorded hashes, plus index structure; corruption
pauses for recovery rather than reporting success. Prompts ask for useful uncertainty;
unknown answers are not execution failures. The runtime validates citation paths
for both input sources, but does not prove the model's prose is factually correct.

## Research continuation

Run `uv run python -m research_agent.main research --run-id orientation_YOUR_RUN_ID
--model-calls 30 --page-budget 20 --input-allowance 160000` (on one shell line).
The existing two-node graph runs the research skill with stage-specific tools.
The model picks material gaps, queries discovery, reads full cleaned evidence,
replaces the flexible profile, and explicitly finishes. No second agent or
research-specific graph is introduced.

`Run.stage` defaults to orientation for existing version-1 records. Explicit
continuation first validates completed orientation artifacts, then atomically
saves `research/start.json` before selecting research in `run.json`. The snapshot
contains version, original orientation run metadata, original task inputs rendered
with the shared user template, research instructions, exact current profile,
validated source references, runtime receipt and explicit allowances. It excludes
the obsolete orientation-only user suffix and compaction receipt. A prepared
transition resumes idempotently with its saved allowances; an already active stage
never resets limits. Unknown versions fail explicitly; legacy jobs remain unsupported.

Research uses `research/checkpoints.sqlite` with the same run ID and an empty
initial history. Orientation's database, prompt snapshots, context snapshot and
transcripts remain unchanged. Each research request starts with the frozen task
and one assistant message containing the research receipt, profile and references,
then all research exchanges. It does not inject the full index or later mutable
profile contents into that baseline. Research updates never compact history.
`debug/research-turn-*` avoids orientation turn collisions; `debug/call-*` remains
cumulative for usage accounting. Selected research instructions remain stable on
resume, even after code or profile edits.

Index search is local, deterministic AND-substring matching (quoted phrases),
with a decoded-path substring filter, sorted URLs, pagination and validated cache
indicators. It returns discovery metadata, never new evidence. Read authorization
uses index entries and links extracted from saved Markdown or cleaner discovery
metadata. Company scope strips a leading `www.` from the supplied hostname and
includes its subdomains; it does not infer a registrable parent domain. External
URLs must be linked directly from company evidence (including supplied starting
pages). Redirect targets must satisfy public-URL validation and scope. Real
Firecrawl requests retain existing public DNS validation before and after capture;
redirect enforcement is post-capture, not control of Firecrawl's network routing.

A research fetch reservation is saved before each new logical scrape, including
failures; cached captures/sources consume no new budget. Unsupported extensions
can be rejected before fetching; unsupported or blocked captures return recoverable
results and remain archived without becoming source evidence. Provider/storage
failures and invalid source hashes pause instead. Successful tool results and
provider responses replay through the existing recovery path. The same external
call crash window and SDK-retry limitations apply as in orientation.

Research start explicitly adds model calls to the cumulative count and sets a
research fetch budget and input allowance. Resume `--max-model-calls` and
`--page-budget` replace total ceilings; `--input-allowance` replaces the byte guard.
Exhaustion preserves incomplete work. Completion requires `research/completion.json`,
written by `finish_research` with a short reason; there is no closing model call.
Research profiles need valid local citations but need not cite every saved source.
Original revisions remain in archived model tool arguments. Content correctness,
coverage and the decision to finish remain model judgments, tested offline for
mechanics rather than factual research quality.

## Verified APIs

Installed versions during rebuild: `openai 3.37.0`, `firecrawl-py 4.54.0`,
`langgraph 1.2.16`, `langgraph-checkpoint-sqlite 3.1.2`. Existing dependency versions
were retained; uv regenerated the lockfile after the package rename and removal
of unused tokenization dependencies.

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling):
  Responses input/output items, strict tools, required tool choice, one tool per
  turn, and reasoning item replay. The installed SDK's typed request adapter and
  mocked HTTP test verify the actual request shape. The [conversation state guide](https://developers.openai.com/api/docs/guides/conversation-state)
  and installed `EasyInputMessageParam` confirm ordinary user/assistant text inputs;
  the snapshot starts a fresh input rather than replaying incomplete tool pairs.
  Compaction tests validate each rebuilt request against the installed SDK.
- [Firecrawl Python SDK](https://github.com/firecrawl/firecrawl/blob/main/apps/python-sdk/README.md):
  installed `FirecrawlClient.scrape`/`map` signatures and source were inspected for
  formats, `sitemap="include"`, `include_subdomains=True`, millisecond server
  timeouts, HTTP timeout and total-attempt retry semantics. The existing official
  distribution `firecrawl-py` is retained; no guessed package replacement is used.
- [LangGraph SQLite saver](https://reference.langchain.com/python/langgraph.checkpoint.sqlite):
  installed `SqliteSaver.from_conn_string` context manager and same-thread
  `invoke(None, config)` resume behavior, verified using fresh SQLite runtimes.
