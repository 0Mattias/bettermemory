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
  deliberate tool call; the recall surface (`recall.py`) is the one
  delivery that bypasses it, and it delivers pointers only: an id and a
  summary, never a body, so the verify-before-relying read path stays on
  `memory_show`. On SessionStart inside a project the session brief is
  two lists, standing first: the caller's standing records in force
  (`standing.standing_ids`: an answer at or above `TAU_STANDING` for the
  current body, or category ambient; history excluded), eight at most,
  each line marked `[in force]`; then the admitted in-force records the
  conversation so far bears on, eight at most, drawn from the hooks'
  pool over a probe of the newest 12,000 characters of owner words in
  the session's transcript (`transcript.owner_words`), history excluded
  (`in_force`), an open contradiction held. Each list ranks by the probe
  through the search ranker; with no transcript, or one not yet written,
  the probe is empty and both rank by recency. A record another in-force
  record extends carries `[extended by <id>]`. The header counts both
  lists, and there is no character cap. On UserPromptSubmit the prompt
  recall probes the owner's words as the one definition of an owner
  prompt reads them (`owner_prompt.py`: the app's envelope blocks
  removed, reminders, pastes and artifact views, then 2,000 characters)
  through the audit's probe and delivers its top three hits admitted for
  the caller, unless held by provenance, an open contradiction or the
  segment; a superseded hit yields its slot to its head, marked
  `[replaces <id>]`. The recall block is cut to `RECALL_CAP_CHARS`, 900
  characters: the snippets are shortened first, from the last pointer
  back, a snippet that would fall under a dozen characters dropped, and
  then trailing pointers go. On both surfaces a disputed record carries a
  `[disputed: cf-…]` mark. A prompt
  the harness composed and no human typed (a task notification, a
  message from another session, an interrupt line, any other harness
  tag, reminder blocks alone) is not probed and records nothing. The
  Stop, capture and the recall review judge a transcript row by the
  same definition, its marks first (`isMeta`, `isCompactSummary`, a
  `turnOrigin` other than `human`), so the recall and the Stop hash a
  prompt alike. A per-session cooldown shows no id twice in a segment,
  by the brief or the recall.
  A clear opens a new segment. A compact opens one only on the
  compaction marker its transcript holds, a `compact_boundary` row newer
  than the one the segments last opened on; without it the compact is a
  resume until a later hook, however late, finds a marker stamped within
  60 seconds of it, which opens the segment at the compact
  (`recall.confirm_compaction`). A resume keeps the segment, and its
  brief omits what the segment has shown. The cooldown is rebuilt from
  the session's own rows whenever they were stamped, so a restart, an
  eviction after a day idle and the CLI path see the segment the daemon
  held. Each show is recorded (`session_brief`, `prompt_recall`), a
  probed prompt that delivered nothing records `recall_held` with its
  cause, and the Stop settles a shown pointer as an applied `use` naming
  its surface when the reply used it, never by the auto fallback; what a
  closing segment never settled is written `unused`, and a turn the
  owner interrupted is audited by the next Stop. Nothing is shown with
  telemetry off.
  `[behavior] prompt_recall` takes `project` (default), `everywhere` or
  `off`; `session_brief = false` turns the brief off.
- Claims about the user commit like any fact but carry the
  `user-inference` label, so a stored inference stays distinguishable
  and correctable; a body that reads as one while filed as `fact` is
  refused until it is relabelled.
- Write gates instead of trust: durability check (rejects transient
  state), credential check (rejects secret-shaped tokens), duplicate
  and tombstone dedup, scope-mismatch check.
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
- Auto-scoping by project membership (the section below); explicit
  cross-project queries.
- Episodes: a journal beside memory for run-state that never pollutes
  durable search; a takeaway that hardens into a fact is written with
  `memory_write` by the model that read it back.
- Tombstones instead of deletes: a removed memory can be restored
  until `bettermemory tombstones prune` deletes its tombstone. A
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

A server that opens a store whose key is not on this machine writes a
new key and appends a `rekey` row: it opens through `Store.open_or_create`,
the owner's opener, which passes `allow_rekey=True`. A plain `Store.open`
does not: since 9.0.0rc10 it leaves such a store readable and refuses the
first write, naming the key directory, so a copy on another machine or a
daemon under the wrong key directory never splits the chain by accident.
`bettermemory log verify` is the whole-chain audit: it walks every row
from seq 1, checks every MAC and every link to the row before, compares
the head checkpoint and the origin anchor, and refolds the log to compare
every table against it. A store whose meta carries a store id other than
the 32 lowercase hex characters `create` writes is refused at open,
since the key ring names its files after that id.

**Segments.** The `rekey` rows split the log into segments, each signed
by one key, and a segment is *held* when this machine's key ring has
that key, current (`<store_id>.key`) or retired
(`<store_id>.<first 16 hex of its fingerprint>.key`). A segment's key is
named by the rekey rows, and the first segment's name is one its own key
never signed (the first rekey row's `old_fingerprint`, or meta), so a
first segment whose named key is not held is also tried against every
key the ring holds, by its first row's MAC (`log.chain_segments`).

- *Leading* segments come before this machine's history of the store
  begins: a store copied here without its key, signed elsewhere. Their
  records keep their stored labels, since their pointers cannot be
  checked here, and stamps copied with the store read as this host's.
  `log verify` reports them `unverifiable`.
- A *foreign* segment is not held and comes after this machine's
  history began: rows put into history this machine signed by a writer
  without its key, who wrote a `rekey` row naming a key of their own.
  Its records read `unaccounted` on every read surface and in the health
  census, save a row this connection's own current key verifies (one a
  daemon appended while the forged row went in), and `log verify`
  reports `foreign_segment` with the segment's seq range: `tampered`.
  The owner's opener keeps a key that signs a segment of the log current
  whatever `meta.key_fingerprint` says, sets meta back, closes a foreign
  tail with a genuine `rekey` row naming the true previous key, and logs
  a WARNING on the `bettermemory.store` logger naming the segment.
- `[security] strict_provenance = true` reads the records of every
  segment that is not held `unaccounted`, leading ones included: only
  what this machine's own keys verify is trusted. The store reads the
  switch lazily, at most once per connection and only when a row in such
  a segment is first labelled, so a store under one key never reads the
  config; a config that cannot be read reads as true, with a warning. A
  running daemon picks a change up on its next start.

**The origin anchor** (9.0.0rc12). Where this machine's history begins
is not taken from the log, whose every row an editor of the file can
rewrite: the key ring keeps `<store_id>.origin` beside the key and the
head, `{"seq": S, "mac": M, "key_fingerprint": F}`, the seq and MAC
column of the first row this machine's key ring signed for the store
and, since 9.0.0rc13, the fingerprint of the key that signed it, written
`0600` through a temporary file and a rename under the sidecar lock
`<store_id>.origin.lock` (`log.KeyRing.write_origin`).

