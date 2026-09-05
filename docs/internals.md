# Under the hood

Mechanics reference. None of this is required to use bettermemory —
the agent operates all of it. It's here for the curious, and for
agents that want the full picture beyond [api.md](api.md).

## The staleness verdict

What the model acts on, as it appears on a search hit:

```jsonc
{
  "snippet": "Auth middleware lives in src/auth/middleware.py …",
  "relevance": "high",
  "staleness_verdict": "spot_check_recommended",
  "path_drift": { "missing": ["src/auth/middleware.py"] },  // file moved
  "commit_drift_count": 12   // commits since the fact was last verified
}
```

The model repoints the path with `memory_update`, attests the rest
with `memory_verify`, and answers from the corrected memory.

The verdict's accuracy is measured, not asserted:
`bench/rot` grades the shipped staleness code
against git ground truth on a preregistered 30-repository corpus, with
the predictions filed before the run and the misses published as
retractions. The claim-level detector that benchmark validated ships as
declared claims on `memory_write` / `memory_verify` (3.40.0).

- Retrieval is opt-in at the tool surface. `memory_search` is a
  deliberate tool call; two deliveries bypass it. The score-gated
  prompt-recall hook (3.41.0) injects a single id + snippet pointer
  where the silent-miss probe's bar clears — the same predicate,
  threshold, and shields as the Stop-hook audit. It fires on two
  lanes (6.2.0): the classic would-be `search_miss` (a rare event by
  design; the measured firing rate lives in docs/eval-results.md),
  and — under `[behavior] recall_in_project`, default on — the
  project cohort the audit deliberately declines to flag: the caller
  stands inside the repo the top hit was written from, so no search
  was *owed*, but memory-resident facts (decisions, run state,
  rulings) are not in the source tree, and the dogfood replay ledger
  put the bulk of replayable misses in exactly this cohort. The
  audit lane is unchanged; delivered recalls stamp
  `delivered_reason` so eval slices the lanes separately. The recall
  hook injects no bodies; the verify-before-relying read path stays
  on `memory_show`. `[behavior] prompt_recall = false` restores
  purely opt-in retrieval.
  The session-start standing tier (3.42.0, `[behavior] standing_tier`,
  default off) is the unconditional one: the SessionStart hint appends
  the caller-scoped `ambient` memories that the index labels `local`
  and whose staleness verdict computes fresh — whole bodies,
  newest-verified first, under a small fixed byte budget
  (`_STANDING_BUDGET_BYTES` in `cli/session_start_cmd.py`), truncating
  only at memory boundaries — because opt-in retrieval cannot serve
  knowledge whose trigger condition is not knowing you need it. Since
  7.0.0 provenance is the first gate: every other admitted ambient
  memory (`synced`, `untracked`, `unaccounted`, or a row the index has
  not classified) renders as a pointer, id, scopes and label under the
  same budget and never the body, because the stamp the verdict reads
  is frontmatter and a planted or pulled file carries whatever its
  writer chose. Admission for a local body runs the same verdict chain
  `memory_show` computes (calendar leg, claim-anchored path drift,
  commit drift); anything not fresh collapses to one aggregate
  "verify to restore delivery" line, which converts the tier's
  verification debt into pressure to pay it. Delivery records nothing
  (the session-start negative mandate is untouched), so v1 adoption is
  deliberately unmeasured; docs/ROADMAP.md records the two rejected
  instrumentation shapes and why.
- Claims about the user always stage for confirmation before commit.
- Write gates instead of trust: durability check (rejects transient
  state), credential check (rejects secret-shaped tokens), duplicate
  and tombstone dedup, scope-mismatch check, optional groundedness
  check against the source transcript.
- Write-time supersession (`supersession.py`): a claim-sized write that
  carries a change cue and diverges on a value from a stored claim about
  the same subject gets a `supersedes` link to it, so the stale hit
  renders `superseded_by`; the same divergence with no cue is filed for
  `memory_conflicts`. Lexical and deterministic, measured on the
  integrity benchmark's sealed corpus before it shipped.
