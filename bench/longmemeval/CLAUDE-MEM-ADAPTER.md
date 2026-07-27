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

## The MCP server drives cleanly — probed, not assumed

`plugin/scripts/mcp-server.cjs` runs under **plain node** (no bun), speaks
JSON-RPC over stdio, and honours `CLAUDE_MEM_DATA_DIR` for full sandbox
isolation. A probe with a temp data dir returns:

```
INIT OK: {"name":"claude-mem","version":"13.12.4"}
[SETTINGS] Created settings file with defaults: <tmp>/settings.json
TOOLS (14)
```

So the read side can be driven exactly as a real client drives it. That
settles **which entry point is canonical**: the advertised step-1 tool is
**`search`** ("Step 1: Search memory. Returns index with IDs"), followed
by `timeline` and `get_observations`. `observation_search` is *not*
exposed. Use `search`.

**`observation_add` is absent from the 14**, confirming the recon note
that it is filtered out unless the runtime is Postgres "server" mode. So
**ingest cannot go through MCP** and must be out-of-band — `SessionStore`
under bun, or the HTTP API. That is not a workaround, it is the same
shape as the bettermemory side, where ingest goes through `Store.write`
rather than the `memory_write` handler. Both systems are written to
out-of-band and read through their real query path, and both halves of
that must be disclosed together.

## Built and working: ingest

`cm_ingest.js` (runs under **bun**; `SessionStore` requires `bun:sqlite`)
reads a JSON job on stdin and writes one observation per round with
`memory_session_id` set to the LongMemEval session id. Verified on
instance `e47becba`:

```
{"sessions_written":53,"rounds_offered":277,"items_written":277,"shortfall":0}
```

The 277 rows land with the right `project`, the right
`memory_session_id`, and `observations_fts` is populated (277 rows).
**Their exact search SQL returns rows against it** —
`SELECT count(*) FROM observations o JOIN observations_fts ON
observations_fts.rowid = o.id WHERE observations_fts MATCH '"degree"'`
→ 2. So open question 4 is answered: bulk insert followed by
`rebuildObservationsFTSIndex()` indexes correctly and does not
double-index.

## CORRECTION: Chroma is ON by default, not off

The pre-registration's arm table implies Chroma-disabled is a mode you
opt into. **It is the other way round.** On first boot the worker
*installs and connects Chroma by itself*:

```
[CHROMA_MCP] Prewarming chroma-mcp uvx environment {command=uvx, ...
             --from chroma-mcp==0.2.6 chroma-mcp --help}
[CHROMA_MCP] Connecting to chroma-mcp via MCP stdio (--client-type persistent
             --data-dir <dataDir>/chroma)
[CHROMA_SYNC] Smart backfill complete {project=longmemeval}
```

It also **picks up bulk-imported rows without any hook involvement**: all
277 observations were embedded, carrying `project`, `memory_session_id`,
`doc_type` and `sqlite_id` in their Chroma metadata. So the semantic arm
does not need a separate ingest path — the same `cm_ingest.js` feeds both.

Two consequences. The crossed-arms design still stands, but the *labels*
were wrong: their default is the semantic arm, and FTS5-only is the
fallback that the phrase-query defect lives in. And a Chroma run is not
free of external dependencies — it shells out to `uvx` and downloads
`chroma-mcp` on first use, which is worth stating whenever this
benchmark's "$0, no key" property is described.

## RESOLVED: it was a default 90-day recency window, not a defect

**The blocker below is solved, and the cause was ours to find, not
claude-mem's to answer for.** `performChromaSemanticSearch` applies a
default recency filter when the caller supplies no date range:

```js
v ? (…user range…) : _ = Date.now() - ms.RECENCY_WINDOW_MS
…
.filter(E => E.meta?.created_at_epoch != null && (!_ || E.meta.created_at_epoch >= _))
```

LongMemEval's corpus is dated **2023-05-20 → 2023-05-31**, roughly three
years old. Chroma was returning matches the whole time; the 90-day window
discarded every one of them before they reached the store lookup. The
FTS5 fallback did not rescue it either, because that branch is guarded on
`n.platformSource` being set.

The parameters are **`dateStart` / `dateEnd`** — not `startDate`/`endDate`
(silently ignored, still zero) and not `start`/`end`. With them supplied,
the unified endpoint returns rows, and the JSON carries
`memory_session_id` directly, which is exactly what the attribution rule
consumes.

