# Operations

The runbook for someone who runs bettermemory on machines other people
use: a team's laptops, a fleet under MDM, a shared build host. Most
installs need none of it; the daemon starts on first use and the store
looks after itself. Everything below is a command that ships with
9.0.0rc12 or later. The read-only commands say so in their `--help`, and
each one offers `--json` and documents its exit codes there.
[SECURITY.md](../SECURITY.md) is the threat model these steps serve;
[internals.md](internals.md) has the mechanics.

## Files and modes

`bettermemory config path` prints every location below for the account
that runs it, with the rule or variable that chose it, and creates
nothing. `bettermemory security audit` grades the modes and owners.

| What | Where by default | Moved by | Mode |
| --- | --- | --- | --- |
| The store directory | `~/.claude-memory` | `BETTERMEMORY_DIR`, then `[storage] directory`, then `./.claude-memory` in the working directory unless `[storage] cwd_store = false` | `0700` |
| `memory.sqlite`, `-wal`, `-shm` | the store directory | | `0600` |
| `memory.sqlite.restore-lock` | the store directory: the record `backup restore --yes` keeps while it runs, removed when it finishes; left by a restore that did not finish (the restore lock itself is a lock on the directory, and makes no file) | | `0600` |
| `config.toml` | `~/Library/Application Support/bettermemory/` on macOS, `~/.config/bettermemory/` on Linux | the platform's config directory | written once by the first command that loads it, every setting commented out; never rewritten |
| The keys directory | `keys/` under the config directory | `BETTERMEMORY_KEYS_DIR` | `0700` |
| `<store_id>.key` | the keys directory | | `0600` |
| `<store_id>.<16 hex>.key` | the keys directory: a retired key, by its fingerprint's prefix | | `0600` |
| `<store_id>.head` | the keys directory: the last row's seq and MAC | | `0600` |
| `<store_id>.origin` | the keys directory: where this host's history of the store begins, and the key that signed that row | | `0600` |
| `<store_id>.path` | the keys directory: the one store file this key ring belongs to | | `0600` |
| `<store_id>.backup_restore.json` | the keys directory: one entry per `backup restore` | | `0600` |
| The state directory | `~/Library/Application Support/bettermemory/` on macOS, `~/.local/state/bettermemory/` on Linux | `BETTERMEMORY_STATE_DIR` | `0700` |
| `daemon-<hash>.json` | the state directory: the daemon's port, pid, version, store and bearer token | | `0600` |
| `daemon-<hash>.log` | the state directory: the daemon's output | | `0600` |
| `daemon-<hash>.lock`, `.spawn.lock` | the state directory: held by the daemon for its life, and by whoever is starting one | | empty |
| `gitdir-pins/<32 hex>.json` | the state directory: for each git directory kept apart with `--separate-git-dir`, the working tree membership follows it from, recorded at the first capture there and named for the git directory's device and inode (since 9.0.0rc18) | | directory `0700`, files `0600` |
| The managed config | `/Library/Application Support/bettermemory/managed.toml` on macOS, `/etc/bettermemory/managed.toml` on Linux and other POSIX systems; on Windows no file, the registry value `HKLM\SOFTWARE\Policies\bettermemory\ManagedConfig` | nothing | root-owned `0644`; on Cygwin and MSYS no folder from `/` down that another account can write, or it is refused; on Windows the key's default access, which only administrators can write |
| Backup bundles | `backups/<UTC stamp>/` in the store directory | `backup create --out DIR` | directory `0700`, files `0600` |

`<hash>` is derived from the store file's canonical path (since
9.0.0rc14: links resolved, and on a volume that folds case each name as
the volume holds it), so each store has its own daemon and files, and
two spellings of one store share them. The key, the head and the origin anchor are what make
the store's provenance checkable: keep the keys directory out of the
store directory, out of git working trees and out of synced folders, and
back it up on its own (below). The `.head.lock`, `.origin.lock` and
`.path.lock` beside them are empty sidecar locks.

The key ring is bound to one store file, `<store_id>.path`, written by
the store's creation, by the first owner's open of a ring that has none
(a ring from before 9.0.0rc13; since 9.0.0rc14 only of a file that
carries the ring's history, and any other file reads `unclaimed`), by
`backup restore`, `key adopt --yes` and `log accept-history --yes`. A second file
with the same store id, a copy of the store in another directory say,
opens for reading only while the bound file still holds that id (or
cannot be read to tell): it writes nothing to the key ring, refuses
every write, and logs a warning
naming both files; `doctor`'s key check fails and `security audit`
reports `key.match` high in the same words ("bound elsewhere"). To move a store, move the file; the next owner's
open of it rebinds the ring, since the old path no longer holds the
store. Two files with one store id cannot both be written on one host:
to hand the ring to the other one, move or remove the bound file, or
run `bettermemory key adopt --yes` with `BETTERMEMORY_DIR` naming the
other file's directory, which binds the ring to it on the record (a
`key_adopted` row in its log). A store copied with its keys directory
to another path on this host is the same case. No file in the keys
directory is placed or deleted by hand: `bettermemory key` shows,
exports, imports, adopts and rotates the ring, and `log accept-history`
accepts a replaced history, each on the record.

Every opener admits a store file before it reads a value from it (since
9.0.0rc14): a file whose schema holds what could run SQL of its own on
this build's connection or lose its writes (a view, a trigger, a virtual
table, an index on an expression, a unique index on one of this build's
tables, one of this build's objects in another form) is refused in one
line, one a later build wrote saying to upgrade, while a plain table or
an index on plain columns that a later build adds is admitted; and
`doctor` and `security audit` report it in one finding each. Admission
itself writes nothing, but SQLite does what it does on opening any
database: a `-wal`, or a hot `-journal`, that came with the file (a
repository that ships `.claude-memory/memory.sqlite` can ship them too)
is replayed into it or rolled back when the file is first read, and a
WAL-mode file gets a `-wal` and `-shm` while it is read, removed when
the opener closes it. So a refused file can differ from the one that
arrived; keep a copy of it before any command reads it if you need the
bytes as they came.

## The daemon

One daemon per store, on 127.0.0.1, owns the store and serves every
session and hook. The first hook or MCP client that finds none starts
it, detached, in the state directory; a client of a newer version
replaces an older daemon that proves itself, and an older client leaves
a newer one alone. A daemon from before 9.0.0rc12 offers no proof and is
never replaced by a client (see "the store S is held by pid N" below).
While another process holds the store's lock (a daemon still starting,
or one busy or hung), a client starts none: it waits up to 8 s for that
daemon to answer and prove itself, and starts one only if the lock comes
free first. When the state file's pid holds the lock and what answers at
its port does not prove itself, the client waits for nothing and starts
nothing. When the wait ends with the lock still held, `up` exits 1
and its line (and its `--json` `error`) ends `: a process holds the
store's lock (a daemon starting, busy or hung) and none answered within
8 s`. A daemon reads the config and the managed config at its
start, so a config change takes effect after `bettermemory down` and the
next start.

```sh
bettermemory up            # start one, or report the one running
bettermemory status        # is it running, on which port, for which store
bettermemory down          # stop it; returns once the store is released
```

`up --json`, `status --json` and `down --json` print one object each and
never the token. `down` asks only a daemon that proved itself (SECURITY.md,
"A local process that reaches the daemon"); a state file whose daemon
does not answer is kept while its process holds the store's lock, so a
starting or busy daemon is never hidden, and removed once that process
is gone.

The daemon keeps SQLite's query planner statistics current (ENG-100): it
runs `PRAGMA optimize` as it starts serving and every hour after that, on
a connection of its own off the event loop, so requests are answered
while it runs, and once more at shutdown on the store's connection, after
its workers have stopped and before the store is closed. A run analyses
only a table whose statistics are missing or behind its size, and writes
them to `sqlite_stat1`, a table every opener admits. The first run on a
store counts every table: on synthetic stores it took 1.8 ms for 5,000
memories (a 20 MB file), 399 ms for 50,000 (193 MB) and 1.8 s for 250,000
(963 MB); a run with nothing to analyse takes 0.01 to 0.08 ms. It never
waits for the write lock: while another connection writes, the run is
skipped and the next one tries again.

### A store another release wrote