- Usage telemetry: `memory_record_use` logs which sentence of a reply
  each memory shaped; a turn-end probe flags retrievals the model
  should have made but didn't; `memory_health` and `memory_curate`
  report and act on the resulting rot.
- Hybrid search (keyword + BM25 fused via RRF), with plural-folding and
  CJK-capable tokenization. Every ranker is deterministic lexical code —
  the project ships no embedding models. (Pre-4.0 an optional semantic
  leg fused in from an `embeddings` extra; the 4.0.0 purist strip
  removed that lane whole; a 5.5.0 opt-in reentry was revoked in
  6.0.0 by owner doctrine — own-built machinery or none.) 5.1 adds rescue expansion, OFF by default:
  discourse-filler words price at a df floor, and when the base
  ranking is not confident a down-weighted third leg over committed
  vocabulary tables (inflection variants, clippings, dev-domain
  synonyms — `expansion.py`) joins the fusion. Tables are readable
  source, derivation-free, query-side only — the persisted index
  stream never changes. Off by default because its preregistered
  held-out check killed default-on: strong on technical-prose stores
  (bench/retrieval), net-negative on conversational ones
  (bench/longmemeval carries the kill and the ablations).
- Typed inter-memory links (`supersedes`, `contradicts`, `extends`,
  `depends_on`), surfaced as trust signals at retrieval; `supersedes`
  is also set by the write path (declared or detected), `contradicts`
  by a `memory_conflicts` verdict.
- Auto-scoping by repo and worktree; explicit cross-project queries.
- Episodes: a sibling journal tier for run-state that never pollutes
  durable search, with promotion when a takeaway hardens into a fact.
- Tombstones instead of deletes; everything is restorable.
- Scales past ~500 memories via a derived SQLite FTS5 index. The
  markdown files stay canonical; upgrades rebuild it automatically,
  and `bettermemory reindex` rebuilds on demand.
- Provenance: the index labels how each memory entered the store
  (`local`, `synced`, `untracked`, `unaccounted`), derived from the
  event log and the sync repo and never from the file; the section
  below.
- Cross-machine sync over your own git remote, and an eval CLI
  (`memory_helped_rate` / `endorsement_rate` / `silent_miss_rate`,
  see [eval.md](eval.md)).

## Provenance

Every trust field a memory file carries (`last_verified_at`, `source`,
`confidence`, `claims`) is frontmatter, and frontmatter is whatever the
last writer chose to put there. Since 6.5.0 the index carries one label
per memory that the file cannot supply, derived at every rebuild:

- `local`: written, ingested, promoted, accepted or restored by this
  host's own code path. Stamped at the index upsert the store's own
  creation paths perform, so every in-process creation is covered even
  with telemetry off, and re-derived at rebuild from the write-side
  events that name the memory id.
- `synced`: no local creation on record, but a `sync pull` event names
  the file, or (for pulls made before pulls recorded) the store's own
  sync repo tracks it while the log covers the memory's creation.
- `untracked`: the event log cannot speak to it. The store keeps no
  events, or the memory predates the oldest surviving event and no
  classified rebuild had seen it arrive.
- `unaccounted`: the log covers the memory's creation window, nothing
  wrote it, nothing pulled it; or it claims a creation date older than
  the log while appearing for the first time after a classified
  baseline existed. The hand-planted shape.

`local` and `unaccounted` are sticky across rebuilds, and every label
survives the drop a schema or tokenizer bump performs. Deleting the
index file and running `bettermemory reindex` discards both and
reclassifies from events alone; that is the documented reset. The label
rides search hits, `memory_show`, `memory_list`, the recall pointer,
`memory_health` and `bettermemory doctor`. Nothing relabels a record:
a memory you recognise is re-admitted through the store (remove, then
restore), and the rest are removed.

