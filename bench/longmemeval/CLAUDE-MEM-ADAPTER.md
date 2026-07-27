# claude-mem adapter — reconnaissance and design

Notes for the unbuilt half of item (e). Everything here was read out of
the **published artifact** (`claude-mem@13.12.4`, Apache-2.0, npm
tarball sha256 `de8b69ce7220a8b46e5fdec6304501f964c3a972c0c1c2a41efae4ea12bf3518`),
not out of their git repository — so it describes what a user actually
installs.

Fetch it:

```sh
mkdir -p bench/longmemeval/vendor && cd bench/longmemeval/vendor
curl -sL -o claude-mem.tgz https://registry.npmjs.org/claude-mem/-/claude-mem-13.12.4.tgz
tar xzf claude-mem.tgz
```

`vendor/` is gitignored. Nothing is executed during the fetch.

## There are two runtimes, and the schema you find first is the wrong one

Grepping for `CREATE TABLE ... observations` surfaces a **Postgres**
schema (`TSVECTOR`, `JSONB`, `TIMESTAMPTZ`, `content_search GENERATED
ALWAYS AS to_tsvector`). That is the hosted "server" mode. The local
default — what a normal install runs — is **SQLite**, defined in
`plugin/sqlite/SessionStore.js`, and its `observations` table is a
different shape entirely:

```sql
CREATE TABLE IF NOT EXISTS observations (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_session_id TEXT NOT NULL,     -- FK -> sdk_sessions
  project           TEXT NOT NULL,
  text              TEXT NOT NULL,
  type              TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  created_at_epoch  INTEGER NOT NULL
);
```

with later migrations adding `title`, `subtitle`, `narrative`, `facts`,
`concepts`, `files_read`, `files_modified`, `prompt_number`,
`discovery_tokens`, `agent_type`, `agent_id`, `content_hash`.

Benchmarking against the Postgres schema would measure a deployment mode
almost nobody runs. **The SQLite path is the one to drive.**

## The attribution rule maps onto their own schema — this is the good news

`observations.memory_session_id` is a first-class column with a foreign
key to `sdk_sessions`. claude-mem therefore has a **native session
concept**, and the item→session mapping the pre-registration fixes is not
something imposed on it from outside: set `memory_session_id` to the
LongMemEval session id on ingest and their own rows carry the label
association.

That makes the two sides genuinely symmetric:

| | bettermemory | claude-mem |
| --- | --- | --- |
| ingest unit | one memory per round | one observation per round |
| session link | side map, id never stored as content | native `memory_session_id` |
| write path | `Store.write` (raw layer) | direct SQLite insert + FTS rebuild |
| guardrails | bypassed, declared | bypassed, declared |

Neither system's *capture* pipeline runs. That is the same limitation on
both sides and it must be stated for both, not just ours.

## Search, and the defect confirmed in the shipped bundle

FTS search lives in `plugin/scripts/worker-service.cjs` (minified). The
observations query:

```sql
SELECT o.*, o.discovery_tokens
FROM observations o
JOIN observations_fts ON observations_fts.rowid = o.id
WHERE observations_fts MATCH ?
  {filters}
{order}
LIMIT ? OFFSET ?
```

and the bound parameter is built as:

```js
d = '"' + e.replace(/"/g, '""') + '"'
```

**The entire user query is wrapped in double quotes**, which makes it an
FTS5 *phrase* query — it matches only contiguous runs of tokens. The
identical construction appears at the `session_summaries_fts` site. This
is the defect the competitive recon reported from their source at
`SessionSearch.ts:285` / `:350`, and it is **confirmed present in the
published artifact**, which is the version that matters for a
comparative claim.

There is an `this._fts5Available` guard with a non-FTS fallback branch
above it, so a run must record which branch it actually took rather than
assume FTS5 was used.

**Do not publish only this arm.** It bites when Chroma is disabled — a
mode claude-mem ships, documents, and recommends in their own issue #707
as a 35 GB-RAM mitigation — but publishing a lexical-only number as
*the* claude-mem result would be indefensible. Both Chroma states go
side by side, as the pre-registration commits to.

## Open questions before this can run

1. **Chroma arm logistics.** The semantic arm needs a ChromaDB instance
   per question-store, or one instance partitioned per question. Cost and
   isolation are both unresolved, and per-question isolation matters as
   much here as it does for bettermemory.
2. **Which search entry point is canonical.** `observation_search` and
   `search` are both advertised MCP tools. Driving the MCP server over
   stdio (`plugin/scripts/mcp-server.cjs`, plain CJS, no Bun needed) is
   more faithful to what a user gets than calling the store function
   directly; calling directly is more controllable. The faithful option
   should win unless it proves impossible, and the choice must be
   recorded before numbers exist.
3. **Ranking semantics.** Their order clause uses `observations_fts.rank`
   with date-ordering alternatives. Whichever is default must be used and
   named — picking the ordering that flatters us would be the same
   category of error as running our embedding arm against their
   Chroma-off arm.
4. **FTS rebuild after bulk insert.** `rebuildObservationsFTSIndex()`
   exists and issues `INSERT INTO observations_fts(observations_fts)
   VALUES('rebuild')`. Bulk-inserting 124,361 rows and rebuilding once is
   almost certainly right, but it must be verified that the triggers do
   not double-index.

## What must not happen

Writing a claude-mem number before these four are resolved and written
down. The pre-registration exists because the last instrument died on a
provenance question nobody asked until after the adapter was planned; the
same discipline applies to the adapter itself.