A store records the schema revision a build must read to open it and
the one it must write to change it (`min_reader_revision` and
`min_writer_revision` in its `meta`; CONTRIBUTING.md, "Store
versions"). A build below the reader minimum refuses the store: every
command, the daemon and the in-process server fail their open with
`StoreVersionError`, whose line names the revision needed, the key and
the newest build that wrote the store, and `bettermemory doctor` names
the build to open it with. Upgrade bettermemory, or run `bettermemory
backup restore` under a build that reads it. A build below the writer
minimum opens the store for reading only and logs one warning saying
so: searches, shows, `log verify` and `export` work, and every write
is refused. The versions are checked when the store is opened, so a
daemon that was running when a later release raised the writer
minimum keeps writing until it restarts: after installing a later
release, stop the old daemon (`bettermemory down`; a supervised one is
replaced by running `bettermemory service install` again). A version
key that does not read as a whole number refuses the open naming the
key: set it back, or restore a bundle taken before the edit.

9.0.0rc15 runs schema step 2 (ENG-200) at the owner's first open: the
`returned` table, and the writer minimum raised to 2. Going back to
9.0.0rc14 or earlier after that serves the store for reading only, so
a switch's way back is this candidate or later, or the backup taken
before the switch, which loses the writes made since. The same open
marks, once, the memories an earlier build brought back into force and
logs `returned: N records an earlier build brought back into force
marked for a standing roster read` when there were any; a backfill that
fails logs a warning and runs again at the next owner's open.

9.0.0rc19 runs schema step 3 (ENG-215) at the owner's first open: an
index of the membership questions by state and kind, which every
sighting reads, and the revision raised to 3 with the minimums left at
1 and 2. Going back to 9.0.0rc15 through 9.0.0rc18 after that still
reads and writes the store, which keeps the index and its revision.

### Running it as a service

`bettermemory service install` writes a launchd LaunchAgent on macOS
(`~/Library/LaunchAgents/io.github.0mattias.bettermemory.plist`) or a
systemd user unit on Linux (`~/.config/systemd/user/bettermemory.service`,
under `$XDG_CONFIG_HOME` when that is set), stops a daemon of the same
store that runs outside the supervisor, and loads the unit. `service
print` shows the unit and writes nothing; `service status` reports
whether it is installed, current and loaded; `service uninstall` unloads
and removes it. On other platforms each prints the command to supervise
yourself and exits 2.

The unit runs `<this interpreter> -m bettermemory up --foreground` in the
state directory, with an owner-only umask, its output appended to the
daemon log. It is started at login and again after an exit with a
failure status, 30 s apart; an exit 0 (a `down`, or a newer client
replacing it) is final. It pins `BETTERMEMORY_DIR`,
`BETTERMEMORY_KEYS_DIR`, `XDG_CONFIG_HOME`, `BETTERMEMORY_FLOCK_TIMEOUT`,
`BETTERMEMORY_INDEX_THRESHOLD` and `PATH` as the installing shell had
them, and always `BETTERMEMORY_STATE_DIR`; the identity variables
(`BETTERMEMORY_CLIENT`, `_MODEL` and the rest) are never pinned. With no
`BETTERMEMORY_DIR` the unit serves the store a daemon resolves from its
state directory: `[storage] directory`, else `~/.claude-memory`, never a
project's `./.claude-memory`, whose daemon hooks still start on first use.

What to know before rolling it out:

- One unit per user. Running `install` again from a shell with other
  `BETTERMEMORY_*` values replaces it; `service status` reports a unit
  that no longer matches what `install` would write as not current.
- `install` refuses an interpreter in a package manager's cache (`uvx`,
  `uv run --with`, `pipx run`), which can be deleted under the unit:
  `uv tool install bettermemory` or `pipx install bettermemory` first, and
  install the service from the same install the clients run, since a
  newer client stops an older supervised daemon and it is not restarted.
- `install` refuses a store inside a backup bundle (`BETTERMEMORY_DIR`
  naming a bundle's directory), exit 1, before it writes the unit, the
  log or the state directory and before it stops any daemon: the unit's
  daemon would refuse that store at every start. Copy `memory.sqlite`
  out of the bundle first, as the message says.
- After `bettermemory down` a supervised daemon stays down until the
  next login or a reload: `launchctl kickstart gui/$(id -u)/io.github.0mattias.bettermemory`
  or `systemctl --user start bettermemory.service`. Meanwhile the next
  hook starts an unsupervised one.
- While a hook-started daemon holds the store, the supervised one exits
  75 and is retried every 30 s, one short process and one log line each
  time, until that daemon goes.
- A systemd user unit runs only while the user is logged in, unless
  `loginctl enable-linger <user>` is set. Logging to the daemon log needs
  systemd 240 or later; older versions log to the journal.
- On macOS a LaunchAgent is its own responsible process for privacy
  prompts: reading repositories under `~/Documents`, `~/Desktop` or
  `~/Downloads` may be prompted for or denied, where a daemon a hook
  started inherits the terminal's grants.
- `service install` run under `sudo` writes root-owned files into the
  user's home and targets the wrong launchd domain. Run it as the user.

## Monitoring

Each of these is read-only and answers through its exit code, so a cron
job, an MDM script or a CI step can run it as is.

| Command | 0 | 1 | 2 | 3 |
| --- | --- | --- | --- | --- |
| `bettermemory status --check` | running and answering as this store's daemon | not running | a state file whose daemon does not answer but may be alive | the port answers, but not as this store's daemon |
| `bettermemory doctor` | every check ok | a check failed | warnings only | |
| `bettermemory security audit` | nothing at warn or high | a high finding | warnings only | |
| `bettermemory config check` | nothing above a note | an error | warnings only | |
| `bettermemory log verify` | the log verifies | tampered, or cannot be verified | no store, or an `--anchor` file cannot be read | |

While a restore of the store runs, `log verify`, `log export`, `log
anchor`, `stats` and `memory history` read nothing and exit 75
(EX_TEMPFAIL): `<store> is not opened: a restore is running; try again
when it has finished`. A gate on `log verify` should try again on 75,
not alert. `key show` (exit 1) and `security scan` (exit 2) refuse in
the same words with the codes they give any store they cannot read.
One that started before the restore holds it off until it
has read what it reads: the restore waits for it and is refused, so
run the restore again once the command is done.

`doctor` checks the install, that the config loads, which rule chose
the store, whether a restore of it runs or did not finish, the store
and its key, the store file's pages and its search index, the daemon,
the Claude Code and other client wiring, the disk, and the keys
directory for key rings whose store is gone, each `ok`, `warn` or
`fail` with a one-line fix.
`doctor --clear-orphan-keys` prints the plan, and `doctor
--clear-orphan-keys --yes` moves those rings to `quarantine/<store_id>/`
under the keys directory, owner-only, each with a `note.txt` line
naming the store file it was bound to and why it was moved. Nothing is
deleted: `bettermemory key import <keys>/quarantine/<store_id>/<store_id>.key`
with `BETTERMEMORY_DIR` naming the store's directory files a key back.
A ring moves only when its store is shown gone: no file is at the path
the binding names, and the nearest directory on the way up that is
there (the store's own, or an ancestor when the directory was deleted
with the store, as a bench's scratch directory under the temporary
directory is) can be read and holds other entries, each read within
0.5 s. A disk unplugged from `/Volumes` reads the same, since its mount
point goes with it: its rings move to the quarantine, and `key import`
takes each back once the disk is back. Every other ring is kept and
named (the nearest directory there empty, as an unmounted mount point
is, a link to nothing on the way, a directory unreadable or not
answering, a file there that is damaged or holds another store, a ring
that names no store file), so run it again once a volume is back, and
run `key adopt --yes` from a store moved by hand before the clear. `security audit` covers what `doctor` leaves out: file modes and
owners, the key against the store, git working trees and synced folders,
and Claude Code's sandbox settings. `config check` grades the contents
of `config.toml` and the managed config: a typo `doctor` cannot see, such
as `[behaviour]`, `enabeld` or `"flase"`, is a warning there. `log verify`
is the one that reads every row; it costs a pass over the whole log.
Exit 2 is also argparse's code for a usage error, for each of them.

`doctor`'s `index` check (ENG-100) runs on the store check's read-only
open, so a store no daemon holds is copied privately once, and runs
SQLite's `PRAGMA quick_check`, which reads every page of every table and
index, and FTS5's integrity-check in the form that compares an
external-content index with the rows it indexes (`'integrity-check'`
with rank 1). That command is an insert, which a read-only connection
refuses, so it runs on a copy in memory of the indexed columns and the
index's shadow tables, made with the store's own FTS5 table definition,
and nothing is written beside the store. Both are linear in the file's
size and neither has a deadline: measured on synthetic stores, the check
took 0.10 s for 5,000 memories (a 20 MB file), 0.69 s for 50,000 (193 MB)
and 3.41 s for 250,000 (963 MB), quick_check 1.17 s and the index copy
and check 1.91 s of the last; the copy is held in memory, and doctor's
peak resident size went from 47 MB to 362 MB on the largest store (56 MB
and 117 MB at the smaller two). A damaged page fails the check (exit 1) and names the
repair: restore the newest bundle that `backup verify` reads ok, which
keeps the damaged file aside. An index that does not match its rows (a
memory it lost, which no search finds, or an entry for text no row
carries) warns and names `bettermemory index rebuild`, as does an index
spelled by an older tokenizer than this build's, which the owner's next
writing open re-spells too. `--json` carries both checks' output in the
check's `details` (`quick_check`, `fts_integrity`, the two tokenizer
fingerprints, the memory count and the seconds).

