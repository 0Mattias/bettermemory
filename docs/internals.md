# Under the hood

Mechanics reference. None of this is required to use bettermemory;
the agent operates all of it. It is here for the curious, and for
agents that want the full picture beyond [api.md](api.md).

## The staleness verdict

What the model acts on, as it appears on a search hit:

```jsonc
{
  "snippet": "Auth middleware lives in src/auth/middleware.py …",
  "relevance": "high",
  "staleness_verdict": "spot_check_recommended",
  "path_drift": { "missing": ["src/auth/middleware.py"] },  // file moved
  "commit_drift_count": 12,  // commits since the fact was last verified
  "commit_drift_basis": "reachability"  // counted in verified_head..HEAD
}
```

The model repoints the path with `memory_update`, attests the rest
with `memory_verify`, and answers from the corrected memory.

The commit count has two bases and says which it used.
`memory_verify` records the origin checkout's HEAD on the record as
`verified_head`; the leg then counts the commits in
`verified_head..HEAD` that touch the memory's anchors
(`basis: "reachability"`), one `git log --boundary --name-only` walk
per distinct anchor and HEAD, memoised, the anchored count read off the
walk in Python. A record with no anchor, or one whose anchor a rewritten
history no longer reaches, keeps the older count, the commits authored
after `last_verified_at` (`basis: "author-date"`). The difference is a
merge: a branch authored before the stamp and merged after it is inside
the range and outside the author-date window, so the author-date count
reads zero on exactly the work that landed. `bench/rot`'s two basis
arms measure that gap on the corpus.

The verdict's accuracy is measured, not asserted: `bench/rot` grades the
shipped staleness code against git ground truth on a preregistered
30-repository corpus, with the predictions filed before the run and the
misses published as retractions. The claim-level detector that benchmark
validated ships as declared claims on `memory_write` / `memory_verify`.

- Retrieval is opt-in at the tool surface. `memory_search` is a
  deliberate tool call; one delivery bypasses it. The score-gated
  prompt-recall hook injects a single id plus snippet pointer where the
  silent-miss probe's bar clears, the same predicate, threshold and
  shields as the Stop-hook audit. It fires on two lanes: the classic
  would-be `search_miss` (a rare event by design; the measured firing
  rate lives in docs/eval-results.md), and, under `[behavior]
  recall_in_project` (default on), the project cohort the audit
  deliberately declines to flag: the caller stands inside the repo the
  top hit was written from, so no search was *owed*, but
  memory-resident facts (decisions, run state, rulings) are not in the
  source tree. Delivered recalls stamp `delivered_reason` so eval
  slices the lanes separately. The recall hook injects no bodies; the
  verify-before-relying read path stays on `memory_show`. `[behavior]
  prompt_recall = false` restores purely opt-in retrieval.
- Claims about the user commit like any fact but carry the
  `user-inference` label, so a stored inference stays distinguishable
  and correctable; a body that reads as one while filed as `fact` is
  refused until it is relabelled.
- Write gates instead of trust: durability check (rejects transient
  state), credential check (rejects secret-shaped tokens), duplicate
  and tombstone dedup, scope-mismatch check, optional groundedness
  check against the source transcript.
- Write-time supersession (`supersession.py`): a claim-sized write that
  carries a change cue and diverges on a value from a stored claim about
  the same subject gets a `supersedes` link to it, so the stale hit
  renders `superseded_by`; the same divergence with no cue is filed for
  the conflicts queue. Lexical and deterministic, measured on the
  integrity benchmark's sealed corpus before it shipped.
- Usage telemetry: `memory_record_use` logs which sentence of a reply
  each memory shaped; a turn-end probe flags retrievals the model
  should have made but did not; the health report
  (`memory_admin(action="health")`, `bettermemory health`) reports the
  resulting rot.
- Hybrid search (keyword plus BM25 fused via RRF), with plural-folding
  and CJK-capable tokenization. Every ranker is deterministic lexical
  code; the project ships no embedding models, and the store's FTS5
  table is the candidate prefilter above ~500 memories.
- Typed inter-memory links (`supersedes`, `contradicts`, `extends`,
  `depends_on`), surfaced as trust signals at retrieval; `supersedes`
  is also set by the write path (declared or detected), `contradicts`
  by a conflicts verdict.
- Auto-scoping by repo and worktree; explicit cross-project queries.
- Episodes: a journal beside memory for run-state that never pollutes
  durable search; a takeaway that hardens into a fact is written with
  `memory_write` by the model that read it back.
- Tombstones instead of deletes; everything is restorable, and a
  restore re-checks the trust the tombstone carried (a claim the
  origin tree now contradicts or an attested path that no longer
  exists is dropped, the stamp with it).
- Provenance: every record points at the signed log row that produced
  it, and the pointer is verified on every read; the section below.
- An eval CLI (`memory_helped_rate` / `endorsement_rate` /
  `silent_miss_rate`, see [eval.md](eval.md)).

## Provenance

Every trust field a memory carries (`last_verified_at`, `source`,
`confidence`, `claims`) is whatever the last writer chose to put there.
The store carries one label per record that the record cannot supply,
read at every search and show:

- `local`: written, verified or restored through this host's own tool
  surface. The record's `log_mac` names a `memory_put` row in the log,
  that row's payload names the record, and its MAC verifies under the
  store's key.
- `imported`: brought in by `bettermemory migrate v8`, this host's own
  record of an 8.x directory. Reads like `local`.
- `synced`: reserved for the cross-host lane, which 9.0 does not ship.
  A `synced` record whose stamp this host never made reads
  `verification.status: "remote"` and `spot_check_required` until a
  local `memory_verify` re-stamps it.
- `unaccounted`: the pointer fails. The log row is missing, is of
  another kind, names another record, or fails its MAC. A row inserted
  around the store reads this way, and so does a row whose log row was
  forged without the key.

Nothing relabels a record: a memory you recognise is re-admitted
through the store (remove, then restore, which writes a fresh signed
row and stamps `local`), and the rest are removed. The label rides
search hits, `memory_show`, the recall pointer and the health report's
`provenance` census. Episodes carry the same label, read the same way,
and the handoff never delivers the body of an unaccounted episode.

A store opened on a machine that does not hold its key cannot verify
any pointer: every row reads `trust_unavailable`, and a stamp of
unknown origin is not read as this host's. `bettermemory log verify`
is the whole-chain audit: it walks every row, checks every MAC and
every link to the row before, compares the head checkpoint, and refolds
the log to compare every table against it.

Cost: one indexed log lookup and one HMAC per row served, nothing per
candidate. What it cannot see: an injection-driven legitimate write,
which is `local` by every test here and truthfully so. Cause provenance
(what was in context at write time) is a different question, and an
open one.

## Module map

Eighty-five Python files under `src/bettermemory/`, its `handlers/`
and `cli/`. A tool call crosses them in one order, and the map is that
order.

**Entry.** `cli/` is the `bettermemory` command; `cli/serve.py` is the
no-argument default and hands off to `builder.build_server`, which
instantiates the MCP SDK server and binds the nine tools. `server.py`
is a re-export shim kept for callers that import `build_server` from
there. `__main__.py` covers `python -m bettermemory`.

**Request.** Every tool handler lives in `handlers/<tool>.py` beside its
`DESC_` description constant; `handlers/admin.py` and
`handlers/episode.py` are the two dispatchers, one function per former
tool behind them. `handlers/_shared.py` holds what all of them reach for
(payload validation, use-token settlement, the turn counter).
`_handlers.py` is the facade that wires those functions onto one class.
`identity.py` resolves who is calling and from where (the declared
client, model and transport session, the attested OAuth principal, and
the channel that named the workspace) and publishes the caller for the
request; on the wire path its middleware asks a roots-capable client
where it works, once per connection. `session.py` resolves the
per-client `SessionState` for the request (disabled scopes, use
tokens), keyed on that caller, and `_response.py` shapes what goes back
on the wire. `_decorators.py` and `time_utils.py` are cross-cutting.

