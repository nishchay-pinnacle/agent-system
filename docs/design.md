# Orientation-only design

Original branch: `main`. Original commit:
`d9eaad6b1be4963f202757e9eba2aeba07516bfb`.
Rebuild branch: `rebuild/orientation`. The original history is preserved and the
rebuild is not merged into main. Old run data is neither deleted nor migrated.
Retrieve an original file with `git show <original-sha>:<path>`.

## Reading the implementation

1. `main.py` validates environment settings and starts/resumes one run.
   `cli.py` owns argument parsing and all terminal presentation: model calls and
   reported token totals, tool arguments/results, skills, completion and errors.
   Usage totals are reconstructed from existing response records, not new state.
2. `agent.py` loads the system prompt, user template, and orientation skill;
   snapshots them; and builds a two-node LangGraph loop: model → tool → model/end.
   These nodes belong to one agent. There is no separate orientation agent.
3. `tools.py` implements only `read_page`, `update_profile`, and `build_site_index`,
   with strict argument validation and narrowly scoped Firecrawl calls.
4. `storage.py` owns run-local path validation, atomic writes and a process lock.
5. `cleaning.py` and `html_cleaning.py` preserve evidence while removing known UI
   controls. The larger HTML cleaner is deliberately reused rather than replaced
   with a smaller but lossy regex cleaner.

The model selects tool calls following the loaded skill. The runtime restricts
reads to the two canonical input URLs, requires source references before saving
a profile, and requires a profile before indexing. It stops immediately after
all completion predicates hold. Tools cannot choose output destinations.

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
file references and a turn counter, not entire research outputs. Full model input
is reconstructed from saved records with no custom compaction or truncation.
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

## Verified APIs

Installed versions during rebuild: `openai 3.37.0`, `firecrawl-py 4.54.0`,
`langgraph 1.2.16`, `langgraph-checkpoint-sqlite 3.1.2`. Existing dependency versions
were retained; uv regenerated the lockfile after the package rename and removal
of unused tokenization dependencies.

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling):
  Responses input/output items, strict tools, required tool choice, one tool per
  turn, and reasoning item replay. The installed SDK's typed request adapter and
  mocked HTTP test verify the actual request shape.
- [Firecrawl Python SDK](https://github.com/firecrawl/firecrawl/blob/main/apps/python-sdk/README.md):
  installed `FirecrawlClient.scrape`/`map` signatures and source were inspected for
  formats, `sitemap="include"`, `include_subdomains=True`, millisecond server
  timeouts, HTTP timeout and total-attempt retry semantics. The existing official
  distribution `firecrawl-py` is retained; no guessed package replacement is used.
- [LangGraph SQLite saver](https://reference.langchain.com/python/langgraph.checkpoint.sqlite):
  installed `SqliteSaver.from_conn_string` context manager and same-thread
  `invoke(None, config)` resume behavior, verified using fresh SQLite runtimes.