`bettermemory stats` reports file sizes, rows per table, memories by
project, scope, client and model, and the activity of the last `--since`
days, from aggregate SQL with no MAC checked; it is the growth report,
not a check.

`bettermemory index rebuild` is the repair for the store's search index
(ENG-100): it spells every memory's index text again from its body and
scopes with this build's tokenizer, rebuilds the FTS5 table from those
columns and records one `index_rebuilt` log row, in one write
transaction. No record changes, so `log verify` reads the store as
before. It writes, so it is not in the table above: run it where a check
names it. It opens the store as the writing commands do, so the daemon
may keep running; the rebuild holds the store's write lock for its
length (0.53 s for 5,000 memories as measured for ENG-100), and a write
that waits past its busy timeout is refused as busy and can be retried.
It exits 1, changing nothing, when the store cannot be written: busy,
full, or open for reading only by its versions.

What the daemon's log and the clients say when something is wrong:

- **"Invalid Host header" (421) or "Invalid Origin header" (403).** A
  client reached the daemon by a name other than `127.0.0.1`,
  `localhost` or `[::1]`, on another port (a port-forward), or from a
  browser page. The daemon answers only clients on its own port.
- **"the process answering at 127.0.0.1:<port> is not the bettermemory
  daemon its state file names (pid N, store S): nothing is sent to it".**
  A stale state file, and another process on its port: the daemon
  stopped without removing the file (a crash, a kill) and something took
  its port. The store's lock is free, so the client starts a fresh daemon
  on another port; find what holds the port with `lsof -iTCP:<port>`.
- **"the store S is held by pid N, and what answers at 127.0.0.1:<port>
  is a bettermemory daemon from before 9.0.0rc12, which offers no proof:
  nothing is sent to it, and no daemon of this version can start while
  it runs. Stop pid N ...".** A daemon of an older install still runs
  after the upgrade. It holds the store's lock and cannot prove itself,
  so a client neither uses it nor stops it, and no daemon of this version
  can start beside it: a hook gives up at once with this line, the shim
  serves the store in-process, and `bettermemory down` stops nothing (it
  posts nothing to, and signals nothing behind, a port that answers
  without the proof). Stop the pid
  (`kill N`; `taskkill /PID N` on Windows), or, where a service
  (launchd, systemd) runs the old daemon, run `bettermemory service
  install`, which reloads the unit with this install; the next call
  starts this version's daemon. Before 9.0.0rc14 a client took such a
  daemon for its own and replaced it by posting it the shutdown with the
  token, which handed the token to whatever held the port. The same line
  with "a process that does not prove itself the daemon its state file
  names" is a state file of this version whose pid holds the lock while
  its port answers without the proof: find what holds the port with
  `lsof -iTCP:<port>` and stop it.
- **"bettermemory hook <event>: NotProven: ... the listener at
  127.0.0.1:<port> did not prove it holds the daemon's token (<why>);
  nothing was sent to it"**, or from the shim, **"the listener at
  127.0.0.1:<port> did not prove it is the bettermemory daemon its state
  file names (<why>); nothing was sent to it"**. A listener took the
  daemon's port between the lookup and the call (the daemon crashed or
  was killed). The call is lost: the hook exits 0 with this line, and
  the shim finds the daemon again and resends once, or answers the call
  with an error. The next lookup starts a daemon beside the listener;
  find the listener with `lsof -iTCP:<port>`.
- **"bettermemory: httpx2 <version> keeps its connection pool where the
  shim cannot prove its connections to the daemon; the shim does not
  start"**. The installed httpx2 keeps its transport's pool elsewhere
  than the httpx2 this release was built with, and the shim, whose proof
  of the daemon rides on that pool, refuses to run on it rather than
  send the token down an unproven connection. Install bettermemory with
  the httpx2 its release locks (`uv.lock`; 2.16 for 9.0.0rc14), for
  example `uv tool install bettermemory --with 'httpx2==2.16.*'`, or a
  bettermemory release that supports the installed httpx2.
- **"a process holds the store's lock (a daemon starting, busy or hung)
  and none answered within 8 s".** A shim found no daemon answering, and
  started none, since another process holds the store's lock: no second
  daemon is started beside one that holds it. A daemon still starting
  answers within moments. `bettermemory status` reports a state file
  whose daemon holds the lock but does not answer, and `bettermemory
  down` stops it; `lsof` on `daemon-<hash>.lock` in the state directory
  finds a holder with no state file. A hook in the same case starts
  none either and gives up after the same wait. `up` says the same
  after `bettermemory: no daemon could be started for <store>:`.
