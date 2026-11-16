# bettermemory native build

The Rust build of bettermemory 9. It reproduces the Python reference in
`src/bettermemory` exactly, so a store and its signed log written by one
open unchanged in the other. The reference behaviour is pinned by the golden
files in `tests/contract/golden`, which `tests/contract/gen_*.py` generate
from the Python code; every test here reads them from there.

Nothing here is wired into the Python product or packaged. CI builds and
tests it on Linux, macOS and Windows against fixtures made on each
("Parity on every platform").

## What the crate holds

One library crate, `crates/bettermemory-core` (`#![forbid(unsafe_code)]`):

| module | reproduces | pinned by |
| --- | --- | --- |
| `json` | `json.dumps` as each call site spells values (`log.canonical_payload` and nine more forms, `review.canonical_json`), `json.loads` | `canonical_json.jsonl`, `json_edges.jsonl` |
| `float_repr` | `float.__repr__` (shortest round trip, CPython's tie and layout rules) | `canonical_json.jsonl` |
| `datetime` | `datetime.fromisoformat` (CPython 3.13's C parser), `isoformat`, `str`, the `strftime` patterns, pydantic's JSON spelling, `parse_event_ts`, `store_rows._dt`, `v8._as_dt`, `origin_history.instant`/`seconds`, `review.ulid_time` | `datetime.jsonl`, `datetime_edges.jsonl` |
| `unicode` | NFC/NFD/NFKC/NFKD, `str.lower`/`upper`/`casefold` (final sigma included), `unicodedata.category`/`combining`, `str.isalpha` and kin, `re`'s `\w` and `\d` | `unicode.jsonl`, `unicode_sequences.jsonl` |
| `lexicon` | `search_lexicon.tokenize`, `_tokenize_unstemmed`, `_strip_stopwords`, `fts_index_text`, `fts_match_query`, `_kebab_parts`, the diacritic fold, `tokenizer_fingerprint` | `lexicon.jsonl`, `lexicon_spelling_tables.json` |
| `log` | the row MAC (HMAC-SHA256 written on `sha2`, RFC 4231-checked), `fingerprint`, the key ring (in memory), the head and origin files, rekey rows, `verify_chain` and its report | `log_chain.jsonl`, `log_keyring.jsonl` |
| `pyvalue` | the Python values these meet (a `str` may hold a lone surrogate), `int()`, `str()`, `repr()` | through the above |
| `search` | `search.search` over records in memory: the keyword scorer, BM25 and its corpus statistics, the fusion, the rescue-expansion leg, the conversational lane, the trim, each hit's score, relevance and match terms | `search.jsonl`, `search_ranker.jsonl` |
| `records` | the record models (`models.Memory`, `TombstonedMemory`, `Episode`, `MemoryLink`, `Origin`, `Actor`) in pydantic's lax Python mode: validation from a log payload (`model_validate`) and from a row (`store_rows._row_to_memory`, `_row_to_tombstone`, `_row_to_episode`, with `_dt`, `_load_json`, `_row_origin`, `_path_targets`), the payload text (`canonical_payload` of `record_payload` / `model_dump(mode="json")`) and the column values written back, the errors (class, title, ordered `(loc, type)` pairs), the record selects on a store file | `models.jsonl`, `models_rows.jsonl`, `models_stores.jsonl` |
| `toml` | `tomllib.loads` (CPython 3.13): the same documents accepted, the same values (ints of any size, floats to the bit, the four date and time types, insertion-ordered tables), the same refusals with the same message, line and column | `toml.jsonl` |
| `config` | `config._config_from_data`, `load_config(create=False)`, the managed file's pins (`_apply_managed`, `managed.read_managed`), `config check --json`, `config show --json`, `config path --json` and the audit's config findings (`posture._settings`) | `settings.json`, `settings_python.json`, `settings_paths.json`, `settings_paths_python.json` |
| `store` | a store file read without writing to it (`connect_read_only`), its admission (`admit_store`), the opening checks and the version contract (`_open_connected`, `check_versions`), meta, every table's rows, `store_rows`' memory, tombstone and episode readers (before model validation), the log walked in seq order and `verify_chain` on it, the key ring read from a keys directory, `query_candidates` and `ranked_rows`; the fold check (`store::fold`: `store_events.refold`, `log_verify`); a new store (`store::create`: `Store.create`, its digest from `store::digest`) | `store_files.jsonl`, `store_bases.json`, `store_admission.json`, `search.jsonl`'s `fts`, `fold.jsonl`, `store_create.json` |

Two tokenizer fingerprints are available: `python_tokenizer_fingerprint()`
is the reference's digest (the one the golden header pins), and
`native_tokenizer_fingerprint()` is the same input followed by
`b"\n\0\nunidata 15.1.0"`, since this build carries its own Unicode tables.

The tokenizer's word lists (stopwords, stem exceptions, acronym plurals,
the unspaced scripts) are written out by hand in `lexicon/mod.rs`. The
reference's spelling-table text, `reference_spelling_tables.json`, is a copy
of the golden `lexicon_spelling_tables.json` that the tokenizer never reads:
it is only hashed into the fingerprint. `golden_lexicon` pins that copy to
the golden file byte for byte and each hand-written list to its entry there.

Dependencies: `sha2`, `subtle` (constant-time MAC comparison), `zeroize`
(key bytes are wiped when a `Key` drops) and `rusqlite` 0.43.0 with
`bundled`, `limits` and `hooks` (its bundled SQLite is 3.54.1; nothing
async); the tests also use `serde_json` to read the
golden files.

## Limits and key material

JSON is read and written without recursion, as is `repr`, and a value
nested deeper than `json::MAX_DEPTH` (9998 containers) is RecursionError.
That is where the reference stops when called from the top of a fresh
stack: on Python 3.13.13, `json.loads`, `json.dumps` (compact, indented,
`default=str`), `log.canonical_payload` (its outer dict included),
`repr`, and the head file, origin file and rekey payload reads under
`verify_chain` all take 9998 and raise RecursionError at 9999 (CPython's C
recursion budget, not `sys.getrecursionlimit()`). `repr` counts every
object it spells, a scalar at the bottom too, so it writes 9997 lists
around an int. `review.canonical_json` first walks nested dicts in Python
(`review._thawed`), so a chain of 1000 dicts is RecursionError there from
any caller (`sys.getrecursionlimit()`); native refuses 1000 and takes 999.
`json_edges.jsonl` pins each site's deepest depth and the next one. The
one difference left: the reference's limit is that budget less whatever
its caller's stack already used, so it can stop lower when called deep in
a stack, while the native limit is fixed. Native never rejects what the
reference accepts.

`sort_keys` sorts as Python compares keys: exactly between an int and a
float at any size, and, for fewer than 64 keys, as CPython 3.13's
`list.sort` leaves a NaN key, which compares false both ways (its run
detection changed in 3.13; `json_edges.jsonl`). Past 63 keys with a NaN
among them the order is a fixed total order, not pinned to CPython's.

Seqs are i64, as SQLite stores them; `verify_chain` counts the expected seq
in i128, so a chain that runs to `i64::MAX` reports as the reference does,
and `next_row` after `i64::MAX` is OverflowError (where SQLite refuses the
reference's insert). `log_keyring.jsonl` pins the first against reports
the reference gave, with the head and origin files, the key ring and the
rekey payloads `verify_chain` reads; `tests/robustness.rs` pins the
second, and feeds every public entry point malformed text, JSON,
numbers, head and origin files and rows under debug assertions.

`log::Key` holds key bytes. Its `Debug` prints the length and a
fingerprint prefix, never the bytes, so `KeyRing`'s derived `Debug` is safe,
and no error type carries key bytes. The bytes sit in a `Zeroizing` buffer,
wiped on drop. The store unit, when it reads key files, must keep their
bytes in such a zeroize-on-drop wrapper from the read on (`Key::new` takes
the `Vec` without copying it), not in a plain `Vec<u8>` or `String`.

## The ranker

`search::Corpus::new` tokenizes and indexes the records once (the
reference's token cache); `Corpus::search` ranks one query with
`search::Options` (mode, `max_results` with Python's slice semantics, half
life, `rrf_k`, the lane and rescue switches, `allow_empty_query`, scope and
excluded-scope filters, a corpus-stats provider callback). The reference's
admission, actor filters, claim anchoring, `hydrate`, `matched_leg_out` and
each hit's snippet and path-drift report need the store or the filesystem
and are not here: the store unit adds them around `Corpus::search`.

Scores are bit-identical to the reference: the same operations in the same
order, `f64` throughout, sums accumulated in the reference's order. Three
operations are not one IEEE-754 operation and are written out in
`search/num.rs`: `int / int` (CPython's correctly rounded true division,
which `timedelta.total_seconds`, `avgdl` and the coverage fractions use),
`round(x, 4)` (decimal rounding, half to even, of the exact binary value)
and `math.log`'s domain errors. The scores depend on the platform's libm
through three functions, the same ones CPython's `math` calls: `log` (the
Okapi IDF), `exp` (the recency decay) and `log1p` (the keyword scorer's
term-frequency residual). Rust's `f64::ln`, `exp` and `ln_1p` call the
platform's `log`, `exp` and `log1p`, so on one machine both read the same
libm; on another libm either may round differently in the last place, and
the golden files pin macOS's. `search_ranker.jsonl` is built so that no
input it hands those functions (662: 150 `log`, 507 `exp`, 5 `log1p`) lies
within 0.05 ULP of a rounding midpoint (the smallest margin is 0.0515,
recorded in its header and asserted by its generator), so any libm that
rounds correctly or nearly gives the same bits; `search.jsonl` has no such
check. The selector arm of the lane multiplies by
`0.0 * 0.7**k`, which is exactly zero for any `pow`, so `pow` is not read.

Where the port refuses what the reference might answer, it returns
`SearchError::InvalidInput`, a name the reference never raises: a NaN
half-life, a timestamp whose fields name no datetime, and candidates that
mix naive and aware `created` (or, browsing, `updated`). On that last one
an in-memory caller outside the store diverges from the reference: there
CPython raises TypeError only where its sort happens to compare a naive
with an aware value (two tied scores), and otherwise answers. A store
cannot produce such a set (`store_rows._dt` reads a naive timestamp as
UTC). Everything else, including the reference's own ValueError,
OverflowError and TypeError (a naive `now`; a record naive in one of
`created` and `updated` and aware in the other, once it scores), is
reproduced as an error of that name.

Accidents of the reference the port copies (and `gen_search_ranker.py`'s
docstring lists): a record's anchor day is the date of `created` in its own
UTC offset while the query's window is read in `now`'s; a month name's
year is `\s+\d{4}` with no word boundary after it ("may 20245" reads May
2024); the order ask `which ... (first|last)\b` has no word boundary
before first/last ("which atlast"); `\d` and `int()` read any Unicode
decimal digit; "last year" at year 1 is ValueError and the relative
periods at the ends of the calendar OverflowError, out of `search` itself;
`_scaffold_floor_stats` drops a provider's `avgdl`, so a temporal query
with a provider prices length against the pool's mean.

Timing the ranker against the reference: every query of
`search_ranker.jsonl` in its four core modes, records prepared before the
clock in both, the query's text in and the ranked hits out:

```sh
cargo test --offline --release --test bench_search -- --ignored --nocapture
../.venv/bin/python tools/bench_search.py 5
```

On an Apple M-series laptop at 10 repetitions (324 records, 208 queries in
four modes, 832 searches a pass), three runs each: the reference 918.5,
915.5 and 915.2 ms per pass, native 24.65, 25.00 and 24.56 ms (37x). The
reference's hits also carry their snippet, which native does not build.

A query's cost grows with its distinct terms times the records they
touch, with no cap, in the reference and here alike: neither bounds the
terms. Measured on 3,000 records of 30 words, a query of 74 corpus words
and N terms no record holds: the reference (Python 3.13) takes 0.70, 6.93
and 75.4 s in keyword mode and 1.19, 11.5 and 112 s in bm25 mode for N =
1,000, 10,000 and 100,000 and 1.91, 18.4 and 188 s in hybrid mode; native release 0.01, 0.06 and 0.53 s (keyword), 0.01,
0.05 and 0.46 s (bm25), 0.02, 0.11 and 1.02 s (hybrid). Upstream nothing
bounds the terms either: `memory_search` takes the query as a plain
string, and the only limit before it is the MCP SDK's 4 MiB request body
(about 400,000 such terms). What upstream does bound is the records: the
handler ranks a pool it draws from FTS5 (`handlers/search.py`,
`POOL_SIZE` 200), not the whole store. No cap is added here.

## The store, read only

`store::StoreFile::open` reads a store file as the reference's read-only
commands do (`store_schema.connect_read_only`):

- a rollback-journal file with no `-wal` is read in place with `mode=ro`;
- a file whose `-wal` and `-shm` are both there (a daemon has it open, or
  one died with it open) is read in place with `mode=ro` through the
  `-shm`: the committed frames of the `-wal` are read and nothing is
  checkpointed;
- a WAL-mode file without both, or a `-wal` without its `-shm`, is copied
  (with its `-wal`) into an owner-only directory
  `bettermemory-native-read-<pid>-...` under the temp dir, taken again when
  either file changed during the copy (three tries), and read
  `immutable=1` (or `mode=ro` with a `-wal`).

Then the file is admitted (`store::admission`, `store_admission.admit_store`
in full) before any table is read: under a 2 s deadline (a progress handler
every 100 virtual-machine steps) and with no value over 16 KiB, its
`sqlite_master` is held to the reference's own schema (built in memory from
`store/reference_schema.json`, the reference's schema texts, pinned to the
golden `store_admission.json`): a view, a trigger the reference never
writes, a virtual table, an index on an expression or with a WHERE, a
unique index on a table it writes, one of its own objects with another
text, a generated column, a default that is not a literal, a check or a
foreign key, and a table of its own not shaped as it shapes it are refused
with the reference's `StoreRefusedError` text (`LaterStoreError`, with its
key and needed revision, when meta names a later revision). Meta is then
read under the same deadline. The reference's regular expressions are
written out by hand over Python's own `\w`, `\s`, `\d` and `re.I`
equivalences. SQLite's limits after admission are its defaults, as the
reference leaves them: an admitted file's tables compute nothing, so a read
costs what the rows hold. No deadline is put on reads after admission (the
reference has none, and one would refuse a large store the reference
reads); the progress handler stays installed and idle.