Episodes carry a label of their own since 7.0.0, read from the event
log at each read rather than stored (`provenance.EpisodeEvidence`):
`local` when an `episode_write` event names the id, `untracked` when
the log holds no in-process event that could have named it (or the
episode predates the oldest surviving one), `unaccounted` when the file
appeared under a session directory with no event, the planted shape.
Session-tag floors carry no label; the read surfaces filter them first.
`episode_search` keeps the body beside the label; `episode_handoff`
delivers takeaways by default, bodies on request (`include_bodies`),
and never the body of an unaccounted episode.

Cost: one pass over the event log per rebuild and one `git ls-files`
in a sync repo; nothing per search. What it cannot see: an
injection-driven legitimate write, which is `local` by every test here
and truthfully so. Cause provenance (what was in context at write time)
is a different question, and an open one.

## Sync admission

Since 6.6.0 `sync pull` judges every memory file the rebase brought
down before it records the `sync_pull` event and before the index is
rebuilt, inside the same store-wide sync lock. The chain, in order:

1. A size cap. A file above the store's read cap (1 MiB) is refused
   without being read.
2. The store's own parser. A file the store could not load is refused.
3. An id alias check. A pulled file whose id another active file
   already carries is refused, and the file already here keeps the id:
   the rebuild collapses duplicate ids by directory order, so without
   the check a hostile body that sorts later would replace the
   legitimate one in the index.
4. The credential gate the write path runs (`ADMISSION_GATES` in
   `handlers/write.py`, the one gate whose refusal is a property of the
   bytes alone). The transient and user-claim gates are soft locally
   (an acknowledgement or the pending handshake lifts them) and neither
   travels with the file, so they are detected and reported as
   advisory flags on admitted files, never refused; the dedup gates
   would score an update pull against the record's own stored copy;
   the scope gate judges a checkout a pull has none of.

A refusal is quarantined: the file stays where git put it, tracked and
unchanged, so the worktree stays clean and no deletion propagates to
other hosts, and the host-local, gitignored sidecar `.quarantine.json`
names it with the reason, a detail that never quotes the body, the
remote, the pull time and the sha256 of the refused bytes. Every
active-file walk skips the names in the sidecar (`iter_active_memory_paths`
in `store.py` is the one definition), so a quarantined file is never
indexed, searched, listed, shown, counted or pointed at. Every later
pull judges the quarantined files again and releases one that passes
(fixed upstream) or drops one that vanished; `bettermemory sync
quarantine` lists them and `--release NAME` runs the chain by hand,
`--force` admitting a credential refusal as it is. The structural
refusals cannot be forced. `sync status` carries the count and doctor's
`sync_quarantine` check warns while any file is held.

### Remote stamps

The index also records `verified_locally_at`: when this host last
stamped the memory through its own `memory_verify`, set at the verify
upsert and cleared by a pull for every file it lands. A `synced` record
whose file carries a `last_verified_at` but whose row has no local
verification reads `verification.status: "remote"` and
`spot_check_required` on every surface, and the recall pointer says
`unverified here`. A local verify re-checks the attested paths against
this machine and lifts the rule. The column is carried across rebuilds
and derived from `verify` and `sync_pull` events when the index is
rebuilt from scratch, so the rule survives an index reset.

## Storage

One file per memory, grep-able and hand-editable:

```markdown
---
schema_version: 1
id: 01HXYZ123ABCDEFGHJKMNPQRST
created: 2025-03-14T10:23:00+00:00
updated: 2025-03-14T10:23:00+00:00
scopes: [projects:acme, infrastructure]
confidence: high
source: explicit-statement
---
Staging deploys via `fly deploy --config fly.staging.toml`; the old
Render service is decommissioned.
```

Verification attestations, origin (repo/branch/worktree), and typed
links are optional frontmatter, added only when populated. Removed
memories move to `.tombstones/` with their `removed_reason`; episodes
live under `episodes/<session_id>/` with a 30-day TTL.

The store resolves to `$BETTERMEMORY_DIR` if set, else `./.claude-memory/`
if it exists, else `~/.claude-memory/`.