- **"store ... was opened without its key; a new key ... was written and
  the log rekeyed".** This host did not hold the store's key: a store
  copied in, or a keys directory that moved. See
  [installation.md](installation.md#troubleshooting).
- **"the key file ... is there and cannot be read ...; the store is not
  rekeyed over it".** The key file, or a link to it, cannot be read: a
  permission, an I/O error, a volume not mounted. The daemon and every
  writing command fail until it can be read, and nothing is rekeyed;
  `doctor` fails its key check and `security audit` reports it high.
  A command ends in that one line, not a traceback, and exits 1
  (`membership migrate` exits 2, and `migrate v8` prints it after
  `migrate v8:`). A daemon writes it last in its log and exits 1; `up`
  and the hooks quote it with the log's path, and a hook still exits 0.
  A keys directory that is a symbolic link reaches a daemon a client
  starts as the link, so a link to a volume not mounted fails there in
  the same line.
  Make it readable to this account again (`0600`), or mount the volume.
- **"store ...: the key ring under ... is bound elsewhere: it belongs
  to the store at ..., which still holds store id ...; every open of
  this file is read-only".** `doctor` and `security audit` say the
  same for the file (since 9.0.0rc14). Two store
  files carry one store id, and the key ring is bound to the other one
  (Files and modes, above). Move or remove one of them, or run
  `bettermemory key adopt --yes` against the one to keep. A process logs
  it once for each
  pair of files, and every later open of the pair at DEBUG, so one
  daemon logs it once however many worker connections open the store.
- **`log verify` reports `foreign_segment`.** Rows signed by a key this
  host does not hold come after its own. Either someone wrote into the
  store file without the key, or the store was moved away and back, or
  opened once under another `BETTERMEMORY_KEYS_DIR`; in those two cases,
  `bettermemory key import FILE` with the other key (a `key export` of
  the keys directory that holds it) files it as a retired key, on the
  record, and `log verify` reads `ok`.
- **`log verify` reports `origin_mismatch`.** The row where this host's
  history of the store begins is gone, carries another MAC or does not
  verify under the key that signed it, which this host still holds.
  Either the history was rewritten, or the store file was copied over
  this host's copy again from the machine it came from. In the second
  case, restoring a bundle or `bettermemory log accept-history --yes`
  accepts the replaced history on the record (a `history_accepted` row,
  and an entry in the rewind record a `log export` cursor is checked
  against), and trusts the file again on purpose. One more cause reads
  the same, reason `signature`: an anchor written before 9.0.0rc13 names
  no key, and when that key file was lost before a 9.0.0rc13 writing open
  could name it, the loss cannot be told from a rewrite. `bettermemory
  key import` of the old key makes it hold again; without the key, `log
  accept-history --yes` accepts the history as it stands.
- **`log verify` reports `origin unverifiable`.** The key that signed
  the row where this host's history begins is no longer in the keys
  directory: its key file was lost, and the store was rekeyed since. The
  anchor stays where it was; `bettermemory key import FILE` with the old
  key files it as a retired key, the anchor holds again, and `log
  verify` reads `ok`.
- **`log verify` reports head `tail_deleted`.** An older copy of the
  store file was put back by hand, so the head names a row its log does
  not hold. `bettermemory backup restore` puts a bundle back on the
  record; for a copy put back on purpose, `bettermemory log
  accept-history --yes` rewinds the head on the record.

### Claim-drift latency

A search, `memory show` or `health` reads the commit drift of each hit
whose memory cites or claims a file in a git repository (the counts and
their bases are in [api.md](api.md)). Every git that read runs reads the
history as its objects hold it, past the repository's commit-graph, so
no changed-path filter answers it (9.0.0rc19, ENG-220). Measured cold on
the author's machine, over a synthetic 60,000-commit repository that
has a commit-graph with changed-path filters, against the same reads
taken through the graph:

| Read | Through the graph | As stored |
| --- | --- | --- |
| A hit on the author-date basis | 350–372 ms | 741–823 ms |
| of which, the log of the claimed file's whole history | 37 ms | 478 ms |
| A hit on the walk, 20 commits since the stamp | 24 ms | 24 ms |
| A hit on the walk, 2,000 commits since the stamp | 59 ms | 67 ms |
| A hit on the walk, 50,000 commits since the stamp | 863 ms | 1,026 ms |
| This repository's whole-history log of one file (2,365 commits) | 28 ms | 40 ms |

A warm daemon memoises each answer per repository and head, so these
are paid once each time a head moves, not on every search. Each git call
claim drift makes is bounded at 5 s, and a call that runs out is a
failed read, which never lowers a count: the hit keeps the conservative
count its basis falls back to. At the measured rates (no graph read),
the bounds fall about here:

- **The walk**, at about 17 µs a commit since the stamp, reaches 5 s
  near 290,000 commits; the hit then reads on the author-date basis.
- **The log of a claimed file's whole history**, at about 8 µs a commit,
  reaches 5 s near 600,000 commits in the repository; past that, a hit
  on the author-date basis counts every commit since the stamp,
  whichever file it touched, on every search.
- **The author dates of the whole history** (`git log --format=%at`), at
  about 5 µs a commit, reach 5 s near 1,000,000 commits; past that the
  hit's count is omitted, and the failed read is remembered for a
  minute, so the searches of that minute do not run it again.

The three bounds are extrapolated from the 60,000-commit runs, not
measured at those sizes. In a repository near them, a memory verified at
a recent head keeps its walk short, and its hit on the walk's basis.

A hit with a claim also reads the claimed files' changed lines, as one
patch over the commits since the stamp, of at most 256 commits. Every
file is diffed as text, a binary one included, so the patch of a large
binary file is about as large as the file. Before it is read, the
sizes of the blobs it would read are summed (one `git log --raw` and one
`git cat-file --batch-check`, about 20–30 ms cold, kept with the
repository's packs); past **32 MiB** the patch is not read and the hit
keeps the claimed files' per-file count, which is never lower. Measured
on the author's machine (git 2.54, packed objects), through the read and
the parse:

| Window | Blobs read | Read and parsed in |
| --- | --- | --- |
| A 5 MB binary file changed 3 times | 29 MiB | 140 ms |
| The same file rewritten whole 3 times | 29 MiB | 520 ms |
| A 5 MB file of short assignment lines rewritten 3 times | 29 MiB | 2.8 s |
| 64 one-line edits to a 200 KB file | 24 MiB | 23 ms |
| A 20 MB binary file changed 3 times | 114 MiB | not read |
| 256 one-line edits to a 200 KB file | 98 MiB | not read |

A call that runs out of its 5 s bound folds into the conservative count
as any failed read does; the hit's resolution is then kept for a minute
under its key, so the searches of that minute do not pay the bound again.

## Capacity

How large a store bettermemory serves well was measured by the scale
bench (`bench/scale/`, ENG-109). It builds synthetic stores at 1, 10 and
100 times the shape of a store in daily use for six weeks (1,475
memories, 355 episodes, 40,789 log rows and 3,913 membership rows in 13
tables, over a dozen projects): sessions recorded through the MCP tools
and the hook bodies, replayed in time order through the store's own
write and sign code, so `log verify` passes on each. Their history
spans 42 days at 1x and 420 days at 10x and at 100x, so a day at 100x
holds ten times the rows of a day at 10x. The bench times the hook
and tool bodies in process, as a daemon worker runs them, without the
hop from the hook's client to the daemon, and runs the whole-store
commands as the CLI. The readings are from one machine (Apple M3 Pro,
12 cores, 18 GB, macOS), each arm started with the 1-minute load
average under 4; the result files under `bench/scale/results/` name the
commit they measured.

| Store | 1x | 10x | 100x |
| --- | --- | --- | --- |
| Memories | 1,499 | 14,933 | 147,578 |
| Episodes | 341 | 3,673 | 35,242 |
| Log rows | 39,473 | 396,645 | 3,945,178 |
| Membership rows | 3,807 | 37,888 | 373,073 |
| Projects, checkouts | 12, 14 | 38, 48 | 120, 152 |
| File | 89 MB | 780 MB | 7.4 GB |

| p50 / p95, ms | Budget | 1x | 10x | 100x |
| --- | --- | --- | --- | --- |
| Session start, a first start | 13,000 | 84 / 129 | 852 / 960 | 84,680 / 89,927 |
| Session start, with a transcript to probe | 13,000 | 55 / 74 | 531 / 650 | 78,967 / 84,774 |
| Prompt hook | 8,000 | 46 / 63 | 332 / 503 | 80,332 / 127,298 |
| Stop hook, in a whole session | 13,000 | 58 / 74 | 367 / 615 | 100,543 / 105,187 |
| `memory_search` | 60,000 | 11 / 26 | 23 / 38 | 191 / 254 |
| `memory_write`, the first call | 60,000 | 207 / 265 | 3,863 / 4,545 | 32,485 / 49,215 |
| `memory_write`, with its review answered | | 438 / 940 | 14,655 / 16,658 | 112,100 / 196,192 |
| Resolution of a known checkout | | 0.3 / 3.5 | 0.4 / 2.4 | 2.5 / 3.6 |
| First sight of a repository | | 5.1 / 6.1 | 5.5 / 5.9 | 9.9 / 51.8 |

A hook's budget is the time its body has for its store rows
(`hook_budget_seconds`: the harness's timeout or the client's wait for
the daemon, the shorter, less 2 s). A tool call's is the stdio shim's
wait for the daemon's reply, 60 s; a write with its review answered is
two to four calls. The stop hook was timed inside the whole sessions the
bench runs (22 calls at 1x and at 10x, 2 at 100x), the others on their
own, 10 to 90 calls each (the result files give each count).

| Wall time, peak RSS | 1x | 10x | 100x |
| --- | --- | --- | --- |
| `log verify` | 1.9 s, 176 MB | 8.4 s, 886 MB | stopped at the 2.6 GB cap, 10.6 s in |
| `backup create` | 1.2 s, 107 MB | 1.9 s, 101 MB | 16.1 s, 103 MB |
| `backup verify` | 1.8 s, 159 MB | 8.4 s, 885 MB | stopped at the 2.6 GB cap, 19.0 s in |

The supported envelope is 10x: 15,000 memories, 400,000 log rows and a
0.8 GB file. There every hook answers in under a second at p95, well
inside its budget, a search in under 40 ms, and `log verify` and
`backup verify` in under 10 s and 1 GB. The one slow path at 10x is a
write: 15 s with its review answered, 4.5 s for its first call at p95.
At 100x nothing a session waits for works: every hook takes six to
sixteen times its budget. Interpolated on a log-log line between the
10x and 100x readings, the hooks cross their budgets at about 40x: a
first session start at 39x at p50 and 38x at p95, the prompt hook at
38x and 32x, the stop hook at 43x and 39x. The first call of a write
reaches the shim's 60 s only past 100x (at about 120x at p95, by the
same line carried on).

Beyond 10x, four things degrade:

- **The hooks grow with the log.** A new session's start reads every
  row of the kinds a session's recall state is rebuilt from, whatever
  its session: `session_events_of_kinds` finds them through the kind
  index and compares the session on each row. The prompt hook's reads
  of its window (`events_of_kinds`) do the same with the stamp, so they
  read every row of their kinds too. Both hooks read every `use` row of
  the log for the contradictions among their hits (`contradicted_at`),
  and session start loads every memory for the brief's search pool and
  reads every active record for the standing backlog line. At 10x
  session start took ten times as long as at 1x, and the prompt hook
  seven times. At 100x the 7.4 GB file no longer fit in the memory the
  machine had free (about 4 GB), so those reads went to the disk, and
  each hook took 80 to 130 s. One profiled session start there took 88 s,
  28 s of it on the CPU: 48 s walking 1,913,550 rows for a new session's
  recall state to find none, 14 s in the contradiction read and 12 s
  loading the search pool. One prompt took 79 s, 17 s on the CPU: 58 s
  walking 2,157,052 rows for the 3,455 its window holds.
- **A write grows with the memories.** The duplicate gate loads every
  memory and tokenizes each one's content on every call
  (`search_dedup`), and the review's supersession check
  (`supersession.detect_supersession`) compares every active memory
  again. Their token cache holds 5,000 bodies (`RAW_TOKEN_SET_ENTRIES`),
  so past 5,000 memories every call tokenizes them all again. A write
  with its review answered took 33 times as long at 10x as at 1x, and
  112 s at 100x; one write under the profiler at 100x spent 271 s of
  its 454 s of CPU tokenizing. The bench's synthetic bodies share more
  words than real ones, so more pairs pass the supersession check's
  first test than a real store's would.
- **`log verify` and `backup verify` hold the whole log in memory.**
  The chain check lists every row and the fold replays every table in
  memory, so the peak RSS grows with the store: 176 MB at 1x, 886 MB at
  10x. At 100x both passed the bench's 2.6 GB cap (set from the memory
  the machine had free) and were stopped: `log verify` held 1 GB at
  3.7 s and 2 GB at 6.8 s. At the 2 KB a log row it grew by from 1x to
  10x, it needs about 8 GB at 100x. `backup create` copies the store and
  stays near 100 MB at every size.