What a read can change on disk: nothing, byte for byte, in the four
situations above (`store_read_only` hashes every file of the store and
keys directories before and after a full read). One recorded exception,
shared with the reference's `mode=ro` read of the same file: a `-wal` and a
stale `-shm` left by a writer that died. Both are there, so the file is
read in place, and SQLite rebuilds the wal-index in the `-shm`; the store
file and the `-wal` are unchanged (`stale_shm_is_rewritten_and_nothing_else`).

The private copy. Its directory is made 0700 under another prefix
(`bettermemory-native-new-*`), its `.lock` created 0600 and locked
(`File::lock`) there, and only then renamed to `bettermemory-native-read-*`,
so a sweeper never sees a live reader's directory unlocked
(`sweepers_never_remove_a_live_readers_directory`: 400 opens against
concurrent sweeps). A store with no `-wal` (the usual closed WAL store) is
read through a copy SQLite opens immutable (no lock, no `-shm`). On macOS
and FreeBSD the copy's name is removed before a byte of the store is
written to it and SQLite reads it as `/dev/fd/N`, so no signal at any
moment leaves the store's bytes under a name
(`a_copy_without_wal_never_has_a_name`). Not on Linux: `/dev/fd/N` there is
a symlink to `/proc/self/fd/N`, whose link text is the removed name plus
" (deleted)", and SQLite's unix VFS resolves the link before opening and
fails ("unable to open database file"; the first CI run). Linux copies the
store under a name, as the `-wal` case below and the reference do, and
removes the name after the admitted connection's first read
(`a_store_without_wal_is_read_through_a_copy`). With a `-wal` (a writer died
before checkpointing), SQLite needs both files by name (it finds the
`-wal` beside the path it opens and builds a `-shm` there), so the copy
and its `-wal` carry names from the start of the copy until the admitted
connection's first read, then both are removed and reads go on from the
open descriptors (`read_only_wal_store_with_orphan_wal`). That window is
the copy's duration plus admission (a 960 MB file copies in 0.51 s here
from the page cache, so a 200 MB store with its `-wal` is named for about
0.1 s, plus the few milliseconds admission takes). What remains after
SIGTERM, SIGINT or SIGKILL (the reader installs no handler; the drop does
not run on any of them): the directory and its empty 0600 `.lock`, and, if
the signal lands in that window, the named 0600 copy (and its `-wal` in the
`-wal` case). On macOS and FreeBSD that window exists only with a `-wal`;
on Linux it exists for every copy. Only a later native open sweeps them: it removes this user's
`bettermemory-native-read-*` directories whose lock can be taken (the
kernel releases a dead reader's lock) or that have no lock and are over an
hour old, and `bettermemory-native-new-*` directories over an hour old. The
sweep's uid is read from a directory it just made with `mkdir`, never a
name another user could take first. The reference's own `bettermemory-read-*`
directories are never touched. On Windows a directory that holds an open
file cannot be renamed (the first CI run's "Access is denied"), so the
directory is made under its swept name and locked in place, and a sweeper
counts it dead only when its lock can be taken and its lock file is over a
minute old;
names cannot be removed while open, so the named copy (and `-wal`) stays
for the reader's whole life, and a killed reader leaves the directory with
the copy until the next open sweeps it. An I/O failure on this path names
its step ("create the private directory's lock file: ...", "rename the
locked private directory: ...", "copy the store: ..."), never a path. The reference removes its copy on close, on
garbage collection and at interpreter exit (`weakref.finalize`), so Ctrl-C
cleans it; SIGTERM (no handler in the CLI) and SIGKILL leave it, and
nothing sweeps it.

Consistency. `verify_chain` runs all its statements in one read
transaction, so the rows come from one snapshot, as the reference's single
`SELECT` gives them; `read_transaction` groups more (bm-verify reads meta,
the counts and the chain in one). The head and origin files are outside
SQLite and are read as the reference reads them, so while a writer appends,
the head can be behind the snapshot (`stale`) or ahead of it
(`tail_deleted`) in both builds; no MAC, link or seq problem appears
(`verify_while_a_writer_appends`: 40 native verifies during 7,738 writes
gave 10 `ok` and 30 `stale`; the reference, 40 verifies during about 180
writes of its own GIL-bound writer, gave 35 `ok`, 2 `stale` and 3
`tail_deleted`). One order differs: the native verify reads the origin file
before the rows, the reference after; only a rekey moves that file.

The cost of that snapshot to a writer, and the batches. On a
rollback-journal store read in place, a read transaction holds SQLite's
SHARED lock, and a writer must wait for it. When this arises: every
reference writing open switches the store to WAL (`store._connect`), so
with the reference's own writers a rollback-journal store exists only
until the first writer opens it: a restored or backed-up file (`backup`
writes DELETE mode), or a store a foreign writer keeps in that mode. The
real case is that first open: it switches the file to WAL and waits for
the reader's lock to do it. Measured (`rotation_race`'s
`a_reference_writers_first_open_under_a_native_verify`, release, the intact
base in DELETE mode with 300,000 rows appended, the reference's
`Store.open` started 0.1 to 0.4 s into a native verify, 10 runs each): under
a one-snapshot verify the open took median 723 ms, worst 853 ms; under the
batched verify median 11.2 ms, worst 12.4 ms (the open's own work
included). `writer_stall` measures the other case with a native writer
held in DELETE mode, standing in for a foreign writer: one small commit
every 5 ms while verifies ran back to back for 20 s, three runs; N1b's
one-snapshot verify let it wait up to 966 ms, the batched verify median
0.32 to 0.34 ms, p99 1.9 to 2.0 ms, worst 4.6, 6.8 and 9.7 ms, longest
single read-lock hold 2.0 to 5.0 ms.

How the batches work. `verify_chain` on such a file (no private copy, not
WAL, no transaction open: `reads_in_batches`) reads the log in batches of
1,024 rows by seq, each its own statement, the chain state carried across
them (`Batched`). The first row, the highest seq (`cap`) and the rekey rows
through `cap` are read in one short transaction. The rows are then read
through `cap` and on to the log's end as it stands when a batch comes back
short, at most `EXTRA_BATCHES` (8) batches past `cap`, so a writer
appending as fast as batches are read cannot keep a pass going
(`a_fast_writer_cannot_keep_a_pass_going`: one row a batch, two rows
appended after every batch, the pass stops after the rows there were and
8 more). A `rekey` row past `cap` is a segment the pass has no key for: it
stops with `StoreError::Changed`. `StoreFile::verify_loading` is what a
command calls (`bm-verify` does): it loads meta's fingerprint and the key
ring through the caller's loader, verifies, and on `Changed` loads them
again (waiting 50 ms at a time while the ring's current key is not yet
the one meta names: a rotation renames its new key in after its commit),
and also verifies again when the head reads `tail_deleted` while the log
now holds the head's row with the head's MAC (a writer appended between
the last batch and the head read). A report that names a row, segment or
origin problem is returned at once: another pass cannot heal it
(`a_tampered_store_under_a_writer_ends_tampered`). At most 20 tries, then
`StoreError::KeptChanging`: "the store kept changing while it was checked;
nothing was found wrong; run the check again", never `tampered`;
`bm-verify` prints it as chain status `kept_changing` and exits 3 (0 ok, 1
a log that does not verify, 2 a failure). The time bound: one pass over
300,000 rows measured 0.83 to 0.90 s, so 20 tries with their 50 ms waits
come to about 18 s on that size in the worst case, with the restore lock
held throughout (a restore is held off that long). A quiet store takes one
pass.
WAL files, copies and a caller's transaction keep one snapshot and run
once, as the reference.

What the batches guarantee, and what not. Every row read is the row the
log held when its batch was read. Under writers that only append (every
writer this package runs), the rows read are a prefix of the log as of the
end of the pass, so a MAC, link or seq problem is named only where one
snapshot at the end of the pass would name it, and on a quiescent store
the report is the one-snapshot report (`golden_store_files` verifies every
case both ways at 1, 2, 3, 7 and 1,024 rows a batch and compares). Not
guaranteed: the rows are not one snapshot. A row changed or deleted in
place, by a writer that is not this package's, after its batch was read is
not seen; one changed before is reported as it is read. The end of a pass
does not read `cap` or the rows behind it again: that would be a second
pass, and only the head-row check above is cheap enough to make.

A rotation during a batched verify (`rotation_race`, release, 300,000
rows, the reference's `rotate_key(apply=True)` started 0 to 1 s into the
verify, 30 runs each): retried with the same ring, as N1b did, 23 of 30
were `tampered` (`foreign_segment`, `final_key_mismatch`); through
`verify_loading`, 30 of 30 `ok`.

A finding about the reference, not changed here: its `log verify` reads
the head file after the rows, so under a concurrent rotation or a writer
on a DELETE-mode store it reports `tampered` with head `tail_deleted` (an
independent review measured 6 of 20 under rotation and 20 of 20 under a
5 ms writer).

The restore lock (`store::restore_lock`, `restore_lock.reading`). An open
takes it before SQLite opens the file and holds it until the `StoreFile`
drops: on POSIX the parent of the store path as given (not resolved)
locked shared with `flock` (`File::try_lock_shared`), tried every 50 ms
for 2 s; held exclusively past that (a restore), the open is refused with
`StoreError::RestoreRunning` (`RestoreRunningError`, "<store> is not
opened: a restore is running; try again when it has finished", exit 75
from the reference's read-only commands). A restore's marker with no lock
held refuses no read. Nothing is created. A directory `flock` cannot lock
falls to the record file `<store>.restore-lock` (checked, never created,
never held). The path as given decides the lock, as in the reference: a
relative path, one through `..` and a linked directory reach the store's
directory, but a link to the store file from another directory locks that
other directory, so a restore of the real store does not see the read
(pinned as the reference does it). Windows: the record file locked and
let go, then the store file held open without delete sharing and the
record checked again behind it; compiled only under `cfg(windows)` and
untested. Known open item: std's `try_lock` on Windows locks the whole
record file (a mandatory `LockFileEx` lock), where the reference locks one
byte at 1 MiB so the marker stays readable; while a native read checks,
a reference restore's marker read or write can fail with a lock
violation. A byte-range lock is out of reach without `unsafe` or a crate
outside `Cargo.lock` (no `windows-sys` there); the hold is kept as short
as the reference's (taken, then let go at once). Not ported: the
reference's pass for the thread that holds the lock itself (no native
restore exists), its one-time log line and process-wide memory of
record-mode directories. Pinned by `store_restore_lock.json`: 4 plain
cases, 3 record-mode cases, 4 path cases, a restore meeting a read, the
wait, poll and exit status. The fixture is POSIX only: on Windows its generator is
not run (`POSIX_ONLY` in `tests/contract/generate.py`, named in the
manifest), `golden_restore_lock.rs` is not compiled, and
`restore_lock_not_posix.rs` records the gap as a class (os) case. The
Windows lock is exercised there only by the tests that open a store.

A finding about the reference, shared by both builds (to be filed): a
read through a symbolic link to the store file from another directory
locks the link's directory, not the store's, so a restore of the real
store does not see that read.

The native verify reads the origin file before the rows, the reference
after (only a rekey moves that file). The native key ring reader reads a
key file over 1 MiB as unreadable.

"Not found" instead of waiting: `StoreFile::open` canonicalizes the path
and checks `is_file` before it takes the restore lock, so a read that lands
in the gap of a restore's swap fails with io NotFound instead of waiting or
being refused (seen once in 23,095 native reads). Left as is: the
reference has the same order (`store.py`: `is_file` before `reading`).

The record file is opened read-write without creating it, as the
reference opens it `O_RDWR`: where it is readable but not writable both
builds read that as nothing to hold. (Opened read-only, as the first
version of this port did, the native build would still check the lock
there: stricter than the reference.)

The fold check is ported (N1e, below); validated records are N1d's.

Readers. `meta`, every table's rows (`each_row`), `store_rows`' memory,
tombstone and episode readers (`MemoryRow`, `EpisodeRow`), the log walked
in seq order, `verify_chain`, `history_replaced` (the `log verify --json`
field), and `query_candidates` with its scope, client, model and cap
filters, and `ranked_rows`. What the reader accepts that the reference's
models would reject (pydantic validation is not ported): an `id` that is
not a ULID; an empty scope list or scopes the scope rules refuse; more
than 64 entries in `verified_paths`, `verified_commits`,
`verified_versions`, `verified_absent_paths` or `claims`, or entries that
are not strings (the reference also turns each into `str`); a
`confidence` or `source` outside its enum; a `category` outside its enum;
`corroborations` below 0; list columns holding JSON that is not a list (an
error here, a ValidationError there); and it does not read `links`,
`origin`, `actor`, the path targets or `last_corroborated`'s model.

`store::keys::load_key_ring` reads the fixed names only, into buffers sized
from the file's length and wiped on drop. Two differences, neither able to
change a report: a file over 1 MiB is read as unreadable, and retired keys
are ordered by the part of the name between the id and `.key` rather than
by whole file name (`ab` and `ab.c` order differently); the ring's order is
only read to find the one key a row verifies under, and two distinct keys
never verify one MAC (`retired_key_order_cannot_change_a_report`).

The fixtures. `store_bases.json` holds five base stores (intact; rekeyed;
restored, with a `store_restored` row; accepted, with a `history_accepted`
row; both rows appended directly through `log.append_history_replaced` with
neutral `/fixture/...` paths, since a real restore or accept-history signs
the host's resolved paths into the row, and the rewind, the moved-aside file
and the head and origin rewrites are not reproduced;
the FTS corpus) as the SQL that rebuilds each and its keys directory's
files, written once through the reference by `python -m
tests.contract.gen_store_files --bases`, which also proves each rebuilt
store answers as its original. `store_files.jsonl` holds 40 cases (a base
and a recipe: SQL, a key file removed or written, a truncation) with the
reference's answers: in full for the five bases, by sha256 digest of the
rows, meta, `sqlite_master` and records for the tamper and version
variants, the exact refusal for the 18 admission cases, and what the
reference raises for the four schema-text cases; the counts are literals in
the test, so a line removed fails it. The Rust tests
rebuild each case and compare with no Python; each read runs under a 10 s
bound on its own thread. `tests/test_contract_golden.py` regenerates the
answers from the committed bases through the reference, so a change in how
the reference reads a store shows there. Not pinned by the bases: how the
reference writes a store (the bases are written once).

The SQLite version: the reference runs 3.50.4; the crate bundles 3.54.1.
`sqlite_spike` rebuilds `search.jsonl`'s FTS5 table: all 27 queries give
the same rows, order and `bm25()` bits. Every case of `store_files.jsonl`
(rebuilt by 3.54.1 here, by 3.50.4 for the reference) reads the same. And
one file is kept as bytes: `store_real.sqlite` (95,744 bytes, page size 512,
VACUUMed, WAL mode, the intact records and the FTS corpus), written by the
reference's SQLite 3.50.4 (`--real`; the header's writer version is checked)
and read as it is by `the_reference_written_file_reads_the_same`: all 1,793
answers equal, the 32 FTS prefilter queries' rows and `bm25()` bits and the
FTS shadow tables' schema rows included. Its bytes are not reproducible, so
`generate` compares a logical dump of it, clock-minted values masked, with a
fresh build.

Schema text that is not text. A `sqlite_master` value that is not UTF-8 is
never decoded lossily (a replaced name would match no table, and the
per-table checks would see nothing, a generated column included): the
reader refuses the file, carrying the `OperationalError` the reference's
fetch raises out of `connect_read_only` (`Could not decode to UTF-8 column
'name' with text ...`). A type, name or tbl_name that is not TEXT, or a sql
that is neither TEXT nor NULL, is refused with its own wording. An accident
of the reference the port does not copy: a BLOB sql makes `_blanked`
(`src/bettermemory/store_admission.py:113-115`) raise `TypeError: cannot
use a string pattern on a bytes-like object`; `store_files.jsonl`'s header
lists it under `deliberate_differences`. Four cases pin these: a name not
UTF-8, one hiding a generated column, a comment not UTF-8, a BLOB sql.

An accident of the reference the port copies: `query_candidates` orders
by `bm25()` alone (`ORDER BY score ASC LIMIT ?`), so equal scores come in
whatever order the plan gives; `ranked_rows` breaks ties by rowid.

`examples/bm-verify.rs`: `bm-verify <store file> <keys dir>` prints one JSON
object: the admission's refusal (its reason names schema objects only) or
the version contract's outcome, each table's row count, the key ring's
shape, the chain report and each phase's milliseconds, from one snapshot.
No record content, payload text or key byte is printed.

`bm-verify --fold <store file> <keys dir>` runs `log verify` instead of
the chain alone and prints its merged report under `log_verify` in the
reference's full shape less `path` (`status`, `store_id`,
`key_fingerprint`, `rows`, `segments`, `head`, `origin`, `problems` sorted
by seq, `fold`, `unaccounted_ids`) and its milliseconds under
`ms.log_verify`; its exit status is then the merged report's, or 3 with
status `kept_changing` when key rotations kept the log moving (below). A chain
problem without a `seq` raises KeyError in the merge, as in the reference.
The fold's labels are record ids and table keys, and a `payload_invalid`
detail is the reference's exception text, which can quote a payload value.