- It is written by `Store.create` (seq 1), by the owner's opener when it
  takes over a store copied here (its `rekey` row), and, on a store with
  no anchor, at the first writing open: the first row of the earliest
  segment this machine holds, current or retired, and only when that row
  verifies under its segment's key. That last case is trust on first
  use: an edit made before that open is not caught by the anchor. With
  no held segment nothing is written.
- No read-only opener writes it (`log verify`, `log export`, `export`,
  `stats`, `memory history`, `doctor`, `backup create` and `verify`,
  `security`), and nothing moves it once written, a lost key included:
  only `backup restore` replaces it, on the record
  (`KeyRing.replace_origin`). An anchor written before rc13 names no
  key; the first writing open that sees a held key verify its row adds
  that key's name, and changes neither the seq nor the MAC
  (`KeyRing.name_origin_key`).
- A file that does not parse (a seq that is not an integer from 1 to
  2^63 - 1, a MAC that is not 64 lowercase hex characters) reads as no
  anchor, and the next writing open replaces it. A file that is there
  and cannot be read is never written over: the open logs a warning and
  goes on. A `key_fingerprint` of any other shape reads as no name.

The reads check the anchor with the segment map, at each connection's
first pointer check (`log.chain_trust`). The row at S must exist, carry
M and verify under a held key:

- `ok`: a segment that is not held is leading only when it ends before
  S (the next segment starts at or before S); any other is foreign.
- `mismatch`, reason `missing`, `mac` or `signature`: the history where
  this machine's begins was rewritten, so which segments lead cannot be
  read off the log. `signature` means the key the anchor names is held
  and does not verify the row (for an anchor that names no key, no held
  key does): a row rewritten under its own MAC column. `log verify`
  reports `origin_mismatch` (`tampered`), and every segment that is not
  held reads as foreign, leading ones included; a held segment keeps its
  labels.
- `unverifiable`: the ring holds no key at all to check the row with,
  or the key the anchor names is no longer held (its key file was lost)
  and no held key verifies the row, so nothing here can tell a rewrite
  from the lost key. The status reads `unverifiable`, never `tampered`,
  and the segments split as they do without an anchor; filing the key
  back makes the anchor hold again.
- `absent`: no anchor (a store from before rc12, or one only read here
  so far), which is no finding; the rules above apply without it.

Rows below seq 1 are never trusted: the segment map and the opener read
only seq 1 and up, the rows `log verify` walks, and a record whose log
row sits below seq 1 reads `unaccounted`. No store writes such a row.

**Moves.** A store moved from this host (A) to another (B) and back, or
opened once under another `BETTERMEMORY_KEYS_DIR`, carries B's segment
after A's anchored history: foreign, so B's rows read `unaccounted` and
`log verify` exits 1. Filing B's key in A's keys directory as a retired
key restores `ok`. A store file copied over this host's copy again from
the machine it came from, with the same store id, changes the history at
the anchor and reads `origin_mismatch`: `bettermemory backup restore` of
a bundle, or deleting `<store_id>.origin` and `<store_id>.head`, accepts
the replaced history and trusts the file again on purpose. A copied
store's own history comes before its takeover row and keeps its labels.
A lost key file leaves the rows it signed leading and the origin
`unverifiable`, so `log verify` reads `unverifiable`; the anchor stays
on its row, and filing the old key back as a retired key reads `ok`
again.

**The key ring's binding** (9.0.0rc13). The key ring is found by the
store id in `meta`, which any file can carry, so it is also bound to one
store file: `<store_id>.path`, `{"path": "<resolved store file>"}`,
written `0600` and atomically under `<store_id>.path.lock`
(`log.KeyRing.bind_store`). `Store.create` writes it; the owner's opener
(`allow_rekey=True`) claims it before any rekey decision, writing it
when the ring has none (trust on first use, for a ring from before
bindings) or when the bound file no longer holds this store id; and
`backup restore` rebinds it to the file it restores
(`KeyRing.rebind_store`), on the record. A reader never writes it.

- The bound file's id is read without opening it as a store
  (`store._store_id_at`): `immutable=1`, or `mode=ro` first when its
  `-wal` and `-shm` are present, so a store a running daemon just
  created, whose tables are still only in its WAL, is seen. No file
  there, a file that is no store (SQLite answers `SQLITE_ERROR`, such as
  no `meta` table, `SQLITE_CORRUPT` or `SQLITE_NOTADB`), one with
  another id, or a symbolic link loop is no store there, and the ring is
  rebound to the file being opened. Any other failure (a permission, a directory that cannot
  be searched, an I/O error, another SQLite error) cannot tell, and
  keeps the binding.
- When the binding names another file that still holds the id, or
  cannot be read to tell, and that is not this file under another
  spelling (`os.path.samefile`), the owner's open retires no key,
  writes no key, sets no meta, appends nothing, moves no head and
  writes no anchor. It opens as a reader with writes refused, even when
  its key matches, and so do its worker connections (`reopen`); a
  WARNING on the `bettermemory.store` logger names both files and the
  remedy (move or remove one store, or delete `<store_id>.path`). A
  reader whose key matches asks the same (`KeyRing.bound_elsewhere`),
  since a reader with the key can write (the session-start hook records
  the brief it shows), and opens the same way.
- A binding file that cannot be read raises, and the owner's open logs
  a warning and goes on as before rings were bound, the file kept. One
  that does not parse, or holds a relative path, is no binding and is
  replaced.
- Cost: every open after the first reads one small file and takes no
  lock.

**A key that cannot be read** (9.0.0rc13). `KeyRing.load_current`
answers None only when the key file is not there. A file that is there
and cannot be read, or a key file or keys directory that is a symbolic
link to nothing (a volume not mounted yet), raises `KeyUnreadableError`
(`.path`, `.reason`). The owner's open fails with it rather than write a
new key and a `rekey` row over a failure that may pass; a reader opens
without the key and logs why; `held_keys` skips the file; `doctor`
fails its key check, `security audit` reports it high, and `backup
restore` refuses before it changes anything.

Cost: one indexed log lookup and one HMAC per row served, nothing per
candidate; the segment map and the anchor add one file read, one row
lookup and one HMAC at each connection's first pointer check. What it
cannot see: an injection-driven legitimate write, which is `local` by
every test here and truthfully so; an edit made before a store's anchor
existed; and a store whose `meta.store_id` an editor replaced with a
fresh id, which detaches it from this machine's key ring so that it
opens as a store copied here, its earlier rows leading. `strict_provenance`
reads the last two `unaccounted`. Cause provenance (what was in context
at write time) is a different question, and an open one.

## Membership