**Write path.** `handlers/write.py` runs the gate chain: `credentials.py`
(secret-shaped strings), `durability.py` (structural durability),
`scope_match.py` (scope mismatch against the caller's repo),
`groundedness.py` (transcript overlap), `claims.py` (declared claims
checked against the worktree), `supersession.py` (does this replace a
stored statement), `conflicts.py` (contradiction candidates). A write
that passes lands in `store.py` as one signed log row and one table
row in one transaction, with `log.py` signing the row and keeping the
chain. `origin.py` stamps where the write happened.

**Read path.** `handlers/search.py` and `handlers/show.py` read the
store's candidate query (the FTS5 prefilter above the index threshold)
into `search.py`, the ranker, with `expansion.py` kept for the retrieval
bench's expansion arm. Ranking is drift-independent; `verify.py` then
annotates each hit with the staleness verdict from path, commit and
calendar drift, with `symbols.py` for advisory symbol citations, and
the store's `trust_rows` supplies the provenance label and this host's
stamp. The warm daemon's caches sit on this path and keep every answer
bit-identical: `origin.py` keeps the captured origin per directory for
two seconds under a signature of the repository's HEAD bytes, config
and environment; `_response.py` and `origin.py` memoise the commit-drift
work per repository root and commit HEAD names, which `githead.py`
reads from the repository files without a git process; `search.py`
memoises the token streams per body and scopes. Every cache registers
its clearer with `_caches.py`, and `_caches.clear_all` empties them.

**Telemetry.** `events.py` is the recorder every handler records
through; each event is a row of the store's log. `audit.py` and
`attribution.py` turn the rows into silent-miss and use telemetry;
`eval.py` computes the effectiveness rates; `health.py` is the rollup
`handlers/health.py` and the CLI serve.

**Hooks.** `hook.py` is the Claude Code side: the Stop-hook turn audit
and the UserPromptSubmit prompt recall, invoked through
`cli/audit_turn_cmd.py`, `cli/prompt_recall_cmd.py` and
`cli/session_start_cmd.py`. These run out of process, so they read the
store rather than the server's session state.

**Offline.** `migrate_v8.py` reads an 8.x directory through the frozen
readers in `v8.py` and writes the store; `mirror.py` writes the store
back out as that directory, with `_frontmatter.py` as the markdown
codec; `rollback.py` removes one actor's writes and leaves everyone
else's; `init.py` and `_install_hints.py` are client onboarding.

**Leaves.** `models.py` (Pydantic models, enums, ULIDs), `config.py`
(config loading and the store-directory rule), `prompts.py` (the
system-prompt addendum), `_fsutil.py` (atomic writes and owner-only
directories), `githead.py` (HEAD and the ref it names, read from the
repository files) and `_caches.py` (the cache registry). Nothing in this group imports a handler or the store, and
a new cross-cutting concept belongs here, not in a cycle.

## Storage

One SQLite file per store, `memory.sqlite` in the store directory
(`store.py`). The tables hold the records: `memories`, `tombstones`,
`episodes`, `conflicts`, `verifications` (this host's stamps),
`quarantine` and `imports`, plus the FTS5 table and triggers the 8.x
index used, kept verbatim so the candidate query returns the same ids
in the same order. Every one of those tables is the fold of the `log`
table: a mutation is appended to the log first, signed, and applied to
its table in the same transaction, and every telemetry event is a log
row too. A row of the log carries a sequence number, a timestamp, a
kind, a session, a JSON payload and an HMAC-SHA256 over all of them plus
the MAC of the row before it. The key is 32 random bytes under the user
config directory (`keys/<store_id>.key`), never in the store; the store
holds the key's fingerprint, and a head checkpoint beside the key
records the last row written. Every record row carries the MAC of the
log row that produced it, which is what the provenance label reads.

The store resolves to `$BETTERMEMORY_DIR` if set, else `./.claude-memory/`
if it exists, else `~/.claude-memory/`. An 8.x directory is read by
`bettermemory migrate v8` and written by `bettermemory export --mirror`;
the running server never reads or writes markdown files.

## Tools

Nine MCP tools, all registered on every start: `memory_search`,
`memory_show`, `memory_write`, `memory_update`, `memory_remove`,
`memory_verify`, `memory_record_use`, `episode` (write, handoff) and
`memory_admin` (restore, tombstones, health, rename_scope, conflicts,
acknowledge_miss, disable_scope, enable_scope). Signatures, defaults and
return shapes: [api.md](api.md).

The surface is charged on every turn whether a memory tool is called or
not, so its size is measured: `bench/toolcost` serialises `tools/list`
and the `initialize` reply and records every description, every input
schema and the `instructions` block, in bytes and in characters, in a
dated artifact under `bench/toolcost/results/`. Quote the artifact
rather than any paragraph when they disagree. The 2026-07-26
head-to-head against claude-mem in that directory is left as it was
rather than re-paired: its bettermemory arm is the 27-tool surface, and
re-running only our side would produce a ratio whose two sides came
from different weeks.

## CLI

`bettermemory` with no arguments is the MCP server (stdio). It also
provides:

```text
bettermemory try                   # offline staleness demo
bettermemory init --client X       # register with a client (idempotent)
bettermemory health                # the health report
bettermemory tombstones list       # removed memories; restore, prune
bettermemory rename-scope OLD NEW  # rename a scope everywhere
bettermemory rollback --by-actor X # remove one actor's writes
bettermemory episodes list         # the journal; prune
bettermemory export                # JSON, or --mirror DIR for 8.x files
bettermemory migrate v8            # import an 8.x directory
bettermemory log verify            # audit the hash chain
bettermemory eval                  # the three metrics, with CIs
```

`bettermemory <command> --help` for flags. `hook session-start|stop|prompt`
is the hook command the plugin wires; it posts the hook's payload to the
local daemon (`bettermemory up`, `down`, `status`), which is also what
`bettermemory` with no arguments serves stdio in front of.

## Configuration

`config.toml` lives under platformdirs (`~/Library/Application
Support/bettermemory/` on macOS, `~/.config/bettermemory/` on Linux).
Defaults are sensible; most installs never edit it. `[behavior]` holds
the knobs (`default_max_results`, `search_mode`, `conversational`,
`write_supersession`, `prompt_recall`, `recall_in_project`,
`verification_stale_days`, `tombstone_retention_days`, the size caps),
`[scopes]` the allowed list and typo exceptions, `[telemetry]` the one
switch, `enabled`. A key 9.0 removed loads with one logged warning and
is ignored; the file's own comments say which.

## Limitations

- No encryption at rest. Do not store secrets (a write-time check
  refuses secret-shaped tokens); use disk encryption if you need it.
- Tamper evidence, not tamper protection. A writer who holds the key
  can sign anything; a writer who holds the store file but not the key
  is detected, not stopped. `SECURITY.md` draws the line.
- One host per store. The cross-host lane is not in 9.0: a store copied
  to another machine reads there, without its key, as
  `trust_unavailable`.
- One writer at a time. SQLite serialises writers; a second server
  process on the same store waits on the busy timeout rather than
  interleaving.