Timing the chain verification: 50,001 log rows of about 650 signed bytes,
written by the reference, three runs each, Apple M-series: the reference's
`verify_chain` 205.8, 205.3 and 203.1 ms (about 244,000 rows a second);
native release (sha2 0.11.1, which uses the aarch64 SHA instructions) 142.8,
148.6 and 141.6 ms (about 347,000 rows a second), 1.4 times the reference.
The prediction of at least ten times is missed: both are bound by
HMAC-SHA256 on the CPU's SHA instructions (the reference's `hmac` is
OpenSSL's), and walking the rows costs about 29 ms of the native time.

## The fold check

`store::fold` (`StoreFile::refold`, `StoreFile::log_verify_loading`, `fold::merge`)
reproduces `store_events.refold` and `log_verify`, read only. The mutation
rows of the log (`MUTATION_KINDS`) are replayed into a SQLite database in
memory built from the reference's own `SCHEMA` and `MEMBERSHIP_SCHEMA`
(the texts `store/reference_schema.json` pins), less the FTS5 table and
its three triggers, which the fold never compares; the
`memory_links_cleanup` trigger, the keys and the unique index on
verifications stay, so SQLite's affinities, conflicts and cascades act as
in the reference. Each kind is applied as `store_rows._apply_mutation`
applies it: the same statements, the values Python would bind in the same
order (a `str()` of a payload value, `int()`, `list()`, `payload.get`'s raw
value), so a binding error is the one Python's sqlite3 raises at the same
parameter. What the reference catches (ValueError and its subclasses
JSONDecodeError and UnicodeEncodeError, KeyError, TypeError,
ValidationError) is a `payload_invalid` problem with the first line of
`str(exc)` cut to 200 characters: `json`'s decode messages
(`json::decode_failure`, CPython 3.13's C scanner, trailing-comma messages
included), the int digit limit, `int()`'s and `list()`'s messages, the
UnicodeEncodeError of a lone surrogate bound as text, pydantic's
`N validation error(s) for <Model>` line, the messages of
`_membership_values`, `_membership_key` and `validate_path_targets`. What
is applied before a row raises stays applied (the reference has no
savepoint per row). Anything else is `FoldError::Raised` with the class
the reference lets out of `refold` (AttributeError, ProgrammingError,
OverflowError, IntegrityError, RecursionError, OperationalError for text
that is not UTF-8 in a live table ...).