- **Concurrent calls share one process's CPU.** Four tool workers, four
  hook workers and the CLI on one store for 90 s (`contend`): the lock
  is not where the time goes. A write transaction waited for it 25 ms at
  p95 at 1x and 17 ms at 10x, never more than 0.5 s. The calls run in
  one process, and each took many times its time alone: at 1x a prompt
  hook 3.8 s at p50, a session start 2.8 s and a write 32 s; at 10x a
  session start 30 s, past its budget, and two hooks that reached their
  state row after their budget ran out failed busy at once
  (`StoreBusy`) instead of waiting for the lock. The arm was not run at
  100x, where a hook alone is past its budget.

Membership resolution barely grows with the store: a first sight takes
5 to 10 ms at p50 from 1x to 100x. ENG-159's arm at 400 projects (300
commit-less repositories and 100 provisional projects) reads a
commit-less first sight's content candidates in 4 store reads and
20.4 ms at p50, against 1,201 reads and 73.4 ms on the tree before
ENG-159's fix, and health in 5 reads and 10.2 ms, against 104 reads and
26.5 ms.

## Backup and restore

`bettermemory export` is not a backup: it carries the memories and
tombstones and leaves out the signed log, episodes and every other
table, and nothing restores a store from it. The backup is
`bettermemory backup`:

```sh
bettermemory log verify                       # back up a store that verifies
bettermemory backup create --out /srv/backups --json
bettermemory backup verify /srv/backups/20261028T120000Z
```

`backup create` snapshots the live store through SQLite's online backup
API, safe while the daemon writes (a transaction still open in the
daemon is not in it), into `DIR/<UTC stamp>/`: `memory.sqlite`, the
head checkpoint at the snapshot and `manifest.json`, which records the
store id, the key fingerprints, the head, the origin anchor (left out,
with a warning, when it lies past the snapshot's last row), row counts
and every file's SHA-256, and is signed with the store's key. The
default `DIR` is `backups/` inside the store directory, on the same disk;
pass `--out` to put bundles somewhere else, outside every bundle (a
`DIR` that is a bundle, or is inside one at any depth, is refused). `create`
does not verify the
bundle, and it takes the bundle's head from the snapshot's own last row,
so a store whose tail was already deleted backs up as a bundle that
verifies: run `log verify` first, and `backup verify` after.

`backup verify` checks every file's size and SHA-256, the manifest's
signature and its claims, then runs the whole log check on a copy in a
private temporary directory; the bundle is never opened in place. On
that copy it also runs the two checks `doctor`'s `index` check runs
(ENG-100): a damaged page is `snapshot_unreadable` with `check`
`quick_check`, the problem the log check gives when its own reads meet
one, so the bundle reads `mismatched` and `restore` refuses it; an index
that does not match its rows is `index_drift`, which leaves the verdict
as it is, since the records are whole, and names the repair: restore,
then `bettermemory index rebuild`. A snapshot an older tokenizer spelled
is no problem: one warning names the bundle's store, and a store restored
from it is re-spelled at its owner's first writing open. Exit 0
ok; 1 mismatched or tampered; 2 unverifiable, when the key is held
nowhere, the manifest is unsigned, or it is signed only by a key the
bundle carries (signature `self_signed`: anyone who can write a bundle
can remake it under a key of their own, so the signature counts only
under a key the keys directory holds). A bundle whose log goes on from a
segment under a key the keys directory holds into one under a key only
the bundle carries, which is this host's snapshot rekeyed by whoever
wrote the bundle, reads `tampered` (`foreign_segment`), as the live store
reads that shape, and nothing restores it. Every command that would open
a bundle's `memory.sqlite` as a store refuses it, since a writing open
switches its journal mode, which changes the file's bytes, and the
bundle would then fail its hash (`security audit` reads it without
changing it, and so, since 9.0.0rc14, do the read-only commands, which
refuse it all the same): a store file with a bundle's
`manifest.json` beside it, through a symbolic link too, is refused
before it is opened, with a message that says to copy `memory.sqlite`
out of the bundle to a directory of its own first and point the command
at the copy. A store file with other names (hard links) is refused the
same way, since one of them may be a bundle's snapshot: one named on
the command line, and one that `BETTERMEMORY_DIR` or `[storage]
directory` reaches. For that one the message names the two ways to a
store that writes: restore it in place from a bundle (`bettermemory
backup restore BUNDLE --yes`, which gives the store a file of its own),
or copy it to a directory of its own, point the directory at the copy
and run `bettermemory key adopt --yes` there, since the key ring stays
bound to the old path and the copy opens for reading only until then.
A `manifest.json` counts when it names the format
`bettermemory.backup` or does not parse, so another tool's file of that
name beside a store changes nothing. `backup create` refuses a bundle's
store too (`BETTERMEMORY_DIR` naming a bundle), with or without `--out`,
exit 1, and writes nothing: keep a bundle by copying its directory as
it is.

**Keep the keys apart from the bundles.** Without `--include-key` a
bundle holds no key, and verifies and restores only where the keys
directory holds it. Back the keys directory up separately, to a place
the bundles' readers cannot reach; `bettermemory key export --out FILE` writes
one store's keys to one owner-only file for that (it warns when FILE
is in a git working tree or a synced folder, from which the keys would
travel), and `bettermemory key import FILE` files them back. A bundle made with `--include-key`
carries the store's key and any retired keys its log names, and anyone
holding it can sign rows as the store. On a machine whose keys directory
does not hold that key it verifies `unverifiable` (`self_signed`) and
restores only with `--trust-bundle-key`, which makes the bundle's key the
store's current key: pass it only for a bundle kept where no one else
can write. The default `backups/` directory is not such a place, since
whatever can write the store can write it.

To restore:

1. Close every session that uses the store, then run `bettermemory down`.
   Restore refuses while the daemon's lock or the spawn lock is held, and
   while any other process has the store open (`serve`, the shim's
   in-process fallback, a hook, a CLI command); once it runs, every
   writing open is refused until it ends (the restore lock, below).
2. `bettermemory backup restore BUNDLE` verifies the bundle (it refuses
   one that does not verify, one signed only by a key it carries
   unless `--trust-bundle-key` is given, and one whose store needs a
   later bettermemory to write it: upgrade, then restore) and prints
   the plan: the files
   it moves aside, the keys it installs, the head checkpoint before and
   after. Without `--yes` it changes nothing and exits 3.
3. `bettermemory backup restore BUNDLE --yes` takes the store's restore
   lock (on the store directory; its record is
   `memory.sqlite.restore-lock`), refuses while any process has
   the store open, which it reads off SQLite's own locks, or reads it
   (a read-only command holds the lock until it is done), and from then
   on every writing open and every read of the store is refused until it
   is done. It
   moves `memory.sqlite`,
   `-wal` and `-shm` aside as `memory.sqlite.pre-restore-<UTC stamp>`
   (never deleted), copies the bundle's database in and checks it against
   the signed hash, installs the keys the bundle needs (a key that was
   current is retired, not deleted), installs the bundle's origin anchor
   or removes a stale one, rewinds the head checkpoint, binds the key
   ring to the restored file, appends the rewind and the binding before
   and after to `<store_id>.backup_restore.json`, appends the same entry
   to the restored log as a signed `store_restored` row, and runs `log
   verify` on the result. Exit 0 only when that verifies.

