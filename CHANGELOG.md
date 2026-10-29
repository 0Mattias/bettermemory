# Changelog

Notable changes between releases. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely. From 1.0
onward the project uses semver in the standard way: major bumps for
breaking changes, minor for additive features, patch for fixes. The
[compatibility contract](CONTRIBUTING.md#versioning-and-the-compatibility-contract)
spells out exactly what's stable.

## 9.0.0 - Unreleased

A major by the [compatibility contract](CONTRIBUTING.md#versioning-and-the-compatibility-contract),
and the first one to change the store. The markdown directory with a
derived index becomes one SQLite file whose tables are the fold of a
hash-chained log; the twenty-seven tools, eighteen of them registered
by default, become nine; ten off-by-default flags leave with their
code; and the modules that served the removed surfaces leave with
them. An 8.x directory is imported once by
`bettermemory migrate v8` and never written again; `bettermemory export
--mirror` writes the store back out as that directory, byte for byte.

A 9.0.0 release candidate (`9.0.0rc1` and later) is a PEP 440
pre-release, so `pip install bettermemory`, `uv tool install
bettermemory` and the plugin's `uvx bettermemory` keep resolving 8.0.0
until the stable `9.0.0` is tagged. Ask for a candidate by version
(`uvx bettermemory==9.0.0rc1`) to run it.

### 9.0.0rc14

The fourteenth candidate is the first cut under the rule that the stable
9.0.0 ships when no feasible foreseeable work is left: from here on the
work the plan of 2026-10-28 tracks lands in candidates. This one breaks
the import cycle between the recall and hook modules, so the module
split can build on it; takes every mutating store call under the write
lock before it reads what it decides on, so two writers can no longer
lose an update or bring a removed record back; and closes a scan on the
recall path that a crafted body could hold for seconds. The store's
schema gains one index, added at the first open of an older store. It
is a release candidate like the thirteenth; the stable 9.0.0 follows
the candidates' testing.

#### Changed

- **`recall` no longer imports `hook`.** The session join
  (`bridged_session`, `joined_sessions`, `event_joined`,
  `harness_joined_sessions`, `live_harness_sessions`, `hook_spans`,
  `runs_beside` and their helpers) moves to a new `bettermemory.sessions`,
  which imports only `events` and `time_utils`. The probe both hooks run
  (`_probe_message`) and the interrupted-turn settlement
  (`settle_interrupted`) move into `recall`, and the caller's origin as a
  hook captures it becomes `origin.capture_caller`. `recall` imported
  `hook` inside four functions, `prompt_recall` among them, while `hook`
  imported `recall` inside three: a cycle the module split could not
  build on, and a hook import paid on every prompt recall. `recall` now
  imports neither `hook` nor any module that does, and
  `tests/test_import_graph.py` pins that rule, the rule that `sessions`
  imports neither, and the absence of any module-level import cycle in
  the package. `hook` keeps the session-join names importable under
  their old path for a script outside the package; the package and its
  tests read them from `sessions`, and a patch on `hook`'s binding
  reaches the Stop alone. The probe and the settlement leave `hook` for
  the recall family (`recall_settle.py`, below) with no re-export, so a
  hook process still imports `recall` only where it calls it. The
  probe dates its report at
  the caller's instant: the Stop's clock reading as before, prompt
  recall's own `now`, and the settlement's `now` for an interrupted turn.
  A test that places the hooks' caller patches `origin.capture_caller`,
  and one that spies on the probe patches `recall_settle.probe_for_miss`;
  `hook.capture_origin` and `hook.probe_for_miss` are gone. Importing
  `bettermemory.hook` measures 203 ms before and 195 ms after, and
  `bettermemory.recall` 253 ms and 251 ms (median of three cold runs
  each). (`src/bettermemory/sessions.py`, `src/bettermemory/recall.py`,
  `src/bettermemory/hook.py`, `src/bettermemory/origin.py`)
- **The location capture's readers move to `location_git`.** `location.py`
  was 2,648 lines, over the 2,000-line bound for a module. The vocabulary
  and bounds, git's discovery and config reader, the `Capture` model with
  its token codec, the HEAD, ref, stamp and manifest readers and the
  history reader (`RepositoryHistory`, `walk`) now live in
  `location_git.py` (954 lines), unchanged. `location.py` (1,789 lines)
  keeps the capture, its cache, the witness exchange and the resolution,
  and re-exports every moved name a caller reads, so no import path
  changes. The new module imports only the standard library and the
  readers the hook client already loads, and the hook client's import
  allow-list names it. (`src/bettermemory/location.py`,
  `src/bettermemory/location_git.py`, `tests/test_membership_capture.py`)
- **`health.py` is split into four modules, with no import path changed.**
  The report model (the dataclasses, caps and shared predicates) moves to
  `health_model.py`, the event-log accumulator, the recommendations and
  the rollup helpers to `health_stats.py`, and the text and JSON
  renderers to `health_render.py`. `compute_health`, the cross-repo and
  commit-drift rollups, the curation counts and `report_for_store` stay
  in `health.py`, which goes from 4,776 lines to 1,861. Every function
  and class moves unchanged, and `health` re-exports every public name
  and every private name read outside the family, so
  `from bettermemory.health import ...` resolves as before; the report
  and the CLI output do not change. (`src/bettermemory/health.py`,
  `src/bettermemory/health_model.py`, `src/bettermemory/health_stats.py`,
  `src/bettermemory/health_render.py`)
- **The eval module is split into three files.** `eval.py` was 4,040
  lines. The pieces the rates are built from (`RateCI` and the Wilson
  interval, the row dataclasses, the threshold and widening rule
  registries, the widening lanes' shared walk, the `--since` and
  `--until` parsing, the recall surfaces' split on each show's rule, and
  the tool-usage kind map) move to `eval_model.py`, and the text and
  markdown renderers to `eval_render.py`. `eval.py` (1,846 lines) keeps
  the report classes, the admin-recorded classification and every
  `compute_*`, and re-exports every name a caller imports from
  `bettermemory.eval`, so no import path changes and every report and
  JSON payload reads byte for byte as before. `test_eval`'s twin-count
  scan now reads every `eval*.py`. (`src/bettermemory/eval.py`,
  `src/bettermemory/eval_model.py`, `src/bettermemory/eval_render.py`,
  `tests/test_eval.py`, `docs/eval.md`)
- **`store.py` is split into eleven modules, with no import path changed.**
  `store.py` was 6,481 lines, over the 2,000-line bound for a module. The
  vocabulary, the table texts, the errors and the paths a store opens by
  move to `store_schema.py`, the row codecs and `_apply_mutation` to
  `store_rows.py`, and the key checks and the key ring's binding to
  `store_keys.py`. The `Store` methods move by area to `store_records.py`
  (records, standing answers, tombstones and the corroboration check),
  `store_journal.py` (the episode journal), `store_tables.py`
  (verifications, conflicts, imports, quarantine), `store_membership.py`,
  `store_events.py` (the telemetry rows, the event reads with their window
  memo, and the log's reads, refold and verification),
  `store_provenance.py` and `store_index.py` (the search seams and the
  document frequency memo), each as a function `Store` binds by name, so a
  call or a patch of a `Store` method reaches it as before. `store.py`
  (1,926 lines) keeps `Store` with its open, its connections and
  transactions, the admission readers and the status, and re-exports
  every name a caller imports from it, as the same object. The code moved
  unchanged but for two names that would shadow a function beside them:
  `Store.get_many` and `Store.load_many` call their argument `ids` (every
  caller passes it by position), and `mark_verified` calls its change set
  `changes`. No module beside `store.py` imports it at run time, and each
  one that logs logs as `bettermemory.store`. A test that patches a name
  whose reader moved (the row readers, the stamp clock, the bound-elsewhere
  guard, the event window bound, the URI helper of the binding check) now
  patches the module that reads it, and `store` no longer binds those
  names, so a patch left on `store` fails at once. One thing changes for
  introspection: `typing.get_type_hints` on a method bound from a module
  beside `store.py` raises `NameError` (106 of the 111 moved; the five
  static methods resolve), since each imports `Store` only under
  `TYPE_CHECKING`, and on Python 3.11 and 3.12 such a method's raw
  `__doc__` loses the class's indentation (68 of them); nothing in the
  package, the bench or the tools reads either.
  (`src/bettermemory/store.py`, `src/bettermemory/store_schema.py`,
  `src/bettermemory/store_rows.py`, `src/bettermemory/store_keys.py`,
  `src/bettermemory/store_records.py`, `src/bettermemory/store_journal.py`,
  `src/bettermemory/store_tables.py`, `src/bettermemory/store_membership.py`,
  `src/bettermemory/store_events.py`, `src/bettermemory/store_provenance.py`,
  `src/bettermemory/store_index.py`, `tests/test_store_transactions.py`,
  `tests/test_bound_elsewhere_warning.py`, `tests/test_sqlite_uri.py`,
  `tests/test_commit_drift_cache.py`, `tests/test_episode_volume_rollup.py`,
  `tests/test_event_window.py`, `docs/internals.md`, `CONTRIBUTING.md`;
  citations only in `src/bettermemory/_fsutil.py`, `src/bettermemory/backup.py`,
  `src/bettermemory/config.py`, `src/bettermemory/_frontmatter.py`,
  `src/bettermemory/init.py`, `tests/test_frontmatter.py`,
  `tests/test_sqlite_uri.py`, `tests/test_import_graph.py`)
- **A test pins every module of the package under 2,000 lines.** The
  bound the splits of `store`, `membership`, `handlers.write`, `recall`,
  `verify`, `search`, `origin`, `eval`, `health` and `location` were
  made to is now asserted over every file under `src/bettermemory`, so
  a module that grows past it fails the suite and is split, not excused.
  With `store.py` at 1,926 lines the package passes for the first time;
  the largest modules are `_response.py` (1,985) and `origin.py` (1,978).
  (`tests/test_module_bounds.py`)
- **`search.py` is split into five modules.** The ranker keeps `search()`,
  the fusion, the snippet and token caches and the conversational rerank,
  and goes from 4,305 lines to 1,686. Beside it, `search_lexicon` holds
  the tokenizer and the FTS index text, `search_bm25` the keyword and
  BM25 scorers, `search_lane` the conversational lane's temporal reading
  and the per-hit labels, and `search_dedup` the write-time dedup. Every
  name imported from `bettermemory.search` still is, as the same object;
  the code moved unchanged and the tokenizer's fingerprint is the same,
  so no index is rebuilt and every ranking reads as before. A test that
  patches the tokenizer or the dedup cache now patches the module that
  reads it. (`src/bettermemory/search.py`,
  `src/bettermemory/search_lexicon.py`, `src/bettermemory/search_bm25.py`,
  `src/bettermemory/search_lane.py`, `src/bettermemory/search_dedup.py`)
- **`origin.py` is split into three modules, with no import path changed.**
  `origin.py` was 2,964 lines, over the 2,000-line bound for a module.
  The repository files it reads without a git process (git's config
  parser with its per-file memo, the root as git spells it, the index's
  `.gitmodules` entry with its memo, the decoder of git's C-quoted patch
  headers, and the file signatures the commit-drift memos key on) move to
  `origin_gitfiles.py` (1,022 lines), and the remote URL parser behind
  `repos_match` to `origin_remote.py` (198 lines), each unchanged.
  `origin.py` (1,821 lines) keeps the capture and its cache,
  `capture_caller`, every git call with the walk and timestamp memos, and
  the origin tests the membership guard pins to it, and re-exports every
  moved name a caller reads, so `from bettermemory.origin import ...`
  resolves as before. A test that patches `_config_variable_names` or
  `_INDEX_FIRST_READ` patches `origin_gitfiles`, where their readers are.
  (`src/bettermemory/origin.py`, `src/bettermemory/origin_gitfiles.py`,
  `src/bettermemory/origin_remote.py`, `tests/test_commit_drift_cache.py`,
  `docs/internals.md`, `SECURITY.md`)
- **`verify.py` is split into three modules.** `verify.py` was 4,231
  lines. The path-candidate extraction (the per-body caps, the
  extraction regexes, the candidate validator with its route, home and
  placeholder rules, the existence check and the body parse memo) moves
  to `verify_candidates.py`, and `detect_path_drift` with its report,
  `ClaimAnchor`, `Anchoring` and the anchored reads of attestations and
  relative citations to `verify_anchoring.py`. `verify.py` (1,726
  lines) keeps verification staleness, commit drift and the staleness
  verdict, and re-exports every name a caller imports from
  `bettermemory.verify`, as the same object, so no import path changes
  and every drift report and verdict reads as before. A test that
  patches the candidate scan, the memo's bound or the existence check's
  `Path` now patches `verify_candidates`. (`src/bettermemory/verify.py`,
  `src/bettermemory/verify_candidates.py`,
  `src/bettermemory/verify_anchoring.py`, `tests/test_verify.py`,
  `tests/test_body_parse_memo.py`)
- **`recall.py` is split into five modules.** `recall.py` was 3,104
  lines. The per-session state (`RecallState`, `rebuild_session` and the
  cooldown's transitions) moves to `recall_state.py` with the vocabulary
  every recall module reads (the rule, the default harness, the delivered
  provenance labels, the state's kinds and bounds, the settlement
  surfaces); the caller's admission and the helpers both surfaces' rows
  read (`caller_admission`, `summarize`, `disputed_marks`, the
  rc3_admitted column) to `recall_admission.py`; the brief's ranking
  pieces, its render, the compaction-marker reader and the orphan notice
  to `recall_brief.py`; and the probe both hooks run with the Stop's
  settlement (`_probe_message`, `settle_shown`, `settle_unused`,
  `settle_interrupted`, `review_unanswered`) to `recall_settle.py`.
  `recall.py` (1,555 lines) keeps both surfaces, the brief's selection,
  the recall block and `confirm_compaction`, and re-exports every name a
  caller imports from it as the same object, so no import path changes
  and `hook` still imports `recall` only inside the functions that call
  it. Every function moves unchanged; none of the four imports `recall`
  or `hook`, and `tests/test_import_graph.py` holds each to `recall`'s
  rule and pins that a cold import of `hook` or of `sessions` loads none
  of the five. A test that spies on the probe now patches
  `recall_settle.probe_for_miss`. (`src/bettermemory/recall.py`,
  `src/bettermemory/recall_state.py`, `src/bettermemory/recall_admission.py`,
  `src/bettermemory/recall_brief.py`, `src/bettermemory/recall_settle.py`,
  `src/bettermemory/hook.py`, `tests/test_import_graph.py`,
  `tests/test_hook.py`, `tests/test_membership_guard.py`,
  `docs/internals.md`)
- **The write gates move to `handlers/write_gates.py`.** `handlers/write.py`
  was 2,132 lines, over the 2,000-line bound for a module. The gate chain
  `memory_write` runs before the review (`GateContext`, the eight gates and
  `_WRITE_GATES` in their order, the user-claim detector `memory_update`
  shares, and `GateBundle` with `apply_write_gates`) now lives in
  `handlers/write_gates.py` (732 lines), unchanged. `handlers/write.py`
  (1,446 lines) keeps `memory_write`, the capture admission, the write
  review and the commit, and re-exports every moved name a caller reads, so
  no import path changes and `memory_write` answers as before. The
  gate-order pin in `tests/test_server.py` cites the order's rationale by
  name, above `_WRITE_GATES`, instead of by line.
  (`src/bettermemory/handlers/write.py`,
  `src/bettermemory/handlers/write_gates.py`,
  `src/bettermemory/handlers/update.py`, `tests/test_server.py`,
  `tests/test_update_user_claim_gate.py`, `docs/internals.md`)
- **`membership.py` is split into eight modules, with no import path
  changed.** `membership.py` was 5,497 lines, over the 2,000-line bound
  for a module. The vocabulary, the records, `relation`, `admitted` and
  the path helpers move to `membership_records.py`, the L1 to L10 lines to
  `membership_lines.py` and the row conversions to `membership_rows.py`.
  The `Membership` class keeps its state and its core (machines, projects,
  evidence, checkouts, history, proposals and `why`) and binds four method
  groups by name: the ladder from `membership_resolve.py`, the
  declarations and the writes made where nothing could be read from
  `membership_declare.py`, homes, surfaces, run-state, anchoring and
  health from `membership_queries.py`, and export and import from
  `membership_portable.py`. Each group's functions take the model as
  `self`, so a call, a class-level patch and a test reach them as before,
  and no module of the family imports `membership` at run time.
  `membership.py` goes to 1,717 lines and re-exports every name a caller
  imports from `bettermemory.membership`, as the same object; the code
  moved unchanged, so every resolution, line and row reads as before. One
  thing changes for introspection: `typing.get_type_hints` on a bound
  method raises `NameError`, since each child imports `Membership` only
  under `TYPE_CHECKING`, and on Python 3.11 and 3.12 a bound method's raw
  `__doc__` loses the class's indentation; nothing in the package, the
  bench or the tools reads either.
  (`src/bettermemory/membership.py`,
  `src/bettermemory/membership_records.py`,
  `src/bettermemory/membership_lines.py`,
  `src/bettermemory/membership_rows.py`,
  `src/bettermemory/membership_resolve.py`,
  `src/bettermemory/membership_declare.py`,
  `src/bettermemory/membership_queries.py`,
  `src/bettermemory/membership_portable.py`, `docs/internals.md`)
- **A test's patch must reach a reader.** `tests/test_patch_targets.py`
  holds every `monkeypatch.setattr`, `patch` and `patch.object` call in
  `tests/` and `bench/` to a name its module reads at run time, or that
  another module of the package, the bench or the tools reads as
  `module.name` at call time: a binding a module keeps without reading
  it, as the parents of the splits above keep their re-exports, is not a
  patch point, and a patch on it replaces nothing the code under test
  runs. The guard's one finding: `bettermemory.server` bound
  `capture_origin` only so that fourteen test patches on it would not
  raise, while the handlers read their own binding; the binding and the
  fourteen patches are gone. (`tests/test_patch_targets.py`,
  `src/bettermemory/server.py`, `tests/test_server_origin.py`,
  `tests/test_server_claims.py`, `tests/test_server_commit_drift.py`,
  `tests/test_server_v12_features.py`, `tests/test_commit_drift_cache.py`,
  `tests/test_membership_anchoring.py`, `tests/test_search_drift.py`)

#### Fixed

- **Every mutating store call takes the write lock before it reads what
  it decides on.** `update`, `mark_verified`, `record_corroboration`,
  `tombstone`, `restore` and `put_memory` read their snapshot in
  autocommit and only then began the write transaction, so two daemon
  workers, or the daemon and a CLI process, could both pass the
  concurrent-update check and one update was lost, and an update racing
  a removal brought the record back beside its tombstone. The read, the
  check and the write now run under one `BEGIN IMMEDIATE`, with one
  owner per transaction: a call nested in another's transaction or in a
  batch joins it under a savepoint, so a nested failure takes back only
  what it wrote and the owner commits once. Of two writes built on one
  snapshot exactly one lands; an update or verify racing a removal never
  brings the record back; a put never writes over a tombstone (only
  `restore` brings a record back); and a duplicate write corroborates
  once per session across workers and processes, decided by the store
  under its lock and served by a new partial index on the log, which an
  older store gains at its first open. A `COMMIT` that fails rolls back
  instead of leaving the connection inside the transaction holding the
  lock until the daemon restarts. Threaded and two-process tests pin
  each case. (`src/bettermemory/store.py`,
  `src/bettermemory/handlers/write.py`)
- **A busy, locked or full store is a refusal that says whether the call
  kept anything, and a daemon stop waits for every worker's connection.**
  A tool call that meets a lock another process holds, or a full disk,
  answers `store_busy` or `store_full (retryable)` only when it kept
  nothing; a call whose write had already committed answers
  `(not retryable)` and says the write stays, so a client does not make
  it twice. The prune and the filings that follow a committed write are
  best-effort, and a prune with nothing to do takes no lock. Removing a
  removed or unknown id is a refusal with its reason. Long prunes and
  scope renames commit in chunks of about 0.1 s of work, each item
  decided again under its chunk's lock, with 0.05 s of free lock between
  chunks, since SQLite's lock is not queued and a waiter must land in
  the gap: with two processes writing beside a prune of 90,000
  tombstones, the writers' worst wait fell from 1.16 s to 0.17 s, a
  thirtieth of the 5 s busy timeout. A chunk already committed stays
  committed when a later chunk fails, and a rerun finishes the job. A
  connection that a failed rollback left inside a transaction is rolled
  back by its next call, so the stdio server's one connection cannot
  hold the write lock against every other writer; a daemon worker that
  finds its connection left inside a transaction closes and reopens it.
  On a stop, a forced exit included, the daemon interrupts every worker
  still inside SQLite each round and waits for every worker connection
  to close before it gives the store's lock back. The supervisor's stop
  timeout rises from 40 to 60 s, which covers a worker's git call (20 s)
  past the daemon's own 30 s bound, and its kill is the hard bound; an
  installed service unit keeps its 40 s until `bettermemory service
  install` writes it again. The three busy timeouts (CLI, hook worker,
  tool worker) stay at 5.0 s and are named as unmeasured, with the
  per-role lock-hold measurement that would set them.
  (`src/bettermemory/store.py`, `src/bettermemory/daemon.py`,
  `src/bettermemory/_handlers.py`, `src/bettermemory/_daemon_client.py`,
  `src/bettermemory/service_units.py`, `src/bettermemory/handlers/write.py`,
  `src/bettermemory/handlers/episode_write.py`,
  `src/bettermemory/cli/serve.py`, `src/bettermemory/cli/tombstones.py`,
  `src/bettermemory/cli/episodes.py`)
- **A dashed run in a memory body no longer holds a daemon worker for
  seconds.** The scan that marks a domain's route so the same path in
  the body is not read as a filesystem citation began a match at every
  letter of a dashed run, and each start rescanned the rest of the token:
  a 32 KiB body of `a-a-a...`, inside the scan cap, took about 9 s on
  every recall that ranked it, holding the interpreter's lock so the
  daemon's other workers waited. A match now starts once per token, so
  the same body scans in about 1 ms and the time grows with the body's
  length; the label bound, which guarded the dotted shape and not the
  dashed one, is gone, so a domain of any length keeps its route. The
  routes found are unchanged on every realistic shape.
  (`src/bettermemory/verify.py`)
- **memory_write and episode writes refuse a body carrying a model's
  chat-template control token.** Nothing read a body for the delimiters
  a prompt format uses to open or close a turn, so a write could store
  `<|im_start|>system`, `<|endoftext|>`, Llama's `[INST]` or `<<SYS>>`,
  or a `\n\nHuman:` turn marker, and every recall, search and handoff
  that read the body back put the token into a later session's context,
  where it can forge a turn boundary: on a stack that parses special
  tokens out of plain text it is one. `memory_write` now refuses such a
  body with `control_token_warning`, second in the gate chain after the
  credential gate, and `episode(action="write")` refuses one in the body
  or the takeaway after its credential check. The markers name each
  token by its template family and its bare name, never with its
  delimiters; the event records the families only; and no acknowledge
  flag or `force` passes the refusal, since a writer steered into
  writing the token would pass the flag as readily, while an honest one
  loses nothing by naming the token without its delimiters, or, for a
  `Human:` or `Assistant:` turn marker, by quoting the line, which the
  hint says. A turn marker is judged at the body's start after any
  leading whitespace, since the store keeps a body stripped. The list is
  the documented templates' (ChatML, tiktoken's special tokens, harmony,
  Llama 2, 3 and 4, Phi, Gemma, Command R, Granite, DeepSeek and the
  legacy Anthropic turn markers), matched exactly, with each template's
  source named beside it; the markup and placeholders ordinary bodies
  spell (`<s>`, `<eos>`, `[TOOL_CALLS]`) stay silent. The scan is
  linear: about 17 ms on 1 MB of prose and 60 ms on 1 MB of nothing but
  `<`, each 2.0x at twice the length. memory_write's served description
  names the gate ("each names its fix" replaces "each names its
  acknowledge flag", within the 8,000-character surface budget), as do
  the system-prompt addendum and the README.
  (`src/bettermemory/handlers/write_gates.py`,
  `src/bettermemory/handlers/episode_write.py`,
  `src/bettermemory/handlers/write.py`, `src/bettermemory/prompts.py`,
  `SECURITY.md`, `README.md`, `docs/api.md`, `docs/internals.md`,
  `docs/system_prompt.md`)
- **A body of long digit-run assignments no longer holds the credential
  scan for a second.** The generic `password=…` rule's value check
  strips a placeholder's number suffix (`changeme12345`) with
  `[-_]?\d+$`, which began a match at every digit of a run and read to
  the run's end from each, so a value made of one long digit run cost
  the square of the run. Every `password=`-style assignment in a memory
  body, an episode, a search query or a prompt the recall hook redacts
  reaches that check with a value of up to 1,024 characters: 425 KB of
  assignments with 1,000-digit values took 1.06 s in the write gate's
  scan and 0.95 s in the query redaction, and doubling the run cost 3.7x
  to 4.0x. The strip now starts a match only where a digit run starts
  and strips exactly what it did, checked over every string of up to
  eight characters from digits, separators, a letter and a newline and
  over every string the test suite holds: the same body takes 0.05 s in
  each, 1.9x to 2.0x at twice the length.
  (`src/bettermemory/credentials.py`)
- **memory_update refuses an edited body carrying a chat-template
  control token.** The control-token refusal above ran on `memory_write`
  and the episode write only, so a writer could store a token by writing
  a clean body and editing the token in, while `memory_update`'s
  description says the write gates apply. It now runs the same check on
  an edited body, right after its credential check:
  `control_token_warning` in `memory_write`'s shape, with a hint of its
  own and no acknowledge flag, and an event that records the template
  families. A metadata-only edit does not read the body, so a record
  stored with a token before this candidate can still be re-scoped, and
  the edit that writes the token's bare name commits. The detector moves
  out of `handlers/write_gates.py` into `control_tokens.py`, which
  imports nothing from the package, so the episode write no longer
  imports the write-gate chain to reach it. The refusal reads a body and
  an episode's takeaway: other model-supplied text, such as `said` and
  `memory_remove`'s `reason`, is not read, and
  `bettermemory security scan` does not look for tokens in records
  stored before this candidate. (`src/bettermemory/control_tokens.py`,
  `src/bettermemory/handlers/update.py`,
  `src/bettermemory/handlers/write_gates.py`,
  `src/bettermemory/handlers/episode_write.py`, `SECURITY.md`,
  `docs/api.md`, `docs/internals.md`)
- **A subagent's compaction is taken back from the session.** Claude
  Code 2.1.296 and 2.1.309 send a subagent's compact SessionStart under
  the session's id and transcript path with no `agent_id`, and the
  subagent's marker lands in the subagent's own transcript after the
  hook, never in the session's. The hook answered it as the session's:
  on 2026-10-28 a 13-line brief went into a subagent while the session
  held a compact signal no marker of its own would answer and counted
  the brief's ids as shown in its segment, which its prompt recall then
  held as `seen_in_segment` (3 of the 14 owner compact SessionStarts
  since rs-v6 were a subagent's). The brief still goes into the
  subagent; the session's bookkeeping is now corrected later, by a
  timing rule. A later read of the held signal (`confirm_compaction`:
  the prompt recall, the Stop or a SessionStart), at least 60 s after
  it, that finds no new marker of the session's own within 60 s of it
  (the marker its segment already opened on counts as none), finds the
  session's conversation gone on past those 60 s (a row of its own
  thread stamped later) with no recent marker of its own newer than the
  one its segment opened on (a compaction of the session's writes its
  marker before its conversation goes on), and finds a marker within
  60 s of the signal in one of the session's subagent transcripts
  (`subagents/agent-<id>.jsonl`, or
  `subagents/workflows/<run>/agent-<id>.jsonl` for a workflow's agents)
  takes the signal back for that subagent. The signal ends, the ids that
  brief printed leave the session's segment and its settled set, and a
  `segment_opened` row records the subagent (`agent_id`, the
  `agentType` of the transcript's `.meta.json` as `agent_type`,
  `agent_from: "transcript"`), the subagent's segment, the `marker`,
  `opened_at`, the ids `withdrawn` and the `brief_event_id`, so the held
  state and its rebuild agree. A signal no transcript answers stays
  held as before; the rule's failure modes are under Known limitations.
  A payload that does name the subagent is answered in the subagent's
  own scope from the start: the daemon and the in-process
  `bettermemory session-start` now pass the payload's `agent_id` and
  `agent_type` to the brief (the hooks reference's common fields for
  the agent a hook fires in; `agent_type` alone names the main thread's
  `--agent` and stays the session's), and a SessionStart that names a
  subagent opens its segment, holds its signal and claims its brief in
  a scope of its own, rebuilt from the session's rows that carry its
  id, on the markers of the subagent's own transcript; its rows carry
  `agent_id`, `agent_type` and `agent_from`, and the session's segment,
  signal and shows are left as they were. No Claude Code build read
  fills `agent_id` on a SessionStart yet. Eval's surfaces count the
  segments opened per session, subagent and number, so a subagent's
  segment 1 and the session's own segment 1 no longer read as one. A
  row without these fields reads as before. (`src/bettermemory/recall.py`,
  `src/bettermemory/recall_state.py`, `src/bettermemory/recall_brief.py`,
  `src/bettermemory/recall_counters.py`, `src/bettermemory/daemon.py`,
  `src/bettermemory/cli/session_start_cmd.py`)
- **The security audit builds no key ring from a store id this build never
  writes.** The audit took meta's `store_id` as the store file gave it
  and built the key ring from it, so a store whose id was a path and a
  glob (`../elsewhere/**/planted`, one a cloned repository can ship under
  `./.claude-memory`) had the audit glob outside the keys directory,
  report a file there as a high "retired key", and say the next owner
  open would rekey the log, when `Store.open` refuses that store and
  nothing ever rekeys it. The key ring now holds a well-formed id or
  nothing: `KeyRing` refuses any id but the 32 lowercase hex digits
  `Store.create` writes, with the pattern in the key ring's module and a
  test pinning it to the one `Store.open` refuses a store by, and the
  ring lists its own retired keys. The audit reports a malformed id as
  one high `key.match` finding ("every opener refuses this store") and
  runs no ring check from it. Backup holds a bundle's and a store's id to
  the same pattern, so a manifest or snapshot naming any other id (rc13
  took any 1 to 128 letters, digits, `_` or `-`) is `manifest_invalid`,
  and restore and create refuse it.
  (`src/bettermemory/log.py`, `src/bettermemory/posture.py`,
  `src/bettermemory/backup.py`, `docs/api.md`)
- **`init` and `doctor` find Claude Code's files where `CLAUDE_CONFIG_DIR`
  puts them.** Claude Code keeps its global config (`.claude.json`, the
  user-scope MCP servers and the project registry) and its user settings
  in the directory `CLAUDE_CONFIG_DIR` names when that is set, the global
  config inside it and the settings as its own `settings.json`. `init`
  and `doctor` read `~/.claude.json` and `~/.claude` whatever the variable
  said, so with it set `bettermemory init --client claude-code` wrote a
  file that Claude Code never reads and reported "added", the deny rules
  rc1 to rc5 added stayed in the settings file it does read (even
  `--config-path $CLAUDE_CONFIG_DIR/.claude.json` looked for them in
  `$CLAUDE_CONFIG_DIR/.claude/settings.json`), and `doctor` judged the
  home layout's files, or reported Claude Code not found. One module now
  resolves Claude Code's paths the way Claude Code does (the
  configuration directory, the global config, the user settings, a
  project's files, and the settings file that pairs with an MCP config),
  and init, doctor, the security audit and `security sandbox` all read
  them through it; the settings file is never derived from the MCP file's
  parent alone. The global config is also found where Claude Code reads it
  on the hosts the old answer missed: a legacy `.config.json` an old
  install left in the configuration directory, which Claude Code reads
  first, and `.claude-custom-oauth.json` under
  `CLAUDE_CODE_CUSTOM_OAUTH_URL`. A `~` in the variable is kept as
  written, as Claude Code keeps it. The test suite clears both variables
  for every test, so a suite run from a shell that sets them cannot reach
  the developer's own files.
  (`src/bettermemory/claude_paths.py`, `src/bettermemory/init.py`,
  `src/bettermemory/doctor.py`, `src/bettermemory/posture.py`,
  `src/bettermemory/sandbox_settings.py`, `docs/clients.md`)
- **The security audit asks the filesystem whether the store is synced,
  not only the folder's name.** `store.synced` and `keys.synced` matched
  path names (`Dropbox`, `~/Library/CloudStorage`, ...) on the path as
  given. With macOS's iCloud "Desktop & Documents Folders" on, which
  macOS offers when an account signs in to iCloud, `~/Documents` is
  synced in place, so a
  store under `~/Documents/<repo>/.claude-memory` (the working-directory
  rule) or a `BETTERMEMORY_DIR` there read `ok` while iCloud uploaded the
  live SQLite file and its `-wal`, and a `BETTERMEMORY_DIR` that was a
  symbolic link into a Dropbox folder was missed because the path was
  never resolved. The audit now resolves the path and reads the sync
  markers on it and every ancestor: on macOS the File Provider domain
  attribute (iCloud Drive and the File Provider clients) through libc,
  and the links iCloud Drive keeps to the folders it syncs in place; on
  Windows a cloud files placeholder's reparse tag or its recall, pinned
  or unpinned attribute. The folder-name match stays as the fallback.
  (`src/bettermemory/posture.py`)
- **`init` patches a symbolically linked client config where the link
  points.** A `~/.claude.json` or `~/.claude/settings.json` linked into
  place from a dotfiles repository was replaced by a detached `0600`
  regular file: the atomic rename landed on the link's own name, so the
  dotfiles copy kept the old content (the rc deny rules included), and
  the next sync or a new machine silently brought it back. The JSON
  patcher, the deny-rule removal and the Hermes patcher now resolve a
  link once, read and write its target, and replace the target beside
  itself in its own directory: the link stays a link, and the file keeps
  the mode it had (a new file is still created owner-only). A link into a
  directory init cannot write (a read-only store a dotfiles manager links
  into) and a dangling link whose target's directory is missing are
  refused by name, with nothing created or changed.
  (`src/bettermemory/init.py`, `src/bettermemory/_fsutil.py`)
- **`init` takes Claude Code's own lock on its global config, and checks
  for a concurrent write by content.** The patchers held a private lock
  and re-checked the file before writing, by its `(mtime, size)`: that
  detected a concurrent write rather than excluding it, though Claude
  Code locks its global config with a `<config>.lock` directory init
  could take, and on a filesystem with one-second mtimes (HFS+, ext3,
  FAT, many SMB and NFS servers) a same-size rewrite in the same second,
  a new session id of the same length, passed the check and was
  overwritten. `init` now takes Claude Code's lock around its read,
  check and write of Claude Code's global config (`.claude.json`, or the
  legacy or custom-OAuth file Claude Code resolves instead), the way
  Claude Code takes it: waiting up to 12 s for a live holder and then
  refusing with nothing written, and taking over a lock older than the
  library's 10 s stale time. It writes only while the lock is still its
  own and removes only its own, so a Claude Code that took the lock over
  as stale keeps it. Every patcher's check is now the SHA-256 of the
  exact bytes it parsed, taken again before the write, so neither a
  same-size rewrite nor a write between a stat and the read can pass it.
  (`src/bettermemory/init.py`)
- **The security audit reads every spelling of the loopback in the
  sandbox's allowed domains, and `allowLocalBinding` is a gap on macOS
  only.** `claude.sandbox.network` matched `allowedDomains` hosts against
  a fixed list (`localhost`, `127.0.0.1`, `[::1]`, ...), so `127.1`,
  `2130706433`, `localhost.` and `[::ffff:127.0.0.1]`, all 127.0.0.1,
  where the daemon listens, went unflagged. Hosts are parsed now:
  `localhost` and `*.localhost` with or without a root dot, any loopback
  or unspecified address, IPv4-mapped ones included, and the legacy IPv4
  spellings the C resolver reads; every loopback or unspecified address
  is flagged now, the ones other than 127.0.0.1 for the rest of the
  loopback they open. `allowLocalBinding` was a warning on
  every platform, though Claude Code's sandboxing documentation says it
  has no effect on Linux, where a sandboxed command's localhost is its
  own; it is information there and a warning on macOS.
  (`src/bettermemory/posture.py`, `docs/installation.md`)
- **A tracked file beside an ignored store no longer reads as a tracked
  store.** `store.git` asked git whether anything under the store's
  directory was tracked, so a committed `.claude-memory/README.md` beside
  an ignored store file read high, "treat what was pushed as disclosed".
  The audit now sorts what git tracks there: the store's content (the
  store file with its `-wal` and `-shm`, a copy of it such as a
  pre-restore or orphaned copy, and anything under `backups/`, where a
  bundle can hold the signing key) reads high, with a fix naming those
  files (`git rm --cached -- ...`, `git rm -r --cached -- .../backups`),
  and any other tracked file is only named. The keys directory, which
  holds nothing but the ring, is still judged whole.
  (`src/bettermemory/posture.py`)
- **doctor holds a repository's Claude Code files to the audit's cap.**
  doctor read a project's `.claude/settings*.json`, its `.mcp.json` and
  a project-scoped client config (`.cursor/mcp.json`) whole, while the
  audit read the same settings files through a 1 MiB cap: a repository
  could ship a file of any size and `bettermemory doctor`, run in it,
  loaded all of it; one shipped as a link to a FIFO or a terminal held
  `doctor` and `security audit` in the read. Both now read through one
  bounded reader, `claude_paths.read_settings_text`: a file over the cap,
  or one that is no regular file, is named as unreadable without being
  read. The global config (`~/.claude.json`) and a client's user-scope
  file are still read whole, once they are regular files: Claude Code
  alone writes the global config, which grows with the state it keeps per
  project. (`src/bettermemory/claude_paths.py`,
  `src/bettermemory/_fsutil.py`, `src/bettermemory/doctor.py`,
  `src/bettermemory/init.py`, `src/bettermemory/posture.py`)
- **The audit no longer reads git's silence as an answer.** When
  `git ls-files` timed out, could not run or failed, `store.git` read the
  store as untracked, so a tracked store was reported as "one `git add
  -A` commits it" (a warning) instead of tracked (high), even under a
  `.gitignore`, since `git check-ignore` applies no ignore rule to a
  tracked file. When `git check-ignore` gave no answer, an ignored store
  got the same warning. Either case is now a skip that names the question
  and why it went unanswered. (`src/bettermemory/posture.py`)
- **A caller in a repository git refuses to read keeps its worktree
  root.** Git refuses a checkout another account owns while
  `safe.directory` does not name it (a dev-container bind mount, an SMB or
  NFS home with mapped ids, a colleague's shared tree), and the origin
  capture read that exit as "not a repository": the caller stood nowhere,
  determinately, while membership's location capture placed it in the
  repository, and `memory_write` with claims was refused "the caller is
  not inside a git worktree". Git's non-zero exit now means "no
  repository" only where the repository's files hold none
  (`githead.locate`). Where they hold one, the root is read from them
  (none where the repository's config could move the working tree), the
  origin is indeterminate with git's reason logged, and the remote and
  branch probes, which git refuses too, are not run, so the capture still
  runs one git process. A claim write there is checked against that root;
  where no root is known, the refusal quotes git's instead of "not inside
  a git worktree" and says what trusting the repository through
  `safe.directory` means. `bettermemory security audit` read the same
  exit as "the store is not in a git working tree", ok, for the store and
  for the keys; where the files hold a repository it now warns, naming
  the repository and git's refusal, that whether git tracks or ignores
  the directory is not known. (`src/bettermemory/origin.py`,
  `src/bettermemory/origin_gitfiles.py`,
  `src/bettermemory/handlers/_shared.py`, `src/bettermemory/posture.py`)
- **The origin cache holds on Windows.** The capture's signature came from
  githead's discovery, which declines off POSIX (git's ownership check
  there reads security descriptors) and in a repository another account
  owns, and a declined signature equals no other, so on Windows nothing
  was kept: every capture, and so every `memory_write` and every hook,
  ran its four git processes. Where githead's discovery declines for the
  platform or the owner, `githead.signature(owner=False)` now reads the
  same slots from the repository `githead.locate` finds, with githead's
  own ref parser (the git directory, HEAD, its ref chain, packed-refs and
  config), and the capture adds config.worktree, the root as git spells
  it and the refs that decide the branch's short name; outside any
  repository it is settled only where no `.git` or `HEAD` entry sits in
  the directory or above it. A reftable repository, whose HEAD names
  `refs/heads/.invalid`, is never kept, on any platform. Five
  captures of one repository with discovery declining ran 4, 4, 4, 4 and 4
  git processes, and run 4, 0, 0, 0 and 0. A capture git answered in
  another account's repository is now kept too, so a `safe.directory`
  withdrawn in the global configuration is seen when the ten minutes
  run out, as any other global setting is. The cache tests run on a
  second leg with discovery declining, and the real Windows leg runs
  them. (`src/bettermemory/githead.py`, `src/bettermemory/origin.py`,
  `src/bettermemory/origin_gitfiles.py`)
- **A moved head reads only the commits it adds, and a failed history
  read is not retried for every row.** The commit-drift legs read the
  author date of every commit a head reaches, memoised per root and head,
  so each commit that landed made the next search read the whole history
  again, up to 16 whole lists per root were held at about 106 bytes a
  commit, and a read that failed (it fails at its 5 s ceiling on a long
  enough history) was made again by every row that needed it. A head
  whose earlier head is its ancestor now reads only `<earlier>..<head>`
  and shares the earlier list's dates, where the shallow, graft,
  replacement and config files read as they did; a failure, and a history
  longer than the memo holds, is remembered for its root and head for a
  minute (a call meanwhile answers None and counts the failure, so nothing
  is kept from it); the dates are read as `%at` into UTC datetimes; and
  the entries hold at most two million dates together. Off POSIX and in
  another account's repository, where git's discovery is not read from
  the files, the root and the head are those the repository's files name
  without the ownership check, so the memo and the extension hold there
  too; under a variable that moves git's discovery the history is still
  read whole. Measured on synthetic linear histories
  (macOS, Python 3.11): the whole read of 500,000 commits went from 2.90 s
  to 2.52 s, its held list from 116 to 56 bytes a commit and its peak
  from 223 to 119; a head moved by one commit went from a whole read
  (2.97 s) to 21 ms; ten rows asking while the read fails spawned ten
  reads and spawn one. The read stays whole on a head's first sight: the
  drift counts are exact author-date counts, which no bound on git's walk
  (by committer date or by count) keeps exact.
  (`src/bettermemory/origin.py`, `src/bettermemory/origin_history.py`,
  `src/bettermemory/githead.py`)
- **Two captures past the alternates registry's cap no longer race.** The
  daemon's workers capture on their own threads, and the registry of a
  remote's other spellings evicted its oldest entry without a lock: two
  registrations past its 32 entries read the same oldest key, and the
  second's pop raised `KeyError` out of `capture`. The check, the
  eviction and the store are now one step under a lock, and the eviction
  tolerates a key already gone. (`src/bettermemory/origin.py`)
- **A declared workspace that does not exist is named as such.** A
  workspace declared through `BETTERMEMORY_WORKSPACE`, the
  `x-bettermemory-workspace` header or a client's root that this host
  does not hold was handed to git, whose spawn failed with the error a
  missing binary raises, and the log said "git binary not found on
  PATH". The capture now stats the directory first and, where it is not
  a directory this host holds, runs no process: the origin is
  indeterminate, counted as a failure as the spawn was, and the warning
  names the path, once per path per process (every hook call probes it
  again, so later ones log at DEBUG). The runner names a missing working
  directory the same way for every caller. (`src/bettermemory/origin.py`,
  `src/bettermemory/origin_gitfiles.py`, `src/bettermemory/_git.py`)
- **No program a repository's configuration names runs on a read.** The
  runner pinned fsmonitor and hooks off, but `log.showSignature=true` in
  a repository's config ran its `gpg.program` for every signed commit a
  `git log` listed, on every read of the author dates, and wrote the
  program's output among the dates; and the patch stream's `git log -p`
  ran a `diff.<driver>.textconv` program for every file whose attributes
  named the driver, and diffed its conversion rather than the text a
  claim reads from the file. Every call now pins `log.showSignature`
  off, and the patch stream passes `--no-textconv` and `--no-ext-diff`,
  so its lines are the blob's own; a file whose attributes say `-diff`
  or `binary` still reads "Binary files differ". The stream's shape was
  also open to two settings: `color.ui=always` wrapped its headers in
  escapes, so a deleted claimed file parsed to nothing and read fresh,
  and `diff.interHunkContext` merged two hunks with context lines and
  lost a binding. It now passes `--no-color` and pins
  `diff.interHunkContext=0`.
  (`src/bettermemory/_git.py`, `src/bettermemory/origin.py`)
- **`capture review` and `capture sample` read a malformed undo row as
  nothing.** Both read each `capture_undo` row with a bare
  `json.loads(...).get("memory_ids")`, so a payload that was not an
  object, or ids that were not a list of strings (a number, an object
  among them), raised out of the command. They now read the rows through
  `capture.undone_ids`, as `capture.undone_hashes` reads the hashes:
  what is malformed contributes nothing, and the well-formed ids still
  count. (`src/bettermemory/capture.py`,
  `src/bettermemory/capture_review.py`)
- **A claim whose root is spelled in another case anchors on macOS.** The
  repository root the claims resolve against is the directory as the
  kernel spells it, and an absolute claim keeps its writer's spelling,
  which resolving it does not change on a volume that ignores case or
  normalization, so `/Users/me/documents/repo/x.py` for a root spelled
  `/Users/me/Documents/repo` was dropped as escaping the repository and
  its commit drift read not-applicable. The ancestor that is the root's
  directory on disk now anchors it, a deleted file's claim too, and a
  path outside the tree is still dropped. (`src/bettermemory/origin.py`,
  `src/bettermemory/origin_gitfiles.py`)
- **`health` and `eval` escape what the store and the log hand them,
  and the eval report's model cell is escaped whole.** The text reports
  printed memory summaries, scopes, actor and `client_model` stamps,
  probe previews, session ids, repos and work trees as stored, so an
  escape sequence in one ran in the terminal of whoever read the report
  (a clipboard write, overwritten lines) and a line break split a line;
  this is the class rc13 closed in the security audit, `doctor` and the
  migrate line. The `eval --report` markdown escaped only `|` in the
  model column, so a `client_model` stamp holding a line break left its
  table row and wrote markdown of its own into a document meant to be
  published. `bettermemory health` (the membership section included),
  `bettermemory eval` (its review and recall-surface sections included),
  `--widening-preview --detail` and `--tool-usage` now print every such
  string through the escaping `doctor` uses, field by field and then
  over every finished line as `doctor` ends its report, so a field
  missed still cannot reach the terminal raw. The report's cell escaper
  writes a backslash, a pipe and the characters that open inline
  markdown backslash-escaped and a line break or other control
  character as its escape; a bare URL, a `www.` host or an email
  address in a model name still autolinks. `--json` is unchanged.
  (`src/bettermemory/health_render.py`,
  `src/bettermemory/eval_render.py`, `src/bettermemory/cli/health_cmd.py`)
- **The health tool lists a record as commit-drifted only where
  `memory_show` shows it drifted.** `memory_admin(health)` built the
  report with no admission, so `commit_drift_debt` took every verified
  record whose origin repo matched the caller's, a record with no home
  row included, while `memory_show` and `memory_search` count commit
  drift only for a record whose home the caller's admission admits. The
  `verify_drifted` recommendation could name records whose `memory_show`
  carried no commit drift at all. The tool now resolves the request's
  caller as `memory_show` does and passes its admission, so
  `commit_drift_debt` takes a record only where that admission counts
  its drift, and counts it in the caller's checkout as `memory_show`
  does, from a linked worktree too. The caller's anchoring reads
  `cross_repo_drift`'s homed groups where design 3.6 resolves their
  cited commit. `bettermemory health` keeps the origin-repo reading:
  resolving the caller registers the checkout (a token under
  `.git/info`, a provisional project and their log rows), and the CLI
  report writes nothing. (`src/bettermemory/health.py`,
  `src/bettermemory/handlers/health.py`)
- **The health report's rare-scopes check takes one pass over the
  scopes.** The bucket of singleton scopes that look like a typo of
  another compared every singleton with every scope, so the time grew
  with the square of the scope count: a store of 2,000 scopes stalled
  `memory_admin(health)` and `bettermemory health` for about 42 s. Each
  singleton's candidates now come from an index of the strings one or
  two deleted characters leave of every scope, and of the name part
  after the namespace, and the same rule decides each candidate, so the
  bucket is unchanged. On synthetic stores of 500, 1,000 and 2,000
  scopes the check took 2.1 s, 8.1 s and 32.2 s before and 0.013 s,
  0.027 s and 0.056 s after, and the whole report at 2,000 scopes went
  from 33.4 s to 0.06 s. A scope longer than 32 characters stays out of
  that index, whose cost grows with the cube of a scope's length (one
  1,009-character scope, which `memory_write` accepts, would take the
  report to 2.6 s and 567 MB), and is compared with the scopes within 2
  of its length instead; the distance check now computes only the cells
  within the threshold of the diagonal. A 4,000-character scope and its
  one-edit typo took the report 3.95 s before and take 0.011 s.
  (`src/bettermemory/health_stats.py`, `src/bettermemory/health.py`)

#### Known limitations

- **The key ring's binding is trust on first use on upgrade.** A ring
  from before rc13 has no `<store_id>.path`, and the first owner's open
  after the upgrade binds it to the file it opens. When that is a file
  that borrowed the store's id (a repository's `./.claude-memory/`
  opened before the home store), it takes the ring as in rc12: the home
  store's key is retired, the head moves past the home log, which reads
  `tampered` (`tail_deleted`), and the home store opens for reading
  only. `[storage] cwd_store = false` closes the repository route, and
  a ring already bound is not taken this way.
- **An rc12 anchor whose key is lost before rc13 names it reads
  tampered.** An origin anchor written before rc13 names no key, and the
  first writing open that sees a held key verify its row names it. When
  the key that signed the row was lost before that open, nothing tells
  the loss from a rewrite: the origin reads `origin_mismatch` (reason
  `signature`), `log verify` reads `tampered`, and every segment whose key
  is not held reads `unaccounted`, where rc12 moved the anchor to the
  `rekey` row. Filing the old key back as `<store_id>.<first 16 hex of its
  fingerprint>.key` makes the anchor hold and be named; without the key,
  deleting `<store_id>.origin` and `<store_id>.head` accepts the history
  on purpose.
- **A subagent's compaction is told from the session's by timing.**
  Claude Code 2.1.309 sends a subagent's compact SessionStart with the
  session's id and transcript path and no `agent_id`, so the hook
  prints the session's brief into the subagent, and the session holds
  the brief's ids as seen until a later hook, at least 60 s after the
  compaction, finds no new marker of the session's own within 60 s of
  it, finds the session's conversation gone on past those 60 s with no
  recent marker of its own newer than the one its segment opened on,
  and finds a subagent transcript's marker within 60 s of it. The
  session then gets the ids back; a prompt recall before that holds
  them as seen, and a settlement already written for them stays. The
  rule rests on an order measured, not promised: a compaction of the
  session's own writes its marker before any row of its conversation
  stamped after the marker (143 of 143 boundaries in the local
  transcripts, 2026-10-28). A session compaction whose conversation
  reached the transcript before its marker, with a subagent's marker
  within 60 s of it, would be taken for the subagent: it would open no
  segment and its brief's ids would be delivered again. A marker of the
  session's own stamped more than 60 s from its hook (Claude Code
  stamps a full compaction's after every SessionStart hook returns, a
  reactive one's before its PostCompact hooks) opens no segment, as
  before, and leaves to the session that signal and any other held up
  to 10 minutes (the default hook timeout) after the marker's stamp.
  Two compactions of one session within about a minute keep only the
  later one's signal, so the earlier one is never taken back (8 of 111
  subagent compactions in the local transcripts). While a signal stays
  held once the session's conversation has gone on, every hook scans
  the session's subagent transcripts again, searching up to 64 of them
  taken by name, so with more alive the compacting one can be missed.
  A subagent's own tool calls are recorded under the session.
- **The reachable-walk memo is bounded at 128 walks, not in bytes.** A
  walk holds about 141 bytes a commit, so 128 walks of 10,000 commits
  hold about 180 MB in a long-lived daemon. (`src/bettermemory/origin.py`)

### 9.0.0rc13

The thirteenth candidate closes three of rc12's known limitations: a
second store file that carries a store's id can no longer take over
that store's bound key ring, Windows reads an organisation's managed config,
and every command that opens a store refuses a backup bundle's. An origin
anchor now stays where it was written, a key file that cannot be read is
never replaced and ends the command that met it in one line, and a store
on a Windows network share passes the checks that read it by a SQLite
URI. Two defaults change, each named under Changed:
the config.toml a first run writes pins nothing, and no second daemon
is started while one holds the store's lock. The store's schema does
not move; the keys directory gains one file per store. It is a release
candidate like the twelfth; the stable 9.0.0 follows the candidates'
testing.

#### Security

- **The key ring is bound to the one store file it belongs to.** The
  key, the head checkpoint and the origin anchor were found by
  `meta.store_id` alone, so a second file with the same id shared all
  three. A repository's store that borrowed a known id, with a key of
  its own and a high seq, made the owner's opener retire the home
  store's key and push the head past the home log, which then read
  `tampered` (`tail_deleted`) for good; a verbatim copy in another
  directory signed its writes with the home key and moved the home
  head. The keys directory now holds `<store_id>.path`, `{"path":
  "<the resolved store file>"}`, written `0600` and atomically under
  `<store_id>.path.lock` by `Store.create`, by the first owner's open of
  a ring that has none, and by `backup restore`. When it names another
  file that still holds this store id, or one that cannot be read to
  tell (a permission, an I/O error, any failure of SQLite's open, and
  any error of its query but `SQLITE_ERROR`, `SQLITE_CORRUPT` and
  `SQLITE_NOTADB`), and that is not this file under another name
  (`os.path.samefile`), the owner's open writes
  nothing: no key retired or written, no meta, no row, no head, no
  anchor. So does a reader's open whose key matches, since a reader with
  the key writes too (the session-start hook, a worker connection). The
  store opens for reading, every write is refused, and a WARNING names
  both files and the remedy: move or remove one of the two stores, or
  delete `<store_id>.path` to bind the ring to this one. The WARNING
  comes once per process for each pair of files, both resolved, and
  every later open of the pair logs the same line at DEBUG, so a
  daemon's worker connections, each of which opens the store again, do
  not repeat it; a new pair warns again, and so does a spelling
  `Path.resolve` does not fold into another (a case variant on macOS;
  Windows's `resolve` reads the case on disk and folds it). A bound file
  that is gone, is not a store, holds another id or is a symbolic link
  loop releases the ring to the file being opened. A reader never
  writes the binding. A binding file that cannot be read is kept, with
  a warning, and the open goes on as it did before rings were bound; one
  that holds a relative path or does not parse is replaced. Every open
  after the first costs one small file read and no lock. `security
  audit` grades the binding's mode as it grades the head's and the
  anchor's (`mode.binding`, "key ring binding", expected `0600`): a
  group or other bit, `0644` say, is a warning (exit 2), and a group or
  other write bit or another owner is high (exit 1); Windows skips it,
  as it skips every mode.
  (`src/bettermemory/log.py`, `src/bettermemory/store.py`,
  `src/bettermemory/posture.py`, `tests/test_keyring_binding.py`,
  `tests/test_bound_elsewhere_warning.py`, `tests/test_posture_binding.py`)
- **An origin anchor is never moved, and a key that cannot be read is
  never replaced.** The owner's opener moved the anchor to its new
  `rekey` row when the current key file was missing and no held key
  verified the anchored row, so an edit made to that row before the key
  went was accepted for good, and a key file that only failed to read
  counted as missing. The anchor now stays where it was written and
  names the key that signed its row, `{"seq", "mac",
  "key_fingerprint"}`. When that key is held and does not verify the
  row, the row was rewritten: `origin_mismatch`, as before. When it is
  not held, the origin reads `unverifiable`, `log verify` reads at most
  `unverifiable` (exit 1), and the segments split as they do without an
  anchor; filing the key back as `<store_id>.<first 16 hex of its
  fingerprint>.key` makes the anchor hold again. An anchor written
  before rc13 names no key, and a row no held key verifies reads as a
  rewrite, until a writing open that sees its row verify adds the name
  without moving it (Known limitations, below). A key file
  that is there and cannot be read, or a key file or keys directory
  that is a symbolic link to nothing (a volume not mounted yet), raises
  `KeyUnreadableError` naming the file: the owner's open fails instead
  of writing a new key and a `rekey` row, a reader opens without the key
  and logs why, `doctor` fails its key check, `security audit` reports
  it high, and `backup restore` refuses before it changes anything. The
  command whose open failed ends in one line, not a traceback:
  `bettermemory: the key file <path> is there and cannot be read
  (<reason>); the store is not rekeyed over it; make it readable and run
  the command again`, exit 1 from the commands that open through
  `cli_context`, `serve`, the shim and `up --foreground`, and 2 from
  `membership migrate`; `migrate v8` keeps its own `migrate v8: ...`
  line, exit 1. A daemon logs the line last and exits 1, and `up` (exit
  1) and the hooks (exit 0) quote it with the log's path. The shim ends
  in the line alone, without its warnings about an in-process fallback
  that never starts; a fallback open that fails any other way still
  follows them, so the daemon's error and log path are not lost. A
  daemon a client starts is handed a keys directory that is a symbolic
  link as the link, as a service unit already was. Resolved, a link to
  a volume not mounted named a directory with no key in it, where the
  daemon wrote a new key and rekeyed the store; now the daemon refuses
  it in that line. An
  anchor file that cannot be read is never written over. A bundle's
  signed manifest carries the anchor's key. (`src/bettermemory/log.py`,
  `src/bettermemory/store.py`, `src/bettermemory/backup.py`,
  `src/bettermemory/doctor.py`, `src/bettermemory/posture.py`,
  `src/bettermemory/cli/_common.py`, `src/bettermemory/_daemon_client.py`,
  `src/bettermemory/shim.py`, `tests/test_origin_anchor.py`,
  `tests/test_key_unreadable_cli.py`)
- **Every command that opens a store refuses a backup bundle's.** `export`,
  `doctor` and `membership export` opened a bundle's snapshot in place,
  which switches it to WAL and fails the bundle's signed hash; the
  writing openers (the commands that open through `cli_context`, `up
  --foreground`, `serve`, the shim, `membership migrate` and `migrate
  v8`) also wrote rows into it, the in-process hook commands opened it,
  `backup restore` replaced it, `backup create` wrote a second bundle
  inside it (its default directory, `backups/` beside the store file, is
  in the bundle), and `service install` wrote a unit for it and `up`
  started a daemon on it. Each now refuses a store file with a
  bundle's `manifest.json` beside it before opening it, through a
  symbolic link too, with one message: ``<path> is in a backup bundle
  (<manifest> is beside it), and opening it would change it and fail the
  bundle's hash: copy memory.sqlite out of the bundle to a directory of
  its own first and point the command at the copy (`bettermemory backup
  verify` checks a bundle where it is)``. A file named by
  `export --mirror --store`, `migrate v8 --to` or `security scan --store`
  that has other names (hard links) is refused too. The refusal exits 2
  from `export`, `membership export` and `membership migrate`, `stats`,
  `log export` and `anchor`, `memory history` and `security scan`; 1 from
  `log verify`, `health` and the other commands that open through
  `cli_context`, `serve`, the shim, `up`, `migrate v8`, `backup restore`,
  `backup create`, `service install` and `doctor` (its store check
  fails); and 0 from the hooks, which print it. `backup create` (its
  line starts `bettermemory backup create:`) refuses with or without
  `--out` and writes nothing: a bundle is kept by copying its directory
  as it is. `service install` (`bettermemory service:`, and the `--json`
  `error`) refuses before it writes the unit, the log or the state
  directory and before it stops any daemon; `up` (`bettermemory:`, and
  its `--json` `error`) refuses before it spawns one. A `manifest.json`
  now counts only when it names the format
  `bettermemory.backup` or does not parse, so another tool's file of
  that name beside a store no longer stops `log`, `stats` or `security
  scan`. `security audit` reads a bundle's file without changing it, as
  before. (`src/bettermemory/backup.py`,
  `src/bettermemory/cli/_common.py`, `src/bettermemory/cli/service_cmd.py`,
  `src/bettermemory/cli/daemon_cmd.py`, `tests/test_bundle_guard.py`,
  `tests/test_bundle_guard_starts.py`)
- **Windows reads an organisation's managed config from the
  registry.** rc12 read none on Windows, since a standard account can
  create folders under ProgramData. The managed TOML is now the
  `ManagedConfig` value under
  `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\bettermemory`, which only
  administrators can write by default and where Group Policy and Intune
  deploy policy: a REG_SZ holding the whole text, or a REG_MULTI_SZ
  holding one line per string, read through the registry's 64-bit view
  (WOW64 shares `SOFTWARE\Policies` between the views, so a 32-bit and
  a 64-bit writer reach the same key). The text goes through the
  managed file's own parse, pins and refusals, a leading byte-order mark
  dropped. An absent key or value is no managed config; anything else
  that keeps the value from loading fails closed, as an unreadable file
  does: a key this account cannot open, a value of another type (a
  REG_EXPAND_SZ is refused, never expanded), text that is not TOML, an
  invalid setting, or text holding an unpaired surrogate, which the file
  refuses as bytes that are not UTF-8. Nothing under `%ProgramData%` is
  read. The loader's errors and notices and `config path|show|check`
  name the source `registry
  HKLM\SOFTWARE\Policies\bettermemory\ManagedConfig`, and `config path
  --json` reports `managed.read` true and `managed.note` null on every
  platform. `examples/managed.toml` gives the `reg add`, Group Policy
  and Intune deployments. POSIX is unchanged.
  (`src/bettermemory/managed.py`, `src/bettermemory/config.py`,
  `src/bettermemory/cli/config_cmd.py`, `tests/test_managed_windows.py`)

#### Changed

- **The config.toml a first run writes pins nothing.** It spelled every
  key at its default, so a fresh install pinned the defaults of the
  release that wrote it, a later release's changed default never reached
  it, and under a managed file every process, each hook among them,
  warned about lines the user never chose. Every setting is now
  commented out (`# key = value`) under its section header, with its
  prose, so loading the file gives exactly the code's defaults; remove
  the `# ` to set one. Most lines show the default; `[storage]
  directory`, `cwd_store = false`, `[policy] forbid_force = true`,
  `forbid_acknowledge = ["credential"]` and `[security]
  strict_provenance = true` show a value that changes it. A config.toml
  that exists is never rewritten, so one an earlier release wrote keeps
  every value it spells. (`src/bettermemory/config.py`)
- **A line that restates the default draws no pinned-key warning.** A
  user's value for a key the managed config pins is still ignored, and
  `config check` (a note) and `config show` still report it; the
  warning that names both files is now logged only when the value
  differs from the pinned one and from the code's default for that key.
  The lines an earlier release's config.toml spelled, `[telemetry]
  enabled = true` among them, draw none. `[behavior] prompt_recall` is
  compared as the loader reads it, since every config.toml 3.41.0 to
  8.0.0 wrote spells it `true`: `true`, `"yes"`, `"on"` and `"1"` read
  as `project`, `false`, `"no"`, `"off"`, `"0"` and `""` as `off`, and a
  mode's name as that mode, each string in any case. A line that reads
  as the default (`project`) or as the pinned value draws none; one that
  reads as another mode, or as nothing the loader knows, still does.
  Every other key is compared as written: `30` restates `30.0` and `[]`
  an empty list, `"true"` does not restate `true`, and a boolean
  restates only a boolean.
  (`src/bettermemory/config.py`, `tests/test_pinned_legacy_spelling.py`)
- **No second daemon is started while one holds the store's lock.** A
  hook, `up`, the shim's start and its reconnect started a daemon
  whenever none answered, even while another process held the store's
  lock: a daemon still starting, one busy past the health probe, or one
  whose state file was gone. The new one waited for the lock and
  exited, a process and a daemon-log line spent on every attempt. While
  the lock is held they now start none and wait for the holder to answer
  and prove itself, and return it when it does; they give up at the same
  timeout as before (8 s), or start one if the lock comes free first.
  With the lock free nothing changes, the hook's fast path included.
  rc12's note that a hook still starts a daemon beside a hung one no
  longer holds. A shim reconnect that times out with the lock still held
  answers `a process holds the store's lock (a daemon starting, busy or
  hung) and none answered within 8 s`, where it named a start that never
  happened, and `up` (exit 1) adds the same words to its line,
  `bettermemory: no daemon could be started for <store>: a process
  holds ...`, and to its `--json` `error`. The lock is probed once the
  wait is over, never held; with the lock free by then, `up`'s line is
  as before. (`src/bettermemory/_daemon_client.py`,
  `src/bettermemory/shim.py`, `src/bettermemory/cli/daemon_cmd.py`,
  `tests/test_followup_defaults.py`, `tests/test_up_held_lock.py`)
- **A restore binds the key ring to the restored file.** `backup
  restore` makes the ring's keys, head and anchor the restored file's,
  and now its binding too, so a store restored into another directory
  while the old one is still there opens for writing; the old one then
  opens for reading only until it is moved or removed. The plan and the
  `--json` report gain `binding: {before, after}`, and the restore record
  `binding_before` and `binding_after`. (`src/bettermemory/backup.py`)

#### Fixed

- **A store on a Windows network share is named by a URI SQLite takes.**
  The reads that hand SQLite a store file by URI, to pass `mode=ro`,
  `immutable=1` or `mode=rw` (the key ring binding check, `backup
  create`'s and `backup restore`'s reads, the daemon's store-moved check
  and `security audit`'s read of `meta`), built it with `Path.as_uri`,
  which on Windows writes a UNC path's server as the URI's authority:
  `\\server\share\d\memory.sqlite` gave
  `file://server/share/d/memory.sqlite`, and SQLite refuses every
  authority but an empty one and `localhost` (`invalid uri authority`).
  A store on a mapped drive or a `\\server\share` path, both of which
  resolve to UNC, failed each of those reads, and the binding check,
  which fails closed, kept the ring bound. An admin share on this host
  was worse: `\\localhost\C$\d` gave `file://localhost/C%24/d`, which
  SQLite opened as `/C$/d` on the current drive, another file. One helper,
  `_fsutil.sqlite_file_uri`, now writes every such URI: a UNC path goes
  whole into the URI's path after an empty authority,
  `file:////server/share/d/memory.sqlite` and
  `file:////localhost/C%24/d`. A store directory given as `\\?\C:\...`
  keeps that form, `file:////%3F/C%3A/...`, which Python 3.11 to 3.13's
  `as_uri` wrote with `%3F` as the authority (refused) and 3.14's writes
  as `file:///C:/...`; it is the one drive-letter path whose URI moves
  on 3.14, and the prefix survives `resolve` only where it was given or
  is needed, a name past `MAX_PATH` say. POSIX and `C:\` paths get
  `as_uri`'s URI byte for byte, on 3.11 to 3.14. There is no config key
  or flag. (`src/bettermemory/_fsutil.py`, `src/bettermemory/store.py`,
  `src/bettermemory/backup.py`, `src/bettermemory/daemon.py`,
  `src/bettermemory/posture.py`, `tests/test_sqlite_uri.py`)
- **Digests in bundles and anchors are compared as bytes.**
  `hmac.compare_digest` raises TypeError on a str that is not ASCII. A
  bundle manifest's file hashes and MAC and an anchor's chain digest now
  go through `log.same_mac`, as the log's own comparisons do, so a
  value that is not ASCII reads as a mismatch, not a traceback. The
  health proof encodes a state file's token with `surrogatepass`, so a
  token holding a lone surrogate reads as a proof that does not match.
  (`src/bettermemory/backup.py`, `src/bettermemory/anchor.py`,
  `src/bettermemory/_daemon_client.py`)
- **A manifest field UTF-8 cannot encode reads as a mismatch.** A
  bundle manifest holding a lone surrogate (`"\ud800"`) in a field the
  shape check does not hold to hex ended `backup verify` and `backup
  restore` in a UnicodeEncodeError traceback. The manifest MAC is now
  computed with `surrogatepass`, so such a bundle reads
  `manifest_mac_mismatch`; every bundle signed before verifies the same.
  (`src/bettermemory/backup.py`)
- **`log verify --help` names the origin check; the scan says to rotate
  first.** The help names the origin anchor beside the MACs, the chain,
  the head and the fold, its two outcomes (`origin_mismatch`,
  `unverifiable`) and the files whose removal accepts a replaced
  history. Every `security scan` remediation starts `Rotate the
  credential at its issuer: that is the fix`, since the log keeps every
  earlier version after an edit, a removal and a prune.
  (`src/bettermemory/cli/log_cmd.py`, `src/bettermemory/secret_scan.py`)
- **The security audit's text report escapes what a finding quotes.** A
  summary can quote a path or an OS error's reason, such as a symbolic
  link's target, and the text report printed it raw: a line break in
  one split the report's line, and an escape sequence reached the
  terminal. The store path, every summary and every fix now print
  through the same escaping `doctor` and `log` use. `--json` is
  unchanged. (`src/bettermemory/posture.py`)

#### Known limitations

- **The key ring's binding is trust on first use on upgrade.** A ring
  from before rc13 has no `<store_id>.path`, and the first owner's open
  after the upgrade binds it to the file it opens. When that is a file
  that borrowed the store's id (a repository's `./.claude-memory/`
  opened before the home store), it takes the ring as in rc12: the home
  store's key is retired, the head moves past the home log, which reads
  `tampered` (`tail_deleted`), and the home store opens for reading
  only. `[storage] cwd_store = false` closes the repository route, and
  a ring already bound is not taken this way.
- **An rc12 anchor whose key is lost before rc13 names it reads
  tampered.** An origin anchor written before rc13 names no key, and the
  first writing open that sees a held key verify its row names it. When
  the key that signed the row was lost before that open, nothing tells
  the loss from a rewrite: the origin reads `origin_mismatch` (reason
  `signature`), `log verify` reads `tampered`, and every segment whose key
  is not held reads `unaccounted`, where rc12 moved the anchor to the
  `rekey` row. Filing the old key back as `<store_id>.<first 16 hex of its
  fingerprint>.key` makes the anchor hold and be named; without the key,
  deleting `<store_id>.origin` and `<store_id>.head` accepts the history
  on purpose.

### 9.0.0rc12

The twelfth candidate closes the provenance gap an editor of the store
file could use to pass forged rows off as a copied store's, hardens the
daemon's HTTP surface and the client's trust in what answers on its
port, and adds the commands an operator needs to run bettermemory on a
team's machines: verified backups, an audit export with external
anchors, a scriptable daemon and a service unit, a self-check, a posture
audit, a secret scan, a usage report, a record's history and a config
report. The store's schema does not move. The new commands and keys are
opt-in; the security fixes change behaviour in the case each one
closes, and Changed below names every default that moved, with its
trigger. `docs/operations.md` is the operator's runbook for all of
it. It is a release candidate like the eleventh; the
stable 9.0.0 follows the candidates' testing.

#### Security

- **A forged log segment after this host's own reads `unaccounted`.** A
  writer who held the store file but not the key could append a `rekey`
  row naming a key nobody holds, a `memory_put` after it and the record
  it puts, and edit `meta.key_fingerprint`: after the owner's next open
  the planted memory read `local`, the health census missed it and `log
  verify` said only `unverifiable`. A segment of the log whose key this
  host's key ring does not hold, coming after one it does hold, is now
  foreign: its records read `unaccounted` on search, show, the recall
  pointer, the episode handoff and the health census, and `log verify`
  reports `foreign_segment` with its seq range, so the status is
  `tampered` and the exit code 1. A row this host's own current key
  signed into such a segment (a daemon that had the store open while the
  forged row went in) still reads `local`. The owner's opener no longer
  takes the old fingerprint from `meta`: a key that signs a segment of
  the log stays current, `meta` is set back, the foreign tail is closed
  with a genuine `rekey` row and a WARNING on the `bettermemory.store`
  logger names the segment. The leading segments, those before the first
  held one, are the documented case of a store copied here without its
  key, and keep their labels unless `[security] strict_provenance` is
  set (Added). (`src/bettermemory/store.py`,
  `src/bettermemory/log.py`, `tests/test_segment_trust.py`)
- **Where this host's history begins is anchored outside the store.**
  The rule above read the start of this host's history off the log, so
  one more edit (rewriting seq 1, or this host's first `rekey` row, so
  that no held key verifies it) made this host's rows and the forged
  segment after them all look like a copied store's, and the planted
  memory read `local` again. The key ring now holds
  `<store_id>.origin` beside the key and the head: the seq and the MAC
  column of the first row this host's key ring signed, written 0600
  under a sidecar lock. `Store.create` anchors seq 1; the owner's opener
  anchors the `rekey` row it writes when it takes over a store copied
  here; a store from before the anchor gets one at its first writing
  open (below). Every read checks the anchor with the segment map: the
  row at its seq must exist, carry its MAC and verify under a held key.
  When it holds, an unheld segment is leading only if it ends before the
  anchor. When it does not, `log verify` reports `origin_mismatch` (exit
  1, reason `missing`, `mac` or `signature`) and every segment whose key
  is not held reads `unaccounted`, leading ones included. The cost is
  one file read, one indexed row lookup and one HMAC per connection's
  first pointer check. (`src/bettermemory/log.py`,
  `src/bettermemory/store.py`, `tests/test_origin_anchor.py`)
- **Rows below seq 1 are never trusted.** The segment map and the
  opener's last-rekey lookup read the log at any seq, while `log verify`
  walks only seq 1 and up, so rows an editor inserted at seq 0 and below
  could form a segment that ended before the anchor and read as copied
  history. Both now read only seq 1 and up, and a record whose log row
  sits below seq 1 reads `unaccounted` on every read surface. No store
  writes such a row. (`src/bettermemory/log.py`,
  `src/bettermemory/store.py`)
- **A read-only open checks pointers when `meta` names another key.**
  A read-only open bound the key `meta.key_fingerprint` names, and none
  when this host does not hold it, so after the forged-segment edit
  above it checked no pointer: `memory history` printed the planted
  record as `local (unverified: no key)` and exited 0 while `log verify`
  read the store tampered. When this host's key ring holds the store's
  origin anchor, or its current key signs a segment of the log, a
  read-only open now checks pointers under that key, writing nothing and
  still refusing every write, so `memory history` reads the planted
  record `unaccounted` and exits 1. `stats` and `export` check no
  pointer and print no label, as before. (`src/bettermemory/store.py`,
  `src/bettermemory/history.py`, `tests/test_memory_history.py`)
- **A store id is the shape `create` writes, or the store is refused.**
  The key ring joins `meta.store_id` onto the keys directory, so a store
  file whose id was a relative path could bind a key planted beside it
  as the store's own. A store whose id is not 32 lowercase hex
  characters is now refused at open with a ValueError ("carries a store
  id this build never writes"). (`src/bettermemory/store.py`)
- **Every daemon route checks Host and Origin.** Only `/mcp` had the
  SDK's DNS-rebinding protection, so a page in a browser could read
  `/health` (the store path among it) through a rebound name. A
  middleware in front of every route now answers 421 unless the Host is
  `127.0.0.1`, `localhost` or `[::1]`, bare or with the daemon's own
  port, and 403 for an Origin, when one is sent, that is not `http://`
  plus one of those; an empty Origin is refused. Bodies under
  `/api/v1/` are capped at 4 MiB, the SDK's `/mcp` cap (413). A
  websocket upgrade is closed with 1008. (`src/bettermemory/daemon.py`)
- **A client trusts only a daemon that proves its token.** The hook
  client, the shim, `up`, `down`, `status` and `ensure_daemon` took any
  `{"status": "ok"}` on the state file's port as the daemon, so after a
  crash another account's listener on that port received the hook
  payloads and the token. `GET /health?nonce=<32 to 128 hex>` now adds
  `proof`, `hex(HMAC-SHA256(token, "bettermemory-health-v1:" + nonce))`;
  every client sends a fresh nonce on the health request it already
  made and compares the proof. A daemon writes `"health_proof": 1` into
  its state file; at the port of such a file a reply without a proof is
  never ours, and gets nothing: no payload, no token, no shutdown, no
  signal. A state file without the key, written by a daemon before
  rc12, is accepted only when the reply repeats its pid and store, so an
  rc11 daemon is still replaced as before. `shutdown_daemon` signals a
  pid only when the daemon proved itself or the pid is alive while the
  store's lock is held, never on a stale state file alone. `doctor`'s
  daemon check asks for the proof too and fails a reply without it, where
  `status --check` answers 3. The state
  directory is made 0700 before the daemon log is opened in it, and the
  log is created 0600. A hook to its own daemon is still one GET and one
  POST. What a listener's reply names, and what a bundle's manifest or a
  store file says about itself, reaches the text of `status`, `down`,
  `doctor`, `log verify`, `backup verify|restore` and `backup create`'s
  warnings and errors with each control and format character written as
  its ASCII escape (`\x1b`), as `stats` and `memory history` print stored
  text; unlike the U+FFFD it replaces, the escape encodes in every code
  page, cp1252 included.
  (`src/bettermemory/_daemon_client.py`, `src/bettermemory/daemon.py`,
  `src/bettermemory/shim.py`, `src/bettermemory/cli/daemon_cmd.py`)
- **Nothing for the daemon goes through a proxy.** The client's
  `health()` and `post()` used urllib, which sends a request for
  `127.0.0.1` through `http_proxy` unless `no_proxy` names it, so a
  configured proxy received the bearer token and the hook payloads. Both
  now use an opener with no proxy handler. This was true of every
  release before rc12. (`src/bettermemory/_daemon_client.py`)
- **An episode carrying a secret is refused.** `episode(action="write")`
  skipped the credential gate. It now runs the write gate's detectors
  over the body and the takeaway and, on a hit, writes nothing and
  answers `{status: "credential_warning", markers: [{kind, snippet,
  field}], hint}`; episodes take no acknowledge flag, so the value has
  to be removed. The refusal's event row records only the detector kinds
  and the fields. (`src/bettermemory/handlers/episode_write.py`)
- **The log's query previews are redacted with every detector.** The
  `query` and `probe_query` previews in the signed log were redacted
  with five token shapes against the write gate's fourteen, so a `gho_`,
  `sk-proj-`, `sk_live_`, `xoxb-` or `AIza` token reached the 32-character
  preview. The previews now pass through the gate's own detector spans
  (`credentials.secret_spans`) after the five legacy patterns, whose
  `[REDACTED:...]` markers are kept; a new span reads
  `[REDACTED:<detector kind>]`. A query with none of the shapes is
  byte-for-byte unchanged. Rows already in a log are not rewritten;
  `bettermemory security scan --log` finds a token left in one. Four
  detectors that took time quadratic in a run of their own prefix
  (`sk-`, `xox?-`, `xapp-`, `eyJ`) now match in linear time with the same
  results, since this path runs them on every search query.
  (`src/bettermemory/events.py`, `src/bettermemory/credentials.py`)
- **Audit rows are recorded with telemetry off.** With `[telemetry]
  enabled = false` the standing guard's rows and the override evidence
  on write and update events were dropped with the telemetry. A
  `standing_guard` row, and a `write` or `update` row with `forced=true`
  or a true `*_acknowledged` field, is now recorded whatever the switch
  says; every other row stays off. (`src/bettermemory/events.py`)
- **Declared identity loses its format characters; the CLI's user is
  the real uid's.** Declared values (client, version, model, session
  ids, `clientInfo`, the Stop hook's transcript actor) are stripped of
  Unicode format characters (bidi controls, zero-width characters)
  before validation, not refused; the workspace path and the attested
  principal keep theirs. The CLI's declaration principal is
  `os:<password-database name of the real uid>` on POSIX, or
  `os:uid:<n>` without an entry, where `$USER` chose it before; Windows
  still asks `getpass`. (`src/bettermemory/identity.py`,
  `src/bettermemory/cli/project_cmd.py`)
- **An organisation can pin settings the user's config cannot loosen.**
  A managed file with config.toml's own sections and keys, at
  `/Library/Application Support/bettermemory/managed.toml` on macOS and
  `/etc/bettermemory/managed.toml` on Linux and other POSIX systems, is
  read after the user's file; every key it sets is pinned, a user value
  that differs gets one warning naming both files, and a list replaces
  the user's whole. No environment variable moves it. It fails closed:
  a file that exists but cannot be read, is not TOML or holds an invalid
  value stops the load with an error naming it. It binds the user's
  config, not a process running as the user, which can still set
  `BETTERMEMORY_DIR` or open the store file. No managed file is read on
  Windows in this release (Known limitations). `examples/managed.toml`
  is a starting point. (`src/bettermemory/managed.py`,
  `src/bettermemory/config.py`)

- **The daemon takes its port for exclusive use on Windows.** It bound
  with `SO_REUSEADDR`, which on Windows also lets a socket bind a port
  another socket holds: a daemon started on a state file's stale port
  shared it with whatever held it, so the hooks reached that listener,
  refused it and had no daemon, and another process could bind the
  daemon's own port beside it. Where the platform has
  `SO_EXCLUSIVEADDRUSE` the daemon now asks for it, so a held port
  fails to bind and the daemon takes another and records it in its
  state file, as on POSIX. (`src/bettermemory/daemon.py`)

#### Added

- **`[security] strict_provenance`** (default false). True reads every
  row in a segment whose key this host does not hold as `unaccounted`,
  the leading segments of a copied store included: only what this
  host's own keys verify is trusted. The store reads it lazily, once per
  connection, and only when a row in such a segment is first labelled;
  a config that cannot be read reads as true, with a warning. It is the
  switch to pin in a managed file, and it is commented out in the
  config.toml a first run writes, as `[policy]`'s keys are, so a pin
  draws no warning about a line the user never wrote. (The keys of
  earlier releases that file sets, `[telemetry] enabled = true` among
  them, are in the files those releases wrote too, and each draws one
  where a managed file pins it to another value; `docs/operations.md`
  lists them.) (`src/bettermemory/config.py`,
  `src/bettermemory/store.py`)
- **`[policy] forbid_force` and `forbid_acknowledge`** (default off and
  `[]`). The first refuses a `memory_write` with `force=true`
  (`memory_update` has no `force`); the second names acknowledge flags
  (`credential`, `transient`, `scope_mismatch`, `user_claim`,
  `truncation`) whose `acknowledge_<name>=true` refuses the call, even
  where its gate would not have fired. A refused call writes nothing and
  records no event; its error starts `policy_refused:` and says whether
  the switch is managed. An unknown name, or a value that is not a
  boolean spelling, stops the config from loading.
  (`src/bettermemory/policy.py`)
- **`[storage] cwd_store`** (default true). False skips the
  store-directory rule's third step, `./.claude-memory/` in the working
  directory, so a cloned repository that commits that directory never
  becomes the store. The hook client and the shim follow it. A value
  that is not a boolean spelling stops the config from loading.
- **`bettermemory backup create|verify|restore`.** `create` writes a
  bundle (`<store dir>/backups/<UTC stamp>/` by default, `--out DIR`
  otherwise; directory 0700, files 0600): an online snapshot of
  `memory.sqlite` that is safe while the daemon writes, the head
  checkpoint at the snapshot and a manifest signed with the store's key
  (store id, key fingerprints, head, origin anchor, row counts, every
  file's SHA-256). Keys stay out unless `--include-key`. `verify` checks
  the hashes, the signature, the manifest's claims and then the whole
  log on a private copy, never in place (exit 0 ok, 1 mismatched or
  tampered, 2 unverifiable). The signature counts only under a key the
  keys directory holds: anyone who can write a bundle can remake it
  under a key of their own and put that key in it, so a manifest signed
  with a key only the bundle carries reads `self_signed` and the bundle
  `unverifiable`. A bundle whose log goes on from a segment under a key
  the keys directory holds into one under a key only the bundle carries
  (this host's snapshot, rekeyed by whoever wrote the bundle) reads
  `foreign_segment` and `tampered`, as the live store reads that shape.
  `restore` refuses while the daemon or
  spawn lock is held, refuses a bundle that does not verify, and refuses
  a `self_signed` one (an `--include-key` bundle on a machine that holds
  none of its keys, or one forged from nothing this host signed) unless
  `--trust-bundle-key`, which makes the
  bundle's key the store's current key and is named in the record. It plans
  without `--yes` (exit 3), moves the current files aside as
  `<store>.pre-restore-<stamp>` and never deletes them, installs the
  keys and the anchor the bundle needs, rewinds the head checkpoint on
  the record (`<store_id>.backup_restore.json`) and runs `log verify` on
  the result. (`src/bettermemory/backup.py`,
  `src/bettermemory/cli/backup_cmd.py`)
- **`bettermemory log export` and `log anchor`, and `log verify
  --anchor`.** `export` writes the log as JSON Lines
  (`bettermemory.audit/1`, one object per row) for a SIEM: seq, ts,
  kind, class, actor, record ids, the payload with bodies and other free
  text replaced by `{sha256, len}` unless `--include-bodies`, the MAC and
  its link, the segment's key fingerprint and `verified`.
  `--since-seq`/`--until-seq` make it incremental, `--kinds` filters, and
  `--out` writes 0600 atomically and reports the cursor to pass next. A
  cursor past the log's end, or inside the range a `backup restore`
  rewound, warns on stderr; a restore onto a host that held no copy of
  the store knows no such range and warns of none, which the runbook's
  `prev_mac` link check catches.
  `anchor` takes `{store_id, seq, mac, chain_sha256, created_at}` of the
  log's last row, to keep off the host; `log verify --anchor FILE`
  (repeatable) reports `anchor_mismatch` when history up to an anchored
  seq was rewritten, even by a process that holds the key.
  (`src/bettermemory/audit_export.py`, `src/bettermemory/anchor.py`,
  `src/bettermemory/cli/log_cmd.py`)
- **`log verify` reports the origin anchor.** `--json` gains `origin`
  (`{status, seq?, reason?}`, status `ok`, `absent`, `mismatch` or
  `unverifiable`) after `head`, and the text report one line, `origin
  ok at seq 1`. `absent` is no finding.
- **Scriptable `up`, `down` and `status`.** `status --check` exits 0
  running and answering as this store's daemon, 1 not running, 2 a state
  file whose daemon does not answer but may be alive, 3 the port
  answers but not as this store's daemon. `status --json`, `up --json`
  and `down --json` print one object each, never the token.
  (`src/bettermemory/cli/daemon_cmd.py`)
- **`bettermemory service install|uninstall|status|print`.** A launchd
  LaunchAgent on macOS (`io.github.0mattias.bettermemory`) or a systemd
  user unit on Linux (`bettermemory.service`) that runs `<this
  interpreter> -m bettermemory up --foreground` in the state directory,
  restarts it after a failure (not after a clean exit), and pins the
  store, keys and state variables the installing shell sets. `print`
  writes nothing; other platforms get the command to supervise and exit
  2.
  (`src/bettermemory/service_units.py`,
  `src/bettermemory/cli/service_cmd.py`)
- **`bettermemory doctor`.** A read-only self-check of the install, the
  config, the store's location, the store and its key, the daemon, the
  Claude Code and other client wiring and the disk, each `ok`, `warn` or
  `fail` with a one-line fix; `--json`; exit 0, 1 on any failure, 2 on
  warnings only. (`src/bettermemory/doctor.py`)
- **`bettermemory security audit`, `security sandbox` and `security
  scan`.** `audit` grades the modes and owners of the store, its WAL
  files, the keys, the head and origin files, the state directory, state
  file and daemon log; the key against the store; the store and keys
  outside git working trees and synced folders; a store chosen by
  `./.claude-memory`; and Claude Code's sandbox settings. It never
  chmods or writes (exit 0, 1 on a high finding, 2 on warnings).
  `sandbox` prints the Claude Code sandbox block for this host's real
  paths (`--format settings|managed`). `scan` finds where a secret-shaped
  string persists in memories, tombstones, episodes and conflicts, and
  with `--log` in every log payload, and never prints the value; it
  refuses a file inside a backup bundle, which opening would change,
  and a `--store` file with other names (hard links).
  (`src/bettermemory/posture.py`, `src/bettermemory/sandbox_settings.py`,
  `src/bettermemory/secret_scan.py`, `src/bettermemory/cli/security_cmd.py`)
- **`bettermemory stats`, `memory history ID` and `completion
  bash|zsh|fish`.** `stats` is a read-only usage and growth report from
  aggregate SQL (no MAC is checked). `memory history` lists every log
  row that made, changed, verified, removed or restored one record, each
  with its MAC checked, and `--diff` adds the body diffs. `completion`
  prints a script generated from the parser itself.
  (`src/bettermemory/cli/stats_cmd.py`, `src/bettermemory/history.py`,
  `src/bettermemory/cli/completion_cmd.py`)
- **`bettermemory config path|show|check`.** `path` prints the config
  file, the managed file, the store directory with the rule that chose
  it, and the keys and state directories. `show` prints every setting in
  force labelled `default`, `user` or `managed`, and names a value of
  the user's that a pin or the loader ignored. `check` reports unknown
  sections and keys with a did-you-mean, removed and renamed keys,
  values that fall back to the default, `[scopes] allowed` entries
  outside the scope grammar, unknown `forbid_acknowledge` names and the
  port range (exit 0, 1 on an error, 2 on warnings only). It never
  creates the config file, the store or the keys.
  (`src/bettermemory/cli/config_cmd.py`)
- **Release provenance.** A tag's GitHub release carries `SHA256SUMS`
  over the wheel and the sdist and a CycloneDX SBOM of the lock
  (`bettermemory.cdx.json`), and every run that passes the gates signs a
  GitHub build-provenance attestation for the files it built
  (`docs/release.md`, "Verifying a release").
  (`.github/workflows/release.yml`)
- **Supply-chain checks.** CodeQL over Python and the workflows, OpenSSF
  Scorecard, a daily audit of `uv.lock` against OSV
  (`tools/audit_lock.py`, with dated exceptions in
  `tools/audit-ignore.toml`) and a license check of the runtime closure
  against `tools/license-policy.toml` (`tools/sbom.py --licenses`); none
  gates a release. `tests/test_workflow_policy.py` holds every workflow
  to actions pinned by commit, read-only top-level permissions with
  write scopes granted per job from an allowlist, checkouts that keep no
  token and no untrusted expression inside a `run:` script.
  (`.github/workflows/`, `tools/`)

#### Changed

- **A store moved away and back reads `tampered`.** A store moved from
  this host to another and back, or opened once under another
  `BETTERMEMORY_KEYS_DIR` (which rekeys it), carries a segment signed by
  a key this host does not hold after this host's own, which is the
  foreign shape: the other key's rows read `unaccounted` and `log
  verify` exits 1, where it read `unverifiable` with the labels kept.
  Filing the other key in this host's keys directory as
  `<store_id>.<first 16 hex of its fingerprint>.key` restores `ok`.
- **A store gets an origin anchor at its first writing open.** On a
  store from before rc12, the first open through the owner's opener
  (the daemon, `serve`, and the CLI commands that open through
  `open_or_create`) writes `<store_id>.origin` in the keys directory, at
  the first row of the earliest segment this host holds when that row
  verifies. A read-only command (`log verify`, `log export`, `export`,
  `stats`, `memory history`, `doctor`, `backup create` and `verify`,
  `security`) writes nothing. Copying a store file
  from its source machine over this host's copy again, with the same
  store id, then reads `origin_mismatch` (Known limitations has the
  recovery).
- **A config section written as a scalar names its file.** In rc11 a
  config.toml with `[storage]` or `[scopes]` written as a scalar
  (`storage = 5`) crashed with AttributeError, and one with
  `[behavior]` or `[telemetry]` with TypeError. Each, and the new
  `[security]` and `[policy]`, now raises `ValueError: malformed config
  in <path>: [<section>] = <value> must be a table`, as the managed file
  already did: the daemon does not start, the hook client prints the
  error and exits 0, and the store's `strict_provenance` lookup reads as
  strict. `[daemon]`, `[capture]` and `[membership]` written as a scalar
  still load that section's defaults, as in rc11, so the hooks keep
  running, and now log a warning that names the file and the line;
  `config check` reports the line as an error. A TOML syntax error or a
  config.toml that is not UTF-8 raises `malformed config in <path>:
  ...`, naming the file. A config.toml that starts with a UTF-8
  byte-order mark now loads, and `doctor` reads it as the loader does.
  (`src/bettermemory/config.py`, `src/bettermemory/doctor.py`)
- **`status` and `down` keep a live daemon's state file.** Both removed
  a state file whose daemon did not answer, so a starting, busy or hung
  daemon disappeared from view, and `down` could no longer find it to
  stop it. A state file is now removed only when its pid is gone or the
  store's lock can be taken. For a daemon that is alive and holds the
  lock but does not answer, `status` now prints `state present, daemon
  not answering (pid N holds the lock); the state file ... is kept`
  where it printed `not running for <store> (stale state file for pid N
  removed)`, and `down` still finds it: after its wait (30 s) it sends
  SIGTERM to the pid that holds the store's lock, and the daemon stops
  once its event loop can act on the signal (`down` exits 1 and keeps
  the file when that takes longer than its second wait). A hook
  still starts a daemon beside a hung one, which finds the lock taken
  and exits, and the hook gives up after the same wait as before (8 to
  9 s); this entry changes what `status` and `down` see, not that. A
  port that answers with something other than a daemon's `/health` reads
  as not ours instead of a traceback. Plain `status` exits 0 as before.
- **The daemon's Host check pins its own port.** `/mcp` accepted any
  loopback port in the Host header, as the SDK does; every route now
  requires the daemon's bound port or none, so a client reaching the
  daemon through a local port-forward (`ssh -L 9000:127.0.0.1:7397`) or a
  browser inspector on another localhost port gets 421 or 403.
- **The query hash changes for a prompt with a newly covered secret.**
  Its preview and hash now carry the new marker, so the Stop hook's
  re-audit dedup, the recall review's probe digest and a re-run of
  `migrate v8` across the upgrade do not match a row written before
  rc12 for such a prompt: one extra audit row, review candidate or
  imported event at most.
- **The workflows and the build.** Dependabot's Python updater moves
  from `pip` to `uv` with `lockfile-only` and a seven-day cooldown, and
  groups minor and patch updates only. The build backend is pinned to
  `hatchling==1.32.4`, which also ships in the sdist. The release build
  no longer uses the uv cache, and no checkout keeps its token.
  `publish-mcp.yml` pins `mcp-publisher` 1.8.6 and checks it against
  its release's checksums before unpacking it.
  (`.github/dependabot.yml`, `pyproject.toml`, `.github/workflows/`)

#### Fixed

- **The read-only commands write no config and read the managed file.**
  `log verify`, `log export`, `log anchor`, `stats`, `memory history`,
  `security scan` and `backup create` and `restore` (its plan included)
  wrote a default config.toml on an account that had none, and `security
  sandbox`, `security audit`, `doctor` and `service print|status` read
  the bare defaults there, without the managed file's pins, so on a
  freshly provisioned account they named the home store where the
  daemon serves the pinned one. Each now reads the config without the
  first-run write and with the managed file applied. A config that does
  not load ends `log export`, `log anchor`, `stats`, `memory history`,
  `security scan`, `backup create` and `restore` and every `service`
  subcommand with the reason on stderr and exit 2 (`log verify`: 1, a
  log that cannot be verified), not a traceback; `security sandbox`
  exits 1 with the reason, and `security audit` and `doctor` report it as
  a finding. A store file that cannot be read ends the first five and
  `log verify` the same way; `backup create` exits 1 (not written) and
  `backup restore` plans over it. `backup verify` reads a bundle file it
  cannot read as `file_unreadable` (mismatched). `log export --out` and
  `log anchor --out` refuse the store file, its `-wal` and `-shm`, and
  any path in the keys directory, which they would have replaced,
  compared by file identity, so a symlink, a firmlink or a spelling in
  another case on a volume that folds case is refused too.
  (`src/bettermemory/cli/_common.py`, `src/bettermemory/sandbox_settings.py`,
  `src/bettermemory/doctor.py`, `src/bettermemory/cli/log_cmd.py`,
  `src/bettermemory/cli/service_cmd.py`)
- **A listener that does not speak HTTP is no daemon.** `health()` let
  `http.client.HTTPException` escape, so a stale port held by another
  protocol made every hook fail and `down` end in a traceback. It now
  reads as nothing answering: the hook starts a daemon beside it and
  `down` reports no daemon. (`src/bettermemory/_daemon_client.py`)
- **The help says what `export` and `tombstones prune` are.** `export`
  was described as round-trippable and meant for backup; it leaves out
  the log, episodes and most tables, and nothing restores a store from
  it, so its help now points at `backup create`. `tombstones prune`
  said it hard-deletes; it removes the tombstone rows, and the log keeps
  every earlier version of the record. (`src/bettermemory/cli/export.py`,
  `src/bettermemory/cli/tombstones.py`)
- **The credential hints no longer mention the removed sync lane.**
  (`src/bettermemory/handlers/write.py`,
  `src/bettermemory/handlers/update.py`)

- **A hand-edited payload nested past 128 levels exports as its text.**
  Python 3.14's JSON parser is bounded by the C stack rather than the
  recursion limit, so on some hosts it parsed a payload nested 100,000
  deep that 3.11 to 3.13 refuse, and `log export --include-bodies` then
  overflowed the stack encoding it. The export now reads a payload more
  than 128 levels deep as text on every Python; the store writes a few.
  (`src/bettermemory/audit_export.py`)
- **A MAC column or a capture batch token that is not ASCII is a
  mismatch.** Both were compared as text with `compare_digest`, which
  raises TypeError on a non-ASCII string, so a store file edited that
  way ended the open, `log verify`, `memory history` or `capture
  review` in a traceback. They are compared as UTF-8 bytes now.
  (`src/bettermemory/log.py`, `src/bettermemory/capture.py`)

#### Known limitations

- **No managed file on Windows yet.** A standard account can create
  folders under ProgramData, so a file there could be one user's,
  binding every other account. `managed_config_path()` answers None on
  Windows until the file's owner and ACL are checked, and `config path`
  says so. 9.0.0rc13 reads it from the registry instead, a key only
  administrators can write, and reads nothing under ProgramData (above).
- **The origin anchor is trust on first use on upgrade.** An edit made
  to a store before its first writing open under rc12 is not caught by
  the anchor. The plain forged-segment recipe is still caught by the
  segment rule, and `strict_provenance = true` catches the rest. An
  editor who sets `meta.store_id` to a fresh id detaches the store from
  this host's key ring, so it opens as a store copied here, and only
  `strict_provenance` reads its earlier rows `unaccounted`.
- **A store file that carries another store's id binds its key ring.**
  The key, the head checkpoint and the origin anchor are named after
  `meta.store_id` alone, an id `log export`, `log anchor` and bundle
  manifests carry. A store file with the id of a store on this host,
  such as a repository's `./.claude-memory/`, retires that store's key
  at its first writing open here, and a high seq in it pushes the shared
  head past that store's log, which then reads `tampered`
  (`tail_deleted`). `[storage] cwd_store = false` closes the repository
  route. 9.0.0rc13 binds the key ring to one store file, and a second
  file with its id opens for reading only and writes nothing; a ring
  from before rc13 is bound by its first owner's open (above).
- **A store file copied over again reads `origin_mismatch`.** Copying
  the store file from its source machine over this host's store again,
  with the same store id, changes the history at the anchor. The
  recovery is `bettermemory backup restore` of a bundle, or deleting
  `<store_id>.origin` and `<store_id>.head` in the keys directory to
  accept the replaced history, which trusts the file again on purpose.
- **A restore is guarded only against the daemon and spawn locks.**
  `serve`, the shim's in-process fallback and the writing CLI commands
  open the store without those locks; close every session before `backup
  restore --yes`. Its text output does not show the origin anchor it
  replaces; `--json` and the restore record carry it.
- **Only some readers refuse a backup bundle's file.** Opening a
  bundle's snapshot as a store rewrites its header, and the bundle then
  fails its hash. `security scan`, `log verify`, `log export`, `log
  anchor`, `stats` and `memory history` refuse a file with a bundle's
  manifest beside it, through a symlink too, and the scan refuses a
  `--store` file with hard links; `export`, `doctor` and `membership
  export` do not check, nor does anything check a hard link named through
  `BETTERMEMORY_DIR`. Copy a bundle's `memory.sqlite` to a directory of
  its own before pointing a command at it. 9.0.0rc13 refuses a bundle's
  file in every command that opens a store (above); a hard link named through
  `BETTERMEMORY_DIR` is still not checked.
- **SIEM rules should alert on `verified: null` as well as `false`.** An
  editor without the key can make later rows of `log export` read
  `verified: null` (a key not held) by editing `meta.key_fingerprint` or
  inserting a `rekey` row; `log verify` reports that store unverifiable
  or tampered.
- **The posture audit reads file-based Claude Code managed settings
  only.** `security audit` reads `managed-settings.json`, its
  `managed-settings.d/` drop-ins and the user's settings; server-managed
  settings and MDM sources (a macOS profile, the Windows registry), which
  Claude Code ranks above those files, are not read, so the audit can
  describe a policy that is not in force there.

### 9.0.0rc11

The eleventh candidate carries the tenth's changes with the two items
the rc9 review recorded and the testing period put on the gap list: a
daemon started by hand runs in its state directory, and the order of a
forced exit against the workers' connections is pinned by tests. It is
a release candidate like the tenth; the stable 9.0.0 follows the
candidates' testing.

#### Fixed

- **A daemon started by hand runs in its state directory.** `bettermemory
  up --foreground` kept the directory it was started from, which Windows
  holds open for the process's life: started from inside the store
  directory, as a supervisor with no working directory set or a hand-run
  start does, it pinned that directory against a rename or a removal
  past `down`, where the spawned daemon has run in its state directory
  since rc10. The foreground command now enters the state directory
  itself before it serves, with the path variables it resolves at
  startup made absolute first, through the one helper the spawn uses
  for its child. (`src/bettermemory/cli/daemon_cmd.py`,
  `src/bettermemory/_daemon_client.py`)

#### Changed

- **The order of a forced exit is pinned.** A second SIGINT makes
  uvicorn skip the app's shutdown event, and the rc9 review recorded
  that the daemon's lock could then be given back with a worker's
  connection still open. It is not: the loop's close cancels the app's
  lifespan, which stops and joins both worker pools before the store is
  closed and the lock given back, under `WORKERS_CLOSE_SECONDS`. Two
  tests now hold a hook body at a forced exit and assert the worker's
  connection is closed before the lock is given back, and that a body
  held past the bound cannot pin the lock: that one connection closes
  as the process ends, which the WAL store tolerates. On Python 3.12 and
  later uvicorn's own stop first waits for the requests in flight, the
  graceful bound at most, since `asyncio.Server.wait_closed` waits for
  their connections there; the test lowers both bounds.
  (`tests/test_daemon_store_watch.py`, `src/bettermemory/daemon.py`)

### 9.0.0rc10

The tenth candidate carries the ninth's changes with the first of the
open list after rc9: the daemon's tool bodies off its event loop, the
brief's claim under the state's lock, a store rekeyed only by its owner,
one git runner, the Stop's settle order, and the test suite's stores
closed. It is a release candidate like the ninth; the stable 9.0.0
follows the candidates' testing.

#### Added

- **The RS-F7 receipts under `bench/`.** The figures the rc7 entry cites
  now have their result files, written from each stage's reading (the
  post-hoc parts labelled as such) and kept as written: Stage A, B and C under
  `bench/recall/results/rsf7-*`, the standing threshold's kappa round
  and the update-relation round under `bench/decisions/results/rsf7-*`.
  Each names the commit it measured; `bench/README.md` says a result is
  read by its commit, since a tree's declared version is not a release.

#### Fixed

- **The brief claims its ids under the state's lock.** The brief read its
  segment's shown set, selected, printed and only then noted what it
  showed, each step outside the lock a prompt recall of the same session
  claims its delivery under, so on a resume (the prompt that resumed the
  session fires with the SessionStart) a recall could deliver an id
  between the brief's read and its print, and the segment showed it
  twice. `RecallState.claim_brief` now decides under the lock, as the
  recall's claim does, and notes the kept ids as the brief's show in the
  same step; the omitted list names the ids a concurrent recall took.
  (`src/bettermemory/recall.py`)
- **A store is rekeyed only by the opener that owns it.** `Store.open`
  replaced a missing or foreign key by default, retiring the key file,
  writing a new one and splitting the chain with a `rekey` row: a reader
  on another machine, a daemon under the wrong key directory or a plain
  `Store.open(path)` split the chain on first contact. The default is now
  to refuse: such a store opens for reading, and the first write raises,
  naming the store, the key directory and the two ways out. The hooks,
  the daemon and the CLI open through `open_or_create`, which rekeys as
  before, and so do the migrations. (`src/bettermemory/store.py`,
  `src/bettermemory/migrate_v8.py`, `src/bettermemory/membership_migrate.py`)
- **An interrupted turn's audit draws its pool as a Stop would, from
  either hook.** Under `[behavior] prompt_recall = "everywhere"` the
  prompt hook probed the interrupted turns it settled under the unscoped
  search's admission, where a Stop settling the same turn used the hooks'
  prefilter, so a `turn_audited` row's verdict could depend on which hook
  wrote it. Both hooks now probe interrupted turns under the prefilter;
  the prompt recall's own pool is unchanged, and the texts that described
  the two pools as one say what each is. (`src/bettermemory/recall.py`,
  `src/bettermemory/hook.py`, `plugin/hooks/hooks.json`)
- **The GitHub release carries the version's CHANGELOG section.** The
  release workflow's generated notes list merged pull requests, and this
  repository lands commits directly, so each pre-release page read as a
  compare link alone. `tools/changelog_section.py` prints the tag's
  section into the release body ahead of that link; the build job runs
  it first and fails when the tag's version has no section, so nothing
  reaches PyPI for such a tag. (`.github/workflows/release.yml`,
  `docs/release.md`)

#### Changed

- **One git runner for the origin capture and the history reader.** The
  origin capture ran git in the caller's environment, so a shell that
  exported `GIT_DIR` and `GIT_WORK_TREE` had a `memory_write` capture the
  environment's repository for an unrelated cwd; the history reader
  scrubbed those variables and pinned fsmonitor and hooks off but logged
  nothing, not even a hang against its 20 s bound. Both now run through
  `_git.run_git`: the scrubbed environment and the pins on every call,
  UTF-8 with errors replaced (or bytes for the object reader), the tiered
  logging and the failure counters. (`src/bettermemory/_git.py`,
  `src/bettermemory/origin.py`, `src/bettermemory/location.py`)
- **The Stop settles its own turn before it audits the past ones.** The
  interrupted turns a Stop audits are walked after its retrieval and
  pointer settlements and its review count, so a long walk delays only
  their rows. (`src/bettermemory/hook.py`)
- **`Store.events_since` reads a window's rows, not the log.** The cut
  compared the stamp's first nineteen characters, which no index serves,
  so every window read scanned the whole log. The read now finds the
  window's first row through the `log_by_ts` index and walks the rows
  from there by sequence number, in the plain read and the event window
  memo's cold read alike: 0.07 ms against 2.1 ms for a 100-row window on
  34,000 rows, the same rows in the same order. No schema change.
  (`src/bettermemory/store.py`)
- **A spawned daemon runs in its state directory.** It inherited the
  working directory of the hook that started it, a project checkout,
  which Windows holds open against a rename or a delete for the daemon's
  life; the path variables it resolves at startup are handed to it
  absolute. (`src/bettermemory/_daemon_client.py`)
- **Every workflow action is pinned to a commit.** `actions/checkout`,
  `actions/upload-artifact`, `actions/download-artifact` and
  `astral-sh/setup-uv` floated on their major tags; each now names the
  commit behind its release tag, with the version in a comment, as the
  publishing actions already did. (`.github/workflows/`)
- **The test suite's shared store fixture closes its Store.** Every
  test that took `store` left a sqlite connection to the garbage
  collector, which Python 3.13 and later report as a ResourceWarning at
  finalization; the fixture now closes it when the test ends, and an
  autouse fixture closes every other store a test opens on its own
  thread, inline or through a function-scoped fixture. The rc7 release
  build logged 1,914 such warnings and the CI test job 1,903 lines,
  1,833 with the shared fixture closed and 38 with the autouse fixture
  too; the rest were the stores the tests open themselves, and 32 of
  the 38 the stores one test's own threads reopened, which that test
  now closes. (`tests/conftest.py`)
- **The daemon runs the tool bodies on worker threads.** The nine MCP
  tool handlers are coroutines whose bodies await nothing: the origin
  capture's git calls, the sqlite reads and the response build ran on
  the daemon's event loop, so a hook of another session sent during a
  tool call waited the call out (a body held for 20 s answered a hook
  sent 0.5 s in after 19.6 s). The daemon now runs them on a second
  pool beside the hooks' (`TOOL_WORKERS` threads, each with its own
  connection), each body over a copy of the handler bundle bound to
  the worker's store; the same hook answers in 13 ms, and a
  memory_search through the shim costs what it did (p50 19.2 ms before,
  19.3 ms after, the read arm of `bench/daemon/latency.py`). Two calls
  of one session may now run at once, so the session's state takes a
  lock. The stdio server runs the bodies inline as before.
  (`daemon.py`, `_handlers.py`, `builder.py`, `session.py`)

### 9.0.0rc9

The ninth candidate carries the eighth's changes with one fix the
eighth's release run found: the rc8 tag was cut on a green CI run, and
the release workflow's Windows leg then failed on a test that moved the
store right after `bettermemory down` returned, so nothing was
published under 9.0.0rc8.

#### Fixed

- **`down` returns once the daemon has released the store.** `down` took
  the daemon as stopped when its port closed, and the daemon closes its
  port first, as the server stops accepting, while its store's connection
  closed only as the process exited, after the lock it holds for its
  whole life was already given back; so for a moment after `down`
  returned the store's files were still open. POSIX renames through an
  open handle and Windows refuses, which is how the release run's Windows
  leg caught it. The daemon now closes its store and removes its state
  file before it gives the lock back, and `down` waits for that lock: a
  free lock means no handle of the daemon is open on the store, so the
  store can be moved, copied or removed the moment `down` returns, on
  every platform. The port stays the signal for a daemon that holds no
  lock (one from before the lock, or a zombie whose handles the OS
  dropped). With `down` waiting for the real end of a stop, the stop is
  bounded: the daemon gives the requests in flight 20 s, the longest
  bound a handler runs under (the git runner's), before uvicorn cancels
  them, and `down` waits that plus the hook workers' 10 s join before
  its signal fallback (5 s before, which covered only the port). A
  SIGTERM now stops the daemon the same ordered way: uvicorn re-raises a
  captured SIGTERM once it has served, and with no handler of its own
  the daemon died there, its state file left behind. A stand-in daemon
  that stops in the real order, port first and lock a second later,
  fails `down` on the old predicate and passes on the new; a second test
  sends a foreground daemon SIGTERM and finds its exit 0, its state file
  gone and its lock free. (`src/bettermemory/_daemon_client.py`,
  `src/bettermemory/daemon.py`, `tests/test_daemon_lifecycle.py`)

### 9.0.0rc8

Tagged as `v9.0.0rc8` on 820d7464 and never published: its release
run failed on the race the ninth candidate fixes, and its changes ship
in rc9.

The eighth candidate adds no behaviour. The dependency lock moves off
the advisories a vulnerability audit found, a refusal keeps its words on
the wire under the SDK versions a fresh install now resolves, and a
store batch whose transaction cannot begin leaves the store as it was.

#### Fixed

- **A refusal's words reach the model on every supported mcp.** mcp
  2.0.0 forwarded the text of every exception a tool raised; from 2.1
  the SDK treats anything but its own `ToolError` as a crash and sends
  the client only `Error executing tool <name>`. The handlers refuse
  with `ValueError`, so a fresh install of rc7, which resolves mcp
  2.3.0, lost every refusal's words: an invalid scope, a cap, an unknown
  or tombstoned id. Each `ToolHandlers` tool method now re-raises a
  `ValueError` as `RefusedError`, which derives from both `ToolError`
  and `ValueError`: the wire form is what 2.0.0 sent, a direct caller
  that catches `ValueError` still does, and a genuine crash stays masked
  as the SDK intends. Two tests that simulate a crash read its words off
  the wrapper's cause chain now, which both SDK versions keep.
  (`src/bettermemory/_handlers.py`, `tests/_mcp.py`)
- **A batch whose BEGIN fails leaves the store transactional.**
  `Store.batch` marked itself open before issuing `BEGIN IMMEDIATE` and
  cleared the mark only around its body, so a BEGIN that failed on the
  busy wait against another writer left the mark set until the daemon
  restarted: every later mutation ran in autocommit with no head
  checkpoint, and every later batch was refused as already open. The
  mark now follows a BEGIN that succeeded. (`src/bettermemory/store.py`)
- **The texts, the example and the template follow the code.** The
  shipped `examples/programmatic_client.py` answered the write review
  without the standing score, so its first write could not commit under
  rc7; it answers the four questions now, and a test drives its review
  flow against an in-process server. The served `memory_admin`
  description, api.md, internals.md, the plugin skill and the
  system-prompt text name the same thirteen actions; the config
  template gives the four-step store-directory rule and the recall's
  top-three delivery; the standing table joins both table lists; the
  rc7 entry's p95 line reads what the bench file holds; the stale
  docstrings in `hook.py`, `cli/__init__.py` and `config.py` no longer
  cite removed modules or surfaces.

#### Changed

- **The dependency lock.** `uv lock --upgrade` within the declared
  bounds, which do not move: mcp 2.0.0 to 2.3.0, starlette 1.0.0 to
  1.10.0, pyjwt 2.12.1 to 2.15.1, cryptography 48.0.0 to 50.0.2,
  python-multipart 0.0.27 to 0.0.32, httpx2 and httpcore2 2.9.1 to
  2.16.0, anyio 4.13.0 to 4.15.1, opentelemetry-api 1.44.0 to 1.45.1,
  and the development tools (ruff 0.17.2, mypy 2.4.0, pyright 1.1.414,
  pytest 9.1.1). pip-audit on the exported runtime closure read 52
  advisories in 9 packages before the refresh and none after it.

### 9.0.0rc7

The seventh candidate puts what is in force in front of the model and
delivers what the evidence supports. Unit RS-F7's measurements decided
each change below (Stage A, 100 live prompts with their top ten hits
graded; Stage B, 60 prompts answered under six recall conditions and
judged blind; Stage C, 41 session starts and compactions answered under
five brief conditions and judged blind), and every constant names the
measurement that set it. The recall's rule name moves from `rs-v5` to
`rs-v6`, and every row of the recall surface and its audit carries it.
The store's schema gains one membership table; a store rc7 opened reads
under rc6 as before.

#### Added

- **Standing memories.** A memory is standing when it directs future
  work until it is replaced: a ruling, a rule, a standing preference.
  The write review asks it as a fourth question, as a probability, and a
  committed write stores the answer beside its record in a `standing`
  table (the `standing_put` row kind, folded like homes; rc6 skips the
  kind, so a rollback still reads the store). A record is standing when
  its answer for its current body is at or above `standing.TAU_STANDING`
  (0.30: on 100 stored records answered blind by the ENG-7 judges, the
  threshold agreeing best with their settled verdicts, weighted kappa
  0.637 where the judges agree with each other at 0.764) or its category
  is ambient. `memory_update` takes `standing=`; `memory_show` carries
  the question for a record with no answer for its current body;
  `memory_admin(action="answer_standing")` answers five at a time, and a
  session's first output names how many wait.
  (`src/bettermemory/standing.py`)
- **What is in force.** `in_force` walks the `supersedes` and `extends`
  links of admitted active records: a superseded record is history and
  its head takes its slot; the newest in-force record extending a record
  is named. The prompt recall gives a history hit's slot to its head,
  marked `[replaces <id>]` and decided by the head's own gates; the
  session brief shows no history row; both mark a record another
  in-force record extends `[extended by <id>]`.
  (`src/bettermemory/in_force.py`)
- **The session brief is two lists ranked by the owner's words.** At a
  SessionStart inside a project: STANDING, the caller's standing records
  in force, eight at most, each line marked `[in force]`; then WORK, the
  admitted in-force records the conversation so far bears on, eight at
  most, drawn from the hooks' pool over a probe of the newest 12,000
  characters of owner words in the session's transcript
  (`transcript.owner_words`), history excluded, an open contradiction
  held. Both lists rank by the probe; with no transcript they rank by
  recency. The header counts both lists, and there is no character cap.
  Stage C decided the shape on a seeded draw of 41 cases from the
  sealed copy: STANDING 8 plus WORK 8 raised the judged reply score by
  +0.45 [+0.11, +0.82] over no brief, the one condition whose interval
  cleared zero (rc6's brief read -0.07 [-0.33, +0.16]). Gone with the
  old brief: the background fill of memoised verdicts, the brief's
  window, its 1,200-character cap and its eight-newest-fresh rule. The
  `session_brief` row records `standing`, `work`, `probe_query` and
  `ranked_by`.
- **Every recall outcome is a row.** A probed owner prompt that
  delivered nothing writes `recall_held` naming its cause (`no_signal`,
  `no_hits`, `every_hit_held` with each hit's reason, `nothing_rendered`,
  `segment_claimed`). Pointers a segment showed and nothing settled are
  written as `unused` when the segment closes. A turn the owner
  interrupted is audited by the next Stop, from the previous audit on
  (`reply_source` `interrupted`). `turn_audited` and `search_miss` rows
  carry `rule`.
- **The bench's brief arm.** `bench/daemon/latency.py --arm brief` times
  the session brief on the sealed copy, as migrated and with standing
  answered on eight records, with the transcript of a session not yet
  written and with one holding a session's worth of prompts, as the
  service time and as the hook's wall time. On the sealed copy the
  brief's service p50 reads 87 ms probed as migrated and 96 ms with both
  lists filled, the hook's wall 129 and 134 ms, every service p95 under
  140 ms and the hook's wall p95 at 145 and 148 ms (part 4's bound:
  300). (`bench/daemon/results/latency-2026-10-24-*`)

#### Changed

- **The recall's gates.** The prompt recall delivers the probe's top
  three unless held by provenance, an open contradiction, admission or
  the segment; the coverage-label hold (`low_relevance`) and the twelve
  deliveries per segment are gone. In Stage A the labels did not
  separate relevant from irrelevant records (high 0.34, medium 0.30, low
  0.28); the live log's 202 segments reached twelve deliveries once; in
  Stage B the unfiltered top three raised the judged reply score by
  +0.39 [+0.17, +0.62] over no block.
- **Admission by declared scope.** A record homed outside any project
  whose `projects:<name>` scopes name a project on the caller's chain
  (relation `global_named`) is admitted on the session brief, the prompt
  recall and the review strata beside `same_project`; one naming only
  other projects is not. In Stage A's grades such records were relevant
  0.44 [0.35, 0.54] against 0.31 [0.28, 0.35] for the project's own, and
  global records naming no project 0.12 [0.06, 0.24]. Names compare
  case-folded.
- **The write review.** `extends` is the relation question's fifth
  label: the new statement adds to the stored one, which stays in force,
  and the answer sets an `extends` link. A conflict answer on a standing
  memory refuses the write (`refused_standing`). A standing memory is
  replaced, edited, removed, demoted or taken out of force through the
  tools only with `said`, the user's words for the change, checked
  against the prompts the hook saw in the caller's checkout as a join's
  words are; every such change is one `standing_guard` row naming the
  door, the memories and the quote. `update` still links only between
  claim-sized statements: the ENG-7 judges, blind, read 6 of 60
  long-record update answers as a replacement (0.10 [0.047, 0.20]).
- **The RS read scripts.** `bench/recall/read_rs_p5.py` and
  `read_rs_p11.py` take each row's rule from the log (a row without one
  takes the nearest earlier rule-carrying row's) and count queued-command
  prompts, what the owner typed while a turn ran, beside the
  denominator; `bettermemory recall review` reads them as user rows
  (`owner_prompt.queued_text`), never as a turn's prompt by position.
  RS-P11 counts an interrupted turn's audit on its own line, not as a
  Stop.
- **The prompt recall's cost.** Six runs of 50 spaced prompts on the
  sealed copy: service p50 median 79.6 ms, hook wall 116.1 ms, against
  rc3's 74.2 and 111.1 and rc6's 77.5 and 119.0, every difference within
  the unloaded spread phase 0 measured (9.8 and 15.5 ms).
- **The hooks' reads.** `transcript.owner_words` opens a regular file
  only, so a named pipe posted as a transcript path cannot hold a daemon
  hook worker; a transcript row carrying `isSidechain` or `agentId` is a
  subagent's, not the owner's; the audit of interrupted turns is best
  effort behind a warning, never at the cost of the Stop's own rows or
  the prompt's recall.

#### Removed

- `bench/recall/brief.py`; `verdict.fill_verdicts` and `FillReport`;
  `RecallState`'s background flag; `BRIEF_WINDOW`, `BRIEF_CAP_CHARS` and
  `BRIEF_MAX_POINTERS`; `SEGMENT_DELIVERY_CAP` and the `low_relevance`
  and `segment_cap` hold reasons; the `--landing` flag of the RS read
  scripts.

### 9.0.0rc6

The sixth candidate retires the one place the product waited on a
human: a membership question could be answered only by a command typed
in a terminal, with the project's name retyped to confirm, and the
installer denied those commands to the agent's shell. The owner's
ruling of 2026-10-21 governs from here: the user types nothing, and the
agent answers the product's questions itself. The store's schema does
not move; a store a candidate before rc6 opened a no-information
question in closes it at the next open.

#### Changed

- **The agent answers membership questions.** `memory_admin` takes
  three new actions. `answer_question` answers an open question, the
  one named by `id` or the caller's checkout's first: `separate` on the
  agent's own judgement (it moves nothing and can be revisited); `join`
  only on the user's words, quoted in `said`, which the daemon checks
  against the prompts its hook saw in that checkout (the last twenty,
  held in memory only, never written) and refuses when the quote is in
  none of them. Where no hook has reported a prompt for the checkout (a
  client without hooks), the quote is logged unchecked and the reply
  says so. `join_project` merges a project the user names into the
  caller's checkout's project, on the same words; `unjoin_project`
  undoes a merge. Every declaration is one logged row naming the agent
  (`agent:<session>`), and a join carries the words it rests on. Every
  question line names the call that answers it, with the question's id.
- **No command needs a terminal or a typed name.** `bettermemory project
  join | new | init` and `bettermemory memory move` print what they are
  about to do and do it, from any stdin, logged with the OS user as
  before. `bettermemory init --client claude-code` removes the
  `Bash(bettermemory project:*)` and `Bash(bettermemory memory move:*)`
  rules an earlier candidate added to `permissions.deny`, and adds
  nothing; its output no longer mentions a terminal.
- **A question whose answer changes nothing is never asked.** The
  migration opens its legacy question (line L6) only when the gone
  folder's recorded commits are held by more than one compatible live
  project, or by citation alone under the same folder name. Held by
  citation alone under another name (one memory citing three commits
  of another project), or through a remote the holder does not carry
  (one seven-character citation that happened to match a commit in an
  unrelated repository), the records stay a project of their own with
  no live checkout and no question, and the report still names what was
  held. At every open of the store, an open legacy question of that
  shape from an earlier candidate is closed as separate by the rule
  (`rule:legacy_separate`), in one row, and the daemon logs the ids.
- **The system prompt addendum** tells the agent how to answer a
  question, and the health report's open questions name the call that
  settles each.
- **The served schema spells an optional parameter as a type array.**
  Pydantic serves `X | None` as a two-branch `anyOf` with a bare null;
  the scrub after registration now folds that into `{"type": ["<x>",
  "null"]}`, the same set of values in forty fewer characters, on every
  one of the forty optional parameters. The session surface (nine
  descriptions, nine schemas, the instructions) stays under its
  declared 8,000-character ceiling with the three actions added (owner
  decision O6: trim, keep the ceiling).

#### Fixed

- **A flaky hook test on macOS.** The suite's fake daemon answered a
  hook's post without reading its body, and macOS reset the connection
  under the hook now and then (`ConnectionResetError` on one run of the
  v9 push matrix, green on the tag's). It reads the body first.

### 9.0.0rc5

The fifth candidate closes what the fourth's CI matrix and the prompt
bench found: the hook's import cost, four reads on Windows, linked
worktrees and token writers on filesystems without birth time or with a
slow view of a new file, and the test legs that put the store on a
network mount. The store's schema does not move.

#### Fixed

- **The hook path imports no capture module.** Every hook posted where
  its caller stands through `location.probe`, and importing `location`
  pulled the capture's readers (`checkout_token`, `gitobjects`,
  `githead`, `project_file`) into every hook process, about 25 ms a
  post. The probe now lives in `_location_probe`, on the standard library
  alone, and `location` is imported only when the daemon's reply asks for
  the witness exchange. `tests/test_hook_client.py` pins the modules a
  hook loads.
- **File URL remotes on Windows.** `remote_key` read `file:///D:/a/repo`
  as the path `/D:/a/repo`, so a clone or submodule whose origin is a
  file URL lost its lineage there. A file URL now keys as the path it
  names on the platform, read as git reads it.
- **Recorded paths keep their spelling.** The model joined and rebased
  paths with the platform's separator, so a path recorded on a macOS
  machine came back with backslashes at a Windows checkout, and the
  reverse. A path is now extended in the spelling of the view it lands
  in, whatever platform the daemon runs on.
- **Verify's reads on Windows.** The files-only git discovery declines
  off POSIX by design (git's ownership check reads security descriptors
  there), and through it every path outside a sparse cone read as missing
  and every cited commit the home could not resolve read unconfirmed on
  Windows. Reading objects runs nothing a repository configures, so those
  two reads now find the repository through `githead.locate`, which
  answers on every platform and compares resolved paths in the platform's
  case.
- **Linked worktrees without birth time.** On a filesystem that reports
  no birth time (NFSv3), a new linked worktree did not join its primary's
  project: the object store's number was unproven and the worktree
  carried no token of the checkout's. Git's own pointers prove the link
  (`common_object+pointer`): the worktree's git directory sits in the
  common directory's `worktrees/`, and the common directory matches a
  live checkout's. A racing writer's location now also counts the token
  it wrote, so eight concurrent first sightings of one checkout register
  one.
- **A restored checkout stays live.** A checkout restored with its old
  inode number and another birth time (a backup put back in place,
  decision B20) was recognised as the same object, but its stored key
  kept the old birth time, so every later read of the view found it
  replaced and a handoff at it read as inherited from a checkout no
  longer live. The key now takes the birth time read at the recognition,
  logged as `checkout_object_refreshed` (no re-key: the object is the one
  recorded, read anew).
- **Token writers and readers on SMB.** Of concurrent writers of one
  fresh token, the losers read the winner's file before its bytes were
  visible on a cifs mount and reported an I/O failure. A loser now reads
  again, in growing pauses for a bounded time, while it finds nothing, an
  unfinished file or a read the filesystem refuses for the moment. The
  token's reader opens non-blocking (a FIFO at the name cannot hold it);
  a cifs client can refuse that open or a read on it with `EINVAL`, and
  the reader then opens plainly once a stat says a regular file is there,
  or reads again with the flag cleared. An unreadable token's detail
  names the call that failed.

#### Changed

- **The membership test legs keep the store on local disk.**
  `BETTERMEMORY_TEST_LOCAL_TMP` names a local directory for the test
  stores, keys and machine state while repositories, tokens and project
  files stay on the mount under `--basetemp`: SQLite's write-ahead log
  needs shared memory a network filesystem does not give, and the legs
  test the repositories. `BETTERMEMORY_TEST_NO_BIRTH` runs the same files
  as a filesystem without birth time reads them, on any disk. The SMB
  leg's remount now renumbers the objects (`noserverino`), the case the
  design's E11 c names. `BETTERMEMORY_TEST_SETTLE_SECONDS` waits out a
  cifs client's attribute cache where a test records a new file's times
  and reads them again after a move (the init stamps of a commit-less
  repository): the client reports its own write time until the cache
  expires and the data is flushed, the server's time afterwards.

### 9.0.0rc4

The fourth candidate carries the recall fixes and membership by checkout
and project. The store's schema version does not move: the membership
tables and log kinds sit beside the record tables, rc3 reinstalled over
an rc4 store reads it as it read its own, and the next rc4 open homes
what rc3 wrote meanwhile.

#### Added

- **The recall fixes** (`06c81f86`, `7032be0c`; the recall surface under
  Added below has the detail). One definition of an owner prompt, read by
  the prompt recall, the Stop audit, capture and the recall review; the
  Stop reads the turn its `prompt_id` names and no other, from the end of
  the transcript; a compact opens a segment only on the compaction
  marker its transcript holds. The rule name moves to `rs-v4`.
- **Membership by checkout and project.** Each memory and episode is
  homed in the checkout it was written in and that checkout's project,
  in the record's own transaction, and the home is never rewritten; a
  write outside any repository and any project file is global. A
  checkout is one git directory, or a folder with a project file, on one
  machine. A project is the set of checkouts one body of evidence ties
  together: the git directory's filesystem object, its object store, the
  commits it holds, its remotes, its project file, its checkout token,
  the path a checkout was seen at and its content. Copies, clones, linked
  worktrees, containers and other machines' checkouts of one project
  share its memories; episodes, the handoff and recall segments stay with
  their checkout. (`src/bettermemory/membership.py`, `location.py`,
  `gitobjects.py`, `machine.py`, `admission.py`)
- **Three outcomes, said in the session's first output.** Silent: two
  independent witnesses agree, or the same git directory or object store,
  or a declaration; the session gets its project's memories from the
  first prompt and no line. Ask once: one witness, witnesses that
  contradict each other, or content alone; the checkout gets a
  provisional project, nothing of another project is delivered there,
  what is written there stays with it, and every session's first output
  there names the question and its two commands until `bettermemory
  project join <id>`, `bettermemory project new` or new evidence (a
  commit, a fetch, a committed project file) answers it. New: a new
  project, named in one line. The lines say which: this folder may be a
  known project, and the evidence in words; a new project; a project file
  that names another project than the commits the repository holds; a
  checkout joined, and by what; a remote that moved away from the
  project's; the records of a folder that is gone that may belong to the
  project here; a sub-project file that names a project outside its
  repository; a folder that cannot be read, where nothing from any
  project is shown; a HEAD or config a file provider evicted, where that
  prompt shows nothing and the next decides; an imported project that may
  be one here; and a match across a tenant boundary, kept apart.
- **Who sees what.** The session brief, the prompt recall and the write
  review's related statements deliver only the caller's own project, with
  the projects it is a part of and the workspaces around it.
  `memory_search` with `auto_scope` on, the hook's candidate pool and the
  links on a hit add the sub-projects inside it and the global records. A
  search with `auto_scope` off reaches every project except across a
  tenant boundary. Outside any project the brief and the recall deliver
  nothing, the scoped search the global records, and the unscoped one the
  global records and every project's. A caller whose folder could not be
  read is shown nothing.
- **`bettermemory project`**: `list`, `show <id>`, `why <record id>` (the
  project, checkout and rule of a record's home and every log row behind
  it), `pending` (every open question with its line) and the declarations
  `join <id>`, `new` and `init [--new | --rebind] [--name N]
  [--boundary]`. **`bettermemory memory move <record id> --project <id |
  global>`** re-homes one record. A declaration refuses without a
  terminal, prints what it is about to do, asks for the project's or the
  folder's name typed exactly, and is logged with the OS user. `bettermemory
  init --client claude-code` adds `Bash(bettermemory project:*)` and
  `Bash(bettermemory memory move:*)` to `permissions.deny` in Claude Code's
  `settings.json`, so an agent's shell cannot run them.
  (`src/bettermemory/cli/project_cmd.py`, `cli/memory_move_cmd.py`)
- **`bettermemory membership migrate [--dry-run]`** builds the membership
  tables from the store's own records and prints its report: each
  recorded root, what was found there, the project and checkout its
  records were homed to and by which rule, the questions opened, the rows
  written, and the record rows rewritten, which is none. `--dry-run`
  prints the report of a run rolled back. `membership export [-o FILE]`
  and `membership import FILE` carry the projects' portable identities
  (file ids, witness commits, remote keys; no local id and no path) and
  the records' homes between stores, and `bettermemory export` carries
  the same for what it exports. (`src/bettermemory/membership_migrate.py`,
  `cli/membership_cmd.py`)
- **The checkout token**, `info/bettermemory` in a checkout's git
  directory (`.git/info/bettermemory`; in a linked worktree under
  `.git/worktrees/<name>/`), is the one file bettermemory writes into a
  repository on its own: two lines, `bettermemory checkout token v1` and
  26 random characters. git never tracks or clones it, and whatever
  copies a git directory copies it, which is how a moved or copied
  checkout is recognised. It is written at a checkout's first sight and
  never into a bare repository, another user's repository or a read-only
  one; the first-sight line says when it was not written, and why.
  (`src/bettermemory/checkout_token.py`)
- **The project file**, `bettermemory.toml`, written by `bettermemory
  project init` and committed by you: `[project]` with `id =
  "bmp_<26 characters>"`, `name`, and optionally `boundary = true`. At a
  repository's root it binds the repository's project for every clone;
  below the root it names a sub-project; in a folder around repositories
  it makes a workspace; in a plain folder, a folder project; with
  `boundary = true`, a tenant that nothing crosses.
  (`src/bettermemory/project_file.py`)
- **`[membership]`** in `config.toml`: `write_token = true` (false writes
  the token nowhere and leaves the other evidence to decide) and `strict
  = false` (true asks once where a checkout would join a project because
  it holds the project's commits; joins by the project file, a
  declaration, the same git directory or object store, or the project's
  old path stay silent).
- `bettermemory health` and the `health` action gain a membership
  section: open questions with their lines, provisional projects,
  suppressed recall turns per checkout, captures that could not be read
  or were deferred, and checkouts with no live view.
- **Rule `rs-v5`.** The session brief and the prompt recall deliver by
  membership, and their `session_brief` and `prompt_recall` rows carry
  the caller's `checkout` and `membership_rule` (its resolution's rule,
  or the outcome that admitted it nothing). `rs-v4` never ran on a live
  store, so a live log goes from `rs-v3` rows to `rs-v5` rows; every
  read splits on the row's rule. The episode handoff names a prior
  session that stood in the caller's checkout and journaled nothing, by
  the checkout on its rows, with the zero-episode note.
  (`src/bettermemory/recall.py`, `handlers/episode_handoff.py`)
- **The shadow column.** Each `session_brief` and `prompt_recall` row
  carries `rc3_admitted`, the ids rc3's rule would have delivered,
  computed from the origin columns. `bettermemory eval` reports the
  recall surfaces' rates for `rs-v5` and for the earlier rules together
  (`by_rule`), and `membership_delta`: the `rs-v5` rows whose delivered
  pointers differ from rc3's, counted as pointers delivered and not
  rc3-admitted and pointers rc3-admitted and not delivered. `--report`
  shows both groups. (`src/bettermemory/eval.py`, `docs/eval.md`)

#### Changed

- **Auto-scope is membership.** `memory_search`'s `auto_scope`, the
  hooks' pool, the brief and the recall decide by the caller's project and
  each record's home, through one function, where they compared the
  origin's repository and worktree. The origin columns are still written.

#### Migration

The membership tables are built at the first open by an rc4 build, with
nothing to run: the daemon at its start, under the store's lock;
`bettermemory serve`; any CLI command; the in-process hook commands.
Every memory, removed memory and episode is homed once, in one
transaction ending in one `membership_migrated` row, and nothing is
written into any repository (each checkout gets its token at its next
sighting). The CLI says so in one line on stderr; the daemon, `serve` and
the hook commands log it. `bettermemory membership migrate --dry-run`
prints the report beforehand and writes nothing. A store whose records
are all homed is not written.

### Added

- **The bettermemory 9 store and its hash-chained log.** One SQLite
  file per store, `memory.sqlite`, whose tables are the records and
  whose FTS5 table and triggers are the v8 index's own, so the candidate
  query returns the same ids in the same order on the same rows. Every
  mutation and telemetry event is a row of the `log` table, MAC'd with
  a key kept outside the store (`keys/<store_id>.key` under the user
  config directory) and chained to the row before it, with a head
  checkpoint beside the key. `bettermemory log verify` reports an
  edited row, a row inserted without the key, a deleted tail, and a
  table edit that no log row accounts for. (`src/bettermemory/store.py`,
  `src/bettermemory/log.py`)
- **Provenance at read.** Every memories, tombstones and episodes row
  carries `log_mac`, the MAC of the log row that produced it, and every
  search, show and handoff verifies the pointer: the row exists, is of
  the producing kind, names the record and verifies under the store's
  key. A row planted around the tool, and a row whose log row was
  forged without the key, read `unaccounted` on the next read without
  a refold. The labels are `local`, `imported`, `synced` and
  `unaccounted`. A server that opens a store whose key is not on this
  machine writes a new key and logs a warning; rows signed under the
  old key keep their stored labels, and `log verify` reports the store
  `unverifiable`.
- **`bettermemory migrate v8` and `bettermemory export --mirror`.** The
  migration reads an 8.x store directory with the frozen v8 readers
  (`src/bettermemory/v8.py`) and writes it into the store in one
  transaction: active memories as `imported` rows with their filenames,
  in the order the v8 index held them; tombstones with the links and
  corroborations their files kept; episodes, conflicts and the ingest
  watermark; every event from every shard, archive and the legacy file
  as a telemetry row under its original timestamp, verbatim query text
  redacted; and one `migrate_v8` control row ahead of them with the
  counts. The directory is never written, a re-run imports only what is
  new, and `--dry-run` reports without writing. The mirror writes the
  store back out as a v8 directory, pinned byte for byte against golden
  files written by 8.0.0 (`tests/fixtures/v8/`), into a directory it
  made or an empty one and never over a store; `bench/parity/migrate_v8.py`
  records the round trip. (`src/bettermemory/migrate_v8.py`,
  `src/bettermemory/mirror.py`)
- **`memory_admin` and `episode`.** Two tools that take an `action`:
  `memory_admin` carries `restore`, `tombstones`, `health`,
  `rename_scope`, `conflicts`, `acknowledge_miss`, `disable_scope` and
  `enable_scope`, each returning what its former tool returned
  (`tombstones` under one key), and `relations`, a capture batch's
  closing call (under Added); `episode` carries `write` and `handoff`.
  `acknowledge_miss` gains a `before` form that writes the
  `silent_miss_cutoff` event `consolidate --acknowledge-misses-before`
  used to.
- **`exclude_scopes` on `memory_search`**: the scopes to drop, applied
  before ranking like the session's disabled set.
- **The warm daemon's caches.** Memoisations that remove the git
  processes, the re-tokenising and the log scans a warm `memory_search`
  paid. Each answers as the uncached code does for the inputs it is
  keyed on, and names beside it what its key does not see. They are: the
  origin captured per directory for ten minutes, by the monotonic and
  the wall clock both, validated on every lookup against the
  repository's HEAD bytes, the stamps of its config, config.worktree and
  the ref files that decide the branch's short name (a tag or a ref named
  like the branch), the root spelled as git prints it (the kernel's
  spelling on macOS, so a rename that changes only the case or the
  Unicode normalisation of the repository's name or a parent's is seen),
  and the environment (`origin.capture`), so a lone session-start hook
  reuses it, while a remote changed outside the repository's config file
  (a global `url.<base>.insteadOf`, a legacy `.git/remotes` file, an
  included file), and such a rename on another platform's filesystem
  that ignores case, are read from the capture for up to those ten
  minutes;
  the commit-drift work keyed on the repository
  root and the commit HEAD names, read from the repository files by
  `githead.py` without a git process, with the whole-history author
  dates per head and the per-hit resolution per head, anchors, claims
  and verify instant memoised (`_response.attach_commit_drift_counts`,
  `origin.commit_author_timestamps`); and the token streams of a memory
  per body and scopes (`search._memory_tokens`, a 5,000-entry LRU). The
  snippet window's scan of a hit's body is kept per scanned slice (the
  first 8,000 characters of the stripped body, all the scan reads) and
  each raw token's normalised surfaces are interned across bodies
  (`search._snippet_tokens`, `search._token_surfaces`), and the telemetry
  rows of `memory_search`'s lookback window are kept per store and
  extended from the log's head, checked by its MAC, instead of read by a
  scan of the whole log (`Store.events_since(..., memoised=True)`). These
  are bounded in entries, not bytes; the docstrings give the bytes
  measured: 13.6 MB for the snippet memos over the public corpus's 1,080
  bodies (52 MB at their bound for bodies of that shape, about 930 MB for
  scans of one-letter tokens), and about 1.3 KB a row the window keeps. A
  search whose hits the memos hold forks no git process while nothing
  their keys read has moved: no commit, no edit to the repository's
  `config`, a `.gitmodules` or an attributes file, and the origin capture
  inside its lifetime. A repository the files do not settle (a split
  index, `GIT_DIR` or `GIT_INDEX_FILE` set, the reftable format) is
  resolved through git on every search, as before the memos.
  A value computed while a git process failed is never kept, and a hit
  with a governed claim keys on the attribute files its patch stream
  reads (`.gitattributes` on the path, `info/attributes`, the global
  attributes file and the repository's `config`), so an edit there is
  read on the next search. A head or an attributes file that moves and
  returns while a search runs leaves nothing memoised (the file stamps
  carry ctime, inode and mode, and the recheck holds the directories),
  and the reachable walks are keyed on the repository's `config`, its
  `.gitmodules`, and the index's `.gitmodules` entry where git reads it
  (while it is unmerged, when git reads none, and where the working tree
  has none), read from the index in-process once per state of the index:
  a `git add` of another file, or a `git status` that refreshes the index,
  keys nothing new. A config keeps governed hits out of the memo where it
  sets `log.follow`, holds a NUL byte (git ends a name at one) or is one
  git refuses, and its variables are parsed once per state of the file;
  the files answer the root unless the config sets `core.worktree` or a
  true `core.bare`, where a branch or remote whose name held the word
  `worktree` used to send every search to git; and where the config sets
  `extensions.worktreeConfig` (as `git sparse-checkout set` does in a
  linked worktree), `config.worktree` is read as git reads it after the
  common config: `log.follow` or `core.attributesFile` there keeps
  governed hits out of the memo, and its stamp is in every walk key, so
  `diff.ignoreSubmodules` or `log.showRoot` written there is read on the
  next search.
  `_caches.clear_all` empties every cache
  and the test suite calls it before each test. The phase 1 gate's
  numbers are in `bench/daemon/results/latency-9.0.0-2026-09-26-u6.json`.
  (`src/bettermemory/githead.py`, `src/bettermemory/_caches.py`)
- `memory_write`'s dedup scans (`find_similar`, `find_similar_tombstones`)
  and the supersession detector read each body's raw content-token set
  from a cache keyed on the text (`search.raw_content_tokens`, a
  5,000-entry LRU registered with `_caches`) instead of tokenising every
  active body on every write. An edited body is a new key and is
  tokenised again. On the 662-memory copy of the write arm, a write's p50
  went from 673.77 ms to 109.27 ms
  (`bench/daemon/results/latency-9.0.0-2026-09-30-write-baseline.json`,
  `latency-9.0.0-2026-09-30-write-cache.json`).
- **The local daemon, the stdio shim and the hook client.** `bettermemory
  up` starts one daemon per store on 127.0.0.1 (`[daemon] port`, default
  7397, an ephemeral port when that one is held), detached, with `down`
  and `status` beside it and `--foreground` for supervisors. The daemon
  owns the store, the recorder and the session registry and serves the
  nine tools over streamable HTTP at `/mcp` (stateless, JSON responses),
  the three hook bodies at `/api/v1/hook/session-start`, `/stop` and
  `/prompt`, `/health` and `/api/v1/admin/shutdown`. Every request but
  `/health` carries the bearer token of the state file
  (`daemon-<store hash>.json` under the user's state directory, or
  `BETTERMEMORY_STATE_DIR`), mode 0600. `bettermemory` with no arguments
  is now a stdio shim: a real stdio MCP server whose `initialize`
  carries the daemon's instructions and this package's version and
  whose `tools/list` and `tools/call` forward to the daemon, starting
  one when none answers, replacing one of an older version (a newer
  one is kept and used, so two installs at different versions on one
  store do not restart the daemon under each other), and serving the
  store in-process (the 8.x shape, `bettermemory serve`) with one
  stderr line when no daemon can be reached or started. Per-client
  session state is keyed on the `x-bettermemory-session` header the
  shim sets per process; the shim forwards the client's `clientInfo`
  and its working directory as the `x-bettermemory-client`,
  `-client-version` and `-workspace` headers unless the environment
  declared them. `bettermemory hook session-start|stop|prompt` reads
  the hook's stdin JSON, posts it to the daemon and prints the answer,
  through a path that imports nothing from the SDK. It reads the version
  a running daemon's is held against from `bettermemory/_version.py`, which
  hatchling's version build hook writes from `pyproject.toml` at every
  build and editable install, without `importlib.metadata` (the
  installed metadata answers only where no build wrote the file); and
  with `BETTERMEMORY_DIR` set it names the store from the variable
  without loading the config, so a malformed config no longer stops it,
  or `up`, `down` and `status`, before they reach a running daemon.
  With none running, the daemon they start still refuses that config.
  A client that starts a daemon watches it: when it exits before any
  daemon answers, the hook, `up` and the shim stop waiting at once and
  report its exit status and the last line of its log
  (`DaemonExited`), where they used to wait out the eight-second start
  timeout and name no cause.
  A daemon whose store file is removed, or replaced by a rename over
  its path, stops through its normal shutdown at its next check (every
  30 s), logging a warning that it stops and an error naming the saved
  store; a copy over the store keeps it running. An open shim starts a
  new one on its next call. The check stats the path for the device and
  inode it named at start and holds no descriptor on the store, whose
  closing would drop the daemon connection's locks.
  Before it stops, the daemon saves the store it still has open through
  its own connection to `<store>.orphaned-<UTC stamp>.sqlite` beside the
  path, making the directory again (owner-only) when it went with the
  store, and saves it again over the same file as it shuts down, once
  the hook workers have closed or their 10 s join bound ran out,
  whichever comes first, so writes still in flight are kept. A `down`
  or a signal before the next check is caught at that same point and
  saves the store too, as is a forced exit (a second Ctrl-C), which
  skips the app's shutdown: the daemon saves as the loop closes, or after
  the server returns.
  A save that fails in an unexpected way is logged and the daemon still
  stops. A store created while one or more such files sit beside it
  names them in its `meta`; one another process created at the path
  after the daemon started, before the orphan was saved, is
  given its name by the daemon, waiting up to 5 s for a writer in
  another process to let go of it, while an older store moved back over
  the path, or a file that is no database, is not. While any named file
  is still there `/health` lists them under `store_orphans` and the
  session brief, whatever its settings, leads with one line naming the
  newest saved file and the restore steps: close every session using
  bettermemory, `BETTERMEMORY_DIR=<dir> bettermemory down` (named
  always, since a plain `down` follows the shell's own store), delete the store's `-wal` and `-shm` files, then copy the
  saved file over it. Deleting the files, or restoring the old store,
  ends the notice.
  `session-start`, `audit-turn` and `prompt-recall` stay as aliases of
  it, and the plugin's `hooks.json` names the new commands.
  (`src/bettermemory/daemon.py`, `shim.py`, `_daemon_client.py`,
  `cli/daemon_cmd.py`, `store.py`, `recall.py`)
- **The write review.** A `memory_write` that passes the gates writes
  nothing on its first call: it returns status `review` with a
  `review_id`, the related stored statements (the engine's top three
  plus the cue code's targets, at most five, each at most 600
  characters) and three questions: a relation per related id
  (`restates`, `update`, `conflict` or `unrelated`), and `unsafe` and
  `secret` scores from 0 to 1. The writing model answers by sending the
  same call again with a `review` argument. The daemon keeps no state
  between the calls: it recomputes the related ids, asks again for any
  left unanswered, and then acts in this order:
  - `secret` at or above 0.30 refuses as `credential_warning`.
  - `unsafe` at or above 0.30 refuses as `instruction_warning`.
  - `restates`, with no `update` answer and no declared `supersedes=`,
    refuses as a duplicate.
  - Otherwise the write commits. An `update` answer sets a supersedes
    link between claim-sized statements, or is named in
    `update_suggested`. A `conflict` answer files the pair with
    detector `review`.

  The credential regex always stands. A cue-code link stands unless the
  answer for its target is `conflict` or `unrelated`: the link is then
  dropped and the pair filed as `review-disagrees`. No model runs
  inside the product; the questions are put to the caller. Both
  thresholds were sealed on the v1 integrity dev split
  (`bench/integrity/v1/operating-points-review.json`). Every review is
  logged as a `review_issued` row. `bettermemory eval` counts reviews,
  and `bettermemory review samples` and `review grade` draw and grade
  samples. (`src/bettermemory/review.py`, `handlers/write.py`)
- **The recall surface: the session brief and pointer recall.** Two hook
  moments put pointers in front of the model without a tool call, each
  an id and an 80-character summary under a header that says they are
  pointers, not instructions, never a body (`src/bettermemory/recall.py`):
  - On SessionStart inside a repository, the session brief: up to eight
    of the newest memories written there whose staleness verdict is
    `fresh` exactly as `memory_show` computes it, local or imported, not
    ambient, without an open contradiction, 1,200 characters at most.
    It replaces the per-scope counts block. `[behavior] session_brief`
    switches it.
  - On UserPromptSubmit, the prompt recall: the owner's words as the one
    definition of an owner prompt reads them
    (`src/bettermemory/owner_prompt.py`: reminder, paste and artifact
    view blocks removed, cut at 2,000 characters), probed through the
    audit's own pool and ranker, and up to three hits delivered when
    they are
    relevant (medium or high), written in the caller's project, local or
    imported, not contradicted and not already shown, retrieved,
    written or updated in the session segment; 900 characters at most,
    snippets of at most 100. The one-hit miss lane is gone: `[behavior]
    prompt_recall` takes `project` (the default), `everywhere` or `off`
    (`true` and `false` read as `project` and `off`), and
    `recall_in_project` retires with a removed-key notice. A prompt the
    harness composed and no human typed (a task notification, a message
    from another session, any other harness tag: the Stop audit's own
    test, now `hook.is_synthetic_prompt`) is not probed and records
    nothing, so no id is held for the rest of the segment on its
    account; the rule name moves from `rs-v1` to `rs-v2` so the log
    tells the rows apart. The Stop audit, capture and the recall review
    judge a transcript row by the same definition, its marks first
    (`isMeta`, `isCompactSummary`, a `turnOrigin` other than `human`)
    and its text second, so a prompt behind reminder blocks is the
    owner's on every surface and hashes alike on the recall and the
    Stop; `hook.human_prompt`, `hook.is_synthetic_prompt` and
    `recall.clean_prompt` are names for it, and `clean_prompt` reads a
    prompt without an artifact view byte for byte as before.
  - A member of a pending conflicts pair is delivered with a
    `[disputed: cf-…]` mark, whatever the detector.
  - A per-session cooldown replaces the 600-second retrieval shield for
    delivery: no id twice in a segment (a clear opens a new one, and so
    does a compact on its marker), twelve recalls per segment at most,
    held in the daemon and rebuilt from the log after a restart. The
    brief on a resume, which keeps its segment, omits the ids the
    segment has already shown and records them as `omitted` on its
    `session_brief` row (a row with no ids when every candidate was
    shown, which the eval and the review do not count as a brief); the
    rule name moves from `rs-v2` to `rs-v3` so the log tells the rows
    apart.
  - A compact SessionStart opens a segment only when the transcript
    holds a `compact_boundary` row newer than the one the segments last
    opened on, named as `marker` on its row (one session sent 45
    compact SessionStarts against one compaction). Without it the
    compact is a resume, and its row carries `marker_absent: true`; the
    boundary reaches the file only after the hook has run (in each of 45
    measured pairs), so a later hook (the prompt recall, the Stop, a
    SessionStart) that finds a marker stamped within 60 seconds of the
    compact, however late it reads, opens the segment at the compact,
    keeps what was shown since, and records `segment_opened` with
    `marker` and `opened_at`. A SessionStart
    without a transcript path reads no marker, so its compact is a
    resume until a later hook finds one. The state is rebuilt
    from the session's own rows whenever they were stamped, where it
    read the last day before, so a session evicted after a day idle, a
    restarted daemon and the CLI path hold what the daemon held. The
    rule name moves from `rs-v3` to `rs-v4` so the log tells the rows
    apart.
  - Each show is recorded at show time (`session_brief`, and
    `prompt_recall` with `shown_ids`, `suppressed_ids`, `rule`,
    `segment`, `prompt_id` and `transcript_path`); a compact or clear
    whose brief shows nothing records `segment_opened`. Nothing is shown
    with telemetry off.
  - At Stop the reply (`last_assistant_message`, the turn's own reply
    when absent) is matched against every pointer shown in the segment;
    a match records one applied `use` naming its `surface` and the
    show's event id, and no match records nothing. Open review chains
    the conversation left since its previous turn are counted into a
    `review_unanswered` event. The Stop's `turn_audited` row carries the
    payload's `prompt_id` and `reply_source` (`payload` or `tail`).
  - The Stop reads the turn the payload's `prompt_id` names and no other
    (`src/bettermemory/transcript.py`), from the end of the file one
    1 MiB chunk at a time, 64 MiB at most, a row that starts exactly at
    the budget's first byte included. Its prompt is the newest of the
    turn's rows the definition accepts; a turn without one records
    `turn_audited` with verdict `no_signal` and `no_signal_reason:
    no_owner_prompt`, which eval counts in `turns_no_signal` outside the
    silent-miss denominator, probes nothing and settles nothing (60 of
    the 122 rc3 Stops replayed on 2026-10-19 had no owner prompt: rc3
    had probed an older turn's prompt for 58 of them and the turn's own
    SDK submission for 2; of the other 62, rc3 had probed another turn's
    prompt for 7, 6 of them because the prompt opened with a reminder
    block).
    The reply is `last_assistant_message`, else the newest assistant
    text of the turn after its prompt, else none, where it was the
    newest text of any turn. A turn the walk cannot decide within its
    budget records nothing.
  - The shim forwards `CLAUDE_CODE_SESSION_ID` as
    `x-bettermemory-harness-session`, the daemon stamps it on show,
    search, use, write, update and verify rows, and the hooks join a
    conversation's tool rows by it, the 9.0.0 worktree bridge when
    nothing carries it or the stamp predates a /clear.
  - `memory_show`, an expanded `memory_search` top hit and the brief read
    one verdict (`src/bettermemory/verdict.py`), whose commit leg comes
    from the per-hit drift memo, so a warm show runs no git process.
    The hook bodies run on worker threads, each with its own store
    connection, and every hook endpoint answers `surface`, `ids`,
    `chars`, `rule` and `event_id` beside `stdout`.
  - `bettermemory eval` gains a `surfaces` section, printed only when
    the window holds the surface's rows: per surface the deliveries,
    pointers shown, opened, used and ignored after the show, with
    `shown_opened_rate`, `shown_used_rate`, `ignored_rate` and
    `delivery_helped_rate`; settlements by surface; segments; unanswered
    reviews; and grades. A settlement is left out of the three rates and
    the tool-call count, so their published series keeps one definition;
    a log without the surface's rows reports byte for byte as before.
  - `bettermemory recall review` draws 12 deliveries, 4 briefs and 4
    silent prompts (turns the recall stayed silent on before the model
    retrieved a project memory) with the prompt read from the transcript
    at review time and never stored: a delivery's as the row its probe
    hashes to, each sample named by its prompt kind, a sample with no
    prompt to grade against passed over for the next in the seeded order
    and listed; `bettermemory recall grade` records a `recall_grade`
    event carrying the sample's rule.
  - The `instructions` block swaps its opt-in sentence for one of the
    same length that names the hook pointers, still 1,000 characters;
    `tools/list` is unchanged. The addendum, the plugin skill, README and
    `hooks.json` comments, `docs/internals.md` and `SECURITY.md`
    describe the surfaces.
- **The stdio shim follows its daemon through a restart.** A request the
  daemon refuses with 401, or one that finds no daemon listening, makes
  the shim read the state file again (starting a daemon only when none
  answers, and never stopping one) and send it once more; any other
  failure answers the call with an error and leaves the shim up.
  (`src/bettermemory/shim.py`)
- **`Store.open_or_create` refuses to create an empty store beside an
  un-migrated 8.x directory** (a memory file, an event shard or the
  tombstone directory present, no `memory.sqlite`), naming
  `bettermemory migrate v8`. A server pointed at such a directory used
  to serve an empty store and say nothing.
- `[daemon] port` in `config.toml`; `BETTERMEMORY_STATE_DIR` for the
  daemon's state files, the shape `BETTERMEMORY_KEYS_DIR` set.
- **Session capture at the Stop hook, by the model in the session.** Off
  by default; `[capture] stop_checkpoint = true` switches it on, for
  Claude Code only. Once the conversation since the last capture holds
  `checkpoint_tokens` (12,000, in tokens of four characters) of the
  user's prompts and the assistant's text, the Stop hook answers with an
  instruction as the hook's `additionalContext`, and the chat model saves
  what the stretch established with `memory_write` from its own context,
  under ten numbered rules (`capture.CAPTURE_RULES`: durable, one claim,
  a plain subject, specific, dated, the why, the user's own words for
  rulings, the category, new, true to the conversation), then tells the
  user in one line what it saved. No other model runs and no API key is
  involved; only a session a person is attending is asked
  (`CLAUDE_CODE_SESSION_ATTENDED` read as `1`, forwarded by the hook
  client), at most `daily_cap` (12) requests per store per UTC day, and
  never while another hook is continuing the turn. A capture is a batch
  on the log: a `capture_open` control row with an unguessable token, a
  `capture_member` row per write in the memory's own transaction, a
  `capture_close` recording the transcript offset captured to, and
  `capture_relations` and `capture_undo` rows for what follows. The
  writes carry the token as `source="capture:<token>"`, which is what
  joins them to the batch and keeps a subagent's writes out; a capture
  write passes every write gate, with no `force` and no acknowledge
  flag honoured, plus the enforced half of the bar
  (`durability.find_capture_rule_hits`, refused as `capture_rule` with
  the rules by number) and a check against bodies an undo reverted
  (`capture_refused`, `previously_undone`). Each capture write carries
  the review's `unsafe` and `secret` scores and commits in one call; the
  related statements it did not answer come back as `relations_pending`,
  answered once for the whole batch with
  `memory_admin(action="relations", relations={...})`, the batch's
  closing call, which applies the review's decisions after the commit. A
  Stop that finds the batch open with the transcript moved on closes it
  as interrupted and leaves the session alone for an hour. The CLI:
  `bettermemory capture review` lists the batches with their outcomes,
  counts, refusals by rule and undos; `capture undo BATCH [--apply]
  [--skip-changed]` tombstones a batch's memories and records the undo
  so the same body is not captured again, refusing a member since
  updated, verified, linked to from outside the batch or filed in a
  conflict unless `--skip-changed`; `capture sample [--n N] [--seed S]
  [--tally]` draws captured memories to grade and `capture grade ID
  --meets | --fails [--rule N]...` records the owner's grade as a
  `capture_grade` event. `bench/capture` measures the gates, the one-call
  review, the attended switch, the undo and the served surface.
  (`src/bettermemory/capture.py`, `capture_hook.py`, `capture_review.py`,
  `handlers/capture_relations.py`, `cli/capture_cmd.py`)
- `bench/toolcost` records the served `instructions` block beside the
  descriptions and schemas (`instructions_bytes`, `instructions_chars`,
  `session_bytes`).
- `bench/toolcost` never reaches a daemon outside its sandbox: the probe's
  child inherits no `BETTERMEMORY_*` variable, `bettermemory down` runs
  only when its state directory is inside the sandbox, and a leftover
  daemon is signalled only when its record is inside the sandbox and
  its port's `/health` reports its pid.
- `bench/daemon/latency.py` measures a lone session-start hook both ways:
  `hook_service_idle`, each call after a fixed 2.2 s idle gap with the
  origin capture reused, and `hook_service_cold`, the same with the
  capture made stale before each call (the ctime of the repository's
  HEAD moved), the path every lone hook paid before 9.0.0.

### Changed

- **Nine tools, always registered.** `memory_search`, `memory_show`,
  `memory_write`, `memory_update`, `memory_remove`, `memory_verify`,
  `memory_record_use`, `episode` and `memory_admin`. The seven single
  tools keep their parameter names, enum values and return keys, with
  one exception: `memory_remove` returns `{removed: id}`, since the
  `tombstone_path` it returned named a file. Every description, the
  server `instructions` block, the system-prompt addendum
  (`docs/system_prompt.md`, `prompts.SYSTEM_PROMPT_ADDENDUM`) and the
  plugin skill are rewritten for the nine.
- **The runtime runs on the store.** Handlers, the response builder,
  health, eval, the audit, the hooks, the CLI and the benches read and
  write `Store`; the recorder writes each event as a log row; episodes
  and conflicts are rows; this host's verification stamp is a
  `verifications` row written by `memory_verify`. Query text is always
  redacted in the log.
- The session-start hint, the Stop hook and the recall hook are one HTTP
  request to the daemon each, after a `/health` check of the daemon the
  state file names; the daemon reads the store for them.
  The `bettermemory` console script now enters through
  `bettermemory._entry:main`, and importing the package imports neither
  the MCP SDK nor `importlib.metadata`: `build_server`, `main`,
  `SYSTEM_PROMPT_ADDENDUM` and `__version__` resolve on first use. A
  hook process with `BETTERMEMORY_DIR` and `BETTERMEMORY_STATE_DIR` set
  pays the interpreter, the standard library and four modules of this
  package (`bettermemory`, `_entry`, `_daemon_client`, `_version`);
  without the first it loads the config as well, and without the second
  it imports `platformdirs` for the state directory.
- The SessionEnd hook that `main` carried unreleased after 8.0.0 is
  gone: session capture runs at the Stop hook (under Added). The Stop,
  SessionStart and UserPromptSubmit hooks stay.
- A config file that still sets a removed key or section loads with one
  logged warning per key and the line ignored.
- **Write-time supersession links one stored claim per cue.**
  `detect_supersession` used to link a write that carries a change cue
  to every stored claim passing its lexical floors, so an update also
  linked other topics' updates. It now keeps the strongest match, plus
  any other match holding that match's old value. No threshold moved.
  (`src/bettermemory/supersession.py`)

### Removed

- **Twenty MCP tools.** `memory_list`, `memory_scope_overview`,
  `memory_write_confirm`, `memory_write_cancel`, `memory_audit_turn`,
  `memory_health`, `memory_curate`, `memory_conflicts`,
  `memory_acknowledge_miss`, `memory_rename_scope`, `memory_restore`,
  `memory_list_tombstones`, `memory_proposals`, `memory_scope_disable`,
  `memory_scope_enable`, `episode_write`, `episode_handoff`,
  `episode_search`, `episode_promote` and `episode_patterns`. Eight are
  `memory_admin` actions and two are `episode` actions. `bettermemory
  export` covers the listings, and the session brief points at the
  newest fresh memories. A promotion is a `memory_write` the model makes
  from a handoff it has read. The rest went with their modules.
- **Ten off-by-default flags, with their code.** `[behavior]`
  `require_write_confirmation` (with the pending-write machinery),
  `rescue_expansion` (the retrieval bench still measures the leg),
  `endorsement_boost` and `outcome_demotion` (a wash on the owner's
  labelled replay), `standing_tier`, `full_tool_surface`,
  `curation_hint_enabled` and `curation_hint_threshold`;
  `[telemetry] log_queries_verbatim` and `max_bytes`; the
  `[consolidate]` and `[proposals]` sections whole; and the `[capture]`
  keys of the capture `main` carried unreleased after 8.0.0 (`enabled`,
  `idle_minutes` and their siblings), which switched on a capture that
  called a model of its own. The 9.0 `[capture]` section carries
  `stop_checkpoint`, `checkpoint_tokens` and `daily_cap` (under Added),
  and the old `enabled = true` never sets it.
- **The modules that served them.** `consolidate`, `proposals`,
  `patterns`, `session_capture`, `llm`, `doctor`, `sync`, `ingest`,
  `migrate` (the origin backfill), `quarantine`, `provenance`, `index`,
  the v8 `episodes` and `store`, and the `doctor`, `reindex`, `sync`,
  `consolidate`, `ingest` and `proposals` commands. The unreleased
  post-8.0.0 capture goes too: its `session-end` command is removed,
  and its `capture` command and `capture_hook` module give way to the
  in-session capture under Added, which shares their names and nothing
  else.
- **The groundedness gate.** `memory_write` loses `groundedness_check`,
  `source_transcript` and `acknowledge_ungrounded`, and the
  `groundedness` module and the `"ungrounded"` status go with them. The
  opt-in gate had refused one write on the owner's store and was never
  switched on in his retained Claude Code transcripts, and its three
  parameters cost 250 characters of the served surface every session.
- The ratchet-test genre (`test_doc_claims`, `test_symbol_citations`,
  `test_resident_footprint`, `test_changelog` and their siblings) and
  the tests of the removed modules.
- The comparative harness under `tests/eval/` (`python -m
  tests.eval.comparative`, its `--live` lane and `run_live.sh`), the
  MSC loader under `bench/msc/` and the seven retrieval census scripts
  under `bench/` that nothing pinned. Releases before 8.0.0 moved from
  this file to `docs/changelog-history.md`, unchanged.
- The programs closed on 2026-10-14 (`bench/longmemeval/`, `bench/l/`,
  `bench/aml/`, `bench/w/`, `bench/instruments/`, `bench/heldout/`,
  `bench/dedup/`, `bench/judge/`, the retrieval census scripts and
  `bench/parity/aml_search.py`), with their receipts and their tests.
  Each stands at the tag `archive/bench-closed-2026-10-23`, where its
  harness runs as it did and a number cited from it is read;
  `bench/README.md` names them.

### Migration

Run `bettermemory migrate v8` once per store directory (`--dry-run`
first to read the report). The 8.x files stay where they are, unread by
the server from then on; `bettermemory export --mirror DIR` writes the
store back out as an 8.x directory at any time. A `config.toml` that
sets a removed key keeps loading, with a warning naming it. Client
registrations do not change: the server key, the binary and the
`instructions` block are served as before.

## 8.0.0 - 2026-09-24

A major by the [compatibility contract](CONTRIBUTING.md#versioning-and-the-compatibility-contract),
with two breaks, each on a surface the "forbidden within a major" list
protects: a `category="user-inference"` write commits on the first call
where it returned `pending`, and the `[behavior] corroboration_boost`
flag 7.6.0 deprecated is removed. Nothing on disk changes:
`SCHEMA_VERSION` stays at 1, the index schema stays at 11, no memory or
episode written under 7.x reads differently, and there is no migrate
subcommand. The first slice of session capture ships beside them as a
library module that nothing calls yet.

### Changed

- **A claim about the user commits on the first call; nothing stops the
  conversation to ask before it is saved.** `memory_write` with
  `category="user-inference"` was documented as "Always returns
  {status:'pending', pending_id} regardless of config", and its hint
  told the model to ask the user in plain language, then call
  `memory_write_confirm`. That contract is gone: the category now
  passes through every gate `fact` does (credential, transient,
  scope-mismatch, groundedness, both dedup gates, declared claims,
  supersession) and returns `committed`. It keeps its label, so a
  stored inference stays distinguishable from an established fact and
  can be found and corrected. `pending_reason: "user-inference"` is no
  longer produced. Under the compatibility contract this changes what
  an existing enum value does rather than adding one, so it is not an
  additive change, and it is why this release is a major.

- **`require_write_confirmation` is the one staging path, and it is
  uniform.** It stages every category, `user-inference` included, with
  `pending_reason: "config"` and a single hint that names
  `memory_write_confirm` and `memory_write_cancel` without scripting a
  question. `memory_write_confirm` / `memory_write_cancel`, the pending
  sidecar, the confirm-time re-gate and `memory_scope_overview`'s
  `pending_writes` count are unchanged and serve that path.

- **A `user-inference` proposal accept commits.** Since 7.3.0 an accept
  through `memory_proposals` staged a pending write from a session,
  mirroring the pending gate above; it now returns `accepted` like every
  other category, from the MCP tool and the CLI alike. The accept is
  still the queue's confirmation step, so the config flag adds no second
  one there. `accept_proposal` loses the `state` parameter that existed
  only to stage.

- **The user-claim gate stays, and says what it is for.** A body that
  reads as a claim about the user filed as `fact` or `ambient` is still
  refused with `user_claim_warning` on `memory_write`, `memory_update`
  and `episode_promote`, because the label is the only thing that marks
  it as an inference. The hint now says re-issuing as `user-inference`
  files it under that label; it no longer says the re-issue stages or
  that the model should ask. `memory_update` still refuses a retag INTO
  `user-inference`, and `consolidate --llm` still proposes only `fact`
  and `ambient`: both rules were written to protect the pending gate,
  and they are kept as they stood rather than widened in the same
  change, with the reasoning now in the comment on the shared whitelist
  in `src/bettermemory/models.py`.

- **Every surface that scripted the ask was rewritten**: the
  `memory_write`, `memory_update`, `episode_promote`,
  `episode_patterns` and `memory_proposals` descriptions, the server
  `instructions` block, the long-form addendum and
  `docs/system_prompt.md`, the plugin skill and README, the README,
  `docs/api.md` and `docs/internals.md`.
  `tests/test_no_ask_before_save.py` reads every served description,
  the `instructions` block, the addendum, those docs, and the live
  hints on each path that used to carry the ask, and fails on the
  phrasings that carried it; its patterns are pinned against the
  removed wording, so a rewrite cannot leave them matching nothing.
  The description baselines are re-measured in the same commit, and
  the footprint table's descriptions row picks up +266 it had not
  recorded since 2026-09-16.

### Removed

- **`[behavior] corroboration_boost`, deprecated in 7.6.0 for removal
  at 8.0.** The flag goes with everything it threaded: the ranking
  factor (`search._corroboration_factor`), the `corroboration_boost`
  keyword on `search()` and `probe_for_miss`, the `RankingInputs`
  field, and its `USAGE_FLAG_NAMES` entry. The 7.6.0 entry carries the
  reason: the factor read the `corroborations` rollup, which bumps only
  on a dedup-rejected write, a bar prose-sized memories do not reach,
  so the flag never changed a ranking. It was also off by default, so
  the shipped ranking is byte-identical;
  `test_rollup_never_moves_a_ranking` pins that the rollup reaches no
  scorer in any mode.

- **A config that still sets the key loads.** `corroboration_boost =`
  under `[behavior]`, with any value, is ignored with one logged
  warning per config file naming 8.0.0 and asking for the line to be
  deleted; every other key in the file loads as before. A stale line
  must not take a server down, the rule a pre-4.0
  `search_mode = "semantic"` already follows. Usage-toggle captures a
  7.x event log recorded under the name are skipped by
  `bettermemory eval --usage-replay` rather than reported as a flag.

- **The `corroborations` / `last_corroborated` rollup stays.** Dedup
  rejections still bump it, `memory_show` and `memory_list` still
  surface it, dead-weight curation still reads it, and
  `eval --usage-replay` still counts corroborated memories.

### Added

- `bettermemory.session_capture`: the prompt, output contract and
  validator that turn a conversation into dated memories, the first
  slice of session capture. Pure functions only (no network, no store,
  no config, nothing calls it yet): `render_turns` numbers each turn and
  writes its timestamp with the weekday, so a model can resolve
  "yesterday" or "last Tuesday" against the line it appears in;
  `build_capture_messages` fences the conversation with a nonce;
  `parse_capture` keeps a memory only when its quote is found in the
  conversation, re-citing a miscounted turn and dropping an invented
  one. The benchmark harness imports the same prompt
  (`bench/aml/extract_claude.py`, arms `--units claude` with
  `--expand keys` or `keys-inline`), so what is measured is what will
  ship.

### Internal

- `1576e92` unquotes the 121 quoted annotations `ce4df17` left, across
  56 files. Every touched file carries `from __future__ import
  annotations`, so each annotation reaches `__annotations__` as the same
  string; no behaviour changes.
- The AML adapter and dataset loaders under `bench/aml/` (`ec6afee`,
  `ccd8aca`, `8ff0dda` and the docs commits after them) and the E1 to E3
  measurements are benchmark code and results; none of it ships in the
  package.

### Migration

- A client that runs the ask-then-confirm flow for `user-inference`
  receives `status: "committed"` and an `id` from the first
  `memory_write`; there is no `pending_id` to confirm. Branch on
  `status`, as for `fact`. To keep a confirmation step before any write
  lands, set `require_write_confirmation = true` under `[behavior]`: it
  stages every category with `pending_reason: "config"`, and
  `memory_write_confirm` / `memory_write_cancel` serve it unchanged.
- `pending_reason: "user-inference"` is no longer produced; a branch on
  it can be deleted.
- Accepting a `user-inference` proposal returns `accepted`. Python code
  calling `bettermemory.handlers.proposals.accept_proposal` drops the
  `state` keyword, which no longer exists.
- Instruction files that tell the model a `user-inference` write stays
  pending until confirmed, or to ask the user before saving one, should
  say it commits with its label; `docs/system_prompt.md` carries the
  current wording.
- A config that sets `corroboration_boost` under `[behavior]` still
  loads: the line is ignored with one logged warning. Delete it. Code
  passing `corroboration_boost=` to `bettermemory.search.search()` or
  `bettermemory.audit.probe_for_miss()`, or reading the field off
  `BehaviorConfig` or `RankingInputs`, drops it; it never changed a
  ranking.
- No stored data changes shape.

## Earlier releases

Releases 1.0.0 through 7.19.2 (2026-05-08 to 2026-09-20) are in
[docs/changelog-history.md](docs/changelog-history.md), moved there
unchanged on 2026-10-14.