The comparison reads each folded and membership table on both sides over
the columns the reference's schema gives it, keyed by `_TABLE_KEYS`, as a
Python dict holds them: an int equals a float of the same value, text never
equals a blob, two rows with one key keep the first key's spelling and the
last row's values (`rows` counts keys, not rows). `unaccounted` and
`missing` are sorted by `str()` of the label, stably. The index text
columns are compared only while the signed log names the reference's
tokenizer (`python_tokenizer_fingerprint`), and are spelled once per row
the replay leaves rather than once per put (the same text: nothing else
reads them).

Not ported: `_unaccounted_memory_ids`, the in-process memo the reference's
`Store` keeps of the last fold's unaccounted ids; nothing native keeps
state between calls. `native_text.mutation_payload` is not needed: it
shapes a payload before it is signed, and the fold reads what was signed.

Deep JSON: the reference parses a payload inside `refold`, several frames
below the top of its stack, so a payload nested 9,996 to 9,999 deep raises
RecursionError out of `refold` there (macOS arm64, CPython 3.13; the window
is lower on Windows, whose default stack is smaller, and moves with the
caller's depth), while native parses up to 9,998 and applies the row. At
20,000 both sides agree: the reference's C scanner hits its own recursion
guard and raises RecursionError, and native's JSON load stops at its
depth limit with `LoadError::Recursion`, which the fold lets out as
`FoldError::Raised("RecursionError")`: the same class out of `refold`. No case pins the window; it is a
platform fact, not a contract.

Memory: the log is read and applied in batches of `FOLD_BATCH` (4,096) rows,
parsed on up to 8 threads, and a payload's text is not kept beside its
parsed value (it is read again only to spell a decode error). The live and
folded tables are compared by a SHA-256 of each row's values in their key
form, so neither side's rows are held twice. A single payload is still held
whole while it is parsed: a single 50 MB payload still peaks at about 1.8
GB (the reviewer's measurement).

### A writer during the fold

`log verify` must describe one state of the log: the chain report and the
fold read the same rows, and an honest writer (one that appends signed
rows and applies them in the same transaction, as the reference's writers
do) never makes it read anything but `ok`.

One read, two consumers. Each log row is read once and the same row is
fed to the chain verifier and to the fold's replay (`fold::OneRead`, a
`LogSource` whose `each` hands every row to `verify_chain_from`'s visit and
then to the replay).

- A rollback-journal store read in place, where a read transaction holds
  the shared lock that blocks every commit: `first`, the rekey rows and the
  log's last seq are read in one short read, then the rows in batches of
  1,024, each its own statement, on to that seq. There the batched phase
  follows a writer: it reads the log's last seq again and goes on in
  batches to it while more than a batch was appended, at most
  `FOLLOW_ROUNDS` (4) times. Then one read transaction is begun and held
  for the rest: the rows after the last batch, the head file (read after
  the rows, as in the reference), the log's row count and first seq, the
  live tables. The tables' shape (`sqlite_master`, each
  table's `PRAGMA table_info`) is read just before it and read again in it
  only if `PRAGMA schema_version` moved. The rows read in it are applied
  after it ends. The hold is bounded by reading the live tables and the
  row count plus the rows appended since the last follow (fewer than a
  batch unless the writer outruns the reader four times). `bm-verify
  --fold` prints the longest hold under `ms.longest_hold`.
- A WAL store, a private copy or a caller's transaction: the same pass,
  the load included, in one read transaction. The head file is outside
  SQLite: if it names a row past the snapshot that the log now holds and
  the report is otherwise clean, the pass runs again.
- A private copy cannot be read forward. A closed WAL store is read as a
  copy; when a writer opens the store after the copy was taken and moves
  the head file, the head names a row the copy lacks. That is no
  tampering: when the report's only complaint is that head, and the store
  or its `-wal` is no longer what was copied (or a writer's `-shm` is
  there now), the check ends "kept changing" (`StoreError::KeptChanging`,
  `bm-verify` exit 3) and the store opened again reads the writer's rows
  in place. A copy whose source nobody wrote keeps its report: a tail
  removed from a closed store reads `tampered`. (Found on the Linux
  runner, CI run 43797351206: one verify in eight read `tampered` when it
  copied the closed store just before the test's writer made its first
  commit. The reference's read-only commands copy a closed store the same
  way and read the head file later, so they can report this as tampered.)

What the report describes, in plain words:

- WAL (and a copy, or a caller's transaction): the store as it was when
  the pass began. An edit made during the pass, of any kind, is not seen
  by this pass and is seen by the next.
- Rollback journal: the log as it was read, batch by batch, and the tables
  as they are at the end. A row appended during the pass is read in its
  batch or in the snapshot and needs no second pass. An edit that touches
  a table row, edits a log row not yet read, or removes or inserts log rows
  (anywhere: the row count and first seq read in the snapshot must be the
  rows the pass visited) reads `tampered` in that pass. A log payload
  edited behind the read position, with the tables left alone, is not seen
  by this pass (its MAC was checked on the bytes read before the edit, and
  the replay used the same bytes): this pass reads `ok`, the next reads
  `tampered`. `fold_writer::an_edit_during_a_pass` pins each case. A key rotation met during the pass (a
rekey row past the rekey read) loads the ring again, at most `LOAD_TRIES`
times, then `StoreError::KeptChanging` (`bm-verify` exit 3); nothing else
reruns a pass.

A not-ok report is a one-snapshot fact. The batches assume the log only
grows. Before a report that is not `ok`, or a raise (from loading the
ring, from `reads_in_batches` or from the pass), is returned,
`PRAGMA data_version` is compared with its value before the pass; if
another connection committed meanwhile, or the snapshot's row count or
first seq is not what the pass visited (whatever the report), the whole
pass runs once more inside one read transaction and that report is
returned. The count costs about 46 ms under the lock on the 310,008-row
store. That costs a
writer one stall, and only when something looks wrong. What may rewrite rows under a read: a restore
replaces the file, and a native read holds the restore lock for the life of
its `StoreFile`, so none can run mid-pass; `log.append_history_replaced`
only appends (one signed control row in its own write transaction); nothing
in `src/bettermemory` updates or deletes a log row. So no honest operation
rewrites a log row under a read.

Finding about the reference: `log_verify` reads the chain (`verify_chain`)
and then the fold (`refold`, which reads the log and each live table) in
separate statements with no transaction around them, so a commit between
them can make it report a row the chain has not seen, or a table row whose
log row it did not replay, as unaccounted. Not changed.

Measured (`fold_writer.rs`, the populated case): 8 verifies per test under
a writer committing every 2 ms (1 row, and 20 rows a transaction), in WAL
and rollback-journal mode, every one `ok`; a planted row under the same
writer, 5 of 5 `tampered` in both modes; 7 edits made from the pass's hook
after its first batch, in both modes (`an_edit_during_a_pass`, above). The
file (5 tests) runs in 6.5 to 7.8 s in a debug build, and no assertion
depends on the machine's speed.
Ignored timings: with 20 rows a transaction back to back, 8 verifies all
`ok` (WAL 0.6 s while 12,340 rows were written; rollback journal 1.6 s,
20,000 rows; release). A writer's commit latency on a rollback-journal
store while verifies run back to back
(`writer_latency_under_native_log_verify`, release), on a 310,008-row store
of the first shape below: one commit every 5 ms, 3 verifies (all `ok`) in
23.9 s while 2,747 commits ran, median 0.50 ms, p99 5.3 ms, worst 621 ms;
commits back to back, 3 verifies (all `ok`) in 37.0 s while 48,816 commits
ran, median 0.40 ms, p99 3.0 ms, worst 715 ms, longest hold 695 ms. With no
writer the longest hold is 392 to 396 ms, nearly all of it the live tables
(310,000 memories) read in the snapshot.

The differential (`native/tools/fold_differential.py`, a tool, not a
test): seeded random stores built by the reference from the three bases
(populated, plus every kind, plus drift), edited behind the log or given a
fuzzed signed payload, each run by the reference's `log_verify()` and the
release `bm-verify --fold` on the same file and compared field for field.
Edits that would put a REAL into a TEXT-affinity column are skipped (the two
SQLites spell it differently). Seed 1, 600 stores: 244 edited, 341 fuzzed,
48 edits skipped, 600 the same, 0 different.

Findings about the reference, reproduced and pinned in `fold.jsonl`, not
changed: (1) a row planted as an exact copy of a `memory_links` row whose
`note` is NULL is not reported (SQLite lets the key repeat since NULLs are
distinct; the dict collapses the two), case `duplicate_null_note_link`;
(2) a store whose `checkouts` lacks the optional `unresolved_json` column
(one an earlier build created, read before its owner's open adds it) reads
every checkout row as unaccounted, since the live tuples are one column
shorter than the replay's, case `optional_column_absent`; (3) payloads the
reference does not catch (a `conflict_put` whose `conflict` is not an
object, a list bound as a column, an int past 64 bits, a NULL in a NOT NULL
column, `int()` of infinity) make `refold` and `log verify` raise instead of
reporting `payload_invalid`, cases `raised_*`; (4) a planted row whose key
is NULL or a blob is labelled by that value (`null_key_planted`,
`blob_key`), and labels that print alike sort in the order the rows were
read (`keys_that_print_alike`).

`fold.jsonl` (`tests/contract/gen_fold.py`): 163 cases, each the
reference's `refold()` and `log_verify()` (`path` left out) on a store the
native test rebuilds: the 40 cases of `store_files.jsonl` (17 refused at
open, compared as a refusal), `store_real.sqlite`, and the intact base
with a prelude from the header (signed rows appended with the base's key,
the table rows the reference's `_apply_mutation` writes for them, the head
file) and a recipe: `populated` (every folded and membership table filled),
66 planted, edited and deleted rows (three per table), 18 hostile or odd
live tables (index text alone, text not UTF-8, a blob, NUL, a huge value,
a blob key, a NULL key, a duplicated row, keys that print alike, one and
two membership tables absent, the optional column absent, an extra
column, a missing column, memories emptied), 4 tokenizer-drift cases, 41
signed rows the fold reports as `payload_invalid` in one case, 6 cases a
payload makes the reference raise, an empty log after `store_created` and
no log at all; and `every_kind`, a prelude that applies every kind in
`MUTATION_KINDS` at least once (the generator asserts it): every delete
kind, membership deletes and membership re-puts over their own key, a re-put
replacing a memory's links, a delete then tombstone then restore of one id,
a verification with a NULL machine replaced, a checkout without
`unresolved_json`, an int standing probability, a tombstone whose
corroborations are a string and links null, a memory with a filename,
verification lists given as a dict and as a string, and re-puts over the
same key in conflicts, imports and quarantine; 22 cases edit one table each
on it, and one adds drift. The generator asserts that every `*_edited`
case reports something its unedited base does not, except the blind spot
`drift_index_text_edited` (the index text is not compared under drift), and
that no recipe's SQL can make a float (a REAL literal, a division, a
function that gives a REAL), since SQLite spells a REAL in a TEXT column by
its own version's rules; `real_for_integer` is exempt (an INTEGER column
keeps 1.5 a REAL). Preludes are shared by name in the header, long ASCII
runs are built from a count, and a table with nothing unaccounted or missing
is written as its row count alone. `golden_fold.rs` compares every case field for field
(`fold_every_case`) and feeds hostile payloads (deep JSON, 2 MB strings,
NUL, lone surrogates, numbers past every range, NaN and infinities) and
tables (text not UTF-8, blobs, NULL and blob keys, extra and dropped
columns, a dropped table, an emptied or altered log) to `refold`, which
must give a report or a typed error (`hostile_inputs_never_panic`).

Timing and memory, `/usr/bin/time -l`, Apple M-series, release, reference
`log_verify()` on `connect_read_only` against native `bm-verify --fold`
(two runs each, WAL and rollback-journal mode alike), on two stores built
through the reference's `_apply_mutation`:

| store | rows | mutation rows | payload bytes | reference | native | ratio | peak RSS reference | native |
|---|---|---|---|---|---|---|---|---|
| puts | 310,008 | 310,007 (memory puts; a third re-put an earlier id) | 264,011,832 | 31.1 s | 7.46 to 7.54 s | 0.24 | 1.82 GB | 0.96 GB |
| mixed | 46,008 | 9,207 (the rest telemetry `search` rows) | 16,211,432 | 1,103 ms | 327 to 332 ms | 0.30 | 164 MB | 80 MB |

Before batching, native peaked at 4.1 to 4.6 GB on a store of the first
shape. The reviewer's measurements on other stores gave ratios of 0.42 and
0.10 (the owner's snapshot). The fold parses the payloads on up to 8 cores
and spells the index text on every core; replaying them is one transaction
on prepared statements, in seq order.

## The search pool, by caller

`store::pool::PoolReader` draws the pool a caller's request ranks from a
store file opened read only, as `handlers.search.resolve_search_pool`
draws it, and `SearchPool::rank` ranks it as `_rank_pool` does
(`search.search` over the pool, priced with the pool's statistics, the
configured half-life and lane, no rescue leg):

- `PoolReader::new(&StoreFile)`; `resolve_search_pool(query, &Filters,
  size, digest_fingerprint) -> Result<SearchPool, PoolError>`: the first
  `size` rows the request admits (`POOL_SIZE` 200) in the full-text index's
  order for the query (`bm25()`, then row number), or newest first for a
  query empty after `str.strip`, each read into the record model as
  `light_many` reads it;
- `Filters` (scopes, excluded scopes, admission, client, model):
  `search.candidate_admitted` over each row's admission inputs (scopes, the
  project its `homes` row names, the declared writer);
- `SearchPool::statistics(terms)`: `Store.ranking_statistics` (the admitted
  collection's size, each term's document frequency in the body and in the
  body or scopes, the admitted rows' average BM25 length from the digest);
- `SearchPool::rank(query, now, mode, max_results, stats_seen)`;
  `clamp_search_width`, `MAX_SEARCH_RESULTS`.

The caller arrives resolved, as data: `pool::Admission` holds what the
reference's `admission.Admission` answers for one store's rows (each
project's admission, and a global record's by its scope list); membership,
`location.resolve_caller` and git are not ported. Anchoring is not read:
in the reference it reaches only each hit's drift report. Not here: each
hit's signals (staleness, drift, claim checks), relations, `expand_top`,
`since_prior_session`, the search's log row, the response.

What differs, by design of a read-only port (`pool.rs`'s module docs
have the whole list). The average BM25 length comes from the digest, and
any write leaves the digest behind the log: the reference's next search
brings it current (`digest_mark(build=False)`, a write) and reads its
lengths, and this port brings the same rows forward in memory over the
same log rows (each mutated id's entry rewritten from its memory row, or
dropped), writing nothing. Where `digest_mark` answers None (no digest
state, another fingerprint, a payload or record that does not read) there
is no average here either. The reference answers None for a digest
behind also when SQLite itself cannot write the store (a read-only file,
a busy lock); a `Store.open` that the key ring marks read-only still
catches the digest up and writes it. This port answers as a reference
whose SQLite can write. The reference pays the catch-up once and writes
it; this port, which never writes the digest, pays it once per
`PoolReader` (each reader keeps what it brought forward, keyed by the
memory head). Its cost is linear in the unseen rows
(`catching_up_is_linear_in_the_unseen_rows`); a payload of 20 MB in an
unseen row costs about 300 MB while it is parsed. An unseen row's id is
read through `str()` for a string, an int, null, a bool or a float, and
its payload from TEXT or from UTF-8 bytes (a leading BOM dropped, as
`json.loads` does for bytes; a TEXT payload with a BOM and bytes that are
not UTF-8 give no average, as there). Not ported, giving no average where
the reference answers: an id that is a list or an object, and a payload
in UTF-16 or UTF-32 bytes. The
digest fingerprint (`store_digest.fingerprint`, which reads the
supersession rules) is passed in, not computed; with none the digest is
never trusted. The reader keeps the rows' admission inputs keyed by the
memory head and that fingerprint, and reads each term's hits from the
index on every call. The reference reads pool records with
`seed_tokens=index_spelled_here()` (token streams split from `body_fts`
where the log's tokenizer stamp is this build's); this port tokenizes the
body, which agrees wherever `body_fts` is the stamped tokenizer's
spelling of the body, as on every row the reference writes. `rank`'s
half-life (30 days) and lane (on) are constants because
`config.FIXED_SETTINGS` fixes both. A scope list holding anything but
strings is refused (TypeError); `int()` of a digest row's number or
length is Python's (a REAL length truncates, a TEXT row number that is no
integer is ValueError), past 64 bits OverflowError.

The fixture. `search_store.jsonl` (generator `gen_store_path`) holds the
schema every store here shares and, per store, its tables' rows (the
tables a search reads, every other table empty, the values the clock and
the key mint give masked, the log cut to the rows the digest's mark and
catch-up read; the generator checks each copy holds the same memory rows
(row number, body, updated, scopes, writer) in the same newest-first order,
keeps each mutation's record id, ranks its queries, reads
the same admission inputs and digest rows, finds the digest in the same
state and names the same mutations after its mark, as the store did), the
SQL a case applies after the rows, each caller's admission as data, and
the digest fingerprint. Floats are recorded as IEEE-754 bits. The stores:

- the main one (18 records): 81 run lines of 7 runs (offline and three
  callers on two surfaces) and 36 `hostile` lines of 2 (FTS5 operators
  and column filters, quotes, NUL, punctuation, stopwords only, empty,
  whitespace, 46,000 and 20,000 characters; the reference raised on none);
- `big` (236 records, 222 active matches for one query, bodies repeating
  every 12 records for exact `bm25()` ties, `updated` repeating every 4,
  tombstones, one and two scopes, five writer shapes including client
  `""`, records homed in two projects, `global` and nowhere): 51 lines
  over scope, excluded-scope, client and model filters and their
  combinations, an empty scope list, pool sizes and widths (200/10,
  50/5, 5/10, 1/1, 300/50, 20/10) and callers including the write
  review's surface (`review_strata`), whose admission tells a global
  record's scope lists apart;
- 19 small stores whose digest is current, one, three or fifteen writes
  behind, behind by an update, a tombstone or a restore, under another
  fingerprint (alone and one write behind), gone, or holds a REAL length
  or a TEXT row number, or is behind by one unseen row whose id is null,
  a float or true, or whose payload is UTF-8 bytes (with and without a
  BOM), bytes that are not UTF-8, or text with a BOM: 4 lines each. The reference raises ValueError for
  the TEXT row number and reads the REAL length as `int(3.5)`.

`golden_search_store` rebuilds each store, opens it and compares 639 runs
on the main store and 127 store lines (pool, every statistics call, hits),
and holds the 81 offline runs to `search.jsonl`'s in-memory ranking.
SQLite: the reference draws the pool with its SQLite (3.50.4 here, 3.53.1
on the runners), the native build with 3.54.1; the pool's order is
`bm25()` then row number, and FTS5's scores were bit-equal across those
versions on every query so far (`sqlite_spike`, `store_real`).

Timing the pool and its ranking: every main-store run line's query and
mode, offline and for each caller's admission (567 calls a pass), store
opened once:

```sh
cargo test --offline --release --test golden_search_store -- --ignored --nocapture
../.venv/bin/python tools/bench_search_store.py 20
```

On an Apple M-series laptop at 20 passes, three runs each: the reference
63.58, 63.32 and 63.35 ms a pass (median), native 18.31, 18.35 and 18.22
ms (about 3.5 times). The prediction of at most a fifth is missed. The
two sides do not do the same work: the reference also runs
`search.search`'s second filter pass (`_filter_candidates`, with
`admission.prime`) and builds a snippet per hit, and keeps its token
cache and each term's document frequencies warm between calls, while
native builds its ranker's corpus and reads each term's hits from the
index on every call. Per call native spends about 8 microseconds in the
FTS5 match, 11 reading and validating the pool's records, 5 building the
corpus, 5 in the statistics' index reads and 5 ranking.

## The settings

`toml::loads` is `Lib/tomllib/_parser.py` rule for rule, over the bytes of
the `&str` (every syntax character is ASCII, so positions and slices agree
with the reference's code-point indexes; a message's column is counted in
code points). Three outcomes are not `TOMLDecodeError` in the reference and
are kept apart (`toml::Error`): an integer of more than 4300 decimal digits
is `int()`'s ValueError, which `tomllib` does not wrap, so the product
reports it without its `malformed config in <path>:` prefix; and nesting
past the stack is RecursionError, which the product does not catch at all
(`config check` and `config show` stop with a traceback; here
`Err(Raised { class: "RecursionError", .. })`). The reference's depth limit
is its stack: from a fresh interpreter's top it takes 497 nested arrays and
331 nested inline tables (two and three Python frames a level) and fails
past them, and fewer when called deeper. Here a level costs the same two
or three and more than 995 is refused, which is the top-of-stack limit
exactly for either kind alone; a mixed nesting near the limit, or the
reference called deep in a stack, can differ (the corpus keeps its deep
documents at 50 and 150 levels and at 400 and more). The parse runs on a
thread with a 16 MiB stack, so an unoptimised build fits the deepest
document the budget admits on any caller's thread; what it returns is at
most 497 levels deep, is dropped without recursion, and is walked by the
reports within that depth (`config_robustness` runs check, show, path and
the audit over values at the budget on a 2 MiB thread). A hostile document costs
linear time (`config_robustness`: a 45 MB document of a million keys, a
6-million-item array and a 10 MB string loads in 4.5 s unoptimised; 2,000
integers of 4300 digits in 1.1 s; a million `[` in 1.3 ms).