A restore that is killed, or fails after it swapped the store file in,
leaves its record, `memory.sqlite.restore-lock`: every writing open then
refuses the store, which may be part restored, naming the bundle and the
step it reached, and `bettermemory doctor` fails its `restore` check
with the record and both ways out, exit 1. Run the same restore again
to finish it, or `bettermemory backup unlock --yes` to keep the store as
it stands (without `--yes` it shows the record); after an unlock, run
`bettermemory log verify` and `bettermemory doctor`, since the store
kept may be part restored. An unlock while a read-only command reads
the store waits for it, then exits 1 saying a command is reading or
opening the store: run it again when that is done. A restore that
stopped after it wrote its record and before its `store_restored` row
(the record names the row it wrote, `row`, null until then) gets the
row from the first owner open after the unlock (the daemon's, or any
command that writes records), or, run again, writes it before its own.
The `key` commands, `export` and `membership` open the store without
the owner's open and append no late row. If a record is written before
the row (by one of those, or by a bettermemory before 9.0.0rc18, which
appends no late row), the row is withheld: on the log it would stand
after that record, and `memory history` would read it as a rewind of a
record the restore never saw. The open says so once, the entry keeps
`row` null with `row_withheld` naming that record's row and why, `log
verify` still reads `ok`, and `log export` still warns from the record
of a cursor inside the rewound range. Should a later restore take that
record off the log, the withheld row is appended then, before the later
restore's own. The moved-aside file, put back by hand, is withheld from
the same way (`replaced`). Where the restore record names no replaced
head past the bundle's (`head_before` null, or not past `head_after`),
only the time of the first row past the bundle's head tells that file
from the restored one: a row stamped before the restore withholds the
row, and the warning says what the file may be: the history the
restore replaced, or, where the restore read no log on this host
(`log_end_before` null), another copy of the store, or the restored
file under a clock set back after the restore. That row stays on the
file, so the file is not placed again; the restore record keeps the
restore either way.

A restore into another store directory, while the store the bundle was
taken from is still there, moves the key ring's binding to the restored
file: the old store then opens for reading only and says why, until it
is moved or removed.

The head checkpoint only moves forward in normal use, which is how a
deleted tail is caught; restore is the one path that rewinds it, and it
writes down that it did. After a restore to an older bundle, every
external anchor taken past the restored head reads `anchor_mismatch`
(`missing`): that is the evidence of the rewind, and the restore record
explains it. A bundle whose store has no origin anchor leaves the store
without one, and the next writing open anchors it again, trust on first
use. The plan and the result print the origin anchor and the key ring's
binding, before and after, as `--json` and the restore record carry
them, and where the log the restore replaces ended.

### When a store's history was replaced

Two signed control rows say a store's history was replaced, and
each keeps an entry in the record beside the head
(`<store_id>.backup_restore.json`) naming its kind and its row:
`store_restored`, appended by `backup restore` when a verified
bundle's snapshot replaces the store file, and `history_accepted`,
appended by `log accept-history --yes` when the file's history is
accepted as it stands. `log verify` lists both under `history
replaced`, `log export` warns a cursor inside the range either left
behind (and names it in its `--json` summary's `rewinds`), and
`memory history` shows each in the history of every record older
than it, as `rewound` and `accepted`. A restore that could not append
its row (one onto a store waiting on a schema step, which it opens for
reading only, or one that failed after its record) gets it from the
owner's next open that can write, with the payload it would have had,
while no record was written after the restore: its `restored_at` says
when the restore ran, the row's own time when it was appended. One a
record came before keeps no row (`row_withheld` in its entry, above).
Which command follows which event:

| What happened | What to run |
|---|---|
| A `backup restore` finished | Nothing more: the restore ran `log verify` on its result, and `log accept-history` would find nothing to accept. |
| `key import` put keys back in the ring | `log verify` only: its `key_imported` row replaces no history. |
| The store comes from a backup taken on another machine | `backup restore BUNDLE --yes` (with `--trust-bundle-key` when only the bundle carries its key), never a copy of the file and `log accept-history`. |
| A store file was put back or copied by hand, with no bundle | `log accept-history --yes`, once `log verify` has shown why its anchor or head no longer fits. |

A schedule that holds up: `log verify && backup create --out <another
disk>` daily from cron or the service manager, `backup verify` on the
new bundle, and old bundles pruned by date. A bundle verifies against
the key, so a key rotated since (a `rekey`) keeps verifying as long as
the retired key file is kept.

## Audit export and anchors

`bettermemory log export` writes the log as JSON Lines for a SIEM,
incrementally, without the memories themselves; [api.md](api.md#the-audit-line-schema)
has the line schema. A cron job keeps a cursor:

```sh
dir=/var/lib/bettermemory-siem
cursor=$(cat "$dir/cursor" 2>/dev/null || echo 0)
bettermemory log export --since-seq "$cursor" --out "$dir/batch.jsonl" --json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["through_seq"])' \
  > "$dir/cursor.next"