## Tools

27 MCP tools; 18 register by default. Nine curation/power-user tools
sit behind `[behavior] full_tool_surface = true`, and most of those
have a CLI counterpart. Grouped: retrieval, writing (with a
staged-confirm flow), lifecycle, verification, curation, session-local
scope toggles, and episodes. Signatures, defaults, and return shapes:
[api.md](api.md).

That default surface is not cheap, and it is charged on every turn
whether a memory tool is called or not. The full serialized
`tools/list` measured 33,960 bytes — 27,092 of it names and
descriptions, 5,252 input schemas — on 2026-07-31 at 3.32.0. Every
figure in that sentence, the version label included, is read off
`bench/toolcost/results/bettermemory-2026-07-31.json`; quote the
artifact rather than this paragraph when they disagree. Method and
fairness rules: the toolcost-bench notes. CI hard-caps the
description component so it cannot drift upward unnoticed.

The 2026-07-26 head-to-head against claude-mem 13.12.4 in that same
directory is deliberately left as it was rather than re-paired. Its
bettermemory arm is the pre-footprint surface, and re-running only our
side would produce a ratio whose numerator and denominator came from
different weeks — the arithmetic would work and the claim would not.
The pair gets re-run as a pair or not at all; until then the number
above is what this project charges, and the ratio is historical.

Two serialization conventions live side by side here and are not
interchangeable. `full_bytes` above is the wire cost: JSON syntax
around every name, description and schema, and it carries neither the
server `instructions` block nor the plugin skill frontmatter.
`tests/test_resident_footprint.py` sums the same components as raw
Python lengths and adds the two the wire figure omits, so its total is
the resident-context cost rather than the `tools/list` payload. Quote
them separately; a review that mixes them will find them disagreeing.

## CLI

`bettermemory` with no arguments is the MCP server (stdio). It also
provides:

```text
bettermemory try              # offline staleness demo
bettermemory init --client X  # register with a client (idempotent)
bettermemory doctor           # diagnose install state (--fix: safe repairs)
bettermemory health           # curation rollup
bettermemory consolidate      # dedup/demote pass (dry-run; --llm for more)
bettermemory eval             # the three metrics, with CIs
bettermemory eval --report    # same telemetry as publishable markdown
bettermemory sync push|pull   # git-based cross-host sync
bettermemory sync quarantine  # pulled files admission refused; --release
```

`bettermemory <command> --help` for flags; `reindex`, `ingest`,
`tombstones`, `proposals`, `rename-scope`, `episodes`, and `export`
also exist.

## Configuration

`config.toml` lives under platformdirs (`~/Library/Application
Support/bettermemory/` on macOS, `~/.config/bettermemory/` on Linux).
Defaults are sensible; most installs never edit it. See
[api.md](api.md) and the file's own comments for the knobs.

## Limitations

- No encryption at rest. Don't store secrets (a write-time check
  refuses secret-shaped tokens); use disk encryption if you need it.
- Sync conflicts are git merge conflicts; there is no auto-resolution.
- Multi-process file locking works on Windows, but not identically to
  POSIX. Both platforms lock a persistent sidecar lockfile next to the
  target; POSIX takes a blocking whole-file `fcntl.flock(LOCK_EX)`,
  Windows takes `msvcrt.locking(LK_NBLCK, 1)` on one byte (offset 0) of
  that lockfile. Two consequences: the byte range is a convention, so
  exclusion holds only between callers that go through `flock_excl`
  (true for every bettermemory writer, and the same cooperative model
  POSIX advisory locks use); and the Windows acquire is non-blocking
  plus retried with capped backoff, so it gives up with `TimeoutError`
  after `BETTERMEMORY_FLOCK_TIMEOUT` seconds (default 30) where POSIX
  would wait indefinitely. Windows also has two degradation paths POSIX
  lacks — if `msvcrt` won't import or the lockfile can't be opened, the
  helper falls back to no cross-process lock and emits a one-shot
  warning rather than failing the write.