Which project a memory belongs to, and so which sessions it is delivered
to, is decided by the checkout it was written in (`membership.py`). A
**checkout** is one git directory on one machine, recognised by its
filesystem object, or a plain folder with a project file (a folder
checkout). A **project** is the set of checkouts one body of evidence ties
together. A **machine** is one OS user on one host, VM or container. Every
memory and episode has one **home**: the checkout it was written in and
that checkout's project, or `global` for a write outside any repository
and any project file. The home row is written in the record's own
transaction and never rewritten; a later decision (a merge, a
declaration) moves a project, not a record.

### The witnesses

The caller's location is read from files in the caller's own namespace
(`location.capture`): the top level, the git and common directories and
their objects, HEAD and up to 64 ref tips, every remote URL as a remote
key, the shallow marker, the token, the init stamps (`description` and
`info/exclude`), the project files from the cwd up, and the content
manifest of a commit-less checkout or folder project. Commits and blobs
are read from loose objects, pack indexes and packs (`gitobjects.py`),
with `git cat-file --batch-check` only where that reader reports itself
incomplete. Nothing reads repository configuration, so git's
`safe.directory` refusal never applies. Each witness is one observable
fact:

| Code | The witness | Read from | Alone |
| --- | --- | --- | --- |
| O | the same git-directory object (device, inode, birth time), or a folder checkout's folder | `stat` of the git directory; without birth time, confirmed by the token, else the last HEAD held, else the init stamps by equality | the same checkout: silent |
| S | the same object store | the common directory's object (a linked worktree) | another checkout of the project: silent |
| H | holds witness commits of the project | the project's bounded witness set, asked of the history reader in one batch | one witness: asks once |
| Hf | holds commits with a witness's fingerprint (sha256 of the author and committer lines and the message) | the repository's commits, where no witness commit is held | with H and Hb, one history witness |
| Hb | holds the file contents of witness commits: at least 2 blobs and half of one witness's sample | up to 8 blobs per witness | counts only beside H, Hf or R; never with a project file alone |
| R | names a remote key of the project | every remote URL in the config file, not only `origin`; a `file://` or path remote names the views seen there | one witness: asks once |
| D | names a remote declared for the project in a shallow clone | `declared_remotes`, written by a `join` answering a remote-only question in a shallow clone | counts for a shallow caller |
| F | carries a project file bound to the project | `bettermemory.toml` at the repository's root | one witness: asks once; with R, two only where history cannot speak (unborn, shallow) |
| T | carries the checkout token of one of the project's checkouts | `info/bettermemory` in the git directory | two only when caller and project are both commit-less and no live checkout carries it; dropped when history disagrees |
| B | sits at the path of a primary checkout of the project whose object is live nowhere, holding its commits, its token compatible | the recorded views on this machine | comes with the project's commits; re-keys the recorded checkout |
| C | the same folder name, and relative paths and sizes at Jaccard 0.9 or both empty | the content manifest | never joins a repository: it turns NEW into a question naming at most 3 candidates (more than 3 is NEW, the line giving the count); in a folder project, C with F counts as two |

Two witnesses are independent when they rest on different facts: H, Hf
and Hb count as one history witness. What a record is about (its scopes,
attested paths, cited commits), the harness's project key, a path alone
and the root commit are no witness.

### The ladder

The first rung that answers decides (`Membership.resolve`):

0. Nothing readable (the cwd unreadable to the session, a dropped mount,
   an rc3 client posting a path the daemon cannot see): nothing from any
   project, line L8 and a `resolution_indeterminate` row. An evicted HEAD
   or config at first sight whose fetch outruns the capture's budget: the
   same for that prompt, line L9 and a `resolution_deferred` row; the next
   prompt decides. Outside any repository and any project file: global.
1. A declaration: an answered question, `project init`, `memory move`.
2. O: the same checkout. A new path adds a view (`checkout_moved` or
   `checkout_view_added`).
3. S: another checkout of the same project (a linked worktree).
4. Two independent witnesses agreeing, nothing contradicting: the
   project (`resolution_joined` with the witnesses). With B the recorded
   checkout is re-keyed (`checkout_rekeyed`); after `git init` in a folder
   project, O of the folder with F re-keys the folder checkout to the git
   directory. Where several projects qualify: the stronger evidence, then
   the most specific line, else a question. With `strict = true` a join
   resting on history without F or B asks.
5. A contradiction, one witness, or content alone: ASK ONCE.
6. No witness: NEW, line L2.

Every sighting adds its HEAD as a witness, its remotes and its root file.
A provisional project re-tallies when its evidence signature (HEAD,
remotes, file, token, shallow) changes and joins by itself when two
witnesses now agree; the question is answered by evidence, with a
`proposal_answered` row naming it.

### Contradictions and questions

- `token_shared_no_commit`: the caller carries the token of a live
  checkout with which it shares no commit (template siblings, a planted
  token). A question.
- `file_names_other_history`: the root's project file names project P
  while the repository holds another project's commits and none of P's. A
  question.
- `token_superseded`: both sides have commits and share none; history
  outranks the token, the token leaves the tally, a row says so, and the
  checkout gets a token of its own at its next sighting. Not a question.
- Across a tenant boundary a witness is dropped and the line says "kept
  apart".

