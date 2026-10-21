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
  under `bench/` that nothing pinned. The receipts under each program's
  `results/` stay, and `bench/README.md` indexes what is live and what
  is closed. Releases before 8.0.0 moved from this file to
  `docs/changelog-history.md`, unchanged.

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