# ship "$dir/batch.jsonl" here; advance the cursor only once it is shipped,
# and only when the batch's first prev_mac is the last mac shipped (below)
mv "$dir/cursor.next" "$dir/cursor"
```

`--out` writes the file `0600` and replaces it atomically when the
export completes; `through_seq` is the cursor to pass next time, whether
or not `--kinds` let the last row through. A cursor past the log's end
gets a warning on stderr: the store was restored, its tail removed, or
it is another store. So does a cursor inside the range a `backup
restore` rewound (the restore record beside the head says from which seq
to which): a cursor taken before the restore would skip the new rows
the restore handed those seqs to, and the warning names the
`--since-seq` to export them from. A restore onto a host that held no
copy of the store cannot know where the replaced log ended, so there
the export warns every cursor past the restored head. The cursor is a
seq, so the export cannot tell such a cursor from one taken after the
restore; the SIEM's link check below can, and is the one to rely on. Or stream stdout into a collector (Vector, Fluent
Bit). The output is
deterministic: the same range of the same log gives the same bytes. On
Windows, stdout gets CRLF line ends and `--out` writes LF.

What to alert on:

- **`verified: false`**: a row's MAC does not verify under its segment's
  key.
- **`verified: null`** as well: the key of that row's segment is not held
  on this host. A store copied in has such rows legitimately, but an
  editor without the key can also make later rows read null by editing
  `meta.key_fingerprint` or inserting a `rekey` row. Alert on it, and run
  `log verify` on the host.
- **A broken link**: each batch's first `prev_mac` should equal the last
  `mac` the SIEM holds (with `--kinds`, check the link on an unfiltered
  export). A batch that does not link is history rewritten or rewound
  past the cursor, a restore among the causes: export again from the
  restored head.
- **`kind: "rekey"`** rows, which a healthy store writes only when its
  key is replaced.
- **`kind: "store_restored"`** rows: a `backup restore` put the store
  back to a bundle. Its `head_after` is the seq it was put back to; rows
  the SIEM holds past that seq are history the restore replaced, and
  the batch carrying the row does not link to them.

The export is a trail, not a verdict: it does not see a deleted tail, a
table edited without a log row, or a foreign segment. `log verify` is the
verdict, and anchors make it hold against a key holder:

```sh
repo=/path/to/anchor-repo
bettermemory log verify && bettermemory log anchor >> "$repo/anchors.jsonl"
git -C "$repo" add anchors.jsonl && git -C "$repo" commit -qm "anchor" && git -C "$repo" push -q
bettermemory log verify --anchor "$repo/anchors.jsonl"
```

`log anchor` does not verify the log first, hence the `log verify`
before it.

An anchor is `{format, store_id, seq, mac, chain_sha256, created_at}` for
the log's last row. `chain_sha256` is the SHA-256 of every row's MAC up
to that seq, each followed by a newline, so it can be checked without
bettermemory, on a copy of the store file:

```sh
sqlite3 copy.sqlite "SELECT mac FROM log WHERE seq <= 1234 ORDER BY seq" | shasum -a 256
```

Keep anchors off the host, where a process on it cannot rewrite them: a
ticket, a private repository, a WORM bucket, the SIEM. An anchor of
another store in the file is listed as `other_store` and changes nothing,
so a check given only a wrong store's anchors passes: check the output
names this store.

## The secret scan

The write gate refuses secret-shaped tokens, but a secret can still be
in the store: written with `acknowledge_credential`, carried in by `migrate
v8`, written before a detector existed, or kept in an older version in
the log.

```sh
bettermemory security scan --log
```

reports each hit's place (table, or log seq and kind), record, field,
detector kind, the record's state (live, tombstoned, pruned) and whether
the write was acknowledged, and never the value. Exit 0 clean, 1 found, 2
no store it can read. `--store PATH` scans another store, a directory
holding `memory.sqlite` or the file itself; to scan a backup bundle, copy
its `memory.sqlite` into a directory of its own first, since opening it
in place breaks the bundle's hash (the scan, as every command that opens
a store, refuses a file that has a bundle's `manifest.json` beside it,
and refuses a `--store` file with other names, since a hard link to a
bundle's file has no manifest beside it).

When something is found: rotate the credential at its issuer first,
because the log keeps every version of every record and no command
erases a log row, so the value stays in the store file and in every
backup made since. Then fix the live record (`memory_update`, or
`memory_remove` and `bettermemory tombstones prune`), and scan again with
`--log` to see what remains. The scan does not read freed pages or older
page images in the store file and its WAL, the full-text index's pages,
saved orphans, backups or exports: a clean scan is not a clean file.

The same scan reports where a model's chat-template control token sits
(since 9.0.0rc18, ENG-189): the tokens the write gates refuse in a body
since 9.0.0rc14 and in the free-text arguments beside it (a removal's
`reason`, the notes, `claims`) since 9.0.0rc18, and the tokens the user's
quoted words and the event log's free-text fields keep by their bare names
since then, which a store written before can hold as typed. Each finding
names the same place, record and field, and the template family with an
offset and a count, never the token; the JSON lists them under `control_tokens`,
beside the credential `findings`, and the summary line counts the two
apart. The scan exits 1 on either kind. A control token is no credential,
so there is nothing to rotate: rewrite the field without it
(`memory_update`, or `memory_verify` for claims and attestations) or
remove the record, so recall, search and `memory_show` stop reading it to
a model. The log keeps the earlier version, as it keeps a secret, and a
log row that is no version of a record (an event, a membership row) has
no record to rewrite: it is reported on every scan with `--log`. A path,
a session, an id or a declared client or model name is kept as given,
since the store compares it with its live value (a checkout whose
directory name holds `[INST]` keeps it in every record's origin and every
event's worktree stamp): those findings are grouped as `paths, ids and
names`, marked `kept_as_given` in the JSON, and nothing rewrites them; a
checkout moved to a path without the token, or a client declared under
another name, keeps new rows clean. Add `--only credentials` to scan for
credentials alone, or `--only control-tokens` for the tokens alone.

## Telemetry and product state

Every event is a row of the store's signed log, and each row is one of
two classes by its kind (since 9.0.0rc14; `events.STATE_FIELDS` is the
one table the writer and a pruning read).

**State** is every kind a product module reads back to decide what it
does next, and the audit trail. A state row is recorded whatever
`[telemetry] enabled` says and is never pruned (`events.prunable`). With
telemetry on it keeps its whole payload. With telemetry off it keeps only
the fields its readers read, listed below, and the stamps every state
row keeps: the session column, the actor block, and `session_id`,
`attribution`, `triggered_from`, `worktree_root` and `harness_session`
where the row has them. No query or prompt text lands, no body snippet,
claim excerpt, note, reason or quoted word, and no transcript path.

| Kind | Kept with telemetry off | Read by |
| --- | --- | --- |
| `show` | `id` | the recall's seen gate, the Stop's settlement, a segment's close, `health` |
| `search` | `returned` | the seen gate, the Stop's settlement, the miss probe's shield, `health` |
| `write` | `id`, `status`, `review_id`, `forced`, `markers`, `credential_kinds`, `control_token_kinds`, `mismatch_kinds`, `policy_flags`, `policy_keys`, `policy_managed`, every `*_acknowledged` | the seen gate, review chains, the override rate, the secret scan, the audit trail |
| `update` | `id`, `status`, `review_id`, `forced`, `markers`, `credential_kinds`, `control_token_kinds`, `policy_flags`, `policy_keys`, `policy_managed`, every `*_acknowledged` | the seen gate, review chains, `health`, the secret scan, the audit trail |
| `verify` | `id`, `status`, `verified_head`, `verified_commits` | the membership migration, `health` |
| `use` | `ids`, `outcome`, `auto`, `surface`, `shown_event_id`, `segment`, `harness`, `agent_id` | the contradiction hold-back, the search response's recent outcomes, the settlements, `health` |
| `turn_audited` | `verdict`, `repeat`, `no_signal_reason`, `prompt_id`, `settled_count`, `probe_query` (its `hash` and `len`) | the next turn's window, the interrupted-turn audit, the re-audit dedup, the miss rate |
| `search_miss` | `event_id`, `top_hits` (each hit's `id`) | `health`'s silent misses, `memory_admin(action="acknowledge_miss")` |
| `miss_ack` | `event_id` | the same |
| `silent_miss_cutoff` | `cutoff_ts` | the same |
| `session_brief` | `event_id`, `ids`, `segment`, `harness`, `marker`, `marker_absent`, `checkout`, `agent_id` | a session's recall state, the episode handoff's quiet-session check |
| `prompt_recall` | `event_id`, `shown_ids`, `segment`, `harness`, `checkout`, `agent_id` | the same |
| `segment_opened` | `segment`, `harness`, `marker`, `marker_absent`, `opened_at`, `brief_event_id`, `withdrawn`, `agent_id` | a session's recall state |
| `review_unanswered` | `root_ids`, `harness` | a session's recall state |
| `review_issued` | `review_id`, `parent_review_id`, `related`, `roster`, `door`, `record` | review chains |
| `scope_disable`, `scope_enable` | `scope` | the scopes a session turned off |
| `episode_handoff`, `episode_write` | the stamps alone | the episode handoff's quiet-session check |
| `standing_guard` | `door`, `standing`, `said_checked` | the audit trail |

The audit rows among them are `standing_guard`; a `write` or `update`
that used an escape hatch (`force`, or an `acknowledge_*` flag that
waved a hit through, `acknowledge_scope_mismatch` included); a refusal
such a hatch passes (`credential_warning`, `transient_warning`,
`user_claim_warning`, `scope_mismatch`, `duplicate`,
`previously_removed`, an update's `truncation_warning`); and a call
`[policy]` refused (`status = "policy_refused"`).

A state row is not best-effort. A write that meets another process's
lock is tried once more, each try waiting up to the connection's busy
timeout (5 seconds), and a failure that remains fails the call:

- A tool that kept nothing answers `store_busy (retryable)`. One that
  had committed before its row failed answers `store_busy (not
  retryable)`: a `memory_write` whose commit landed and whose row did
  not, which leaves its review chain reading as open. The `use` row a
  call's turn advance writes for a retrieval it settles on its own can
  fail that call, a call of another tool, the same way.
- A hook shows nothing, writes one line on its stderr and leaves a
  WARNING in the daemon's log, so nothing is shown that the log does not
  hold. A hook's body runs under its budget: the timeout `hooks.json`
  gives it (20 s for the brief, 10 s for the prompt, 30 s for the Stop),
  capped at the client's 15 s wait, less 2 seconds. No row is tried past
  it, and the body's other writes wait at most that long. The brief and
  the recall fail closed: a store held past the budget shows nothing, and
  the line reads `bettermemory hook prompt: StoreBusy: the store was
  busy, so the recall was not shown: another writer held it past this
  hook's time; run bettermemory doctor`.

**Measurement** is every kind no product module reads: `recall_held`,
the grades (`recall_grade`, `review_grade`, `capture_grade`), the
conflict scans and verdicts (`conflict_scan`, `conflict_verdict`),
`remove`, `restore`, `rename_scope`, `list_tombstones`,
`answer_standing` and `use_token_expired`. `[telemetry] enabled = false`
records none of them. A measurement row is best-effort (a failed write
is logged at WARNING and the call goes on) and prunable at any age.

So telemetry off strips what `health` and `eval` measure: queries and
their previews, scores, snippets and timings, the held recalls and the
grades. It refuses the grade commands. A memory's history loses its
`remove` and `restore` events (the removal itself is a store row and
stays). With no text kept, the search response's recent negative
outcomes carry no note or claim excerpt, `health`'s silent misses no
query preview and its resolution timeline no notes. It does not turn off
the session brief, the prompt recall, the Stop's settlement of what a
turn searched or was shown, the contradiction hold-back, review chains,
scope toggles, the miss acknowledgements or the audit trail. `health`'s
override rate reads the same either way: a refusal and the override
that answers it are both audit rows and keep their markers. A store
open for reading only (its key is not held here, or a later build's
writer revision) records no row of either class, with one warning per
row.

## The managed config

An organisation pins settings with a managed config, which takes
config.toml's sections and keys; every key it sets is pinned, so the
user's own config cannot loosen it. `examples/managed.toml` is the
starting point:

```toml
[storage]
cwd_store = false

[security]
strict_provenance = true

[policy]
forbid_acknowledge = ["credential"]
```

