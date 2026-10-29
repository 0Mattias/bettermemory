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
  brief omits what the segment has shown. Claude Code raises a
  subagent's compaction as a SessionStart under the session's id and
  transcript path; one whose payload names the subagent (`agent_id`) is
  the subagent's, kept in a scope of its own (its rows carry `agent_id`,
  its segments open on its own transcript's markers), and the session's
  segment, signal and shows are left as they were. Claude Code names no
  subagent on that SessionStart today, so the hook answers it as the
  session's: the brief goes into the subagent and the session holds the
  signal and the brief's ids. A later read of the signal, a minute or
  more after it, that finds no new marker of the session's within 60
  seconds of it (the one its segments last opened on counts as none),
  finds the session's own conversation gone on past those 60 seconds (a
  row of its thread stamped later) with no recent marker of its own
  newer than the one its segments opened on (a compaction of the
  session's writes its marker before its conversation goes on), and
  finds a marker in a subagent transcript (`<session>/subagents/agent-<id>.jsonl`, or
  `subagents/workflows/<run>/agent-<id>.jsonl` for a workflow's agents)
  takes the signal back for that subagent: the ids that brief printed
  leave the session's segment, and a `segment_opened` row carrying the
  subagent, the ids `withdrawn` and the `brief_event_id` says so. This
  is a timing rule resting on a measured order (see the rc14 known
  limitations). The cooldown is rebuilt from the session's own rows
  whenever they were stamped, so a restart, an eviction after a day idle
  and the CLI path see the segment the daemon held. Each show is
  recorded (`session_brief`, `prompt_recall`), a
  probed prompt that delivered nothing records `recall_held` with its
  cause, and the Stop settles a shown pointer as an applied `use` naming
  its surface when the reply used it, never by the auto fallback; what a
  closing segment never settled is written `unused`, and a turn the
  owner interrupted is audited by the next Stop. These rows are product
  state, recorded with telemetry off too, then with only the fields
  their readers read (since 9.0.0rc14; operations.md lists them), so the
  brief and the recall show with telemetry off; a row that cannot land
  before the hook's budget (its hooks.json timeout less a margin) fails
  the hook, and nothing it would have recorded is shown.
  `[behavior] prompt_recall` takes `project` (default), `everywhere` or
  `off`; `session_brief = false` turns the brief off.
- Claims about the user commit like any fact but carry the
  `user-inference` label, so a stored inference stays distinguishable
  and correctable; a body that reads as one while filed as `fact` is
  refused until it is relabelled.
- Write gates instead of trust: durability check (rejects transient
  state), credential check (rejects secret-shaped tokens),
  control-token check (rejects chat-template control tokens), duplicate
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
  back (`bettermemory key import`) makes the anchor hold again.
- `absent`: no anchor (a store from before rc12, or one only read here
  so far), which is no finding; the rules above apply without it.

Rows below seq 1 are never trusted: the segment map and the opener read
only seq 1 and up, the rows `log verify` walks, and a record whose log
row sits below seq 1 reads `unaccounted`. No store writes such a row.

**Moves.** A store moved from this host (A) to another (B) and back, or
opened once under another `BETTERMEMORY_KEYS_DIR`, carries B's segment
after A's anchored history: foreign, so B's rows read `unaccounted` and
`log verify` exits 1. Filing B's key in A's keys directory as a retired
key (`bettermemory key import` of a `key export` taken on B) restores
`ok`. A store file copied over this host's copy again from the machine
it came from, with the same store id, changes the history at the anchor
and reads `origin_mismatch`: `bettermemory backup restore` of a bundle,
or `bettermemory log accept-history --yes`, accepts the replaced history
on the record and trusts the file again on purpose. A copied
store's own history comes before its takeover row and keeps its labels.
A lost key file leaves the rows it signed leading and the origin
`unverifiable`, so `log verify` reads `unverifiable`; the anchor stays
on its row, and filing the old key back as a retired key (`key
import`) reads `ok` again. The exception is an anchor written before rc13 whose key was
lost before a writing open could name it: it names no key, so the loss
reads as the rewrite (`mismatch`, reason `signature`), `log verify`
reads `tampered` and every segment that is not held reads as foreign.
Filing the old key back (`key import`) makes it hold and the next
writing open names it; without the key, `log accept-history --yes`
accepts the history on purpose, on the record.

**The key ring's binding** (9.0.0rc13). The key ring is found by the
store id in `meta`, which any file can carry, so it is also bound to one
store file: `<store_id>.path`, `{"path": "<resolved store file>"}`,
written `0600` and atomically under `<store_id>.path.lock`
(`log.KeyRing.bind_store`). `Store.create` writes it; the owner's opener
(`allow_rekey=True`) claims it before any rekey decision, writing it
when the ring has none and this file carries the ring's history (below)
or when the bound file no longer holds this store id; and
`backup restore` rebinds it to the file it restores
(`KeyRing.rebind_store`), on the record. A reader never writes it.

- The bound file's id is read without opening it as a store
  (`store_keys._store_id_at`): `immutable=1`, or `mode=ro` first when its
  `-wal` and `-shm` are present, so a store a running daemon just
  created, whose tables are still only in its WAL, is seen. The URI is
  `_fsutil.sqlite_file_uri`'s, as for every open that passes SQLite a
  query parameter (`posture.read_meta`, `backup._read_only_uri`, the
  daemon's store-moved check `daemon._tell_successor`): `Path.as_uri`'s
  URI for a POSIX or a drive-letter path, and for any other Windows path,
  a UNC path first, the whole path after an empty authority
  (`file:////server/share/d/memory.sqlite`), since SQLite refuses an
  authority other than `localhost` and drops `localhost`, which would
  turn `\\localhost\C$\d` into `/C$/d` on the current drive. A
  verbatim `\\?\` or device `\\.\` path keeps its prefix
  (`file:////%3F/C%3A/d`). No file
  there, a file that is no store (SQLite's query answers `SQLITE_ERROR`,
  such as no `meta` table, `SQLITE_CORRUPT` or `SQLITE_NOTADB`), one with
  another id, or a symbolic link loop is no store there, and the ring is
  rebound to the file being opened. Any other failure (a permission, a
  directory that cannot be searched, an I/O error, another SQLite error,
  and any failure of SQLite's open itself, whatever its code, since an
  open reads nothing of the file) cannot tell, and keeps the binding.
- When the binding names another file that still holds the id, or
  cannot be read to tell, and that is not this file under another
  spelling (`os.path.samefile`), the owner's open retires no key,
  writes no key, sets no meta, appends nothing, moves no head and
  writes no anchor. It opens as a reader with writes refused, even when
  its key matches, and so do its worker connections (`reopen`); a
  WARNING on the `bettermemory.store` logger names both files and the
  remedy (move or remove one store, or `bettermemory key adopt --yes`
  against the other, on the record). The
  WARNING comes once per process for each (store file, bound file)
  pair, both resolved (`store_keys._BOUND_ELSEWHERE_WARNED`, tested and
  added under one lock, since worker connections open at once), and
  later opens of the pair log the line at DEBUG; a spelling `resolve`
  does not fold (a case variant on macOS, where `resolve` keeps the case
  it is given; on Windows it reads the case on disk) is a new pair. A
  reader whose key matches asks the same (`KeyRing.bound_elsewhere`),
  since a reader with the key can write (the daemon's workers connect
  through `Store.reopen`, which opens without a rekey, and its hook
  workers record the brief they show), and opens the same way.
- A binding file that cannot be read raises, and the owner's open logs
  a warning and goes on as before rings were bound, the file kept. One
  that does not parse, or holds a relative path, is no binding and is
  replaced.
- A ring with no binding (one from before 9.0.0rc13) is bound only to a
  file that carries its history (since 9.0.0rc14,
  `store_keys._unclaimed_fit`): the row at the origin anchor's seq
  carries the anchor's MAC (and verifies under the anchor's key while
  this host holds it) and the log reaches the head, the rule `key adopt`
  binds by (`store_keys.ring_fit`). Any other file with the id reads
  `unclaimed`, read before the claim: the owner's open binds nothing,
  rekeys nothing and opens as a reader with writes refused, as do its
  worker connections (`KeyState.withheld`), and a reader with the key
  opens without it too; a WARNING names the reason and the remedy (the
  store the ring belongs with, by the anchor's and the head's seqs: open
  it first, or `key adopt --yes` there, which refuses a file that does
  not carry the history; never `log accept-history` on this file, which
  would hand it the ring), once per process for the pair of
  the file and the binding path. A ring with neither an anchor nor a head
  is bound on first use, as before. Without a connection (`security
  audit`) the file is read through a read-only one.
- Cost: every open after the first reads one small file and takes no
  lock; an open of a file whose ring has no binding reads the anchored
  row and the head's row.

**A key that cannot be read** (9.0.0rc13). `KeyRing.load_current`
answers None only when the key file is not there. A file that is there
and cannot be read, or a key file or keys directory that is a symbolic
link to nothing (a volume not mounted yet), raises `KeyUnreadableError`
(`.path`, `.reason`). The owner's open fails with it rather than write a
new key and a `rekey` row over a failure that may pass; a reader opens
without the key and logs why; `held_keys` skips the file; `doctor`
fails its key check, `security audit` reports it high, and `backup
restore` refuses before it changes anything. The CLI's owner openers
(`cli._common.open_owner_store`, which `cli_context`, `serve`, the
shim's in-process fallback and `up --foreground` reach, and
`membership migrate`) end the command in one line through
`cli._common.exit_if_key_unreadable`, `bettermemory: <the error>; make
it readable and run the command again`, exit 1 (2 from `membership
migrate`); `migrate v8` prints `migrate v8: <the error>`, exit 1. A
daemon's line is the last of its log, which `up` and the hooks quote
through `DaemonExited`. The shim opens its fallback's store
(`cli.serve.open_for_serve`) before it warns that it falls back, so that
line, a `SystemExit`, ends it alone; any other failure of that open
logs its warnings first (the daemon's failure, when there was one, and
the fallback). `_daemon_client.daemon_process_env` hands
a daemon it starts the keys directory resolved but for its last
component (`_pin_keys_dir`), so a keys directory that is a symbolic
link reaches the daemon as the link, as a service unit pins it, and a
dangling one fails there rather than naming a directory with no key.

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
to, is decided by the checkout it was written in (`membership.py` and
the family it binds). A **checkout** is one git directory on one
machine, recognised by its filesystem object, or a plain folder with a
project file (a folder checkout). A **project** is the set of checkouts
one body of evidence ties
together. A **machine** is one OS user on one host, VM or container. Every
memory and episode has one **home**: the checkout it was written in and
that checkout's project, or `global` for a write outside any repository
and any project file. The home row is written in the record's own
transaction and never rewritten; a later decision (a merge, a
declaration) moves a project, not a record.

`design N.N`, `decision Dn`, `decision Bn`, a witness code (O, T+H), a
case row (I34, T6, X2) and an experiment (E10) in a comment or
docstring cite [the membership design](membership-design.md), the
round 2 design this family was built from, with its decisions D1 to D19
and build decisions B1 to B24 in two appendices.

### The witnesses

The caller's location is read from files in the caller's own namespace
(`location.capture`): the top level, the git and common directories and
their objects, HEAD and up to 64 ref tips, every remote URL as a remote
key, the shallow marker, the token, the init stamps (`description` and
`info/exclude`), the project files from the cwd up, and the content
manifest of a commit-less checkout or folder project. Commits and blobs
are read from loose objects, pack indexes and packs (`gitobjects.py`),
with `git cat-file --batch-check` only where that reader reports itself
incomplete. Repository configuration (the remote URLs, `core.worktree`
and `core.bare`, the files `include.path` and `includeIf "gitdir:"`
name, and the global config's `url.<base>.insteadOf`; ENG-160) is read
as bytes by the module's own bounded parser, never through git, so
git's `safe.directory` refusal never applies; the system config is not
read. A `.git` file is followed only
where git's own pointers say the folder is its git directory's working
tree (ENG-158): a linked worktree's git directory registered at
`<common>/worktrees/<name>` that names the file back, a git directory
whose `core.worktree` names the folder, a `--separate-git-dir`
directory, or a pointer back that names nothing any more. A git
directory that is the `.git` of the folder above it (compared as a
filesystem object, so a symlink or another case is the same), a
`commondir` naming a repository the directory is not registered under,
a `core.worktree` naming another folder and a bare repository read as no
repository, and the walk goes on upward. Each witness is one observable
fact:

| Code | The witness | Read from | Alone |
| --- | --- | --- | --- |
| O | the same git-directory object (device, inode, birth time), or a folder checkout's folder | `stat` of the git directory; without birth time, confirmed by the token, else the last HEAD held, else the init stamps by equality | the same checkout: silent |
| S | the same object store | the common directory's object (a linked worktree) | another checkout of the project: silent |
| H | holds witness commits of the project | the project's bounded witness set, asked of the history reader in one batch | one witness: asks once |
| Hf | holds commits with a witness's fingerprint (sha256 of the author and committer lines and the message) | the repository's commits, where no witness commit is held | with H and Hb, one history witness |
| Hb | holds the file contents of witness commits: at least 2 blobs and half of one witness's sample | up to 8 blobs per witness | counts only beside H, Hf or R; never with a project file alone |
| R | names a remote key of the project | every remote URL in the config file and the files it includes, not only `origin`, each keyed as written and as the longest `insteadOf` prefix rewrites it; a `file://` or path remote names the views seen there | one witness: asks once |
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
   the most specific line, else a question. The most specific line is
   one breadth-first traversal from every tied witness commit, reading
   `WALK_LIMIT` (512) commits in all (ENG-151), about 512/n below each of
   n; a relation past it reads as none, so the projects it could not
   order are kept and the question says the bound was read. With
   `strict = true` a join resting on history without F or B asks.
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
it; so does evidence arriving. The lines (`line_*` in `membership_lines.py`):
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
own, keyed on the machine and the cwd path as a view records it
(`location.view_path`: each symbolic link and junction resolved,
ENG-155), with a checkout of kind `unresolved` whose `unresolved_json`
records the writing sessions and the project that replaced it. When the same harness session later resolves
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
(`MEMBERSHIP_EVENT_KINDS` in `store_schema.py`):

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
stderr.
`bettermemory membership import` homes the records its document names
first and runs it after. A store the owner's open hands back without its
key (a copy beside its original, whose key ring stays bound to the
original, opens for reading) is left as it is, and a process with no
machine id logs one line and opens the store as it is; the next open
that holds the key, or has a machine id, migrates. `bettermemory membership migrate
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
waiting for that one instead (`start_and_wait`). A client keeps a
daemon of its own version or a newer one (`_keeps`), and a shim that
finds its store's daemon again at a version other than the one its
session began with logs one warning asking for a restart of the MCP
client, which still holds the first daemon's tools
(`_session_version`);
`cli/daemon_cmd.py` is `up`, `down` and `status`, and
`cli/service_cmd.py` with `service_units.py` writes and loads the
launchd or systemd unit that supervises the daemon. `cli/serve.py` is
the in-process server the shim falls back to when no daemon can be
reached or started; both hand off to `builder.build_server`, which
instantiates the MCP SDK server and binds the nine tools. `server.py`
is a re-export shim kept for callers that import `build_server` from
there. `__main__.py` covers `python -m bettermemory`.

**The daemon's proof.** `GET /health?nonce=<32 to 128 hex>` answers
`proof`, hex(HMAC-SHA256(token, `bettermemory-health-v1:` + nonce))
under the bearer token in the daemon's state file
(`_daemon_client.health_proof`, which `daemon.build_app` computes);
without a nonce `/health` answers as it always did. A client sends a
fresh nonce (`secrets.token_hex(16)`) and recomputes the proof from the
state file's token (`_daemon_client.proves`): only the process that
wrote the file holds the token, so a reply with the right proof is that
daemon's, and a reply without one is never the daemon's, whatever pid
and store it repeats (since 9.0.0rc14; a daemon from before rc12 offers
none and reads as another listener). Discovery runs it at every lookup
(`identify`, through `_answering`, `wait_for_daemon` and
`ensure_daemon`), and so do `status`, `down`, `doctor` and the service
install. Since 9.0.0rc14 it is run again on every connection that will
carry the token, on that connection, before the token is sent: `post`
opens its own connection, asks the proof on it and sends its request
down the same connection only when the proof holds
(`_proven_connection`; `http.client` with `auto_open` off, so a lost
connection is never silently reopened, and a reply that closes the
connection fails the proof), so a hook pays one more `/health` round
trip per post; the shim's connection pool opens each connection through
`shim._ProvingBackend`, which proves it before the pool's first request
on it, one pool per port and token (`shim._DaemonTransport`), so a
connection only carries the token its listener proved. A connection the
pool keeps is not proven again while it stays open, since its peer
cannot change; after a reset, a timeout, a refused connection or a
close, the next connection is proven before it carries anything. A
listener that does not prove itself fails the connection
(`_daemon_client.NotProven`, `httpcore2.ConnectError` in the shim) with
nothing sent to it but the proof's request: the hook reports it and
exits 0, and the shim finds the daemon again and resends once, or
answers the call with an error when no daemon proves itself. Both
judge the proof's reply by one rule, `_daemon_client.proof_failure`
(status 200, at most `PROOF_REPLY_CAP` bytes, a connection left open, a
JSON `/health` with the proof). The shim's pool replaces httpx2's own
private `_pool` on its transport (`shim._ProvenTransport`); the shim
checks that httpx2 keeps its pool there (`shim._unprovable_httpx2`) and
refuses to start where it does not (`shim.refuse_unprovable_httpx2`),
so a later httpx2 can make the shim fail closed, never send the token
down a pool it did not prove.

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
on the wire. `time_utils.py` is cross-cutting.

**Write path.** `handlers/write.py` first lets `policy.py` refuse a
call that passes an escape hatch `[policy]` forbids, then checks
declared claims against the worktree (`claims.py`) and any declared
`supersedes` list. It then runs the gate chain, the gates of
`handlers/write_gates.py`: `credentials.py` (secret-shaped strings; its
`secret_spans` is what the log's query redaction shares), the
control-token check (`control_tokens.py`: a model's chat-template
delimiters, which `memory_update` and the episode write run too), the
capture gates on a capture write (the rules in `durability.py`, and a
body an undo reverted before), `durability.py` (structural durability),
the user-claim check, `scope_match.py` (scope mismatch against the
caller's repo), and duplicate and tombstone dedup. `review.py` then
puts the write review's questions to the caller. `supersession.py`
decides whether the write replaces a stored statement; the review runs
it to build its related set, and the commit runs it when the review did
not. A cue-less divergence is
filed in the conflicts queue (`conflicts.py`). A write that passes lands
in `store.py` as one signed log row and one table row in one
transaction, with `log.py` signing the row and keeping the chain.
`store.py` is the `Store` class, with its open, its connections and
transactions, the admission readers and the status; it re-exports
what a caller imports from its family and binds the family's method
groups by name: `store_schema.py` the vocabulary, the table texts,
the errors, the paths a store opens by and the version check,
`store_steps.py` the numbered schema steps and their runner,
`store_rows.py` the row
codecs and `_apply_mutation`, `store_keys.py` the key checks an open
makes and the key ring's binding, `store_records.py` the record,
standing-answer and tombstone methods, `store_journal.py` the
episode journal, `store_tables.py` verifications, conflicts, imports
and quarantine, `store_membership.py` the membership tables' rows,
`store_events.py` the telemetry rows, the event reads and the log's
refold and verification, `store_provenance.py` the provenance labels
and the trust reads, and `store_index.py` the reads behind search
and the document frequency memo.
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
and environment, validated on every lookup (`origin_gitfiles.py` reads
the config, the index's `.gitmodules` entry and the file signatures
those memos key on, and `origin_remote.py` parses the remote URL);
`_response.py` and `origin.py` memoise the commit-drift work per
repository root and commit HEAD names, which `githead.py`
reads from the repository files without a git process (`origin_history.py`
keeps the whole-history author dates, which a moved head extends with
the commits it adds); `search.py`
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
audits from the previous audit's row on), and since 9.0.0rc14
`settled_count`, the memories the turn's audit settled as used (its
retrievals and the shown pointers its reply used; zero for an
interrupted turn, whose pointers stay pending). The Stop records the row
after those settlements and before the interrupted turns' walk.
`recall.py` is the recall surface, the session brief and the prompt
recall, and re-exports its family: the per-session state and cooldown
(`recall_state.py`), the caller's admission (`recall_admission.py`),
the brief's ranking and render (`recall_brief.py`) and the probe both
hooks run with the Stop's settlement (`recall_settle.py`);
`standing.py` says what is standing and `in_force.py` what is in force
at delivery;
`owner_prompt.py` is the one definition of an owner prompt; `verdict.py`
is the memoised verdict `memory_show` reads. The bodies run in the
daemon but outside the MCP request path, so they read the store rather
than a session's state. They join the conversation's tool rows by the
harness session stamp, the worktree bridge when nothing carries it.
Claude Code names the conversation anew on /clear and /resume while
the server keeps the id it was spawned with, so a server stamped with
another id is another window's only when that id's hooks run beside this
conversation (a hook row at or after its first); otherwise the bridge
may take it as this window's own (`sessions.runs_beside`, which the eval's
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

**Membership.** `membership.py` is the model: the `Membership` class
over the store, with `why`, and its family, whose records, lines and
rows it re-exports and whose method groups it binds by name:
`membership_resolve.py` the ladder, `membership_declare.py` the
declarations and the writes made where nothing could be read,
`membership_lines.py` the questions' lines, `membership_records.py` the
records, `relation` and the surface table, `membership_rows.py` their
table rows, `membership_queries.py` run-state and anchoring, homes and
health, and `membership_portable.py` export and import. `location.py`
captures where a caller stands, from files, and resolves it under a
per-location lock, and `location_git.py` holds its readers: git's
discovery and config, the `Capture` model and its token codec, the
HEAD, ref, stamp and manifest readers and the history reader;
`gitobjects.py` reads commits and blobs from the repository's object
files; `checkout_token.py` and `project_file.py` read and write the two
files; `machine.py` is the machine id and the store id; `admission.py`
holds one request's admissions; `membership_migrate.py` is the
migration. `cli/project_cmd.py`, `cli/memory_move_cmd.py` and
`cli/membership_cmd.py` are the commands.

**Offline.** `migrate_v8.py` reads an 8.x directory through the frozen
readers in `v8.py` and writes the store; `mirror.py` writes the store
back out as that directory, with `_frontmatter.py` as the markdown
codec and `mirror_format.py` holding the v8 file names and frontmatter
it writes, which the migration shares, so the mirror imports neither v8
module and stays when they go; `rollback.py` removes one actor's writes and leaves everyone
else's; `init.py` and `_install_hints.py` are client onboarding.

**Operations.** The commands an operator runs, every one of them
read-only except a restore and `backup unlock`. `backup.py` is
`bettermemory backup` (`cli/backup_cmd.py`): the bundle, its signed
manifest, its verification on a private copy and the restore, which
appends a signed `store_restored` row to the restored log.
`restore_lock.py` is the restore lock, on the store's directory, which a
restore holds for its length and every writing open takes shared in
`store._connect`, and the record a restore that did not finish leaves,
which `backup unlock` removes. `audit_export.py` and `anchor.py` are
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

The `meta` table also says which builds may read and write the store
(ENG-107; CONTRIBUTING.md has the contract): `schema_version`, the major
line's format; `schema_revision`, what the store holds within it;
`min_reader_revision` and `min_writer_revision`, the lowest revisions that
may read and write it; and `engine_version`, the newest build that opened it
to write. Every open checks them (`store_schema.check_versions`, which
`store_versions.py` holds and `store_schema` re-exports, one call in
`Store.open` after the store id and the schema version have passed). A
build below the reader minimum refuses the store with `StoreVersionError`.
A build below the writer minimum opens it for reading only: the connection
is set `PRAGMA query_only`, so every write, `BEGIN IMMEDIATE` included,
fails with SQLite's `attempt to write a readonly database` as on read-only
media, and the owner's open goes on as a reader's (no rekey, no key ring
claim, no origin anchor), with one warning per process. A tool's read
on such a store resolves its caller in a transaction rolled back at its
end (`store_schema.writes_rolled_back`, from
`handlers/_shared.caller_anchoring`), with the checkout token's write
off, so the membership rows and events a resolution writes are never
kept, and a write through a tool is refused in words
(`_handlers._read_only`). Otherwise the
store's schema steps run (`store_steps.run_steps`): `STEPS` numbers every
change to the schema within the major, and the owner's open applies each
step above `schema_revision` in order, recording its number, raising the
minimums to the step's own and listing it in `meta`'s `schema_steps`.
Step 1 is the additive step of decision D9 (`_add_membership_tables`),
recorded in a `BEGIN IMMEDIATE` of its own after it runs. A step that is
not additive runs under the write lock after `backup.create_backup` has
written a bundle of the store as it stands (its snapshot reads through
its own read-only connection, which sees the store before the step) and
`backup.verify_bundle` has read it `ok`, or `unverifiable` when the
store's key is not held, and is applied and recorded in that one
transaction, so a crash leaves the store as it was and the bundle beside
it. A reader's open runs no step; while such a step is pending it sets
`PRAGMA query_only` and reads the store as it stands, which is how
`backup verify` and `backup restore` under the build that ran the step
read the bundle taken before it. The owner's open then
names this build in `engine_version` when it is the newest, in a
`BEGIN IMMEDIATE` taken only then; a store that cannot be written is left
as it is, and the next writing open records what is missing.

Before any of that, every opener of an existing store file admits it
(`store_schema.admit_store`, ENG-172): `trusted_schema` off, then, the
schema reads held to 2 s of virtual-machine time together and to strings
of at most 16 KiB, its `sqlite_master` checked (`schema_refusal`), each
plain table's columns read (`table_refusal`) and each index the build
never creates read once as SQLite made it (`index_refusal`: one
`index_xinfo` an index, so the reads grow with the indexes, not their
square). Names compare in any case, as SQLite's do. One of this build's
objects must have its text (a plain table, its name; an index SQLite made
for a constraint, its key columns); one it never creates is admitted when
it is what a later build of the major may add without raising
`min_reader_revision` (CONTRIBUTING.md, "Store versions"): a plain table
with no generated column, CHECK, foreign key or default but a literal
(SQLite's grammar, `TRUE`, `FALSE`, hex and exponent numbers included), a
column on one of this build's tables that is `NOT NULL` only with a
default, or an index on columns alone (no expression, no `WHERE`, only
SQLite's own collations) that is not unique on one of this build's tables.
A view, a trigger this build did not write, a virtual table, an index on
an expression or with a `WHERE`, a unique index on one of this build's
tables (the full-text table's shadow tables included), a `NOT NULL` column
with no default that this build's own table text does not have, and any of
its own objects with another text are refused with `StoreRefusedError`:
each runs SQL of the file's choosing on this build's connection, or turns
its writes into losses no one is told of (on a unique index, `INSERT OR
REPLACE` deletes a row, `INSERT OR IGNORE` and FTS5's replace drop one,
and `log verify` then reads the chain as tampered). The admission check is
that boundary for every build, so a later build that adds such an object
raises `min_reader_revision`, and an older build that refuses a file whose
`schema_revision` is above its own says to upgrade: `LaterStoreError`, a
`StoreRefusedError` and a `StoreVersionError`, in `check_versions`' words
when the reader minimum is above its own too, naming what the admission
refused either way. It reads that revision only from a plain `meta` the
table check passes, in a 2 s window of its own and under the same length
limit, and keeps the admission's refusal for a file with no such `meta` or
with a revision at or below its own; an admitted file's `meta`
(`read_store_meta`) is read in a window of its own as well. `doctor` and
the audit read that refusal as a version refusal first: their remedy is
the later build, not moving the file aside.

The search index is spelled by a tokenizer, and the signed log names which
(ENG-100). Each memory row keeps its index text beside its body:
`body_fts` and `scopes_fts`, its body and its space-joined scopes as
`search.fts_index_text` spells them, which the FTS5 table indexes, and
the log's `store_created` row, then the `index_rebuilt` row of each
re-spelling, name that spelling by its fingerprint (`meta` keeps a copy, read
only for a log that names none: `store_index.stored_fingerprint`)
(`search.tokenizer_fingerprint`: a digest of a probe corpus's spelling, of
every table in `search_lexicon` and of the FTS5 table's definition, so an
edit of a stopword list or a stem exception moves it whatever the probes
spell, and a change to the code moves it only through a probe that
exercises it). A change that moves it is a schema step raising the
store's minimum writer revision (CONTRIBUTING.md), so a build with the
older tokenizer opens the store for reading only and never re-spells it
back. A query matches a row
only while one tokenizer spelled both, so every open compares the
fingerprint with this build's (`store_index.reconciles_tokenizer`, the
decorator on `Store.open`, run once the open has returned the store by
any of its paths). When they differ, the owner's writing open
(`allow_rekey`, on a connection that holds the key and may write)
re-spells the store in one `BEGIN IMMEDIATE` (`store_index.rebuild_index`):
it reads the log's fingerprint again under the lock and stops if another opener got
there first, sets each row's two columns from its body and scopes (only
the rows whose text changes are written), rebuilds the FTS5 table from
them (`'rebuild'`), sets `meta`, and appends one `index_rebuilt` control
row naming why, both fingerprints and the counts. No record changes, and
the fold applies no `index_rebuilt` row. The fold (`store_events.refold`)
spells the index text with this build's tokenizer as it replays, so it
compares a row's `body_fts` and `scopes_fts` only while the log names that
tokenizer, where an edit of the text alone reads as tampering; while
it names another, both columns are left out on both sides, and a
tokenizer change never reads as tampering. The fingerprint is read from
the MAC'd rows, not from `meta`, which nothing signs, so an edit of `meta`
moves neither. `log verify` never covers the FTS5 index itself: an edit
of its shadow tables (a memory taken out of it, or an entry put in for
text no row carries) reads ok there, on this build as on the ones before,
and only `doctor`'s `index` check and `backup verify`'s `index_drift` see
it (`bettermemory index rebuild` repairs it). A rebuild that fails,
whatever it raises (busy, full, or a row it cannot spell, such as scopes
that are not text), is rolled back, logged once a process naming
`bettermemory index rebuild`, and the open goes on. Every other open (read-only,
read-only by its versions, or not the owner's: a worker's `reopen`,
`backup verify`'s copy) re-spells nothing, logs one warning a process
naming both fingerprints, and searches the index as it stands, so a row
the old tokenizer spelled may be missed until the owner's next open, as
a reader's open runs no schema step either. The `index_rebuilt` row is
one of the rows the document frequency memo is keyed on
(`store_index._memory_head`).

A record a later build wrote may carry fields this build's models do not
define; the models read past them (pydantic's `extra='ignore'`). Every
mutation of a memory or a tombstone (`store_rows._apply_mutation`: a put,
a removal, a restore, a purge or a prune) first reads the payload of the
log row each of the record's rows points at (`log_mac`, through
`log_by_mac`) and refuses with `UnknownFieldsError` when it names a field
this build does not define, so an older build never writes such a record
back without the field. The fold replays into a scratch database whose log
is empty, where the check finds nothing, and it reads only the columns
this build's schema gives each table, by name, so a column a later build
added is not compared.

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
switch, `enabled` (false switches off the measurement rows; the state
rows the product reads back and the audit rows are recorded either way,
with only the fields their readers read, `events.STATE_FIELDS`,
operations.md's "Telemetry and product state"),
`[daemon]` the port (7397 by
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
  managed; since 9.0.0rc14 it leaves one audit row, a `write` or `update`
  row with `status = "policy_refused"` and the flags, the switches and
  the managed ones (`policy_flags`, `policy_keys`, `policy_managed`),
  recorded with telemetry off too. These three switches take only a
  boolean spelling, and the list only known names: anything else stops
  the load.
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
type, a REG_EXPAND_SZ included, is refused. A key found without its
value, and last written (`QueryInfoKey`) within
`managed._REWRITE_RECENT` seconds, is read again after each pause in
`managed._REWRITE_PAUSES` (half a second in all), the gap a Group Policy
rewrite leaves (`_read_across_rewrite`, since 9.0.0rc14); a key absent
at the first read, or one without its value written earlier, is no
managed config at once, and a write a process already waited out is not
waited for again (`managed._WAITED_OUT`). Nothing under
`%ProgramData%` is read, and no environment variable moves either
source. Cygwin's and MSYS2's Python (`sys.platform` starting `cygwin` or
`msys`) read the Linux path, under a `/` that is a Windows folder:
since 9.0.0rc14 `managed._refuse_planted` refuses the file, failing the
load, when a folder from `/` down to it, or the file, as written or
resolved, lies under a mount `/proc/mounts` lists with the `user`
option (one `/etc/fstab.d/<user>` added) or on one with `noacl`, has a
group or other write bit, or passes `os.access(W_OK)` for this account
(`_planting_reason`, `_writable_by_users`); an unreadable `/proc/mounts`
is a refusal too. The registry text goes through the file's own parse and
refusals, a leading byte-order mark dropped and text holding an
unpaired surrogate refused as the file refuses bytes that are not UTF-8;
errors and reports name it `registry
HKLM\SOFTWARE\Policies\bettermemory\ManagedConfig`. It is read after the
user's file, and every key it sets is pinned: the user's value is
ignored, with one logged notice per key whose value the loader would
not read as the pinned one and is not the code's own default for the
key (`config._restates_pin_or_default`), and a list replaces the user's
whole. Against the pin, the line is read alone by the loader itself
(`_reads_as_pin`, since 9.0.0rc14): it restates the pin only when it
loads, with no refusal, fallback, clamp or lossy coercion, to the pinned
value as loaded, so `"true"` under a pinned `true` draws no notice and
`1` under one does (a boolean reads the integer as its default, a switch
refuses it). That read logs nothing: a context variable collects a note
from each coercion that does not take the line as written (whatever the
logging configuration), and a filter on the `bettermemory.config`
logger keeps their warnings off the log. `config check` and `config
show` ask the same question through `config.line_reads_as_pin`. Against the
default, the value is compared as written (`_restates_default`, a
boolean restating only a boolean), except for a key with a legacy
spelling (`_LEGACY_SPELLINGS`): `[behavior] prompt_recall`, which
3.41.0 to 8.0.0 wrote as `true`, is compared as `_prompt_recall_spelling`
reads it, so `true` (read as `project`) restates the default and a
spelling it does not read still draws the notice. Unknown sections and keys in it are ignored, as in config.toml,
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
- A ring from before 9.0.0rc13 is bound to a file that carries its
  history, and a byte-for-byte copy taken since the store last wrote
  carries it as the store does: the first owner's open of either binds
  the ring (Provenance, above).
- A restore holds off every writing open through `store._connect`
  (`restore_lock.py`); on Windows, and on a mount where `flock` cannot
  lock a directory, an opener holds the lock only for its check, and a
  process writing the file through a connection of its own is not held
  off. A read-only command refuses to start while a restore runs, exit
  75, but one that started before it can read the store between the
  restore's steps.
- The store's version keys are only what the file says. `meta` is not
  covered by the log's MACs, so an editor of the file can raise a minimum
  to keep builds out, or lower one. Builds from before the contract
  (9.0.0rc13 and earlier) read none of the revision keys and write a store
  whatever its minimums say. A store opened for reading only by its
  version keeps its key bound, so a write fails with SQLite's
  `attempt to write a readonly database` (which an MCP client receives as
  the SDK's bare tool error), and a daemon whose start-up membership
  migration finds work on it exits.
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