**End-to-end proof**, instance `e47becba`, question *"What degree did I
graduate with?"*, evidence session `answer_280352e9`:

```
total observations returned: 20
distinct sessions (ranked): 12
  1. sharegpt_QZMeA7V_17
  2. answer_280352e9   <== EVIDENCE
  ...
recall@5 = 1.0
```

**This has to be declared in the protocol, because without it
claude-mem scores 0.0 on every question for a reason that has nothing to
do with retrieval quality.** A benchmark that shipped that number would
be worse than useless — it would be a false accusation. bettermemory has
no comparable recency filter, so there is nothing symmetric to apply on
our side; the disclosure is simply that their default window is
incompatible with a historical corpus and the harness widens it
explicitly.

Also worth carrying forward: **the first hypothesis was wrong twice.**
It was not the phrase-query defect (single tokens failed too), and it was
not the `cm__claude-mem` collection-naming oddity (a fresh store from a
neutral cwd reproduced it identically). Both looked convincing. Neither
survived a control.

## The phrase-query defect, now confirmed BEHAVIOURALLY

With a working FTS path (`/api/search/observations`), the defect
reproduces with a clean control rather than by reading source:

| query | result |
| --- | --- |
| `degree` | **1 hit** ×2 rows |
| `kitchen pantry` | **1 hit** |
| `pantry kitchen` | **0 hits** |
| `organize my kitchen pantry` | **1 hit** |
| `graduate degree` | **0 hits** |
| `What degree did I graduate with?` | **0 hits** |

`kitchen pantry` versus `pantry kitchen` is the decisive pair: identical
tokens, reversed order, 1 versus 0. That is a phrase query, not an AND.
The practical consequence for this benchmark is that natural-language
questions essentially never match in the FTS-only arm, because a
question's exact wording is never a contiguous substring of its evidence.

## ~~OPEN BLOCKER~~ (resolved above): `/api/search` returned zero for every query

This is unresolved and is the thing standing between here and a
claude-mem number.

`GET /api/search?query=…&format=json` returns
`{"observations":[],"sessions":[],"prompts":[],"totalResults":0}` for
every probe tried — including single words that demonstrably match.
Established so far:

- Not a data problem. 277 observations present, correct project,
  `observations_fts` populated, all 277 embedded in Chroma with correct
  metadata.
- Not an index problem. Their own JOIN query returns 2 rows for
  `'"degree"'` executed directly against the SQLite file.
- Not the DB-path problem. `/api/observations` on the same worker returns
  the imported rows, so the worker is reading the right database.
- **Not the phrase-query defect.** This was the first hypothesis and the
  controls killed it: `degree` — a single token, immune to phrase
  wrapping — returns zero through the API while `MATCH '"degree"'`
  returns 2 in SQL. Whatever this is, it is upstream of the quoting.
- Not obviously a filter. Dropping `project`, adding `type=observation`,
  and varying `limit` all return zero.
- The search path emits **no log lines at all** at
  `CLAUDE_MEM_LOG_LEVEL=debug`, which is itself a clue.

Next probe: construct the `SessionSearch` class directly against the
database file in a bun script and call `searchObservations` with the same
arguments the route builds, to isolate routing from search logic. Using
an internal for *debugging* is fine; the published arm still goes through
the real `search` tool.

## Open questions before this can run

1. **Chroma arm logistics.** The semantic arm needs a ChromaDB instance
   per question-store, or one instance partitioned per question. Cost and
   isolation are both unresolved, and per-question isolation matters as
   much here as it does for bettermemory. **This is the big one** — 500
   isolated vector stores is the expensive part of the whole benchmark.
2. ~~Which search entry point is canonical~~ — **RESOLVED: `search`**, per
   the probe above.
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
5. **Whether `search` returns enough to reach a session id.** It returns
   "an index with IDs"; the mapping from observation id to
   `memory_session_id` then comes from `get_observations` or from the DB
   directly. Which one is used changes nothing about the labels but must
   be recorded, and the retrieval depth has to be set in the same terms
   as bettermemory's `RETRIEVAL_DEPTH = 200`.

## What must not happen

Writing a claude-mem number before these four are resolved and written
down. The pre-registration exists because the last instrument died on a
provenance question nobody asked until after the adapter was planned; the
same discipline applies to the adapter itself.