On macOS and Linux it is a file. Deploy it as root, through MDM or
configuration management, at `/Library/Application
Support/bettermemory/managed.toml` on macOS or
`/etc/bettermemory/managed.toml` on Linux, root-owned, mode `0644`.

Cygwin's and MSYS2's Python read the Linux path too, and there `/` is
a Windows folder (`C:\cygwin64`, `C:\msys64`), which a folder made
under `C:\` lets every signed-in account write by default. Since
9.0.0rc14 the file is refused there, failing the load as an unreadable
one does, when any folder from `/` down to it, or the file itself, as
written or through a link, lies under a user mount, lies on a mount
without access lists (`noacl`), has a group or other write bit, or can
be written by the account running bettermemory: a file one account
plants there would bind every other account, the reason nothing under
`%ProgramData%` is read. Cygwin makes `/etc/fstab.d` writable by every
account and mounts what `/etc/fstab.d/<user>` names for that user, so
one account can mount a folder it owns over `/etc/bettermemory` for
another; the mounts are read from `/proc/mounts`, and a table that
cannot be read is a refusal too. The error names the file, the folder,
why and what to do, and `config check` reports it. To deploy there,
install under a folder only administrators can write, on a mount with
access lists (MSYS2 mounts without them, `noacl`, by default), and
remove any line under `/etc/fstab.d` that mounts something on the way,
or use a native Windows Python, which reads the registry value below.
An elevated process can write every folder, so it is refused too: run
bettermemory unelevated.

On Windows it is no file: the managed TOML is the `ManagedConfig` value
under `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\bettermemory`, a REG_SZ
holding the whole text or a REG_MULTI_SZ holding one line per string,
read through the registry's 64-bit view. Only administrators can write
under `SOFTWARE\Policies` by default, and nothing under `%ProgramData%`
is read, since a standard account can create folders there. WOW64 shares
`SOFTWARE\Policies` between the 32-bit and 64-bit views, so a 32-bit
and a 64-bit writer reach the same key. From an elevated prompt:

```bat
reg add "HKLM\SOFTWARE\Policies\bettermemory" /v ManagedConfig /t REG_MULTI_SZ /reg:64 /f /d "[storage]\0cwd_store = false\0[policy]\0forbid_acknowledge = ['credential']"
```

Group Policy deploys the same value with a Registry preference item
under Computer Configuration (action Update, hive `HKEY_LOCAL_MACHINE`,
key path `SOFTWARE\Policies\bettermemory`, value name `ManagedConfig`,
type `REG_SZ` or `REG_MULTI_SZ`), and Intune with a PowerShell script
run as SYSTEM in 64-bit PowerShell; `examples/managed.toml` has the
script. A leading byte-order mark, which text taken from a file Windows
PowerShell 5.1 saved can carry, is dropped. A REG_EXPAND_SZ or any type
but the two above is refused, never expanded, and so is text holding an
unpaired surrogate, which the file would refuse as bytes that are not
UTF-8. Reports name the source `registry
HKLM\SOFTWARE\Policies\bettermemory\ManagedConfig`.

A preference item with the Replace action, and a refresh of
registry-based policy, delete the value and write it again. Since
9.0.0rc14 a load that finds the key without its value, and the key
written in the last five seconds, reads it again, five reads over half a
second, so a daemon or a hook starting in that gap still gets the pins.
A key found absent is no managed config at once, so a machine without
one never waits, and so is a key whose value was removed earlier, with
`reg delete /v` or a policy set back to Not Configured: a retired config
costs a hook one read, no wait. A key written in the last five seconds
whose value never comes back costs each process the half second once,
and the process logs one warning naming it; retiring the config by
deleting the key, not only the value, avoids even that. A rewrite that
deletes the whole key, or one longer than half a second, can still be
read in its gap as no managed config; the Update action rewrites
nothing.

No environment variable moves either source. Then restart the daemons:
each reads the managed config at its start. The hooks read it at each
config load when `BETTERMEMORY_DIR` is unset, to resolve the store.

It fails closed. A file or value that exists but cannot be read, is not
TOML, writes a top-level value outside a section, or holds an invalid
value, or on Cygwin and MSYS a file another account could have planted,
stops the config from loading, and the daemon does not start; the
hooks then print the error and change nothing. Only an absent file, key
or value means there is no managed config. Check it on one machine
before the fleet: `bettermemory config check` grades both sources and
names the error, and `bettermemory config show` labels each setting
`managed`, `user` or `default`. `config check` reads only the fixed
location, so a candidate is checked where it is deployed.

A user's config.toml line for a pinned key is ignored. When the loader
would read it as something other than the pinned value, and it is not
the code's own default for that key, each process logs one warning
naming both sources, on stderr for a hook, until the line is deleted.
Against the pin a line is compared as the loader would load it on its
own (since 9.0.0rc14): `"true"` under a pinned `true` draws no warning,
while `1` under a pinned `true` draws one, since a boolean reads the
integer as its default and a `[policy]` switch or `cwd_store` refuses
it; so does a line the loader reads only through a fallback, a clamp or
a lossy coercion (`checkpoint_tokens = 100` under a pinned `500`, `port
= 7400.9` under a pinned `7400`). A line that restates
the default draws none. The config.toml a first run writes sets nothing: every setting is
commented out, so the code's defaults apply and a pin finds no line. One
an earlier release wrote spells every key it knew at that release's
default; a line that is still today's default, `[telemetry] enabled =
true` among them, draws no warning when a pin overrides it. Against the
default, lines are compared as written, but `[behavior] prompt_recall`,
which releases 3.41.0 to 8.0.0 wrote as `true`, is compared as the
loader reads it (`true`, `"yes"`, `"on"` and `"1"` are `project`;
`false`, `"no"`, `"off"`, `"0"` and `""` are `off`; strings in any
case): a line that reads as the default draws none. `config check`
reports every ignored line that does not read as the pin as a note,
and `config show` names its value, by the same comparison against the
pin: `1` under a pinned `true` is in both, `"true"` in neither. Such a file also keeps those values for the user where
nothing pins them: delete the lines nobody chose, and a later release's
changed default reaches the account.

The managed config binds the user's config, not the account: a process
running as the user can still set `BETTERMEMORY_DIR`, which decides the
store ahead of a pinned `[storage] directory`. SECURITY.md has the rest.

## Enterprise deployment checklist

1. **Install one version, verified.** Check the release's files against
   `SHA256SUMS` and its build-provenance attestation
   ([release.md](release.md#verifying-a-release)), and install it as a
   tool (`uv tool install bettermemory==<version>`), the same install the
   clients and any service unit run. Register clients with `bettermemory
   init --client <client>`.
2. **Deploy the managed config** (the file, or on Windows the registry
   value) with at least `[storage] cwd_store = false` and `[security]
   strict_provenance = true`, plus the `[policy]`
   switches your rules call for. Under Cygwin's or MSYS2's Python, deploy
   it under a `/` only administrators can write, on a mount with access
   lists, or it is refused ([The managed config](#the-managed-config)). Decide telemetry on purpose:
   `[telemetry] enabled = false` drops the measurement rows (held
   recalls, grades, conflict scans: the usage figures in `health` and
   `eval`) and keeps of each state row only the fields its readers read:
   ids, statuses, counts and stamps, never a query, a prompt, a body
   snippet or the user's quoted words. The session brief, the prompt
   recall, the Stop's settlement and the audit rows work either way
   ([Telemetry and product state](#telemetry-and-product-state) lists
   what each row keeps).
3. **Deploy Claude Code's sandbox as managed settings.** `bettermemory
   security sandbox --format managed` prints the block for a host's
   paths ([installation.md](installation.md#5-optional-managed-deployment));
   it writes nothing. Paths under the home directory are spelled `~/`, so
   one block serves accounts that keep the default locations.
4. **Check each machine.** `bettermemory doctor`, `bettermemory security
   audit` and `bettermemory config check`, from the MDM agent as the
   user, with their exit codes reported.
5. **Optionally run the daemon as a service** (`bettermemory service
   install`, as the user) and watch `bettermemory status --check`.
6. **Back up** with `backup create --out` to another disk and `backup
   verify`, and back the keys directory up separately.
7. **Ship the audit trail** with `log export` and a cursor, alert on
   `verified` false or null, and keep `log anchor` output off the host.
8. **Scan for secrets** with `security scan --log --only credentials`
   on a schedule, and rotate anything it finds before cleaning the
   record; run it without `--only` by hand, and rewrite or remove a
   record it names for a control token.
9. **After any config or managed-file change, restart the daemons**
   (`bettermemory down`; the next session starts one).