`config` takes everything a load reads as data: the environment is a map
(`config::Env`), every file and path operation goes through `config::Fs`
(the tests hand in the generator's world; `report::SystemFs`, over the
real file system, is not for use yet, below), and the platform's locations (`config::Locations`: the
user config file, the managed file or registry value, the default keys and
state directories, the system prefixes) are inputs. The loader's one-shot
notices (`_DEPRECATED_KEY_WARNED_PATHS`) are a `config::Guards` the caller
holds. The fixture records each command as a CLI process of its own runs
it, with the set cleared before each of check, show and the audit, and the
tests give each command a fresh `Guards`; a caller that runs several
commands in one process and holds one `Guards` across them gets the
reference's in-process behaviour (a notice logged once is not logged
again). A path built from text (a location variable, `[storage]
directory`) is spelled as `str(Path(text))` spells it (`config::pure_path`:
`PurePath._parse_path` and `_format_parsed_parts` over `posixpath` and
`ntpath.splitroot`, compared under both flavours with CPython's spelling of
800 texts in `settings_python.json`'s `pure_paths`), and a relative one is read
against the working directory. The flavour is the world's
(`Locations::flavour`), never the platform the crate was built for, so the
tests' POSIX world reads the same on every runner and a Windows caller
passes `Flavour::Windows`. A NUL in a path that is stat'd or resolved
raises as `os.lstat` does (`config::nul_in_path`), and a first segment
`~name` that does not expand raises `Path.expanduser()`'s RuntimeError
(`config::no_home`): `config path` stops on it, check finds nothing
(`_system_dir_findings` catches it) and show never reaches it. The test
world has one user, so a `~name` that does expand is not covered. Its file
system has a root (`/`, and `//`, which `Path` keeps as two), and the
fixture holds no case that resolves above `<scratch>`: the reference prints
the host's own directory there, and `gen_config._within_scratch` refuses a
case whose output names any place outside `<scratch>` but the root. The
recorded config files are the text the reference read: the scrubber sees
only what the commands printed. The three
commands return `(text, exit code)`, the text being `json.dumps(document,
indent=2, ensure_ascii=True, default=str)` and a line feed; `Err` is an
exception the reference lets escape. The difflib guess (`_close`) is
`SequenceMatcher` with autojunk, ported (`config::difflib`), and the
`utf-8-sig` decode reproduces CPython's `UnicodeDecodeError` text, its
position counted after a byte-order mark.

The settings fixtures are split by how far their bytes hold.
`settings.json` (ungated) holds what reads the same under every Python the
suite runs and on every platform: the catalogue, the fixed settings, the
coercion table, `config path` in the generator's own environment, and the
cases worded by bettermemory alone (each regenerated identically under
3.11.15, 3.12.13 and 3.13.13 here; 3.14 and Windows were not run, and the
reason to expect the same there is that nothing in them is the
interpreter's or the OS's wording, and the paths they print are under the
run's directory, which the scrubber rewrites). `settings_python.json`
(Python-gated) holds the cases whose text is the interpreter's (a `tomllib`
or codec message, `int()`'s digit limit, a float conversion, `repr`,
RecursionError, bare NaN) and `pure_paths` (3.11's pathlib spells 307 of
the 800 otherwise). `settings_paths.json` (POSIX only: not generated on
Windows, read by `cfg(unix)` tests, recorded as class "os" by
`settings_paths_not_posix.rs` elsewhere) holds the `config path`
environments and the cases whose directory is expanded and resolved;
`settings_paths_python.json` (both) holds a NUL in a path, whose ValueError
3.11 words otherwise. `toml.jsonl` is Python-gated: its 927 documents read
the same under 3.11.15 and 3.12.13 (only the header's `python` differs),
though their `tomllib` sources differ from 3.13.13's by 14 lines.

Accidents of the reference reproduced and pinned (the settings fixtures):

- `[daemon] port = inf` (any integer setting read from an infinite float)
  is an OverflowError that escapes `_parse_one`'s `except ValueError`:
  `config check` and `config show` stop with a traceback (case
  `inf_port`). `port = nan` is a ValueError without the `malformed config
  in <path>:` prefix, so check's finding reads "cannot convert float NaN to
  integer; the config does not load." (case `nan_port`).
- An integer past 4300 digits in either file is reported un-prefixed, from
  `int()` (cases `int_past_the_digit_limit*`).
- The managed loader's refusal of a top-level value prints it with `repr`
  (`datetime.datetime(1979, 5, 27, 7, 32, tzinfo=...)`, `[{'enabled':
  False}]`), where every other message spells values as TOML.
- A NUL in `[storage] directory` (either file) makes `config check` stop
  with `ValueError: lstat: embedded null character in path` from
  `Path.resolve()` (`_system_dir_findings` catches OSError and
  RuntimeError only), and `config path` too wherever the directory chooses
  the store; `config show` and the audit answer (cases `nul_*`,
  `directory_*_nul`). A location variable cannot hold a NUL (`os.environ`
  refuses one, as `execve` would), so no case sets one; natively the same
  error is raised.
- `config show --json` prints a non-finite float a file holds bare, as
  `Infinity`, `-Infinity` or `NaN` (`json.dumps` with `allow_nan`), which
  is not JSON (case `non_finite_floats`).
- Configs with 494 to 497 nested arrays, or 329 to 331 nested inline
  tables, make the reference CLI stop with a RecursionError (its entry
  point runs `tomllib` a few frames down the stack; measured through
  `cli.main` in a fresh interpreter), where native loads them: the budget
  is `tomllib`'s at the top of a stack, which `toml.jsonl` pins.
- `config check` reads the managed file through `read_config_file`, so its
  `unreadable` finding says `malformed config in`, while the load (`show`'s
  error, check's `load_error`) says `malformed managed config in`.

Not ported: reading the Windows registry value (`ManagedSource::Registry`
takes its text, or its error, from the caller; no crate here reads the
registry), Cygwin's and MSYS2's planted-file refusal (`_refuse_planted`),
`load_config`'s first-run write, the text (non-`--json`) renderings, and
argument parsing. `report::SystemFs` is not for use until a later unit
compares it with `pathlib` on a real tree (a symlinked home, `..` through
a symlink, `~user`, relative paths, a config path that is a directory, an
unreadable file, prefixes that differ in case): its `resolve` pops `..`
after canonicalising, `expanduser` takes only `~` and `~/`, `under` is
case-sensitive and `is_dir` follows symlinks. No entry point uses it by
default; every compared case goes through the test's world.

## Running the tests (offline)

```sh
cd native
export CARGO_NET_OFFLINE=true CARGO_TARGET_DIR="$PWD/target"
cargo build --offline
cargo test --offline
cargo test --offline --release
cargo clippy --offline --all-targets -- -D warnings
```

Each golden test checks every case of its file and fails if the number of
cases it checked is zero or differs from the file's. The test
`unicode_tables_regen` runs the table generator in check mode, under
`BM_REFERENCE_PYTHON` or the repository's `.venv/bin/python`;
no store test needs it.

Timing the tokenizer against the reference: both take every input of
`lexicon.jsonl` as text and return its tokens (`tokenize_str` on a `&str`;
the inputs that hold a lone surrogate, which no `&str` can, as code points
through `tokenize`), in one process, repeated:

```sh
cargo test --offline --release --test bench_tokenize -- --ignored --nocapture
../.venv/bin/python tools/bench_tokenize.py 50
```

On an Apple M-series laptop at 200 repetitions: the reference 7.22 ms per
repetition, native 0.91 ms (7.9x). The 10x target is not met; what remains
is mostly one `String` per output token and the NFKC pass over non-ASCII
text.

## Parity on every platform

The committed golden files were made on one machine: darwin-arm64, Python
3.13.13, SQLite 3.50.4, Unicode 15.1.0 (`tests/contract/generate.py`'s
pins). Three things tie some of their values to that machine. Ranking
score bits go through the platform's `log`, `exp` and `log1p` and its
double arithmetic, in Python and in SQLite's `bm25()` (10 of
`search_ranker.jsonl`'s 208 queries score other bits on Linux x86-64); a
CPython patch release can change what `python_edges` pins (3.13.18 reads
three sub-second-offset texts otherwise than 3.13.13); and the port itself
carries Unicode 15.1's tables and CPython 3.13.13's datetime parser, sort
and nesting budget. So the native build is held to the reference on the
platform it runs on, against fixtures made there.

**`BM_GOLDEN_DIR`.** Every test reads its golden files through
`tests/common`'s `golden_path`, which reads the directory `BM_GOLDEN_DIR`
names, else the committed one. Two files are also compiled into the crate
(`include_str!`): `src/lexicon/reference_spelling_tables.json` and
`src/store/reference_schema.json`. They stay compiled from the committed
text: a test pins each copy to the committed file always, and then
compares it with the directory under test, where a difference fails unless
that file's gate is open (below). Both are the reference's source strings,
not anything a platform computes, so a set made anywhere under the pinned
Unicode holds the same text.

**The as-found set.** `python -m tests.contract.generate --out DIR
--as-found` runs every generator under whatever Python, SQLite, Unicode and
platform is present and writes the whole set into DIR with `platform.json`:
the platform key (`sys.platform`-`machine`, as `PLATFORM` spells it), the
machine, the libc where Python knows it, the Python, SQLite and Unicode
versions, the committed set's pins, each generator's files or the error it
raised (a generator that raises is recorded and the run goes on, exit 1),
the gates under which it differs from the committed files, and for each
file its size, SHA-256, whether it is byte-identical to the committed one
and, if not, how many lines differ and where (the `_differing` detail of
`tests/test_contract_golden.py`, now `tests/contract/_diff.py`).
`store_real.sqlite` is a store file SQLite 3.50.4 wrote once; it cannot be
written again byte for byte, so every generator run, the as-found one
included, copies the committed file and records in `store_real.json` its
SHA-256, its writer (3.50.4, always) and its answers as the running
Python's SQLite reads them. The native test checks the writer is 3.50.4 in
every set (the first CI run wrongly expected the manifest's SQLite, 3.53.1
on the runners) and reads the file against those answers; its PARITY line
names both SQLites. On this Mac the as-found set is byte-identical to the
committed one.

**Four classes.** A test's class is that of the golden file it compares
with, by the generator that writes it, mirroring the Python side's
`*_GATED` sets (`tests/common/parity.rs`, `gates_of`):

- (i) platform- and version-independent: compared strictly, always.
- (ii) tied by design to the Python patch release (3.13.13) or the Unicode
  (15.1.0) the port reproduces: strict where the files were made under
  those; under another, a differing case is set aside.
- (iii) floating-point score bits: strict where the files were made on this
  platform; elsewhere, a differing case is set aside. (These files are also
  Unicode-gated.)
- (os) an expectation the operating system cannot meet, whatever made the
  files: on Windows the private copy keeps its name while it is open (an
  open file cannot be removed there) and has no mode bits. The test checks
  what does hold there, and the case is named and counted as set aside in
  both steps. No golden file has this class; only the two private-copy
  tests in `store_read_only.rs` record it.

SQLite is no gate: the native build links its own (3.54.1), so the
reference's SQLite version is recorded, and its effects compared strictly.

**Nothing is hidden.** Where a test's gates are all shut (the files were
made here, under the port's versions) a difference fails as it always did.
Where one is open, every case is still compared: an equal case counts as
compared, a differing one prints `NOT COMPARABLE HERE` with the case, its
difference and the reason, and counts as set aside. Every gated test prints
one `PARITY` line (`N compared, M set aside (why)`) and appends it as JSON
to `BM_PARITY_LOG` when set. A gate opens only on the files' provenance:
the committed set is darwin-arm64/3.13.13/15.1.0, an as-found set says what
it is in `platform.json`, and `BM_PLATFORM_KEY` makes both sides report
another platform (for trying the gates locally, nothing else).
`unicode_tables_regen` under a `BM_REFERENCE_PYTHON` of another Unicode is
set aside the same way, but only when the files under test were made under
that Unicode too.

| test (file: function) | golden file | class |
| --- | --- | --- |
| `golden_canonical_json`: `canonical_json_every_case` | `canonical_json.jsonl` | i |
| `golden_datetime`: `datetime_every_case` | `datetime.jsonl` | i |
| `golden_log_chain`: `log_chain_every_row_and_variant` | `log_chain.jsonl` | i |
| `golden_log_keyring`: `log_keyring_every_case` | `log_keyring.jsonl` | i |
| `golden_search`: `search_ranker_tables` | `search_ranker.jsonl` header (word lists) | i |
| `parity_provenance`: both tests | `generate.py`, the manifest | i |
| `golden_toml`: `toml_every_document` | `toml.jsonl` | ii (Python) |
| `golden_settings`: `settings_catalogue_and_fixed`, `settings_coercion`, `settings_cases`, `settings_config_path` | `settings.json` | i |
| `golden_settings`: `settings_python_cases`, `settings_pure_paths` | `settings_python.json` | ii (Python) |
| `golden_settings`: `settings_paths` (unix) | `settings_paths.json` | i; os elsewhere (`settings_paths_not_posix`) |
| `golden_settings`: `settings_paths_python` (unix) | `settings_paths_python.json` | ii (Python); os elsewhere |
| `config_robustness` (6) | none | i |
| `robustness` (5), `search_robustness` (8), `store_robustness` (3), `store_read_only` (11), the library's unit tests | none, or `store_bases.json` / `store_files.jsonl` as input only | i |
| `golden_datetime_edges`: `datetime_edges_every_case` | `datetime_edges.jsonl` | ii (Python) |
| `golden_json_edges`: `json_edges_every_case` | `json_edges.jsonl` | ii (Python) |
| `golden_lexicon`: `lexicon_every_case`, `lexicon_fingerprint` | `lexicon.jsonl` | ii (Unicode) |
| `golden_fold`: `fold_every_case` | `fold.jsonl` | ii (Unicode, pydantic-core) |
| `golden_fold`: `hostile_inputs_never_panic` | `fold.jsonl` as input only | i |
| `fold_writer` | `fold.jsonl`'s `populated` prelude as input only | i |
| `golden_store_create`: `a_native_create_equals_the_references`, `every_refusal_is_the_references`, `stale_sidecars_end_as_the_references` | `store_create.json` | ii (Unicode); modes: os on Windows |
| `golden_store_create`: `the_pinned_digest_fingerprint_is_the_committed_fixtures` | `store_create.json` as committed | i |
| `store_create_cross`, `store_create_kills` | `store_create.json` as input; the reference Python (set aside, class i, when `BM_REFERENCE_PYTHON` cannot import `bettermemory`) | i |
| `store_create_robustness` (12) | `store_create.json` as input only | i |
| `golden_lexicon`: `spelling_tables_match_the_golden_file_and_the_rust_tables` | `lexicon_spelling_tables.json` (compiled-in copy pinned to the committed file) | ii (Unicode) |
| `golden_unicode`: `unicode_every_part`, `unicode_sequences_every_record` | `unicode.jsonl`, `unicode_sequences.jsonl` | ii (Unicode) |
| `unicode_tables_regen`: `regenerating_the_unicode_tables_gives_the_same_bytes` | `src/unicode/tables.rs` | ii (Unicode) |
| `golden_search`: `search_every_query_every_mode` | `search.jsonl` | iii |
| `golden_search`: `search_ranker_every_query_every_mode` | `search_ranker.jsonl` | iii |
| `sqlite_spike`: `fts5_prefilter_matches_the_reference_sqlite` | `search.jsonl` (bm25 bits) | iii |
| `golden_search_store`: `every_query_every_caller_pool_and_hits`, `every_store_line`, `offline_runs_equal_the_in_memory_ranking` (and three class (i) checks: the clamp, the digest's fingerprint, awkward queries) | `search_store.jsonl`, `search.jsonl` | iii |
| `golden_store_files`: `every_case_every_answer` | `store_files.jsonl` | iii |
| `golden_store_files`: `the_reference_written_file_reads_the_same` | `store_real.json`, `store_real.sqlite` | iii |
| `golden_store_files`: `the_crates_schema_texts_are_the_references` | `store_admission.json` (compiled-in copy pinned to the committed file) | iii (by its generator; its text is platform-independent) |
| `bench_search`, `bench_tokenize`, `search_differential_files`, `cases_from_env`, `answers_of_env_store`, `time_reads_of_env_store` | files an environment variable names | tools, `#[ignore]`d, not part of parity |

The pinned literal counts (`CASES`, `RANKER_*`, `datetime_edges`' 32/189/105
and 77 per-platform cells, the 4 FTS shadow rows, 32 FTS rows) count cases
the generators enumerate from fixed inputs, so a set regenerated anywhere
has the same counts. Under a Python minor other than 3.13 the version-gated
files' headers say so (`"python"`), and the tests read it from the manifest.

**The CI job** `native parity (<os>)` (`.github/workflows/ci.yml`) runs
`tools/parity.py --lint` on ubuntu-latest, macos-latest and windows-latest,
and the same command runs it here:

```sh
uv run python native/tools/parity.py --lint     # what CI runs
```

It lints (`cargo build --locked`, `clippy --all-targets -D warnings`, `fmt
--check`, all under the toolchain `rust-toolchain.toml` pins, 1.94.1);
step 1 runs `cargo test --locked --no-fail-fast` (every test binary runs,
whatever fails) against the committed files;
step 2 regenerates the set as found with the running Python and runs
`cargo test --locked --no-fail-fast` with `BM_GOLDEN_DIR` at it and `BM_REFERENCE_PYTHON`
at that Python: the parity claim on that platform. It prints the manifest
and a per-test table (compared, set aside, why) to the log and to
`$GITHUB_STEP_SUMMARY`, and fails on any strict difference, a generator that
raised, a class (i) or (iii) case step 2 set aside, or a step 2 set not made
on this platform. A class (ii) difference under a Python or Unicode other
than the port's, and a class (os) case, is printed and counted, and does
not fail. The job is not
a release gate (release.yml skips it).

## Regenerating the Unicode tables

`crates/bettermemory-core/src/unicode/tables.rs` is generated from the
reference Python's own Unicode data (3.13, Unicode 15.1.0) and committed:

```sh
../.venv/bin/python tools/gen_unicode_tables.py          # write
../.venv/bin/python tools/gen_unicode_tables.py --check  # compare, exit 1 on a difference
```

The generator refuses any other Unicode version. Its output depends only on
that data, so the same interpreter writes the same bytes.

## The store's writes: the reference's order (N3a design)

Read from `src/bettermemory` at 3cfa9f76; `create` is now ported
("Creating a store", below), the rest is not. Line numbers are that
commit's. Checked against the code and by running the reference: the
random draws are the store id, the key, then the membership id (meta),
then the digest build's token (`store_digest._build`, never a final
byte); the clock is read three times (meta's `created`, the row's `ts`,
the head's `ts`).

`Store.create` (`store.py:576-652`):

1. `store_path`; refuse an existing path (`FileExistsError`, :589).
2. `prepare_store_dir` (`store_keys.py:125`): refuse a dangling keys-dir
   link; make the store's directory owner-only.
3. `orphans_beside` (:592).
4. `_connect` (`store.py:370-411`): restore-lock admit; `sqlite3.connect`
   (creates the file, 5 s busy timeout, autocommit); `admit_store`;
   `PRAGMA page_size = PAGE_SIZE`; `foreign_keys = ON`; `journal_mode =
   WAL` with `schema_version` in one script; `synchronous = NORMAL`;
   chmod 0600 on the file, `-wal`, `-shm`; restore-lock confirm.
5. `executescript` of `SCHEMA`, `MEMBERSHIP_SCHEMA`, `ADDITIVE_SCHEMA`,
   `PROPOSALS_INDEX_SCHEMA`, `SESSION_INDEX_SCHEMA`, each its own implicit
   commit (:595-599).
6. store id (`secrets.token_hex(16)`), `KeyRing.create_key`
   (`log.py:530`: keys dir made owner-only, 32 random bytes,
   `atomic_write_bytes` 0600 before rename: tmp, fsync, `os.replace`,
   dir fsync). The key file exists before any meta row does.
7. One transaction (`_transaction`, `store.py:1177`): `BEGIN IMMEDIATE`,
   `require_current_writer`, meta rows, `_append` of `store_created`
   (`log.append_row`, `log.py:875`: last row read, seq, ts from the clock,
   canonical payload, MAC, INSERT), `COMMIT`.
8. `_move_head` after the commit (`store.py:1353`; `write_head`,
   `log.py:638`: `flock_excl` on `<id>.head.lock`, forward only, JSON with
   its own clock `ts`, atomic write 0600). An `OSError` is logged, not
   raised.
9. `_record_origin` (`KeyRing.write_origin`), then `_claim_key_ring`
   (`<id>.path`), then chmod 0600, then `digest_mark()` (a digest build:
   its own transactions on `digest_*` tables, no log row).

`_mutate` (`store.py:1390`), inside the caller's `_transaction`:
`prepare_mutation` (`store_native.py`), `store_returned.before`, `_append`
(meta's key read again under the lock, `_signing_key`), `_apply_mutation`
(the fold's function), the `returned` candidates noted; at the owner's
end `_flush_returns` appends `returned_put`/`returned_delete` rows before
`COMMIT`; then the head moves. A failure anywhere up to and including
`COMMIT` rolls back (`_roll_back`); the head moves only after a commit.

What a kill leaves at each boundary, by that order (read, not yet run):

| killed after | file | keys dir | `log verify` | next `create` / `open` |
| --- | --- | --- | --- | --- |
| create 4-5 | schema, no meta | nothing or dir | open fails: no store id | `FileExistsError`; open `ValueError` |
| create 6 | schema, no meta | an orphan key | as above | as above, by hand only |
| create 7 (commit) | whole | key, no head | `unverifiable` (head absent) | opens; the next write writes the head |
| create 8 | whole | key, head | `ok`, no origin | opens; writing open settles origin, binds |
| write before COMMIT | as before | as before | as before | opens |
| write after COMMIT, before head | with the row | head one behind | `ok`, head `stale` | the next write moves the head |

Findings about the reference (the first confirmed by running it, the
kill rows not run against the reference): a refused create leaves
such a file too: a keys directory that is a file, or one it cannot
write, fails at step 6 with `store/memory.sqlite` (schema, no meta)
already there (`store_create.json`, `keys_dir_is_a_file`). A create killed between steps 4 and 7 leaves a file neither
`create` nor `open` will take, recoverable only by removing it (no
memory is lost: there are none yet); a create killed between its commit
and its head write reads `unverifiable` until a write. A writing open
does not check or repair the head; `write_head` only moves forward, so a
head ahead of the log stays `tail_deleted` until the log passes it.

Not ported yet, and needed for a write equal to the reference's: the
writing open's steps (`store_open._open_connected`: `check_versions`,
`_add_membership_tables`, `key_state` with the binding claim,
`_close_foreign_tail`, `_settle_origin`, `restore_rows.settle`, and the
decorators `keeps_digest` and `reconciles_tokenizer`), `store_returned`'s
marks, `_relink` (which reads `provenance_for`) and the digest's
record entries (a create indexes none).

## Creating a store

`store::create::create(path, keys_dir, provider) -> Result<Created,
CreateError>` is `Store.create(path, keys_dir=keys_dir)`: `path` names the
store file or its directory (`store_path`); every path is the caller's (no
`~`, no default keys directory). `Provider` gives what the reference draws
at random or reads from the clock, in its order (`store_id`, `key`,
`membership_store_id`, `now` three times) and the engine version; tests pass
`create::Fixed`. `create::System` is the operating system's, on every
platform: the clock, and random bytes from the `getrandom` crate (0.4.3,
default features off; no feature is needed for these targets), which calls
`getentropy(2)` on macOS, the `getrandom(2)` system call on Linux (falling
back to `/dev/urandom`, after polling `/dev/random`, only on a kernel
without it) and `ProcessPrng` (bcryptprimitives.dll) on Windows 10 and
later. Key bytes are drawn into a wiping buffer that moves into the `Key`
uncopied. SQLite's `randomblob()` is not used.

`CreateError`: `Exists` (anything at the store file's path, a dangling
link included), `KeysDirUnreadable` (the reference's text), `Io { step }`,
`Sqlite`, `RestoreRunning`, `RestoreUnfinished` (an unfinished restore's
marker, `<store>.restore-lock` not blank, lies beside the store: the
reference's `RestoreLockedError` text, as `restore_lock.admit` refuses it
before its create opens the file), `Provider` (a store id not 32 lowercase
hex);
`class()` names the reference's exception. Every refusal leaves nothing at
the store path; the store's directory may have been made.

What it writes equals the reference under the same values
(`store_create.json`): the five schema scripts (`admission::schema_texts`)
and `store::digest::DIGEST_SCHEMA`, `_connect`'s pragmas in its order, meta
in its order (with `created_after_orphans` when orphans lie beside it),
the `store_created` row (`log::next_row`), `digest_state` (`fingerprint`,
`seq` 0, `mac` empty; `digest::DIGEST_FINGERPRINT` is pinned, not
computed, since it reads the supersession rules), the key, head, origin and
binding files with their lock files, modes 0700/0600. The reference's
digest fingerprint changes whenever the supersession rules, the tokenizer
or the digest's shape change; `the_pinned_digest_fingerprint_is_the_committed_fixtures`
then fails and says to port the change and set the constant. `schema_version`
(63) matches too. `store::digest::bm25_length` is the one record length,
used by `pool`'s catch-up.

The order, and what a kill after each step leaves (`store_create_kills`:
104 kills, each of the 14 boundaries ended twice by `abort` and twice by
`exit`, every run with stale junk `-wal`, `-shm` and `-journal` at the
store's name, plus 48 kills from outside at fixed pseudo-random delays; every outcome checked
by the native verify, a fresh create after every no-store kill at a
boundary and after the first random kill at each, and the resulting stores
by the reference's `log_verify` in one child process):

| after | store dir | keys dir | outcome |
| --- | --- | --- | --- |
| directory made | nothing | nothing | no store; create again |
| temp locked | `<temp>.lock` | nothing | no store |
| connected, pragmas | `<temp>`, `-wal`, `-shm`, lock | nothing | no store |
| five schema scripts | as above | nothing (a kill mid-write: `<id>.key.<n>.tmp`) | no store |
| key written | as above | `<id>.key` | no store; an orphan key |
| meta, row, digest committed | as above | key | no store |
| head written | as above | key, head(.lock) | no store |
| origin written | as above | key, head, origin(.lock) | no store |
| connection closed, synced | `<temp>`, lock, stale sidecars | as above | no store |
| stale sidecars removed | `<temp>`, lock | as above | no store |
| linked to `memory.sqlite` (no clobber) | store, `<temp>`, lock | as above | store, verifies ok |
| temp name removed | store, lock | as above | store, verifies ok |
| binding written | store, lock | all seven files | store, verifies ok |
| lock removed | store | all seven files | done |

A failed create removes every key ring file it made (its fresh id's key,
head, origin, binding and their lock files: nothing else can own them).
Key files and staging files are written create-new (staged, then linked;
never renamed over a name), and a create refuses (`KeyRingExists`,
FileExistsError) where any of `<id>.key`, `.head`, `.origin`, `.path`
already exists; the reference's `create_key` would replace a key there,
which a random id never meets. A kill can still leave an orphan
`<id>.key`, `.head`, `.origin` (and lock files) for a store that never came
to exist: clutter holding a key for nothing. Key files are never swept on a
guess; this unit's own staging names in the keys directory
(`<id>.<key|head|origin|path>.<pid>x<n>.tmp`, exactly) are removed by a
later create once over an hour old.

Stale sidecars. With no file at the store's name, a `-wal`, `-shm` or
`-journal` there is an orphan (another store's live WAL, a hot journal,
junk). The reference's create ends with only `memory.sqlite` and its new
store (SQLite, creating the file there, discards them); a native build
under a temporary name never met them, and SQLite would replay a valid
foreign WAL onto the linked file at its first open (the reference then
reads the old store's id: `unverifiable`). So native removes each, right
after looking again that no file is at the store's name, before the link
(`store_create.json`'s `created_over_sidecars`: another store's live WAL,
junk, a hot journal; the end state is the reference's). The window left: a
creator that makes `memory.sqlite` and its `-wal` between the look and the
unlink (microseconds) loses its `-wal` name; only a reference create racing
at the same path can, its WAL then holds only that create's schema, which
it still checkpoints through its open handle, and the native link then
fails `Exists`. Two creates racing at one path is already undefined for the
reference itself. The sidecars are cleared twice: once as a boundary of its own,
and again immediately before the link call, with nothing between but the
link. What remains: a file placed at those names between that last clear
and the link, a few instructions apart, by something that is not a store
creator. A sidecar seen after the link belongs to an opener of the new
store and is left. `DanglingStoreLink` comes before the store's directory
is made or its mode tightened; `class()` names that refusal where the
reference raises nothing.

A later create in the same directory removes the temporary files of a
create whose lock it can take (`sweep_temps`, as `sweep_private_copies`
decides by its lock file), before anything else, even before refusing an
existing store: a kill between the link and the temporary name's removal
leaves a second name of the live store, which goes then. The store-file
name a link made is never removed. The sweep opens only a regular file as
a lock, takes it, checks it is still the file at its name, and removes it
while holding it; a create that opened the same file finds, once locked,
that it is no longer at its name and takes another. Liveness rests on an
`flock` held for the create's whole life; where locks are emulated per
process (NFS on Linux) two creates in one process do not exclude each
other.

Other differences, named: a dangling symbolic link at the store's name is
refused (`DanglingStoreLink`, its own words) where the reference follows it
and creates the store at the link's target; the reference's OSError texts
(`[Errno 20] Not a directory: '...'`) are not reproduced: native errors
carry the step and the OS error, with the same class and errno. `Fixed`
(fixed values, hidden from docs) fails with a typed error when its clock
holds no reading. `store_create.json` has no SQLite gate: its FTS5 shadow
rows, `schema_version` and page bookkeeping depend on SQLite, 3.50.4 and
3.54.1 agree, and a SQLite that differs fails the golden test loudly on
purpose: then compare the two SQLites' FTS5 and schema changes and decide
whether the native build follows; do not gate the file. Beside the reference: a kill before the link leaves no
file at the store path where the reference's (after its connect, before
its commit) leaves one neither build opens; a kill between commit and head
cannot leave a head-less store (`unverifiable`): the head is written before
the link. An orphan key, head and origin for an id no store has are left as
the reference leaves an orphan key. A store linked before its binding is
written opens and verifies; the reference's writing open binds it. Where
the file system has no hard links (FAT, some network shares), `rename`
is the only move and it replaces an existing file on every platform
(`rename(2)`; on Windows `std` documents a replacing move), so a
look-then-rename could overwrite a store another process made in between.
Instead the name is claimed with an exclusive create of an empty
placeholder (failing when anything is there) and only that placeholder is
renamed over. Left undecided: a kill between the claim and the rename
leaves an empty `memory.sqlite` that neither build opens, as the
reference's own window does: the one boundary, on such file systems only,
where native is no better than the reference. Durability: files and the directory are
synced (`sync_all`, which on macOS is `F_FULLFSYNC`, stronger and slower
than the reference's `fsync`).

## Not built yet

The store's writes past `create` (the writing open, mutations, schema
steps, the digest's ingest), membership and
the caller's resolution, and each hit's snippet, signals and drift report,
the daemon, the MCP tools and the hooks; reading and writing the key ring's
files on disk (the ring here holds their bytes); `datetime.astimezone` of a
naive value (the reference reads it as the machine's local time).

## Records and their errors

`records` reproduces what pydantic does with the inputs the reference's
record models meet, not pydantic in general: `str` (bytes decoded as UTF-8,
else `string_unicode`), `int` (bools, integral floats inside +-2**63, text
of an integer optionally followed by `.0...`, trimmed of White_Space, at
most 4300 bytes), `bool` (0/1, 0.0/1.0, the twelve words in any ASCII
case), `datetime` (speedate's RFC 3339 grammar with `T`, `t`, space or `_`,
`.`/`,` fractions truncated past six digits, `Z`/`z`/`+-HH[:]MM` below 24
hours; unix timestamps as numbers or text, past 2e10 read as milliseconds,
including speedate's second division for text floats and pydantic-core's
rounding of a float's fraction; year 0 is `datetime_parsing`, any other text
failure `datetime_from_date_parsing`), the `str` enums (bytes accepted),
lists, dicts (a key holding a lone surrogate comes back with each surrogate
as three U+FFFD, in the record and in the error location, as pydantic-core
rebuilds it), nested models, and the validators `models` adds.

A refusal is `RecordError::Validation { title, errors }`: the model's class
name and each error's location and pydantic's `type`, in pydantic's order;
never pydantic's message text. Anything else the reference raises on the
way (a row reader's `AttributeError`, `JSONDecodeError`, `IndexError` for
a missing column ...) is `RecordError::Raised(class name)`.

Validation never recurses: a deep value is matched one level at a time
and never cloned (`tests/records_robustness.rs`).

Two reference findings the port copies (filed, not changed here): a
`links_json` column holding a JSON object or string fails the whole read
as MemoryLink `model_type` (it iterates the keys or characters), and
`_path_targets` lets a numeric `attestation_payload` raise TypeError out
of the read (it catches only ValueError and RecursionError).