A question (a proposal) is opened once per checkout and question, with
its kind (`first_sight`, `file_contradiction`, `remote_diverged`,
`subproject_contradiction`, `legacy`, `import`), its candidates, its
evidence, its reason (the contradiction, or what fell short:
`one_witness`, `content_only`, `several_projects`, `a3_history_join`;
the migration's `legacy_no_live_checkout`; `unresolved_location`, below)
and its exact line. Until it is
answered the checkout's project is provisional, nothing of another
project is delivered there, and the line leads every session's first
output there. `bettermemory project join <id>` and `project new` answer
it; so does evidence arriving. The lines (`line_*` in `membership.py`):
L1 a checkout that may be a known project, L2 a new project, L3 a project
file naming another project, L4 joined, L5 a remote that diverged, L6 the
records of a gone folder that may belong here, L7 a sub-project file
naming a project outside its repository, L8 unreadable, L9 deferred, L10
an imported project that may be one here.

### The surfaces

`relation(home, caller)` answers `same_project` (the home's project is on
the caller's chain: its project, the projects it is a part of, the
workspaces around it), `sub_project` (the home is a part of the caller's
project), `other_tenant` (the two sit on different sides of a boundary),
`global_named` (a record homed outside any project whose `projects:<name>`
scopes name a project on the caller's chain, the names compared
case-folded), `global`, `other_project`, or `unknown` (nothing could be
read where the caller is, or it was deferred). `admitted(surface, home, caller)` applies
one fixed table (`IN_PROJECT_POLICY` and `OUTSIDE_POLICY`), and nothing
else decides delivery (`admission.py` caches its answers for a request):

| Surface | Caller in a project | Caller outside any project |
| --- | --- | --- |
| `session_brief` | `same_project`, `global_named` | nothing |
| `prompt_recall` | `same_project`, `global_named` | nothing |
| `review_strata` (the write review's related statements) | `same_project`, `global_named` | nothing |
| `search_auto_scope` (`memory_search` with `auto_scope` on) | `same_project`, `sub_project`, `global`, `global_named` | `global` |
| `pool_prefilter` (the hooks' candidate pool) | `same_project`, `sub_project`, `global`, `global_named` | `global` |
| `links` | `same_project`, `sub_project`, `global`, `global_named` | `global` |
| `search_unscoped` (`memory_search` with `auto_scope` off) | `same_project`, `sub_project`, `global`, `other_project` | `global`, `other_project` |

`unknown` and `other_tenant` are admitted nowhere. A record with no home
row is admitted nowhere. A record written where nothing could be read
(below) is admitted only to a caller resolved to its provisional project,
which no caller is until a merge or a declaration settles it.

Episodes, the handoff and recall segments key on the checkout. A primary
checkout with no episode of its own is offered the newest session of a
primary checkout of its project that is not live here, labelled; nothing
takes a live sibling's run-state, and another machine's checkout is never
live from here. Claims resolve at the home checkout's live view and
translate across its recorded views.

### The checkout token

`info/bettermemory` inside the git directory (`.git/info/bettermemory`;
in a linked worktree `.git/worktrees/<name>/info/bettermemory`; under a
separate git directory or `GIT_DIR`, there), two lines:

```text
bettermemory checkout token v1
0XNK4RY1ZCMBN31P1J2EN2STQP
```

The second line is 26 random Crockford base-32 characters (130 bits).
git keeps `info/` local: `git status` never shows the file, `git clone`
never copies it, and git's maintenance leaves it; whatever copies a git
directory copies it. `checkout_token.write` puts a token in place only
where none is: the bytes go to a temporary file in `info/`, flushed, then
hard-linked to the name (CreateHardLinkW on Windows), so of two writers
exactly one succeeds and both read the same token; where hard links are
refused, an exclusive create, and a reader that finds the file empty or
cut short reads it once more. Before it makes its temporary file it
removes `info/bettermemory.tmp-*` files older than an hour, and nothing
else. `remint` replaces the token, for a checkout that leaves its line
(`project new`, or a superseded token).

A read is `present`, `absent`, `malformed` or `unreadable`. A write is
`written`, `exists` (a token was there, never replaced), `malformed` (a
file that is no token, left as it is), `skipped:bare`,
`skipped:foreign_owner` (another OS user owns the git directory),
`skipped:policy` (`write_token = false`), `failed:readonly` (EACCES,
EPERM, EROFS) or `failed:io`. A skipped or failed write is named in the
first-sight line (L1, L2). A checkout's `token_status` also reads
`superseded` when the migration registered it with a token its history
supersedes; its next sighting re-mints, and the written token clears the
status.

### The project file

`bettermemory.toml`, written by `bettermemory project init` and by nothing
else (`project_file.write`), for the user to commit:

```toml
[project]
id = "bmp_0XNK4RY1ZCMBN31P1J2EN2STQP"
name = "app"
boundary = true            # optional
```

The id is `bmp_` and 26 random Crockford base-32 characters and decides;
`name` is for display, and the directory's name when absent. The nearest
file at the cwd or an ancestor names the session's project: at a
repository's root it binds the repository's project and travels with
every clone; below the root it names a sub-project; above the root it is
a workspace around repositories; in a plain folder it makes a folder
project. A file that cannot be used is an error with a reason
(`unreadable`, `evicted`, `not_a_file`, `too_large`, `toml`,
`no_project_table`, `id_missing`, `id_malformed`, `name_malformed`,
`wrong_type`) and a line in the first output. At the root or below, such
a file names no project and the other witnesses decide; above the root it
is treated as a tenant boundary until it is fixed.

`boundary = true` is honoured at every placement: the project the file
binds becomes a tenant, nothing crosses it in either direction, and a
boundary outranks a sub-project's sharing with its parent. A flag added
to a bound file is applied at the next sighting, logged and said in the
first output. A flag removed from the file does not lift the boundary:
the first output says the file no longer declares it, and `bettermemory
project init --rebind` in that folder lifts it by binding a fresh file
without the flag.

### Locations nothing could be read for

A write at a location nothing could be read for, or deferred, is never
refused and never global. It is homed in a provisional project of its
own, keyed on the machine and the cwd path, with a checkout of kind
`unresolved` whose `unresolved_json` records the writing sessions and the
project that replaced it. When the same harness session later resolves
that path to a checkout, the provisional project merges into that
checkout's project (one `project_merged` row; the records keep their
home rows). When another session resolves there, or records remain at a
later sighting, a `legacy` question (reason `unresolved_location`) opens
in that checkout; `join` merges, `new` keeps the records as a project of
their own with no live checkout. Until then no surface delivers them, and
health lists them. A process with no machine id homes its writes the same
way, under the machine `<no machine>`, and a later resolution at that
path on any machine finds them.

### The machine

The machine id lives in the state directory (`machine.py`): `machine-id`
holds `bettermemory machine id v1` and 26 random Crockford characters,
and `machine.json` the facts it was minted with: the hardware or machine
id (IOPlatformUUID on macOS through `gethostuuid`, or `ioreg` once per
process where that call is missing; `/etc/machine-id` on Linux;
MachineGuid on Windows), the hostname, and the id
file's own object (device, inode, birth time). Every open compares them.
Where both sides have a hardware id it alone decides; without one, a
different hostname and a different id-file object together mean another
machine. Another machine (a restored laptop, a cloned VM) mints a new id
naming its predecessor, and a `machine_cloned` row precedes any
resolution; a fact that changed on the same machine (a renamed host) is
written back with a `machine_facts_updated` row. Checkouts never re-key
across machines. The store carries its own id under the meta key
`membership_store_id`; at import a machine is keyed by (machine id, store
id), and an import bringing a record that carries this store's id, which
this store never wrote, means the store was copied whole and both copies
wrote: the store takes a new id with a `store_forked` row.

### The tables

Added beside the record tables, folded by the same replay, schema version
unchanged; an earlier build opening the store reads its own tables as
before. Every change is a mutation row of its own kind (`<table>_put`,
`<table>_delete`), MAC'd and chained, so `bettermemory log verify` covers
membership: a planted or edited home, binding or answer reads as
unaccounted.

| Table | Rows |
| --- | --- |
| `machines` | id, facts, predecessor |
| `projects` | id, name, merged_into, provisional reason, parent, tenant, folder, boundary |
| `project_witnesses` | project, commit, arrival order, source, fingerprint; at most 64 per project, the 16 oldest kept |
| `project_blobs` | project, blob, witness; at most 8 per witness |
| `project_remotes` | project, remote key |
| `project_files` | file id, project, evidence |
| `declared_remotes` | remote key, project, principal |
| `checkouts` | id, project, machine, kind (`primary`, `linked`, `bare`, `folder`, `unresolved`), object key, common object key, git directory, token, token status, init stamps, last HEAD, branch, remotes, content manifest, legacy (registered by the migration), retired, `unresolved_json` |
| `checkout_views` | checkout, machine, path, first and last sight |
| `homes` | record id, kind, project, checkout, rule, decided at |
| `proposals` | id, checkout, kind, candidates, evidence, reason, line, state, answer, answered by, signature |
| `standing` | record id, probability, body sha256, source, answered by, answered at: a record's answer to the standing question for its current body (`standing.py`; `standing_put` rows, which a build before rc7 skips) |

`checkouts.unresolved_json` is the one column added to an existing
membership table, by `ALTER TABLE` at open where it is missing. A checkout
unseen for 180 days is retired from health; no rule reads the flag. Homes
are indexed by project, so a session's selection is one indexed join and
a merge is one project row.

### The rows

Each is a control row of its own, written whatever the telemetry setting
(`MEMBERSHIP_EVENT_KINDS` in `store.py`):

- `resolution_indeterminate`: nothing could be read at the caller's
  location (L8).
- `resolution_deferred`: an evicted file is being fetched (L9).
- `resolution_session`: a write at a path the daemon cannot read (a
  container client's), homed where the same harness session's latest
  hook resolution stands: that checkout has a view at the path or above
  it (B24). The row names the checkout, the project, the session and the
  path; no provisional project is minted.
- `resolution_joined`: a checkout joined a project, with the witnesses.
- `resolution_new`: a project minted for a checkout no witness named.
- `proposal_opened`: a question opened in a checkout, with its line.
- `proposal_answered`: a question answered, with the principal of the
  declaration or the evidence.
- `checkout_registered`: a checkout seen for the first time.
- `checkout_moved`: a known checkout seen at a new path, the old one gone.
- `checkout_view_added`: a known checkout seen at another path.
- `checkout_rekeyed`: a recorded checkout given a new object (B, or `git
  init` in a folder project).
- `checkout_object_refreshed`: a recorded checkout kept at its device and
  inode number with another birth time (a restore that got the number
  back, decision B20): the stored key takes the birth time read now, so
  the view is live by it afterwards.
- `checkout_retired`: a checkout unseen for 180 days, out of health.
- `token_superseded`: a token history disagrees with, left out of the
  tally.
- `token_reminted`: a checkout's token replaced, with the write's status.
- `remote_declared`: a remote declared a project's for shallow clones.
- `project_minted`: a project created, with its reason.
- `project_merged`: a project folded into another, with the evidence.
- `project_absorbed`: a project merged into the caller's checkout's by the agent's `join_project`, naming the checkout and the user's words; `project_unmerged`: a merge undone by `unjoin_project`.
- `project_file_bound`: a project file id bound to a project.
- `project_reparented`: a part carried to another project by the
  declaration that moved its checkout.
- `machine_cloned`: this state directory found on another machine.
- `machine_facts_updated`: a fact of this machine changed.
- `witness_put`: a commit recorded as a project's witness.
- `witness_released`: a witness dropped at the bound of 64.
- `import_homed`: a record homed by an imported document.
- `recall_suppressed`: a brief or recall that delivered nothing because
  membership held its candidates back, with them and why.
- `membership_migrated`: the migration's run, with its counts and its
  decision per recorded root.
- `store_forked`: the store took a new store id at an import.

`bettermemory project why <id>` prints a record's project, checkout and
rule and every row behind them. Health reports the open questions with
their lines, the provisional and unresolved projects, suppressed recall
turns per checkout, indeterminate and deferred captures, and the
checkouts on this machine with no live view.

### The migration

`membership_migrate.migrate` builds the tables from the store's own
records: every memory, removed memory and episode without a home gets
one, through the store's home rows, and no record row is rewritten. Each
distinct recorded root (`origin.worktree_root`) is probed once, read
only: a capture with the token write off.

- A live root (a repository's top level, or a folder with a project
  file) is resolved by the ladder, which registers its checkout; the
  commits its records recorded that it holds become witnesses. Rule
  `live:<the ladder's rule>`.
- A root that is not live now but is a recorded view of a known checkout
  (an earlier run or a session saw it there) keeps that checkout: `view`.
  A root an earlier run decided otherwise keeps that run's decision.
- A root that is gone, replaced or unreachable takes the first rule that
  answers: `rehome`, a prior view of a live checkout when the log shows
  its records re-homed there; `lineage`, a checkout of the one live
  project holding its records' verified heads, remotes compatible;
  `lineage_cited+same_name`, of the one holding their cited commits under
  the same folder name; `remote`, of the one carrying its remote when no
  commit was recorded; else `none`, a project of its own with no live
  checkout, or `ambiguous` when several live projects hold its verified
  heads. The verified heads and remotes no live project holds become that
  project's evidence.
- A `legacy` question (reason `legacy_no_live_checkout`, line L6) opens
  once for each such project whose recorded commits a live checkout
  partly holds, in the live checkout holding the most of them.
- A recorded commit is evidence whether it is a full id or an
  abbreviated one (a memory's `verified_commits` carry the short ids
  models write, B19): the repository resolves an abbreviation to the one
  object with that prefix, read from its loose objects and pack indexes,
  or by `git cat-file --batch-check` where that reader is incomplete. An
  ambiguous prefix, or one the repository does not hold, is not held.
  Witness rows carry the full id only.
- A record with no root: no origin is `global:no_origin`. A remote key
  joins the one project carrying it, live or not: the one live carrier
  (`remote_no_root`), else the one carrier with no live checkout, decided
  in this run or an earlier one (`remote_no_live_checkout`); with several
  carriers nothing is minted and the record goes to the first minted
  (`remote_ambiguous`), the report naming them all. Only a remote no
  project carries mints a project, named after the repository and
  carrying the key (`remote_no_live_checkout`), so no key is carried by
  two projects the migration minted. Otherwise a cwd inside a recorded
  root is that root's checkout (`cwd_in_root`); a cwd in a live
  repository or folder project takes its resolution (`cwd:<rule>`); else
  `global:outside_repository`.

Every rule is stored with the prefix `migration:`. The run is one store
transaction ending in one `membership_migrated` row; `--dry-run` is the
same transaction rolled back, with the same report. It writes no token
and no file into any repository: a checkout gets its token at its next
sighting, in its caller's namespace, and a checkout found carrying a
token its history supersedes is listed as due one. It is idempotent: a
store with every record homed is not written, and a later run (records
an earlier build wrote meanwhile) homes only those, keeping the decision
an earlier run made for a root that is not live now and resolving a live
root again; a later run that adds records to a project whose question is
open restates the question's counts.

It runs at the first open by an upgraded build (`ensure_migrated`, when a
record has no home): the daemon at its start, under the store's lock and
before it binds its port, logging one line with the totals; `bettermemory
serve`, logged; every CLI command through `cli_context`, with one line on
stderr; the in-process hook commands, logged at info and never on stderr.
`bettermemory membership import` homes the records its document names
first and runs it after. A store opened without its key is left as it
is, and a process with no machine id logs one line and opens the store as
it is; the next open migrates. `bettermemory membership migrate
[--dry-run]` opens the store itself and prints the report.

### The switches

`[membership]` in `config.toml`:

- `write_token = true`. False writes the token nowhere (`skipped:policy`);
  every rung works without it, and a moved or copied commit-less checkout,
  which its token would have recognised, is asked about.
- `strict = false`. True asks once where a checkout would join a project
  because it holds the project's commits without its project file or B;
  joins by the project file, a declaration, the same git directory or
  object store, or the project's old path stay silent.

Declarations are the agent's (design 8.8; the owner's ruling of
2026-10-21: nothing in the product waits on a human at a terminal). A
question's line names the `memory_admin` call that answers it:
`answer_question` with `separate` on the agent's own judgement (it moves
nothing), or with `join` on the user's words, quoted in `said`, which
the daemon checks against the prompts its hook saw in that checkout
(`recall.WORDS`, the last twenty per checkout, in memory only); where
the hook saw no prompt for the checkout at all, the quote is logged
unchecked and the row says so. `join_project` merges a project the
user names into the caller's checkout's project; `unjoin_project` undoes
a merge. The CLI declarations (`project join`, `project new`, `project
init`, `memory move`) run from any stdin, an agent's shell included,
and log the OS user. A migration's legacy question is opened only when
its evidence decides something (`membership.legacy_asks`: the records'
commits held by more than one compatible live project, or by citation
under the same name); held by citation only under another name, or
through a remote the holder does not carry, the records stay a project
of their own with no question, and a store a candidate before 9.0.0rc6
opened such a question in closes it at its next open
(`membership_migrate.settle_at_open`).

## Module map

The Python files under `src/bettermemory/`, its `handlers/` and
`cli/`. A tool call crosses them in one order, and the map is that
order.

**Entry.** `cli/` is the `bettermemory` command, entered through
`_entry.py`, which imports nothing it does not need so a hook process
stays small. With no arguments it runs the stdio shim (`shim.py`): a
real stdio MCP server whose `tools/list` and `tools/call` forward to
the local daemon (`daemon.py`, one process per store on 127.0.0.1 that
owns the store and serves the nine tools over streamable HTTP and the
hook bodies under `/api/v1/hook/`), started on first use through the
client in `_daemon_client.py`, which the hook command shares and which
proves the daemon it reaches by the token before it sends anything,
and which starts none while another process holds the store's lock,
waiting for that one instead (`start_and_wait`);
`cli/daemon_cmd.py` is `up`, `down` and `status`, and
`cli/service_cmd.py` with `service_units.py` writes and loads the
launchd or systemd unit that supervises the daemon. `cli/serve.py` is
the in-process server the shim falls back to when no daemon can be
reached or started; both hand off to `builder.build_server`, which
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

**Write path.** `handlers/write.py` first lets `policy.py` refuse a
call that passes an escape hatch `[policy]` forbids, then checks
declared claims against the worktree (`claims.py`) and any declared
`supersedes` list. It then runs the gate chain: `credentials.py`
(secret-shaped strings; its `secret_spans` is what the log's query
redaction shares), the capture gates on a capture write (the rules in
`durability.py`, and a body an undo reverted before), `durability.py`
(structural durability), the user-claim check, `scope_match.py`
(scope mismatch against the caller's repo), and duplicate and
tombstone dedup. `review.py` then puts the write review's questions
to the caller. `supersession.py` decides whether the write replaces a
stored statement; the review runs it to build its related set, and
the commit runs it when the review did not. A cue-less divergence is
filed in the conflicts queue (`conflicts.py`). A write that passes lands
in `store.py` as one signed log row and one table row in one
transaction, with `log.py` signing the row and keeping the chain.
`origin.py` stamps where the write happened.

**Read path.** `handlers/search.py` and `handlers/show.py` read the
store's candidate query (the FTS5 prefilter above the index threshold)
into `search.py`, the ranker, with `expansion.py` kept for the retrieval
bench's expansion arm. Ranking is drift-independent; `verify.py` then
annotates each hit with the staleness verdict from path, commit and
calendar drift, with `symbols.py` for advisory symbol citations, and
the store's `trust_rows` supplies the provenance label and this host's
stamp. The warm daemon's caches sit on this path and keep every answer
bit-identical: `origin.py` keeps the captured origin per directory for
ten minutes under a signature of the repository's HEAD bytes, config
and environment, validated on every lookup; `_response.py` and
`origin.py` memoise the commit-drift work per repository root and
commit HEAD names, which `githead.py`
reads from the repository files without a git process; `search.py`
memoises the token streams per body and scopes. Every cache registers
its clearer with `_caches.py`, and `_caches.clear_all` empties them.

**Telemetry.** `events.py` is the recorder every handler records
through; each event is a row of the store's log. `audit.py` and
`attribution.py` turn the rows into silent-miss and use telemetry;
`eval.py` computes the effectiveness rates; `health.py` is the rollup
`handlers/health.py` and the CLI serve.

**Hooks.** `bettermemory hook session-start|stop|prompt` runs the
standard-library client in `_daemon_client.py`, which posts the hook's
payload to the daemon, and `daemon.py` serves the three bodies at
`/api/v1/hook/`. `hook.py` is the Stop body: the turn audit and
settlement. The audit reads the turn the payload's `prompt_id` names,
and no other (`transcript.py`: the user rows carrying that `promptId`,
read from the end of the file one 1 MiB chunk at a time, 64 MiB at
most; the newest user row's turn when the payload names none). Its
prompt is the owner's text of the turn's newest row the definition
accepts; a turn with none records `turn_audited` with verdict
`no_signal` and reason `no_owner_prompt` and probes and settles
nothing, and a turn the walk cannot decide within the budget records
nothing. The reply is the payload's `last_assistant_message`, else the
turn's newest assistant text after its prompt, else none; the row
carries `prompt_id`, `rule` and `reply_source` (`payload`, `tail`, or
`interrupted` for a turn the owner interrupted, which the next Stop
audits from the previous audit's row on).
`recall.py` is the recall surface, the session brief and
the prompt recall with their per-session cooldown; `standing.py` says
what is standing and `in_force.py` what is in force at delivery;
`owner_prompt.py` is the one definition of an owner prompt; `verdict.py`
is the memoised verdict `memory_show` reads. The bodies run in the
daemon but outside the MCP request path, so they read the store rather
than a session's state. They join the conversation's tool rows by the
harness session stamp, the worktree bridge when nothing carries it.
Claude Code names the conversation anew on /clear and /resume while
the server keeps the id it was spawned with, so a server stamped with
another id is another window's only when that id's hooks run beside this
conversation (a hook row at or after its first); otherwise the bridge
may take it as this window's own (`hook.runs_beside`, which the eval's
surfaces section reads a stamp by too). Its costs: a /resume joins from
its second hook call; and when another window in the same checkout is in
a turn that began before a /clear, the two servers look alike until that
window's next hook row, so the bridge sets both aside once both have
written since the clear (a missed join) and takes the other window's
while it alone has (a foreign join).
`recall_counters.py` and `recall_review.py` are the surface's
measurement: the eval's surfaces section and `bettermemory recall
review`.

**The write review and session capture.** `review.py` is the write
review: the questions, the related set and the pure decision, with
`handlers/write.py` as the daemon's side of it (the review response,
the `review_issued` row, the refusals and the commit) and
`review_samples.py` and `review_counters.py` as its grading surface
(`cli/review_cmd.py`). `capture.py` is session capture's bar, its
instruction and its batches on the log; `capture_hook.py` is the Stop
endpoint's capture half, when a turn end asks the chat model to save
the stretch since its last capture and how the batch settles;
`handlers/capture_relations.py` is a batch's closing call, the
relations answered at once; `capture_review.py` is `bettermemory
capture` (`cli/capture_cmd.py`): review, undo, sample and grade.

**Membership.** `membership.py` is the model: the ladder, the
questions and their lines, `relation` and the surface table, run-state
and anchoring, `why` and health. `location.py` captures where a caller
stands, from files, and resolves it under a per-location lock;
`gitobjects.py` reads commits and blobs from the repository's object
files; `checkout_token.py` and `project_file.py` read and write the two
files; `machine.py` is the machine id and the store id; `admission.py`
holds one request's admissions; `membership_migrate.py` is the
migration. `cli/project_cmd.py`, `cli/memory_move_cmd.py` and
`cli/membership_cmd.py` are the commands.

**Offline.** `migrate_v8.py` reads an 8.x directory through the frozen
readers in `v8.py` and writes the store; `mirror.py` writes the store
back out as that directory, with `_frontmatter.py` as the markdown
codec; `rollback.py` removes one actor's writes and leaves everyone
else's; `init.py` and `_install_hints.py` are client onboarding.

**Operations.** The commands an operator runs, every one of them
read-only except a restore. `backup.py` is `bettermemory backup`
(`cli/backup_cmd.py`): the bundle, its signed manifest, its verification
on a private copy and the restore. `audit_export.py` and `anchor.py` are
`log export` and `log anchor` and the anchors `log verify --anchor`
checks (`cli/log_cmd.py`). `history.py` is `memory history`
(`cli/memory_move_cmd.py`), and `cli/stats_cmd.py` the usage report.
`doctor.py` is `bettermemory doctor` (`cli/doctor_cmd.py`). `posture.py`,
`sandbox_settings.py` and `secret_scan.py` are `security audit`,
`security sandbox` and `security scan` (`cli/security_cmd.py`).
`cli/config_cmd.py` is `config path`, `show` and `check`, and
`cli/completion_cmd.py` prints the shell completion it builds from the
parser.

**Leaves.** `models.py` (Pydantic models, enums, ULIDs), `config.py`
(config loading and the store-directory rule), `managed.py` (the
organisation-managed config's fixed location, a file or on Windows a
registry value, and its reader), `prompts.py` (the
system-prompt addendum), `_fsutil.py` (atomic writes and owner-only
directories), `githead.py` (HEAD and the ref it names, read from the
repository files) and `_caches.py` (the cache registry). Nothing in this group imports a handler or the store, and
a new cross-cutting concept belongs here, not in a cycle.

## Storage

One SQLite file per store, `memory.sqlite` in the store directory
(`store.py`). The tables hold the records: `memories`,
`memory_links` (each memory's links, folded with it), `tombstones`,
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
holds the key's fingerprint, a head checkpoint beside the key
(`<store_id>.head`) records the last row written, the origin anchor
(`<store_id>.origin`) the first row this machine signed, and the binding
(`<store_id>.path`) the one store file the key ring belongs to
(Provenance, above). Every record row carries the MAC of the
log row that produced it, which is what the provenance label reads. The
membership tables (the section above), the `standing` table among them,
are folded by the same log.

The store resolves to `$BETTERMEMORY_DIR` if set, else
`[storage] directory` in `config.toml` if set, else `./.claude-memory/`
if it exists (unless `[storage] cwd_store = false`), else
`~/.claude-memory/`. `bettermemory config path` prints the directory and
the rule that chose it. An 8.x directory is read by
`bettermemory migrate v8` and written by `bettermemory export --mirror`;
the running server never reads or writes markdown files.

## Tools

Nine MCP tools, all registered on every start: `memory_search`,
`memory_show`, `memory_write`, `memory_update`, `memory_remove`,
`memory_verify`, `memory_record_use`, `episode` (write, handoff) and
`memory_admin` (restore, tombstones, health, rename_scope, conflicts,
acknowledge_miss, disable_scope, enable_scope, relations, a session
capture's closing call, answer_question, join_project and
unjoin_project, the membership declarations, and answer_standing, the
standing question for the records that wait for it). Signatures,
defaults and return shapes: [api.md](api.md).

The surface is charged on every turn whether a memory tool is called or
not, so its size is measured: `bench/toolcost` serialises `tools/list`
and the `initialize` reply and records every description, every input
schema and the `instructions` block, in bytes and in characters, in a
dated artifact under `bench/toolcost/results/`. Quote the artifact
rather than any paragraph when they disagree. The 2026-07-26 artifact
in that directory predates 9.0, so it measures 3.29.0's default
surface. It is left as it was.

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
bettermemory log verify            # audit the hash chain; --anchor FILE
bettermemory log export            # the log as JSON Lines for a SIEM
bettermemory log anchor            # an anchor to keep off this host
bettermemory backup create         # a verified backup bundle; verify, restore
bettermemory stats                 # usage and growth, read-only
bettermemory memory history ID     # every log row behind one record
bettermemory doctor                # read-only self-check of the install
bettermemory security audit        # file modes, key, sandbox; sandbox, scan
bettermemory config check          # the config's lines; path, show
bettermemory completion bash       # a completion script; zsh, fish
bettermemory eval                  # the three metrics, with CIs
bettermemory review samples        # grade reviewed writes; review grade
bettermemory recall review         # grade what the hooks showed; recall grade
bettermemory capture review        # session capture's batches; undo, sample, grade
bettermemory project list          # projects; show, why, pending, join, new, init
bettermemory memory move ID --project P  # re-home one record
bettermemory membership migrate    # build the membership tables; export, import
bettermemory up                    # the local daemon; down, status
bettermemory service install       # a launchd or systemd unit; uninstall, status, print
bettermemory serve                 # the MCP server over stdio, in this process
```

The commands an operator runs (`log export` and `anchor`, `backup`,
`stats`, `memory history`, `doctor`, `security`, `config`, `status
--check`) are read-only, save `backup restore`; each takes `--json` and
documents its exit codes in its `--help`. [api.md](api.md#cli) lists
their flags, and [operations.md](operations.md) is the runbook.

`bettermemory <command> --help` for flags. `hook session-start|stop|prompt`
is the hook command the plugin wires; it posts the hook's payload to the
local daemon (`bettermemory up`, `down`, `status`), which is also what
`bettermemory` with no arguments serves stdio in front of.

## Configuration

`config.toml` lives under platformdirs (`~/Library/Application
Support/bettermemory/` on macOS, `~/.config/bettermemory/` on Linux).
Most installs never edit it. The first command that loads the config
writes it with every setting commented out (`# key = value` under its
`[section]`, `config.DEFAULT_CONFIG`), so the code's defaults apply,
a later release's changed default included, until a line is
uncommented; a file that exists is never rewritten. `[behavior]` holds
the knobs (`default_max_results`, `recency_boost_half_life_days`,
`search_mode`, `conversational`, `write_supersession`, `prompt_recall`,
`session_brief`, `heavily_used_min_applied`,
`cold_endorsement_ratio_threshold`, `verification_stale_days`,
`tombstone_retention_days`, the bounds `max_content_bytes`,
`max_takeaway_bytes` and `max_scopes_per_write`, and the floor
`min_content_tokens`),
`[scopes]` the allowed list and typo exceptions, `[telemetry]` the one
switch, `enabled` (false switches telemetry off; the audit rows, a
`standing_guard` row and a write or update that used `force` or an
acknowledge flag, are still recorded), `[daemon]` the port (7397 by
default), and
`[capture]` session capture's switch and bounds (`stop_checkpoint`,
`checkpoint_tokens`, `daily_cap`), and `[membership]` the token's
policy switch and the strict switch (`write_token`, `strict`). Since
9.0.0rc12:

- `[storage] cwd_store = true`: false skips the store-directory rule's
  third step, `./.claude-memory/` in the working directory.
- `[policy] forbid_force = false` and `forbid_acknowledge = []`: true
  refuses a `memory_write` with `force=true`; the list names acknowledge
  flags (`credential`, `transient`, `scope_mismatch`, `user_claim`,
  `truncation`) whose `acknowledge_<name>=true` refuses the call. The
  refusal starts `policy_refused:` and says whether the switch is
  managed. These three switches take only a boolean spelling, and the
  list only known names: anything else stops the load.
- `[security] strict_provenance = false`: true reads every row in a log
  segment whose key this machine does not hold `unaccounted`
  (Provenance, above).

A removed key or section
(`full_tool_surface`, `[consolidate]`, `[proposals]` and the rest
`config.py` lists) loads with one logged warning that names it, the
release that removed it and why. It is then ignored. A section written
as anything but a table, a TOML syntax error, or a file that is not
UTF-8 stops the load with a `ValueError` that names the file (`malformed
config in <path>: ...`); a leading byte-order mark is accepted. A
misspelled section or key is ignored without a word, and a switch
written as anything but a TOML boolean or one of the strings `"true"`,
`"false"`, `"yes"`, `"no"`, `"on"`, `"off"`, `"1"` and `"0"` falls back
to its default the same way (an unknown `search_mode` or `prompt_recall`
and a clamped `checkpoint_tokens` log a notice):
`bettermemory config check` reports each, with a did-you-mean, and
`config show` prints every setting in force and where it came from.

**The managed config.** An organisation pins settings in a managed
config with the same sections and keys (`managed.py`): the file
`/Library/Application Support/bettermemory/managed.toml` on macOS and
`/etc/bettermemory/managed.toml` on Linux and every other POSIX system,
and on Windows, since 9.0.0rc13, the `ManagedConfig` value under
`HKEY_LOCAL_MACHINE\SOFTWARE\Policies\bettermemory`
(`managed.MANAGED_REGISTRY`), read through the 64-bit view: a REG_SZ is
the text, a REG_MULTI_SZ its lines joined with newlines, and any other
type, a REG_EXPAND_SZ included, is refused. Nothing under
`%ProgramData%` is read, and no environment variable moves either
source. The registry text goes through the file's own parse and
refusals, a leading byte-order mark dropped and text holding an
unpaired surrogate refused as the file refuses bytes that are not UTF-8;
errors and reports name it `registry
HKLM\SOFTWARE\Policies\bettermemory\ManagedConfig`. It is read after the
user's file, and every key it sets is pinned: the user's value is
ignored, with one logged notice per key whose raw value differs from
the pinned one and is not the code's own default for the key
(`config._restates_default`: compared as written, a boolean restating
only a boolean), and a list replaces the user's whole. Unknown sections and keys in it are ignored, as in config.toml,
but a top-level value outside a section is an error. It fails closed: a
file that exists but cannot be read, does not parse or holds an invalid
value stops the load with an error naming it. `Config.managed_keys`
records the pinned `section.key` names, which `config show` labels
`managed`. A running daemon reads both files at its start only.

## Limitations

- No encryption at rest. Do not store secrets (a write-time check
  refuses secret-shaped tokens); use disk encryption if you need it.
- Tamper evidence, not tamper protection. A writer who holds the key
  can sign anything; a writer who holds the store file but not the key
  is detected, not stopped. `SECURITY.md` draws the line.
- One host per store. The cross-host lane is not in 9.0. A store
  copied to another machine without its key is rekeyed there: its rows
  keep the labels they arrived with unless `[security]
  strict_provenance` is on, and `log verify` reports it `unverifiable`.
  Moving it back, or copying the file over again, reads as tampering
  (Provenance, above).
- The origin anchor is trust on first use for a store made before
  9.0.0rc12: an edit made before its first writing open under rc12 is
  not caught by the anchor.
- The key ring's binding is trust on first use for a ring from before
  9.0.0rc13: the first owner's open after the upgrade binds it to the
  file it opens, which may be a second file that borrowed the store's
  id (Provenance, above).
- A restore is guarded against the daemon and the spawn lock only;
  close every session before `backup restore --yes`.
- One writer at a time. SQLite serialises writers; a second server
  process on the same store waits on the busy timeout rather than
  interleaving.
- Init stamps read within a network client's attribute cache. A cifs
  client reports its own write time for a file it wrote until its
  attribute cache expires (`actimeo`, 1 s by default) and the data is
  flushed; the server's time, which every later read reports, can be
  tens of milliseconds later. A commit-less checkout on such a mount
  that reports no birth time and holds no token, sighted within that
  second of its `git init` and moved afterwards, is asked about rather
  than continued by its init stamps: the stamps recorded are the
  client's. A sighting later than the cache's lifetime records the
  server's times.
