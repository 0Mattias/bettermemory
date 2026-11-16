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

Two crates. `crates/bettermemory-sys` holds the OS calls the standard
library does not offer safely, one function so far (`fsync`: `fsync(2)`
through `libc`, tried again on EINTR, as `os.fsync`); it is the only place
`unsafe` may appear, each use one call on a borrowed standard-library
type. The library crate, `crates/bettermemory-core` (`#![forbid(unsafe_code)]`):

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
| `gitconfig` | the text of one git config file as `gitconfig.py` reads it: `git_entries` and `_value` (the lexer and parser), `_spelled`, the NUL cut of `_parse_config`, `spaced` and the `KEEPS_SPACES_SINCE` rule, `git_int`, `git_bool` | `gitconfig.jsonl` |
| `gitconfig::wildmatch` | git's `wildmatch` with `WM_PATHNAME` (and `WM_CASEFOLD`) as `gitconfig.py` ports it, its step and depth budgets, `_any_match`, `_literal_head` | `gitconfig_wildmatch.jsonl` |
| `gitconfig::remote_rules` | where git's reader of remotes dies on a variable: `_remote_rule` with its worded reasons, `refname_valid`, `refspec_valid` | `gitconfig_remote_rules.jsonl` |
| `githead` | HEAD and the refs its chain names, from a git directory's files, as `githead.py`'s resolution half reads them: `head_bytes`, `_parse`, `_lookup`, `_packed`, `_resolve`, `head_sha`, `head_ref`, `head_unborn`, `packed_names`, `head_commit` up to the object read; the file system is a trait (`RefFs`: `read`, `lstat_is_dir`) with `StdFs` and `MemFs` | `githead.jsonl`, `githead_posix.jsonl` |
| `gitfile_rule` | unit 1 of the rule a `.git` entry is followed by: `_parse_gitfile` (both flavours of `os.path`), `_gitfile_target`, `_small_text`, `_headref_content`, `_judge_format` and `object_format` (the git on PATH as data), `path_gone`/`path_missing`, the bounded reads with the time bound as data | `gitfile_leaves.jsonl`, `gitfile_leaves_posix.jsonl` |
| `config` | `config._config_from_data`, `load_config(create=False)`, the managed file's pins (`_apply_managed`, `managed.read_managed`), `config check --json`, `config show --json`, `config path --json` and the audit's config findings (`posture._settings`) | `settings.json`, `settings_python.json`, `settings_paths.json`, `settings_paths_python.json` |
| `store` | a store file read without writing to it (`connect_read_only`), its admission (`admit_store`), the opening checks and the version contract (`_open_connected`, `check_versions`), meta, every table's rows, `store_rows`' memory, tombstone and episode readers (before model validation), the log walked in seq order and `verify_chain` on it, the key ring read from a keys directory, `query_candidates` and `ranked_rows`; the fold check (`store::fold`: `store_events.refold`, `log_verify`); a new store (`store::create`: `Store.create`, its digest from `store::digest`); an existing store opened for writing (`store::open`: `Store.open(allow_rekey=True)` in its plain states, the digest's record entries, ingest and build); what a resolved caller is admitted to see (`store::caller`) | `store_files.jsonl`, `store_bases.json`, `store_admission.json`, `search.jsonl`'s `fts`, `fold.jsonl`, `store_create.json`, `store_open.jsonl`, `caller_admission.jsonl` |
| `printable`, `membership_lines` | `_printable.printable`, `quoted`; `membership_lines`' `folder_said`, `run_in`, `_named`, `_answer`, L1 to L10, `line_unresolved` and the lines without a number (all but `line_restated`) | `membership_lines.jsonl`, `membership_lines_unicode.jsonl` |
| `store::mint` | the text of a record id as `models.generate_ulid` mints it from its milliseconds and ten random bytes, `int(time.time() * 1000)` and its 48-bit mask, `is_valid_ulid` and the milliseconds `review.ulid_time` reads | `ulid.jsonl` |

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

## What a caller is admitted to see

`store::caller` reproduces, as pure functions over data, what the
reference's `admission.Admission` answers for a caller its resolution
describes: `membership_records.relation` and `admitted` (design 3.5's
table: `IN_PROJECT_POLICY`, `OUTSIDE_POLICY`, the seven `SURFACES`, the
write review's `review_strata` among them), `Admission.nothing`,
`project_home`, `admits_project`, `admits_row`, `admitted_projects`,
`admits_global`, and the membership reads they make (`canonical`,
`chain_of`, `named`, `project`, `projects`).

- `Resolution::from_json(text)` / `from_value`: the dataclass as
  `dataclasses.asdict` writes it (a field missing or of another type is a
  `ResolutionError`).
- `Membership::read(&Connection)` (a `StoreFile`'s or a pool reader's
  connection; it only reads), `Membership::from_projects(rows)`.
- `Admission::new(surface, Option<&Resolution>, &Membership)`, its
  answers (`Result<_, ModelError>`), and `pool_admission(&Connection) ->
  pool::Admission`: the data form the native search takes, computed for
  every project a `homes` row or the projects table names and `global`,
  and every scope list of a memory or a live digest row, so a recorded
  resolution drives `PoolReader::resolve_search_pool`. The data form stays
  as it was.

Inputs. Of the resolution: `outcome`, `project`, `chain`, `tenant` (the
other fields are parsed and not read). Of the store: the `projects`
table's `id`, `name`, `merged_into`, `provisional`, `parent`, `tenant`
(the read selects all nine columns, so a missing one or text that is not
UTF-8 in any raises), and the `project` rows of `native_membership_rows`
(`key_text`, `row_text`), which replace the legacy row their key names.
Nothing else: no `homes` row is read for an answer (a record's recorded
project is the question's input), no checkout, view or witness.

The rule in plain words. A caller whose resolution raised is admitted
nothing; one whose outcome is `indeterminate` or `deferred`, or that
names no project, says so in `nothing`. A record's home is its recorded
project with merges followed (`canonical`; a falsy result falls back to
the recorded id), that project's chain (itself and its parents, each
parent's merges followed, nearest first) and its own row's tenant; it is
`unresolved` while that row's `provisional` is `indeterminate` or
`deferred`. The relation: `unknown` for a caller with no project;
`global_named` for a `global` record whose `projects:<name>` scopes
(case-folded) name a project on the caller's chain (or the caller's
project when its chain is empty) and the caller is not outside any
project, else `global`; `other_tenant` when the home's tenant and the
caller's differ (empty reads as none); `other_project` for every project
record when the caller is outside any project; `same_project` when the
home's project is on the caller's chain; `sub_project` when the caller's
project is on the home's chain; `other_project` otherwise. The surface
admits the relation by the in-project or the outside policy, except that
an unresolved home is admitted only as `same_project`, and `unknown`
never. A record with no home row is admitted nowhere.

Special cases, each a fixture table: merges and merge chains (a merge
into a missing row, into `global`, into `""`); parents and nested
sub-projects (a parent merged away, a missing parent); boundaries and
tenants (an empty tenant); provisional and unresolved projects (merged
into, and below, an unresolved one); names folded (`ß`, final sigma) and
names held only by a native row; cycles in `merged_into` and `parent` (a
cycle stops where it would revisit a project) and 40-long chains; native
rows that replace, add, and malformed ones that still remove the legacy
row they key; a missing native table, a native table missing a column,
a missing projects table or column; values of other types in a table
with no column affinity (Python's `==`, truthiness and `str()` decide);
a blob id or mixed number and text ids beside a native row (TypeError);
text that is not UTF-8 (OperationalError).

A native row's JSON held as bytes is read as `json.loads` reads bytes:
`json.detect_encoding` (the UTF-32, UTF-16 and UTF-8 BOMs, then the NUL
pattern of the first four bytes, or of two), then the decode with
``surrogatepass`` (a surrogate's UTF-8 spelling and a lone UTF-16 or
UTF-32 surrogate are kept); bytes that do not decode (an odd UTF-16
length, a UTF-32 length not a multiple of four, a code point past
U+10FFFF, any other invalid UTF-8) make the row malformed, which still
removes the legacy row of its key. The reads are linear in the rows: the
native keys are hashed as Python's dict files them (an int, an integral
float and a bool of one value are one key). A resolution whose `tenant`
is not text is not a `Resolution` here. Any SQLite error of the read is
OperationalError, as Python's `sqlite3` classes the ones these reads meet.

The fixture. `caller_admission.jsonl` (generator `gen_caller_admission`,
no gate: ASCII and casefold-stable names, no ranking): 23 table cases,
each a store `Store.create` built, its rows put through `put_membership`
or written by direct SQL (the case says which), the two tables recorded
as rows with typed cells, one of them native rows whose JSON is bytes in
every encoding `json.detect_encoding` names, whole, odd, truncated and
empty; 145 resolution cases (none, `global`,
`indeterminate`, `deferred`, and each table's own of `silent`, `ask` and
`new`), each with `nothing` and, per surface, `admits_project` of every
key, `admits_row` of every scope list and home, `admitted_projects` and
`admits_global`; `project_home` of every key per table. Its header holds
`search_store.jsonl`'s main store's projects rows and its three callers'
resolutions (the scratch path in `lines` written `<scratch>`).

Tests. `golden_caller_admission` (class i on `caller_admission.jsonl`:
`every_case_answers_as_the_reference`, 168 cases; class iii on
`search_store.jsonl`: `main_store_callers_compute_the_tabulated_admission`,
which computes the six admissions of the main store's callers from their
resolutions, holds each to the tabulated one, and draws and ranks the
486 caller runs through them). `caller_admission_robustness`: cycles,
100,000-long chains closed into cycles, every value type in every
column, missing tables, columns and rows, malformed, nested and large
native rows, native bytes of every short shape, resolutions that do not
parse: typed outcomes, no panic; and `reading_native_rows_is_linear`
(best of five reads of 2,000 and 20,000 legacy rows each replaced by a
native row: at most thirty times as long plus 50 ms, where linear is ten
and quadratic a hundred).

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
| `golden_gitconfig`: `gitconfig_every_case` | `gitconfig.jsonl` | i |
| `gitconfig_robustness` (7) | none | i |
| `golden_gitconfig_wildmatch`: `gitconfig_wildmatch_every_case` | `gitconfig_wildmatch.jsonl` | i |
| `gitconfig_wildmatch_robustness` (8) | none | i |
| `golden_gitconfig_remote_rules`: `gitconfig_remote_rules_every_case` | `gitconfig_remote_rules.jsonl` | i |
| `gitconfig_remote_rules_robustness` (2) | none | i |
| `golden_capture_vocabulary`: `capture_vocabulary_every_case` | `capture_vocabulary.jsonl` | i |
| `golden_capture_vocabulary`: `capture_vocabulary_python_every_case` | `capture_vocabulary_python.jsonl` | ii (Python) |
| `capture_vocabulary_robustness` (5) | none | i |
| `golden_membership_lines`: `membership_lines_every_case` | `membership_lines.jsonl` | i |
| `golden_membership_lines`: `membership_lines_unicode_every_case` | `membership_lines_unicode.jsonl` | ii (Unicode) |
| `membership_lines_robustness` (4) | none | i |
| `golden_ulid`: `ulid_every_case`, `ulid_order_and_round_trip` | `ulid.jsonl` (the second none) | i |
| `golden_githead`: `githead_every_case` (both flavours) | `githead.jsonl` | i |
| `golden_githead`: `githead_posix_every_case` (unix) | `githead_posix.jsonl` | i; os elsewhere (`githead_not_posix`) |
| `githead_robustness` (7), `githead_stdfs` (2 on unix, 1 elsewhere) | none | i |
| `golden_gitfile_leaves`: `gitfile_leaves_every_case` | `gitfile_leaves.jsonl` | i |
| `golden_gitfile_leaves_posix`: in memory and on disk (unix) | `gitfile_leaves_posix.jsonl` | i; os elsewhere (`gitfile_leaves_not_posix`) |
| `gitfile_leaves_robustness` (9; 8 off unix) | none | i |
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
   WAL` with `schema_version` in one script; `synchronous = FULL`;
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
does not repair the head; `write_head` only moves forward, so a head ahead
of the log stays `tail_deleted` until the log passes it. (Corrected by the
writing-open unit: the open does read the head, in one place: a key ring
bound to no file whose head the log does not reach is `unclaimed`, and the
open goes on as a reader's; see "Opening a store for writing".)

Not ported yet, and needed for a write equal to the reference's:
`store_returned`'s marks and `_relink` (which reads `provenance_for`). The
writing open's plain states and the digest's record entries are ported
("Opening a store for writing").

(Corrected by the writing unit, "Writing a record mutation": `_mutate`
reads meta's key under the lock but signs with the key the open read for
as long as meta names it; it reads the key ring again only when meta names
another key. `_flush_returns` runs at the owner's end, so a lone
`_mutate` in its own `_transaction` writes its `returned` rows after its
own row, in the same commit.)

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
where native is no better than the reference. Durability: files and the
directory are synced as the reference syncs them, with `fsync(2)`
(`bettermemory_sys::fsync`; the standard library's `sync_all` is
`F_FULLFSYNC` on macOS, which neither the reference nor SQLite's own
commits ask for, and which made a write's head move 23 times slower).

## Opening a store for writing

`store::open::open_for_writing(path, keys_dir, provider) -> Result<WritableStore,
OpenError>` is `Store.open(path, keys_dir=keys_dir, allow_rekey=True)`, the
owner's writing open that `open_or_create` makes for the hooks, the daemon
and the CLI, in its plain states. `provider` gives the engine version
(`__version__`); nothing is drawn at random that reaches a byte.
`WritableStore` holds the connection (for reads) and what the open
established (`store_id`, `key_fingerprint`, `head`, what became of the
digest, whether it bound the ring); `close()`. Appending and mutating come
with the next unit, as methods on it. It holds no key: the open reads the
key only to decide, and the reference signs with the key it reads again at
write time (`Store._signing_key`, under the write's lock in
`_finish_for_signing`); the next unit must read it there too, never trust
a key read at open.

### The reference's order, and what each step writes

Read from `src/bettermemory` at the create unit's tip (4a84448f; line
numbers are that commit's), and checked by running it
(`store_open.jsonl`). `open` is wrapped `keeps_digest(reconciles_tokenizer(open))`
(`store.py:655-657`), so the open's own steps run first, then the
tokenizer's, then the digest's.

1. `store_path`; not a file: `FileNotFoundError("no store at <path>")`
   (`store.py:732-734`).
2. `_connect` (`store.py:370-411`): `restore_lock.admit` (held shared until
   the connection is made; a running restore or an unfinished one's marker
   refuses), `sqlite3.connect` (5 s busy timeout, autocommit),
   `admit_store`, `PRAGMA page_size` (a no-op on a store), `foreign_keys =
   ON`, `journal_mode = WAL` with `schema_version` (switches a
   rollback-journal store to WAL: written), `synchronous = FULL`, chmod
   0600 on the file, `-wal`, `-shm` (written).
3. `_open_connected` (`store_open.py:40-143`): meta read; no store id or
   fingerprint, an id of another shape, a later `schema_version`: ValueError.
4. `check_versions(writing=True)` (`store_versions.py:187-247`): a later
   minimum reader: `StoreVersionError`; a later minimum writer: `PRAGMA
   query_only` and the open goes on as a reader's; else `run_steps`
   (`store_steps.py:360-401`: each step above meta's revision applied and
   recorded in meta, with the clock's `applied`), then `_record_engine`
   (`store_versions.py:142-182`: `engine_version` set in its own `BEGIN
   IMMEDIATE` when this build is newer by `version_key`).
5. `_add_membership_tables` (`store_schema.py:1214-1291`): any membership or
   additive object missing, the membership store id (minted at random), an
   optional column: created in one transaction.
6. `key_state(claim=True)` (`store_keys.py:605-677`): `finish_rotation`
   (`:935`, renames `<id>.key.pending` over the key when it is the key meta
   names); `load_current` (a key file there and unreadable, or a dangling
   link: `KeyUnreadableError`); `_unclaimed_fit` (`:823`: a ring bound to no
   file that holds an anchor or a head this file's log does not carry is
   `unclaimed`); `_claim_key_ring` (`:353`, `log.py:771` `bind_store`): no
   binding, one that does not read, or one naming a file that no longer
   holds the id: `<id>.path` written under `<id>.path.lock`, atomically,
   0600, as `{"path": "<the store file, resolved>"}` (`json.dumps`); a
   binding naming a file that still holds the id: the open goes on as a
   reader's; a binding that cannot be read or written: logged, nothing
   written.
7. Key present: `_close_foreign_tail` (`store.py:795`, a `rekey` row when
   the last one names another key), `_settle_origin` (`:934`, an anchor
   written when none is there; `_name_origin_key` names the key of one
   that names none), `restore_rows.settle` (`restore_rows.py:688`,
   `store_restored` rows of a restore that could not append them). A key
   missing or foreign: `_reclaim` (`store.py:847`) or `_open_rekeyed`
   (`store_open.py:206`), each appending a `rekey` row.
8. `reconciles_tokenizer` (`store_index.py:1227-1249`, `settle_tokenizer`
   `:1190`): an index spelled by another tokenizer than the log names
   (`stored_fingerprint`, `:1071`) is re-spelled and an `index_rebuilt`
   row appended.
9. `keeps_digest` (`store_digest.py:520-537`): `digest_mark()` with
   `build=True` (`:476-509`): current, nothing; built under these rules,
   one transaction applying the record mutations after its mark
   (`_ingest`, `:409`: each mutated id's entry rewritten from its row
   through the model, `_index_record` `:348`, `_put_entry` `:291`, its
   postings, the state's `seq` and `mac`); else `_build` (`:423`: the
   tables dropped and made again with a random build token, one
   transaction per 2,000 records, then the token removed and the state at
   the log's head at the start), then `_ingest` again. Every failure is
   logged and the digest left as its last commit left it.

The head file is never written by a writing open (no log row is
appended in a plain state); it is read in one place, the unclaimed check.

### The native order

Native reads every decision first, on the admitted connection, with
nothing written (but see "What a refusal can change"): steps 3 to 8 as questions (the version contract, the
membership step, the rotation, the key, the binding and the fit, the
foreign tail, the anchor, the restore record, the tokenizer). Any state for
which the reference would write something this unit does not port is
`OpenError::NotPorted` (below), with nothing written. Then it writes, in
the reference's order: step 2's pragmas and modes, `_record_engine`, the
binding, the digest. A refusal of the reference's own (steps 1 to 4) is its
class and text; the reference has by then switched the file to WAL and
made it 0600, native has not (a difference, on purpose: a refusal writes
nothing).

### Refused states (`NotPorted`), and why

| state | the reference would | 
| --- | --- |
| `later_writer` (meta's minimum writer above 6) | open a reader (query_only, no claim) |
| `schema_steps` (revision below 6) | apply and record steps (clock in meta) |
| `membership_step` (an object, the membership id or a column missing) | create them, mint an id |
| `rotation_pending` (`<id>.key.pending` is the key meta names) | rename it over the key |
| `unclaimed` (no binding; the ring's anchor or head not carried by this file) | open a reader |
| `bound_elsewhere` (the binding names another file that still holds this store id, or cannot be read to tell) | open a reader |
| `key_missing`, `key_foreign` | write a new key and a `rekey` row, or reclaim |
| `foreign_tail` (the last `rekey` row names another key) | append a `rekey` row |
| `settle_origin` (no anchor, or one that does not read) | write an anchor |
| `name_origin_key` (an anchor that names no key) | name it |
| `restore_record` (a restore record entry for this store with a null row) | append `store_restored` rows |
| `tokenizer_drift` | re-spell the index, append `index_rebuilt` |
| `unreadable` (a log row, head or payload that does not read here) | (varies) |

The binding follows the reference's rule exactly (`bind_store`,
`_bound`, `_store_id_at`). Its text is read as `Path(value)` reads it
(`config::pure_path`: repeated separators collapse, `.` segments and a
trailing separator go, `..` stays), so `.../memory.sqlite/` names the
file, never a directory below it. A binding naming this file under any
spelling (a symbolic link, `..`, a resolved or unresolved prefix:
`same_file`, equal paths or one device and inode) is left as it is; one naming a file that is not
there, not a regular file, not a database, a database with no meta or no
store id, a store refused with no meta table, or a store with another id
is rebound to this file; one naming a file that holds this store id, or
that cannot be read to tell (cannot be opened, a read past the 2 s
admission deadline, a refused file with a meta table, an I/O error) is
`bound_elsewhere`, whose message carries the reference's words: which
file, why, and the remedy (move or remove one of the two stores, or
`bettermemory key adopt --yes`). The other file is read read-only and
admitted (`mode=ro&immutable=1`, or `mode=ro` first when its `-wal` and
`-shm` are there, a 2 s busy timeout). The decision reads it once; the
binding's write reads the binding again under `<id>.path.lock` and asks
again, and a second opener that bound the ring meanwhile to a file that
holds the store makes this open refuse there, as `bound_elsewhere`, after
step 2's writes and `engine_version` (the reference's own writes by then)
and before the digest is touched, with no store handed out
(`store_open_robustness`, a hook between the decision and the bind).

Every not-ported refusal says what to do: open the store once with the
Python build, whose writing open settles the state (a later writer: open
it with a build at least as new as its last writer; a ring bound
elsewhere: the reference's remedy above).

One refusal is wider than the reference's writes, on purpose: every
restore-record entry with a null row is taken as pending, whatever its
kind.

### What a refusal can change

The decision reads through the read-write connection the open then
writes with, not through a read-only one: SQLite opens a store with a hot
rollback journal only to roll it back (a read-only connection fails with
`SQLITE_READONLY_ROLLBACK`), and a look through a copy or a second
connection would leave a gap between the decision and the writes that the
reference does not have. So a refusal (`NotPorted` or the reference's own)
can change, and changes only:

- a hot rollback journal (a writer in rollback-journal mode died
  mid-transaction): rolled back at the first read, the journal removed, as
  the reference's open and any connection that can write do;
- a `-wal` with frames (a writer has the store open, or died with it
  open): left in place, not checkpointed (the connection is closed with
  `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE` until the open decides to write); a
  stale `-shm` beside it has its wal-index rebuilt, a `-wal` without a
  `-shm` gets one;
- an empty `-wal` (and its `-shm`) a reader left: removed at the close, as
  every close of a writing connection removes them.

The fixture holds both sidecar states: `not_ported_live_wal` (a refusal
beside a live WAL: nothing changes) and `not_ported_hot_journal` (a
refusal beside a hot journal: exactly what rolling the journal back
leaves, which the generator records as `rolled_back` from a copy rolled
back by a plain connection). A pending key meta does
not name and a restore record with nothing pending are opened as the
reference opens them (nothing written for them).

### What a kill leaves

`store_open_kills` (debug build, about 5.5 s on an M-series Mac): three
states that make the open write (`intact`: the ring to bind,
`engine_version` to record, the digest behind; `fts` with no digest: a
build of 18 records; `intact` in rollback-journal mode, bound: the WAL
switch), each ended at every boundary it passes by `abort` and by `exit`,
plus 60 kills from outside at fixed pseudo-random delays: 108 kills. After
each: native verify ok with the same record count; a copy opened for
writing, verified and counted by the reference (all 108 ok); a further
native writing open succeeds and verifies.

| killed after | store | keys | digest |
| --- | --- | --- | --- |
| swept, checked | as before | as before | as before |
| connected | WAL from here (a rollback store switched) | as before | as before |
| engine recorded | meta's `engine_version` | as before; a kill inside the bind leaves `<id>.path.<pid>x<n>.tmp` and `.path.lock` (swept by a later open or create after an hour) | as before |
| bound | | `<id>.path`, `.path.lock` | as before |
| build begun | | | tables empty; state `fingerprint`, `build` (a later open rebuilds) |
| build batch | | | some entries; still `build` |
| build ended | | | whole at the build's start: `fingerprint`, `seq`, `mac` |
| digest ingested, closed | | | current |

### Two openers

The reference lets two writing opens of one store proceed side by side:
every write of the open is its own `BEGIN IMMEDIATE` under the 5 s busy
timeout, read again under the lock (`_record_engine`, the digest's
transactions; a build that finds another build's token stops), and the
binding is written under its own `flock`, read again under it. Native does
the same, tested in `store_open_robustness`: two native opens at once on a
store with no digest (one builds, the other ingests or finds it current;
the result equals the fixture's); a native open while a reference process
holds the store open and writes 20 records (the native open proceeds,
the reference's writes and verify are ok); a reference open while a native
connection holds the write lock for 1.5 s (the reference waits, about
1.2 s, then opens and verifies ok).

### Tests

| test | class | what |
| --- | --- | --- |
| `golden_store_open` | ii (Unicode), POSIX | 74 cases of `store_open.jsonl`: same, refused, raises, not ported |
| `store_open_not_posix` | os | records the fixture's gap off POSIX |
| `store_open_cross` | i, reference | both directions; the native digest equals the reference's |
| `store_open_kills` | i, reference, POSIX | 108 kills |
| `store_open_spellings` | i, reference, POSIX | 504 binding spellings onto this file, a copy, another store and nothing: outcome and `.path` bytes equal the reference's |
| `store_open_robustness` | i (two need the reference) | hostile files, refusals unchanged byte for byte, two openers, a binding race lost to a copy |
| `digest::tests`, `open::tests` | unit | the computed digest fingerprint equals the pinned one; provenance, sentences, version order |

`store_open.jsonl` (133 KB) is Unicode-gated and POSIX only (its generator's
docstring says why).

The digest fingerprint the code reads and writes is computed from the
ported rules once per process (`digest::fingerprint`); the pinned
`DIGEST_FINGERPRINT` is only the tests' expectation. What the computed
fingerprint covers is what the reference's covers: the digest's shape
string, the tokenizer's fingerprint, and the features of four probe
sentences. A change to the rules that keeps the same pattern strings and
the same results on those probes (a rule that only moves a sentence no
probe holds) does not move it, here or in the reference, and a digest
built under the old rules is kept.

`digest_state.seq` is read as the reference reads it: `str.isdigit`, then
`int()` (`pyvalue::int_from_str`: Unicode decimal digits, so Arabic-Indic
digits read as their value; a superscript is a digit and not a decimal,
a ValueError the digest catches and leaves the digest as it is); a seq
past an SQLite INTEGER is the reference's `OverflowError` out of
`Store.open` (on every writing open, in both builds, until the digest is
rebuilt; a read-only open still verifies: a finding about the reference) (`OpenError::Overflow`, raised after step 2's writes and
`engine_version`, as the reference's; fixture case `digest_seq_overflow`).

Not ported on Windows (and on a POSIX directory `flock` cannot lock):
the writing open's restore-lock admission there (`_RecordCheck`: a handle
held on the store file, the lock read again behind it, and again at
`confirm` once the connection has read the store, with the file's
identity compared on POSIX). Native takes the reader's admission
(`restore_lock::reading`) for the writing open; on macOS and Linux, where
the directory `flock` is held shared until the connection is made, that is
the reference's admission.

Not ported here, and found on the way: the CLI's and the daemon's
`ensure_backfilled` after the open (`store_returned.py:371`), which is not
part of `Store.open`.

## Writing a record mutation

`WritableStore::mutate(kind, payload, session, provider)` (`src/store/write.rs`)
is `with store._transaction() as tx: store._mutate(tx, kind, payload,
session=session)`, then the head's move. The row's MAC is `log::next_row`
(the one MAC of the crate); the table effects are the fold's applier
(`fold::Applier`), reshaped to take any connection: on the live store it
spells the index text per put (`_write_memory_row`) and runs
`_refuse_unknown_fields` against the log, where the fold's scratch log is
empty. There is no `append` of a row that changes no table: no such row
is on `_mutate`'s path (telemetry, membership events and control rows
each come through callers of `_append` with rules of their own).

### The reference's order (read at e8747231, and run)

1. `_transaction` (`store.py:1177`): `_clear_residue` when the connection
   is inside a transaction nobody owns; `BEGIN IMMEDIATE` (:1212; the
   connection's busy timeout is the wait); `require_current_writer`
   (`store_versions.py:250`, meta's minimum writer read under the lock).
2. `_mutate` (`store.py:1390`): `prepare_mutation`
   (`store_native.py:89`: a record origin holding a surrogate moves into
   `native_origin`, which needs a revision-5 writer); `store_returned.before`
   (`store_returned.py:140`, `memory_put` and `memory_delete` only).
3. `_append` (`store.py:1361`): `_signing_key` (`store_keys.py:1016`):
   meta's `key_fingerprint` read again; the key the open read while meta
   names it (the key file is not read: a key file replaced, removed, a
   pending rotation meta does not name, a ring bound elsewhere since the
   open change nothing; the fixture's `key_edits`); else the ring's current
   key when meta names it, else `finish_rotation`, else still the open's
   key (`meta_rekeyed`). Then `append_row` (`log.py:875`): the last row,
   seq, the clock (`_utcnow_iso`), canonical payload, MAC, INSERT.
4. `_apply_mutation` (`store_rows.py:545`) on the same payload: the model
   validates (`Memory.model_validate`), `_refuse_unknown_fields` (:528),
   the provenance, `validate_path_targets` (in `_record_columns`), the
   upsert; the links; the FTS5 index follows through the schema's
   triggers (`memories_ai`/`_ad`/`_au`). Any failure here rolls the row
   back with it.
5. `store_returned.candidates` (:162), then at the owner's end
   `_flush_returns` (`store.py:1418`): `settle` (:178) may append
   `returned_put`/`returned_delete` rows (stamped by the wall clock,
   `datetime.now`, not the log's clock).
   Ported with the transaction (below): `mutate` is a transaction of one.
6. `COMMIT` (:1222); any failure up to here rolls back (`_roll_back`).
7. `_move_head` (:1353): `KeyRing.write_head` (`log.py:638`) under
   `flock_excl(<id>.head.lock)`: the head read; written only forward (a
   head at or past the row stays: `head_ahead`), the clock read only
   then, `atomic_write_bytes` 0600. An `OSError` is logged and the call
   succeeds; the commit stands with the head behind.

A write does not touch the digest (confirmed: the fixture's states keep
`digest_records`, `digest_tokens` and `digest_state` as the open left
them): the next writing open or search brings it forward.

The native order is the same, with `WriteBoundary` hooks after BEGIN
(`Begun`), the INSERT (`Appended`), the apply (`Applied`), COMMIT
(`Committed`), the head's lock (`HeadLocked`) and the head
(`HeadMoved`). The payload applied is the caller's, as `_mutate` applies
its mapping: a tuple stays a tuple (`tombstone_put`'s `isinstance(links,
list)` refuses a non-empty one), a datetime a datetime (bound as sqlite3's
default adapter binds it, `isoformat(" ")`), a path a path (sqlite3 cannot
bind one: ProgrammingError; pydantic's `str` refuses one). The row holds the
payload's canonical text (`default=str`), which `refold` replays; the
fixture's `python_values` sequence pins that the reference's `refold`
reads every row such a payload left as `ok`, with no problem, and that the
native fold check says the same. The models' lax `list` takes a tuple, as
pydantic-core's does.

### Kinds and refusals

In scope (`write::KINDS`): `memory_*`, `tombstone_*`, `episode_*`,
`verification_*`, `conflict_*`, `import_*`, `quarantine_*` (put and
delete): `RECORD_MUTATION_KINDS`, on which `_mutate` does the row and its
table effects and nothing else, but for the `returned` marks below. Not in
scope, refused as `NotPorted::Kind` before anything (`home_put` and
`standing_put` are written only through `store::home`): every membership kind
(`machine`, `project*`, `declared_remote`, `checkout*`, `home`,
`proposal`, `standing`, `returned`, `native_member`), each with rules of
its own in the public API, and any other text.

Refused with the transaction rolled back, nothing written:

- `NotPorted::NativeOrigin`: a `memory_put`, `tombstone_put` or
  `episode_put` whose record's origin holds a surrogate in a `str` (a path
  value is no `str` to `has_surrogates`, and is not projected).
- `NotPorted::Rekeyed`: meta names a key the ring's current key is not,
  and `<id>.key.pending` holds 32 bytes whose fingerprint is meta's: the
  rotation `finish_rotation` would finish by renaming it. Wider than the
  reference by one test, named here: the reference does not finish when the
  ring is bound to another file (it signs with the open's key), native
  refuses either way. A pending key meta does not name is passed over by
  both (`meta_rekeyed_pending`). meta's fingerprint is read as `str(value)`
  of any stored type, NULL as `None` (`meta_key_integer`).
- `Refused { class, text }`: the reference's own refusal (KeyError,
  ValueError, TypeError, ValidationError with its first line,
  UnknownFieldsError with its text, UnicodeEncodeError).
- `NotAnObject`, `LaterWriter`, `Sqlite` (busy past the timeout, a schema
  altered after the open), `Clock`.

Texts: the product's own messages are reproduced, as is `int()`'s
`invalid literal for int() with base 10: ...`, whose value CPython quotes
as its repr cut to the first 200 code points (`%.200R`; measured against
3.13 for str and bytes, escapes counted as written; `int_text` pins the
lengths around the cut). The other reprs of caller data a refusal quotes
(`unknown provenance ...`, `unknown mutation kind ...`, a membership row's
unknown columns) are f-string `!r`s, whole in both builds;
`UnknownFieldsError` cuts each name's repr to 66, ported. No write or fold
path words a `float()` failure. Exceptions from the standard library or
SQLite whose wording is the interpreter's (AttributeError, OverflowError,
RecursionError, sqlite3's errors) are returned with their class and no
text: those texts are not reproduced, and the fixture compares only the
class.

Deliberate differences: the reference raises OverflowError or
RecursionError out of a committed call when the head file holds an
infinite seq or deep nesting (`write_head`'s `read_head` lets them out,
`_move_head` catches only OSError; reproduced: the row is committed and
the caller sees an error); native returns the commit with
`HeadMove::Failed`. On macOS std's `sync_all` is `F_FULLFSYNC` where the
reference's `os.fsync` is not: the head's move is more durable natively
and much slower (below).

### What a kill leaves (`store_write_kills.rs`, 208 kills, run)

| killed after | WAL | rollback journal |
| --- | --- | --- |
| `Begun`, `Appended`, `Applied` | log and tables as before (the WAL's uncommitted frames ignored) | a hot `-journal`, rolled back by the next connection that may write: as before |
| `Committed`, `HeadLocked` | the row and its effects whole; head one behind (`ok`) | same |
| inside the head's replace | head behind or moved; at worst a `<id>.head.<pid>x<n>.tmp` left in the keys directory | same |
| `HeadMoved` | whole, head at the row | same |

Every kill: the record tables equal those of the log's length, FTS5's
`integrity-check` passes, the head never names a row the log lacks, both
builds verify ok, and a further write succeeds.

That holds for a killed process, whose writes the OS still holds. For a
power loss or an OS crash (ENG-328): the writing connection, in both
builds, is WAL with `synchronous = FULL` (create's and open's, read back
as 2 on the connection that commits, pinned in `store_create.json`'s
`writer_synchronous` and `store_open.jsonl`'s `outcome.synchronous`).
SQLite's `sqlite3WalFrames` syncs the WAL inside a COMMIT when the
connection's sync flags carry FULL (`WAL_SYNC_FLAGS`, set from `fullSync`
in `sqlite3PagerSetFlags`; the bundled 3.54.1 amalgamation), and the
head's replace comes after the COMMIT returns, so the head never names a
row the WAL has not been synced to hold. Until 9.0.0rc20 it was NORMAL,
which leaves the WAL to the next checkpoint: after the OS cache was lost
the head could name a row the log lost, read as `tail_deleted`. The sync
is a plain `fsync` (no `F_FULLFSYNC`, no `PRAGMA fullfsync`), as the
head's own is; a drive that loses or reorders its cache is outside it.
In a rollback-journal store (a WAL switch that failed; ENG-329) FULL also
syncs the journal's header, but the commit point is the journal's unlink,
which SQLite syncs into its directory only at `synchronous = EXTRA`: a
commit there can still roll back after a power loss while the head names
it. Nothing here tests a lost OS cache, and no test observes SQLite's own
sync calls (no syscall tracer without privileges on this Mac).

### Two writers

Each process moves the head after its own commit, under the same
`flock` on `<id>.head.lock`, forward only; so does native. A native and a
reference writer, and two native writers, 200 puts each at once: both
finish, no row lost or doubled, seqs gapless, both builds verify ok, the
head at the last row (`store_write_concurrent.rs`). In rollback-journal
mode each writer switches its own connection after its open: each open
asks for WAL (`_connect`; native's `connect_pragmas`; both ignore what the
pragma returns) and gets it while the other writer is idle, and a switch
away from WAL needs no other connection in WAL, so the reference's writer
opens first. Both builds' opens behave alike here. (An earlier report that
the reference's open differed was the test's: its second native writer
switched back to rollback-journal mode itself, and the file's mode was read
after the reference's check had opened the store, which asks for WAL.)

### Tests

| test | what | class |
| --- | --- | --- |
| `golden_store_write.rs` | `store_write.jsonl` (26 sequences, 108 mutations, 269 KB, a refold per sequence; Unicode and pydantic-core gated, POSIX only): rows byte for byte, the whole state's digest, the head, FTS5 hits; its two `not_ported:returned` steps (whose mark stamps the file left to the real clock) are now written and checked by row count, `store_tx.jsonl` holding such writes byte for byte | ii |
| `store_write_not_posix.rs` | the fixture's gap elsewhere | os |
| `store_write_kills.rs` | 208 kills, both journal modes, the reference's verify | i |
| `store_write_cross.rs` | both directions with the reference | i |
| `store_write_concurrent.rs` | two writers, both builds | i |
| `store_write_readers.rs` | native check and search under a writer | i |
| `store_write_robustness.rs` | hostile inputs and states; the ignored throughput bench | i |

Throughput (`bench_five_thousand_puts`, release build, this Mac, three
runs each; and `native/tools/bench_write.py` under the worktree's Python
3.13), with the head synced by `bettermemory_sys::fsync` as `os.fsync`
syncs it: native 2566, 2592 and 2549 puts a second; the reference 1905,
1961 and 1952. With `synchronous = FULL` (ENG-328; measured
on a machine other jobs were loading, load average 6 to 11): native 2397,
2371 and 2421 puts a second against 2391, 2065 and 1915 at NORMAL just
before (COMMIT 0.64 to 0.65 s of the run); the reference 1358, 1502 and
1746 against 1601, 1560 and 1367, and interleaved in one session 1453,
1513 and 1779 at FULL against 1854, 1611 and 1870 at NORMAL. Where native's 1.94 s go: the head's move (lock, read,
write, `fsync`, rename, directory `fsync`) 0.89 s; COMMIT 0.53 s; the
apply (model, row, links, FTS5 triggers) 0.32 s; the head's lock 0.11 s;
the row (key check, MAC, INSERT) 0.09 s.

## The write transaction

`store::tx::Transaction` (`src/store/tx.rs`) is the transaction every
public record writer runs inside: `Store._transaction` with its outermost
and nested entries, `Store._mutate`, `_flush_returns` and
`Store.release_returned`. `WritableStore::mutate` is a transaction of one.

### The reference's order (read at 81bfa627, `store.py` unless named)

1. Outermost entry (`_transaction`, 1185-1240): with the depth at 0, a
   connection still inside a transaction is a residue and is rolled back
   (`_clear_residue`, 1242); `_last_appended`, `_tx_first_seq`,
   `_tx_records` and `_tx_returns` are reset; `BEGIN IMMEDIATE`, then the
   depth is 1; `require_current_writer`.
2. The body. Each `_mutate` (1399-1425): `prepare_mutation`,
   `store_returned.before` (`store_returned.py` 140-159: `memory_put` and
   `memory_delete` only; the record's id, whether it is active, and its
   `supersedes` targets when it is), `_append` (1370-1397: `_signing_key`,
   `append_row`, the log clock), `_apply_mutation`, then each of
   `candidates` (162-175: a removal of an active record names its targets
   in order and then itself; a put names the targets it dropped or added,
   sorted) into `_tx_returns.setdefault(id, (record_id, session))`: the
   first write that names a record keeps its via and session.
   `_put_restored` names a restored record the same way
   (`store_records.py:693`).
3. A nested entry (`_joined`, 1252-1284): `_require_open`; the last
   appended row, the first seq, `_tx_records` and a copy of `_tx_returns`
   are kept; `SAVEPOINT nested_<depth>`. On success `RELEASE`. On any
   failure those four are put back, and while the connection is in a
   transaction `ROLLBACK TO` and `RELEASE`; if those fail the owner's
   transaction is rolled back with it. The error goes on to the caller,
   which may catch it and go on.
4. The outermost end: `_require_open` (1287: "the write transaction on
   store {path} was rolled back by an error inside it; nothing it wrote
   was kept", a sqlite3.OperationalError), `_flush_returns` (1427-1443),
   `COMMIT`; any failure rolls back (`_roll_back`). Then `_move_head`
   once, to `_last_appended` if any: a transaction that kept no row moves
   nothing.
5. `_flush_returns` takes `_tx_returns` and calls `settle`
   (`store_returned.py` 178-212). When anything is named and the
   `returned` table exists it reads the wall clock once (the stamp, 198),
   then for each named record in id order: inactive, or superseded by
   another active record (`_superseded`, 115), and marked: a deletion
   `{"record_id", "dropped": "removed"}` (inactive) or `"superseded"`;
   active, not superseded, not marked: a put `{"returned": {"record_id",
   "via", "returned": stamp}}`; otherwise nothing (already marked, or out
   of force with no mark). No table: nothing, and no clock read. The puts
   are appended (each a `_mutate`, so each reads the log clock), then the
   deletions, each with the session of its naming.
6. `release_returned` (1445-1456) is a nested entry: `returned_rows` of
   the one id (a missing table reads as no mark; any other SQLite error
   raises), and when marked a `returned_delete {"record_id"}`.

Two clocks: a row's `ts` and the head's `ts` are the log clock
(`log._utcnow_iso`, `log.py:313`); a mark's stamp is `datetime.now` read
in `store_returned` (198), `_flush_returns` passing no `now`. The provider
carries both: `Provider::now` and `Provider::wall_now` (by default the
same clock; `create::Clocks` sets the wall clock apart; `System` reads the
system clock for both).

### The native rule

`WritableStore::begin(provider)` is step 1 (a residue rolled back, `BEGIN
IMMEDIATE`, `LaterWriter` when the writer check refuses).
`Transaction::mutate(kind, payload, session)` is step 2, under a savepoint
of its own (`native_step`), so a mutation that fails takes back what it
wrote. `Transaction::scope(body)` is step 3: `body` gets the transaction
and returns `Result<T, E>` for any `E: From<WriteError>`; on its failure
the last row and the named records (and the list of rows kept) are put
back as they stood at its entry, and the savepoint is rolled back to.
`Transaction::name_returned(id, via, session)` is the `setdefault` a
restore makes. `Transaction::release_returned(id, session)` is step 6
(`WritableStore::release_returned` in a transaction of its own).
`Transaction::commit()` is steps 4 and 5 and returns `Committed { rows,
head }`: every row kept, the transaction's own then its marks, and the
head's move (None when no row was kept). A transaction dropped without a
commit, or whose commit fails, is rolled back. `Transaction::connection()`
reads the store inside the lock. The hook (`begin_with`) sees `Begun`,
each row's `Appended` and `Applied` (the marks' and a rolled-back scope's
rows among them), `Committed`, `HeadLocked`, `HeadMoved`.

A later writer: `let mut tx = store.begin(provider)?;` then
`tx.scope(|tx| { /* read through tx.connection() */ tx.mutate(..)?;
tx.name_returned(..); Ok(..) })?;` then `tx.commit()?`. Its own typed
error converts from `WriteError` and rolls back its scope only.

Refused with nothing written (the transaction can then only be rolled
back; `commit` returns the refusal): `NotPorted::Kind` (every membership
kind and any other text, `returned_*` among them: marks are written only
by the flush and by `release_returned`), `NotPorted::NativeOrigin`,
`NotPorted::Rekeyed`. Typed errors otherwise: `Refused { class, text }`,
`NotAnObject`, `LaterWriter`, `Sqlite`, `Clock`, and `RolledBack(path)`,
whose text is `_require_open`'s.

Deliberate differences: (1) a mutation the reference refuses after
appending its row leaves that row in the transaction when a caller catches
the error in the outermost scope without a savepoint (no reference writer
does: each runs in a `_transaction` of its own); native takes the failed
mutation back. (2) A not-ported write anywhere refuses the whole
transaction, in a scope or not, since the reference would have written
it. (3) As in the reference, the signing key a rekey switched to is not
put back by a scope's failure.

### Tests

| test | what | class |
| --- | --- | --- |
| `golden_store_tx.rs` | `store_tx.jsonl` (22 cases, 78 transactions, 217 KB; Unicode and pydantic-core gated, POSIX only, as `store_write.jsonl` is and for its reasons): per transaction the outcome and each failed scope's error, every clock reading read once and in order (the log clock and the wall clock set apart), the rows byte for byte, the whole state's digest, the head, FTS5 hits; a refold per case | ii |
| `store_tx_not_posix.rs` | the fixture's gap elsewhere | os |
| `store_tx_cross.rs` | native's 40 transactions read by the reference and equal to a twin it writes with the same ones; the reference continues and native continues after it; the reverse from a store the reference marked | i |
| `store_tx_kills.rs` | 156 kills (every boundary of a four-row and a three-row transaction by abort and exit, 60 timed), WAL and rollback journal, both builds verify | i |
| `store_tx_robustness.rs` | hostile payloads in nested scopes (5000-deep nesting, a 1 MiB body, a lone surrogate), a transaction dropped or forgotten, a second writer, a `returned` table missing or misshapen, an ended transaction, a failing wall clock, a native origin in a scope | i |

`store_tx.jsonl`'s cases: zero mutations; a mark put, deleted as removed,
deleted as superseded and put again; a put that drops one link and adds
another; three marks in one flush, put and deleted; a chain and a
diamond; a record named twice in one transaction, and named directly;
already marked; candidates that end where they started; releases with and
without a mark, and in a scope that fails; no `returned` table;
rollback-journal mode; failures in the outermost scope; refused mutations
inside a scope and outside; a not-ported kind; a nested failure before the
first, the middle and the last step of a four-step scope, with the outer
transaction going on and with it failing after; scopes inside scopes. The
generator drives the reference through `tests/contract/_fixed.py`, which
fixes every clock and mint the record writers read (`log._utcnow_iso`;
`datetime` in `store_returned`, `store_records` and `store`; `utcnow` and
`generate_ulid` in `store_records` and `store_journal`) with the log clock
and the wall clock apart, for this unit and the writers after it.

## A git config file's text

`gitconfig` is the text half of `src/bettermemory/gitconfig.py`: bytes in,
the ordered entries out, or a plain refusal. It is pure: no file, no git, no
store. Where the text half lies in the reference (line numbers at v9's
052fb76c):

- `git_entries` (1675-1735) and `_value` (1738-1785), with their patterns
  and constants (1537-1560): `gitconfig::git_entries`, which returns
  `Result<Vec<Entry>, Refused>`. `Refused` carries no reason, since the
  reference returns None there and words no reason.
- `_spelled` (389-400) and `subsection_bytes` (403-405): `Entry::spelled`
  and `gitconfig::spelled`, giving a `Name` (section, subsection as
  bytes, key).
- The NUL cut `_parse_config` makes of each value (427), outside
  `git_entries`: `Entry::value_cut`.
- `KEEPS_SPACES_SINCE` and `spaced` (447-460), and the rule their callers
  apply (`location_git.py:673`, `gitfile_rule.py:346`, `_include` at
  `gitconfig.py:493`): `gitconfig::spaced` and
  `gitconfig::reads_as_written(value, version)`, with the version as data
  (`None` where git has not named it). This is the only place the text half
  depends on git's version. `git_entries` reads every version alike, so it
  takes no version.
- `git_int` (1788-1831) and `git_bool` (1834-1850).

The tree half starts with `_Name`/`Values`/`ConfigValues` (51-117) and is
everything else: `config_tree` (255), `_config_bytes` (292),
`_read_config` (358), `_parse_config` (408) apart from its cut,
`_include` and the include targets (463-680), the remote rules
(`_remote_rule`, 744; `refname_valid`, `refspec_valid`), the legacy remote
files (893-1075), the `includeIf` conditions and `wildmatch` (1078-1535),
`read_git_config` and its memo (1564-1672). Not ported here, and left to
that next unit: the 2 MiB bound `_FORMAT_LIMIT` (1536), which only the
readers of files apply (`gitconfig::FORMAT_LIMIT` is the constant, and
`git_entries` reads any size, as the reference's does); and every worded
reason (`"{path} holds a line git's parser refuses"`, 424; `"{path} is a
config git cannot parse"`, 1650 and 1661), all of which name a file.
An `include` or `includeIf` line comes back as an entry and is not acted on.
For the next unit: apply the 2 MiB bound before parsing, not after. The
entries take about 40 times their input at peak (2.1 GB for a 50 MB file of
one-letter keys). Also, `Entry::name` builds the name, copying the
header's part on every call. `Entry`'s equality is the reference's
`(name, value)`, compared on the built name.
`Entry` shares its header's part of the name among the entries under it, so
a long subsection over many keys costs its length once.

`gitconfig.jsonl` (`tests/contract/gen_gitconfig.py`, about 244 KB, not
gated: `re` over bytes with ASCII classes, no interpreter wording, and the
same bytes under 3.11.15 and 3.13.13) holds 2,874 texts (1,230 refused)
and 69 values. The cases are built by rule: each header, key and value form
alone and in pairs, under each line ending; every escape byte; every byte
as a value, key and subsection; each quoting state at the end of a line and
of the file; NUL bytes in values (with the cut value recorded where it
differs) and in subsections; the byte order mark; real-shaped configs mutated byte by byte;
errors at the start, middle and end of a file; boundary sizes and hostile
recipes. Each text's reading is recorded under git none, 2.34.8, 2.44.4,
2.45.0 and 2.56.0. The generator's texts were cross-checked against
`git config --list -z --file` (git 2.54.0, outside the fixture), with no
disagreement in 2,848 distinct texts of 1 MiB or less.
`gitconfig_robustness` covers a 50 MB file, a 10 MB line, 4 Mi quotes,
escapes, backslashes, NULs, 0xFF bytes and a 4 MiB subsection over 1 Mi
keys. It also checks that six shapes read in time linear in their size:
1M pieces take at most 30 times as long as 100k, plus 50 ms, the best of
five runs each. Linear growth is 10 times and quadratic 100 times.

## git's wildmatch

`gitconfig::wildmatch` ports the matcher that the `includeIf` conditions
call (`gitconfig.py` at a983e3bb): `wildmatch`, `_dowild`, `_star`,
`_bracket` and `_CLASSES` (1227-1520), the budgets `_WM_STEPS` (200,000,
line 1274) and `_WM_DEPTH` (150, line 1277), and the two helpers
`_any_match` (1113-1126) and `_literal_head` (1215-1224). The entry points
are `wildmatch(pattern, text, icase, &mut Steps) -> Result<bool, Spent>`,
`any_match(pattern, texts, &mut Steps, icase) -> Option<bool>` and
`literal_head(pattern, text, literal, icase) -> bool`.

The rule is git's `dowild` with `WM_PATHNAME`. `*` and `?` match no `/`,
and a `**` that is a whole component matches across components. Brackets
take ranges, `!` or `^`, escapes and the ASCII classes of git's
`sane_ctype`. A bracket never matches `/`. An unterminated bracket or an
unknown class aborts the whole match. With `icase`, only ASCII letters
fold, and only where git folds them: the text byte, the pattern byte
outside brackets, ranges, and `[:upper:]`. A byte after a backslash and a
plain byte inside a bracket are compared as written while the text byte
is folded, so with `icase` `\A` and `[A]` match neither `a` nor `A`, and
`\a` and `[a]` match both.

The port counts a step wherever the reference counts one (each pattern
byte `dowild` reads, each turn of a star's walk, each byte of a bracket).
A match whose step count passes the budget gives `Spent::Steps`, and one
that nests more than 150 stars gives `Spent::Depth`. The budget is shared:
`Steps` is passed by the caller, as one `config_tree` read shares it. The
reference can also fail with `RecursionError`, but its 151 nested stars at
two frames each stay far inside Python's limit, so that path is not
ported. Here the recursion is at most 151 `dowild` calls, each with one
`star` frame: the deepest match the budget allows and the shapes refused
for depth run on a 512 KiB thread in a debug build. The steps are
decremented with saturation, so a budget at `i64::MIN` is spent, as the
reference's unbounded integer is, rather than wrapping.

The work no step counts is the search for the next `/`. The reference does
it with `text.find(b"/", ti)` (1400) and `_SLASH in text[ti:]` (1396) at
every star pass that asks, each over the rest of the text. For
`[!/]***a?**/**/*a` against 200,000 bytes of `a`, that is 43,749 searches
and 8,749,725,002 bytes, which `memchr` reads in 0.22 s. For `*a\a*/b*`
against 100,000 `a` and then 100,000 `/`, it is 3,199,980,000 bytes in
0.11 s. The first port searched byte by byte the same number of times
(4.7 s and 0.9 s in release). `wildmatch_scanned` now keeps the first
search's answer. At the first search that answer cannot settle, it indexes
the text's `/` positions once (a bit a byte, and for each 64-byte block
the next block holding one), and every later search reads at most two
words. Those two shapes now read 249,997 and 139,999 bytes, and take
0.56 ms and 0.53 ms in release, with the same answers and the same steps.
A star's scan for its next literal byte reads byte by byte, as the
reference's Python loop does, and the same number of times. The worst
shape found, `**/**/**/**/****/*/aa****a*/` against `a`, reads six times
the text in 11 steps in both, and native is faster per byte. The counter
returned by `wildmatch_scanned` counts both kinds of read.

`gitconfig_wildmatch.jsonl` (`gen_gitconfig_wildmatch.py`, about 155 KB,
not gated: the reference compares integers and reads no Unicode data,
locale or platform; the same bytes under Python 3.11.15 and 3.13.13) holds
5,893 matches and 21 helper cases. The matches are 628 that match, 372
spent by steps and 4 spent by depth. Each match row records the answer and
the steps it took. The cases are built by rule: 99 constructs alone
against 12 texts, 196 pairs of 14 constructs against 7 texts, 22
case-folding constructs (escaped, bracketed, ranged, negated and named, in
both cases) alone and inside two longer patterns against 4 texts, each with
`icase` where a letter is on either side, and every 16th case again at the
budget it took and one step under it. Shapes built to spend the budget
include the literal at 199,999, 200,000 and 200,001 bytes and stars nested
148 to 152 deep. Cross-check, outside the fixture: the 1,473 rows without
`icase` that are usable as an `includeIf "onbranch:<pattern>"` against a
HEAD of `ref: refs/heads/<text>` (git 2.54.0, neutral environment) agree
on 1,469. The 4 that differ are the 199,999- and 200,000-byte literals,
which git does not match through this route. The independent review
settled why: git refuses a one-component branch name past 255 bytes, and
the reference's caller gives `UNSETTLED` for such a name, so those cases
cannot arise through the caller. `icase` was not
cross-checked: `gitdir/i:` rewrites the pattern and needs real
directories.

`gitconfig_wildmatch_robustness` checks the following:

- `**/` 24 times and the literal one byte past the budget are spent at
  exactly 200,001 steps.
- 10,000 nested stars are spent by depth.
- 10 MB patterns and texts end: a run of stars is one star, and an
  unterminated 10 MB bracket is spent.
- 10,000 texts share one budget.
- Steps grow linearly with the text (`?*b`: 2,002 steps for 2,000 bytes,
  20,002 for 20,000) and stop at the budget for 2 MiB.
- The bytes the searches read, from `wildmatch_scanned`, for the two
  shapes above and the worst found, and, for 500 generated star shapes
  against four texts, at most two readings of the text, one more a star,
  and 64 bytes a step. That bound is measured, not proved.
- A budget of `i64::MIN`, `i64::MIN + 1`, -1 or 0 is spent at the first
  step.
- The depth test on a 512 KiB thread.

Every number in it was taken from the reference.

## The remote rules

`gitconfig::remote_rules` ports where git's reader of remotes dies on one
variable (`gitconfig.py` 713-877): `remote_rule(&Name, Option<&[u8]>) ->
Option<&'static str>` (`_remote_rule`, 744), `refname_valid(name,
pattern, onelevel)` (806) and `refspec_valid(spec, fetch) ->
Option<bool>` (831). It is pure: a name as `gitconfig::spelled` splits it
(a section or key that is not lower case is not recognised, as in the
reference), and the value cut at a NUL as `_parse_config` keeps it. An
accepted variable gives `None`. A refused one gives the reference's words:

- `"no boolean"`: `remote.*.promisor` for any name, and the
  `_REMOTE_BOOLS` keys of a remote whose name does not start with `/`,
  where `git_bool` refuses the value.
- `"no value"`: a key with no `=` for `remote.<n>.partialclonefilter` (not
  a `/` name), `remote.pushdefault`, the `_BRANCH_STRINGS`, `_URL_STRINGS`
  and `_REMOTE_STRINGS` keys.
- `"an empty branch name"`: a `branch ""` section, any key.
- `"a refspec whose reading turns on the object format"`: a negative
  fetch or push refspec of 40 or 64 hex digits.
- `"a refspec git refuses"`: any other refspec `parse_refspec` refuses.

`refname_valid` is `check_refname_format`. It refuses an empty name, a
lone `@`, a trailing dot, `..`, `@{`, control bytes, DEL, space and
`~^:?[\`, more than one `*` (any `*` without `pattern`), an empty
component, a component that starts with `.` or ends in `.lock`, and one
component alone without `onelevel`. Bytes at or above 0x80 are allowed.

`gitconfig_remote_rules.jsonl` (`gen_gitconfig_remote_rules.py`, about
134 KB, not gated, the same bytes under 3.11.15 and 3.13.13) holds 1,820
rule cases (270 refused), 368 ref-name cases and 658 refspec cases. The
cases are built by rule: every key the rule names under each section,
with each subsection form (named, empty, `/`-named, dotted, upper case,
non-ASCII, none) and ten values (none, empty, booleans in two
spellings, `-1`, `2k`, words `git_bool` refuses, a good, a bad and a
hex-negative refspec). There are also other spellings of the names; ref
names at each `check_refname_format` edge and with each forbidden byte,
under each flag; and refspecs of 18 sides with `+`, `^` and none, each
alone and with 5 right sides, as fetch and as push. Cross-check, outside
the fixture: `git check-ref-format` (git 2.54.0) agrees on 362 of the 364
ref-name rows without a NUL. The 2 that differ are `-a`, which that command's
line reads as an option. git accepts a ref named `-a` (the review showed it
through the refspec route), as the reference does. `gitconfig_remote_rules_robustness`
gives 10 MB names, values and refspecs an answer in one pass.

## Design notes for the rest of the tree half

These notes are for the units that build on these two modules: `config_tree` and its includes, and the legacy
remotes (`gitconfig.py` at a983e3bb).

- Inputs the reference takes from the machine, to become arguments:
  - HOME, through `_home` (671). On Windows, if HOME is unset or empty, it
    takes `expanduser("~")`.
  - `~user` through `pwd.getpwnam` (`_user_home`, 578), which is always
    none on Windows.
  - `os.name` and `os.sep`/`altsep`: `_git_absolute` (612), `_native`
    (654), `_slashed` (1171), and the trailing-separator tests of
    `_branch_condition` (1145) and `_gitdir_matches` (1198, 1204).
  - `realpath` of the including file and of the git directory, and
    `abspath` of the git directory, in `_gitdir_matches` (1199, 1209).
    These need the current directory and link resolution.
  - The `IncludeContext` answers, each asked once: the branch HEAD names
    (`UNSETTLED` where not known), the `%(prefix)` directory (`AS_WRITTEN`
    or none) and whether git takes `hasconfig`.
  - The environment variable `GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME`
    (`_default_branch`, 1045): when it is set and not empty, it names the
    branch a `branches/` file's remote fetches by default, before the last
    `init.defaultBranch` and `master`. A name that is not
    `refs/heads/<name>`-valid gives None.
  - The legacy reader's `_Legacy` (971): `resets`, from
    `gitsystem.Sources.resets` (from git 2.46 an empty URL empties the
    list, `_add_url` at 1033; before that it is kept as an empty URL), and
    `default`, the default branch above.
- File-system operations a trait must give:
  - a non-blocking open;
  - fstat: directory, FIFO, socket or regular file, and identity (device,
    inode) for the cycle check (`_read_config`, 380);
  - a read bounded at `_FORMAT_LIMIT` + 1;
  - `access(R_OK)` (`_accessible`, 343);
  - the error kinds: missing (`path_missing`), permission, and other;
  - stat for stamps (`file_stamp`, 620);
  - realpath;
  - listdir of `remotes/` and `branches/` (`_legacy_names`, 947), and
    reads of their files bounded at 64 KiB.
- Refusals (`ConfigValues.dies`), with their texts:
  - `git cannot read {path} ({Exc})` (317, 335)
  - `{path} is a folder, which git cannot read as a config` (323)
  - `{path} is included past git's depth of 10` (379)
  - `{path} is included in itself, which git follows past its depth` (381)
  - `{path} holds a line git's parser refuses` (424)
  - `{path} sets {name}: {why}` (433, with the reasons above)
  - `{including} sets include.path with no value`, or `... an includeIf's
    path with no value` (492)
  - `{including} includes {raw!r}, which git cannot expand` (606)
  - the legacy remotes: `{name}'s legacy refspec turns on the object
    format` (1005)
  - `read_git_config`: `{path} could not be read ({Exc})`, `{path} is not
    a regular file`, `{path} is larger than 2 MiB`, `{path} is a config
    git cannot parse` (1601-1672)
  - the legacy files: `cannot be read`, `is not a regular file`, `is
    larger than 64 KiB`, `cannot be listed`, `the legacy remote folders
    name more than 256 remotes` (905-970)
- Incomplete reads (`complete = False`):
  - a file over 2 MiB;
  - a FIFO or socket;
  - more than 64 files read in all (`_INCLUDE_FILES`, 75);
  - an include value holding a tab or a CR (493);
  - a `%(prefix)/` whose prefix is not known (556).

  Under an undecided condition these go to `maybe_dies` and `undecided`,
  not to `dies`: a refusal keeps its text, and an incomplete read is
  worded `{exc} is not read whole` (517), with `{exc}` the path or value
  the read stopped at.
- What changes no answer: the format memo (`_FORMAT_MEMO`, 1590; a digest
  of the bytes read, so it only spares a parse) and the `_Once` holders.
  Stamps (`files`, `file_stamp`, `unchanged`) describe what was read for a
  caller's cache. They are not part of the values, so a port without them
  answers the same; port them when a caller needs them.
- Platform split: `_git_absolute`, `_native`, `_home`, `_user_home`,
  `_slashed` and `_gitdir_matches` call the host's `os.path`, `os.name` and
  `pwd`. The reference cannot produce its Windows answers on POSIX, so the
  tree fixture needs a POSIX-only file of real trees, plus a pure-rule file
  for the Windows path rules, gated as `settings_paths.json` and
  `settings_paths_python.json` are. Also, a folder opened as a file fails
  at the open on Windows and at the fstat on POSIX; `_accessible` exists
  for the Windows path (343-351).

## A git config tree with includes (piece 1 built)

Design for porting `config_tree`, its includes and conditions, and
`read_git_config` (`src/bettermemory/gitconfig.py` at 7855277e). Piece 1 of
the split at the end of this section is built (see "Piece 1: built");
pieces 2 and 3 are not. Line numbers were checked against the file at
7855277e.

### Data

- `_Name` (54): `(section, subsection | None, key)`, section and key lower
  case, subsection as bytes read back whole (`_spelled`, 389). Natively the
  subsection stays bytes.
- `ConfigValues` (78): an insertion-ordered map name to values (each value
  cut at its first NUL, 427, or None for a key with no `=`), plus
  `complete` (True), `dies` (None), `maybe_dies` (None), `undecided` (a map
  of the same shape), `files` (stamps, only with `stamped`),
  `asks_remote_urls`, `forbidden`.
- `IncludeContext` (126): `branch()`, `prefix()`, `hasconfig()`, each asked
  at most once and kept (`_Once`, 189) across `collect()` and `with_urls()`;
  `remote_urls` (None until collected) and `collecting`. `branch` answers a
  name, None (detached or another ref) or `UNSETTLED` (`b"\0unsettled"`,
  207). `prefix` answers a directory, `AS_WRITTEN` (`"%(prefix)"`, 532) or
  None. `hasconfig` answers True, False or None. Natively these are plain
  values given up front (an answer that is never asked changes nothing,
  since `_Once` only memoises); the port takes `Option`s in a struct.
- `_Reading` (224): files taken so far, the identity chain, `stamp_self`,
  `gentle`, the context, `forbidding`, `asks_remote_urls`, `forbidden`, and
  one `Steps` budget shared by every pattern of the read. `remote_rules`
  (248) is "a context that is not collecting".

### The rule, function by function

- `config_tree` (255): one `_Reading`, `_read_config(path, depth 0)`;
  `_Overflow` sets `complete = False`, `_Dies` sets `dies`; the read stops
  at the first of either. `asks_remote_urls` and `forbidden` copied out.
- `_config_bytes` (292): non-blocking open. Open error: None where the
  error is "missing" (`path_missing`) and `access(R_OK)` also fails
  (`_accessible`, 343), or with `gentle` and a permission error; otherwise
  dies `git cannot read {path} ({ExcName})` (317). After the open: a
  directory dies `{path} is a folder, which git cannot read as a config`
  (323); a FIFO or socket is `_Overflow(path)` (325); identity
  `(st_dev, st_ino)` recorded where `st_ino != 0`; read up to limit + 1
  bytes; a read error dies with the 317 text (335); more than 2 MiB is
  `_Overflow(path)` (339). A device is read as a file.
- `_read_config` (358): count the file first (a missing file counts too);
  past 64 is `_Overflow(path)` (371). Stamp if asked. Read the bytes;
  missing is skipped (returns). Only then: depth > 10 dies
  `{path} is included past git's depth of 10` (379), and an identity
  already on the chain dies
  `{path} is included in itself, which git follows past its depth` (381).
  Push the identity, parse, pop.
- `_parse_config` (408): `git_entries`; None dies
  `{path} holds a line git's parser refuses` (424). For each entry in
  order: append the value; with `remote_rules` and not `forbidding`, a
  `_remote_rule` refusal dies `{path} sets {raw name, utf-8 replace}: {why}`
  (433); `include.path` (no subsection) includes unconditionally;
  `includeif.<sub>.path` includes under `<sub>`; any other key of an
  `includeif.<sub>` whose sub starts `hasconfig:remote.*.url:` evaluates the
  condition for its side effect only (`asks_remote_urls`, 444).
- `_include` (463): evaluate the condition (None for plain include:
  holds). False: nothing. None (undecided): read into a separate
  `maybe` map. Then: no value dies
  `{including} sets include.path with no value` or
  `{including} sets an includeIf's path with no value` (492); a value with
  a tab or CR is `_Overflow(value to its NUL, fsdecoded)` (495); the target
  is `_include_target`. A holding condition under a collecting context, not
  already forbidding, goes to `_forbidding` (508). Otherwise
  `_read_config(target, depth + 1)` into `out` or `maybe`. Under `maybe`
  a `_Dies` or `_Overflow` is caught: `maybe.maybe_dies` is the refusal's
  text or `{exc} is not read whole` (517, `{exc}` the path or value). In
  every case `maybe`'s values and its own `undecided` are merged into
  `out.undecided`, and `out.maybe_dies` keeps its first of
  `out.maybe_dies`, `maybe.maybe_dies`, `maybe.dies` (523).
- `_interpolate` (535): a leading `%(prefix)/`: prefix `AS_WRITTEN`
  returns the path unchanged; None (or no context) is `_Overflow(path)`;
  else an absolute rest (`_git_absolute`) is the rest, otherwise
  `prefix + "/" + rest`. A leading `~`: the user is the text to the first
  `/`; empty user is `_home()` (realpath-ed when `real_home`, which only
  the gitdir pattern asks; on Windows its backslashes made `/`), otherwise
  `_user_home(user)`; None when that is None, else home + the rest from
  the slash. Anything else unchanged.
- `_user_home` (578): `pwd.getpwnam(user).pw_dir`, None on any error;
  always None on Windows.
- `_include_target` (594): fsdecode, `_interpolate(real_home=False)`; None
  dies `{including} includes {raw!r}, which git cannot expand` (606, `!r`
  is Python's repr: the port must reproduce repr of a str, including
  surrogate escapes as `\udcXX`); a relative result is joined to
  `dirname(including)` (`os.path.join`); then `_native`.
- `_git_absolute` (612): POSIX: starts with `/`. Windows: starts with `/`
  or `\`, or second char `:` and first `isalpha()` (Python's Unicode
  `isalpha`, not ASCII only: a finding to pin, see below).
- `_native` (654): replace `altsep` by `sep` (Windows `/` to `\`; POSIX
  nothing).
- `_home` (671): `HOME` if set (empty is kept on POSIX); on Windows an
  unset or empty HOME falls back to `expanduser("~")` (USERPROFILE, then
  HOMEDRIVE+HOMEPATH), None if that leaves `~`.
- `_forbidding` (683): read the target with `depth + 1` into a sink whose
  values are dropped, `forbidding` set; afterwards merge the sink's
  `undecided` and first `maybe_dies` (not its `dies`: a `_Dies` propagates
  as an exception instead), and set `reading.forbidden` if the sink or its
  `undecided` holds any `remote.<name>.url` (`_remote_url_names`, 703).
- `_condition` (1081): `gitdir:` / `gitdir/i:` with no git dir: None, else
  `_gitdir_matches`. `onbranch:`: `_branch_condition`.
  `hasconfig:remote.*.url:`: with a context, `hasconfig()` not True returns
  that answer (False or None); then `_remote_url_condition`. Anything else
  (other `hasconfig:` included): False.
- `_branch_condition` (1129): no context: None; `UNSETTLED`: None;
  None branch: False; the pattern as bytes, `**` appended where it ends in
  `/` or `os.sep`; `_any_match` against the one branch.
- `_remote_url_condition` (1150): no context: None; collecting: True;
  URLs not collected: set `asks_remote_urls`, None; else `_any_match` of
  the pattern as written against the URLs sorted as bytes.
- `_gitdir_matches` (1177): `_interpolate(real_home=True)`; `_Overflow`
  gives None; None (cannot expand) keeps the pattern as written. `./` (or
  `.` + `os.sep`) is replaced by the realpath of the including file's
  directory, slashed, and that head plus one byte is matched literally
  (`literal_head`); else a non-absolute pattern gets `**/` before it; a
  pattern ending in `/` or `os.sep` gets `**`. The texts are the realpath
  and the abspath of the git dir, slashed, deduplicated in that order;
  `_any_match` with `icase` for `gitdir/i:`.
- `_any_match` (1113): True at the first match; a spent budget is
  remembered; None if any was spent and none matched, else False.
- `read_git_config` (1601): no includes, no conditions. Open missing: no
  entries, no reason. Other open error: `{path} could not be read
  ({ExcName})` (1625). Not a regular file: `{path} is not a regular file`
  (1631). `st_size` over 2 MiB, or more than 2 MiB read:
  `{path} is larger than 2 MiB` (1633, 1645). Read error: 1641's text.
  Parse failure: `{path} is a config git cannot parse` (1650, 1661). With
  `names`: keep entries whose name is in `names` or starts with one ending
  in `.`. The memo (1589) is keyed by a digest of the bytes read and
  answers what a parse would; it changes no answer.

### Bounds

Depth 10 (66): checked after the file is found to exist, so a missing file
at depth 11 is skipped, as git skips it. Files 64 (75): counted before the
file is opened, missing files included, so the 65th include of a missing
file still makes the read incomplete. Bytes 2 MiB (1536): read to limit + 1
and refused before parsing. The `Steps` budget (`wildmatch.rs`) is shared
by every pattern of one read; spent gives None (undecided), never a
refusal.

### Machine facts, to be arguments

HOME and the Windows fallback; `~user` lookup; flavour (`os.name`, `sep`,
`altsep`); the current directory (`abspath`); link resolution (`realpath`
of the including file and of the git dir, and of HOME for gitdir patterns);
`access(R_OK)`; file identity; the context's branch, prefix and hasconfig.
`GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME` and the legacy `resets` belong to the
legacy remotes, not this unit.

### What changes no answer

`files`, `file_stamp`, `unchanged` (cache validity: the callers in
`location_git.py` 381 and 888 stamp, but no value, refusal or flag depends
on a stamp) and the format memo. The port leaves them out.

### Findings from reading, for piece 2 (not yet reproduced)

- `_git_absolute` (616) uses `str.isalpha()`, which is true for non-ASCII
  letters, while git's `has_dos_drive_prefix` takes only ASCII letters:
  `é:/x` reads as absolute on Windows here and not in git. Reading only.
- `_include_target` (606) puts Python's `repr` of the value into a refusal
  text: a native port must reproduce `repr` (quote choice, escapes,
  surrogate escapes) byte for byte.


### Piece 1: built

- `src/fsdata.rs`: `trait Fs { type File; open(path) (non-blocking,
  follows links); fstat(&mut File) -> Stat { kind, identity, size };
  read(&mut File, max) (at most `max` bytes, empty at the end);
  access_read(path); lstat(path) -> Kind }`. Errors are `PyErr`, the Python
  class the reference's call raises (`FileNotFoundError`,
  `NotADirectoryError`, `PermissionError`, `OSError`, `ValueError`, ...),
  with `missing()` = `path_missing`. `Kind`: regular, directory, FIFO,
  socket, link, other (a device). `FileId` is opaque bytes: device and
  inode on POSIX, the resolved path in `MemFs`, and on Windows the
  canonical path (Rust gives no file index without `unsafe`; the
  reference uses `st_ino`, the file index: piece 2's cycle check must
  hold the two to the same answer). `Flavour` is `githead`'s, re-exported.
- `StdFs`: opens with `O_NONBLOCK`, taken from the platform's C library
  (`bettermemory_sys::O_NONBLOCK`, `libc::O_NONBLOCK`), so a FIFO never
  blocks on any architecture.
  `access_read` is an open on POSIX (no `access(2)` without `libc` or
  `unsafe`): it answers as `access(R_OK)` for files, directories and
  FIFOs, not for a socket (the open fails, `access` succeeds); the readers
  ask it only where the open said "missing", where the two agree. On
  Windows it is CPython's `os.access`: `GetFileAttributesW`, which
  describes a link itself (a dangling link answers true), and R_OK holds
  wherever attributes are found (the read-only attribute bears on W_OK
  only); natively `symlink_metadata(path).is_ok()`.
- `MemFs`: nodes `File`, `Dir`, `Link`, `Hard` (another name of a file:
  its bytes, mode and identity), `Fifo`, `Socket`, `Device` (reads empty)
  and `Endless` (reads without end, the zero device), and `modes`, the
  owner's permission bits (POSIX only), checked as the OS checks them for
  the owner: search on every directory a component is looked up in (`.`
  and `..` included, links resolved as met), read on the file opened or
  asked by `access`. A trailing separator, in a path or a link's target,
  asks only that the last component be a directory (a final link
  followed); no `.` is looked up, so it needs no search permission, while
  `d/.` does. The empty path, and a link whose target is empty, name
  nothing (`FileNotFoundError`; on Windows assumed from `_wopen("")`
  setting ENOENT, by reading). A link to "" is pinned only natively:
  macOS answers ENOENT through it, and Linux refuses to make one
  (`symlink(2)`: ENOENT for an empty target; `path_resolution(7)` is not
  on this machine, so that is from the man page as known, not read here).
  Links are counted once across the whole path, failing past
  `link_hops`.
  A `Hard` node pointing at a directory, a missing node or another
  `Hard` reads as `FileNotFoundError`; one pointing at a link behaves as
  that link. `link_hops` (default `LINK_HOPS` = 32, macOS's
  limit; Linux's is 40) bounds the links one path passes through, then
  `OSError` (ELOOP); the generator asserts no case has 30 links or more,
  so a chain that ends is shorter than either limit and one that loops
  fails at both alike. A file in a directory's place is
  `NotADirectoryError` on POSIX, `FileNotFoundError` on Windows.
- Windows, a directory: `_wopen` refuses it (EACCES, `PermissionError`):
  `_config_bytes` dies `git cannot read {path} (PermissionError)`, or skips
  it with `gentle`; `read_git_config` says `could not be read
  (PermissionError)`. A path ending in a separator: the open answers
  `FileNotFoundError`, `access` finds a directory there, and
  `_config_bytes` dies (the case `_accessible` exists for); one naming a
  file is missing. `MemFs`'s Windows flavour does exactly this, and
  `StdFs` gets it from Rust's `File::open` (`CreateFileW` without backup
  semantics refuses a directory with ERROR_ACCESS_DENIED). All of it rests
  on reading CPython, the CRT and Rust's std: no Windows run of the
  reference is in a fixture. `fsdata_stdfs.rs` asserts the directory
  refusal on the Windows runner.
- `src/gitconfig/file.rs`: `config_bytes(fs, path, gentle) -> ConfigBytes
  { Skipped | Read { data, identity } | Dies(Dies) | Overflow { path } }`
  with `Dies::{CannotRead { path, error }, Folder { path }}` and
  `Dies::why()` giving the texts of lines 317/335 and 323; `Overflow` is
  325 and 339. `read_git_config(fs, path, names) -> GitConfigRead {
  entries, why: Option<(path, Unread)> }`, `why_text()` giving the texts of
  1625/1641, 1631, 1633/1645 (the stat's size first, then the bytes read)
  and 1650/1661. At most `FORMAT_LIMIT + 1` bytes are read, in the
  reference's loop, before any parse; a file system that hands back more
  than asked is cut to it. The format memo is left out (it answers what a
  parse of the same bytes would).
- `githead::RefFs` is left alone: its `read` takes `follow` (`O_NOFOLLOW`)
  and pre-checks with `stat` rather than opening, and its errors are
  `githead::FsError`. Unifying would mean an `open_nofollow` on `Fs`, a
  `PyErr` to `FsError` map in `githead`, and its `StdFs` reads moved to
  open-then-fstat; every githead fixture would have to be re-run to show
  nothing moved.
- For the includes unit (piece 2): on Windows `FileId` is the canonical
  path, so two hard links to one file get different identities where the
  reference's `st_ino` (the file index) gives one; a cycle through hard
  links would then be caught only by the depth limit. A real file index
  needs a Windows handle call (`GetFileInformationByHandle`), which
  belongs in `bettermemory-sys`; it is not built.

Fixture `gitconfig_file.jsonl` (`tests/contract/gen_gitconfig_file.py`,
POSIX only, 91 cases, 32,474 bytes): real trees with regular files, odd
names (space, NFC UTF-8, newline, backslash, quote), missing files and
directories of the path, trailing slashes, directories, links to files,
directories, nothing, themselves, `/dev/null` and FIFOs, link loops and
chains, links whose target ends in `/`, `/dev/zero`, FIFOs, modes 000 to
700 on files and directories, `.` and `..` through a directory that cannot
be searched, hard links (with whether two paths share an identity),
files at the bound minus
one, the bound, one past and further (recipes); for each, both readers,
the gentle read, the open's kind, `access` and `lstat`. Reasons name the
case's directory `<scratch>`.

Tests: `golden_gitconfig_file.rs` (`cfg(unix)`, class i): every case over
`MemFs`, and every case written to a scratch directory and read through
`StdFs` (two PARITY lines); `gitconfig_file_not_posix.rs`
(`cfg(not(unix))`, class os); `gitconfig_file_robustness.rs` (a 50 MB file
through a counting file system: at most limit + 1 bytes handed out, none
when the stat tells; a file system that gives more than asked; one that
errors on every call; NUL and non-UTF-8 paths in both flavours; 10,000
`..` components; link loops; the Windows flavour's directory rules);
`fsdata_stdfs.rs` (`StdFs` on every platform in a scratch directory).

### Proposed split

Read in full, this is still too large to land cleanly with fixtures, a
general file-system trait and three-platform CI in one run. Proposed:

1. Trait and one file: the `fsys` trait with `MemFs` and `StdFs`,
   `_config_bytes` (292-356) and `read_git_config` (1564-1672), with every
   refusal and incomplete read of one file; fixture of `read_git_config`
   reasons and one-file `config_tree` refusals.
2. Includes and bounds: `config_tree`, `_read_config`, `_parse_config`,
   `_include`, `_interpolate`, `_user_home`, `_include_target`,
   `_git_absolute`, `_native`, `_home` (255-292, 358-388, 408-445, 463-618,
   654-681), unconditional `include.path` only, depth, 64 files, cycles,
   the pure path-rule fixture.
3. Conditions: `_condition`, `_any_match`, `_branch_condition`,
   `_remote_url_condition`, `_slashed`, `_gitdir_matches` (1078-1212), the
   `undecided`/`maybe_dies` merge in `_include` (481-488, 513-525), and
   the collecting and forbidding pass (`_forbidding`, 683-700, 504-509),
   with the remote rules at 430-433.

## A record id's text

`store::mint` is `models.generate_ulid` (`models.py:56-64`) with its clock
and its randomness taken as arguments; it reads neither. Pure:

- `mint::ulid(ms: u64, random: &[u8; 10]) -> String`: the milliseconds
  masked to 48 bits, as the reference does before writing them (so
  `2^48` writes as `0`: the reference wraps, it does not refuse), then
  `_encode_crockford` (48-53), ten characters of time and sixteen of the
  bytes read big-endian.
- `mint::millis(seconds: f64) -> Result<u64, NotAnInstant>`:
  `int(time.time() * 1000) & ((1 << 48) - 1)` from the float: one IEEE
  product, truncated toward zero (not rounded: `0.0015` s is 1 ms, and
  `0.0009999999999999998` s is 0 ms), a negative value wrapping as
  Python's `&` of a negative int (`-0.5` s is `2^48 - 500`), a finite
  product of any size exact to its low 48 bits. An infinite product
  (`1.7976931348623157e308` included) is `Overflow` (the reference's
  OverflowError) and NaN `NotANumber` (ValueError).
- `mint::is_valid_ulid` (the `records` one, `models.py:71-75`) and
  `mint::ulid_ms`, the milliseconds `review.ulid_time` (`review.py:1184`)
  reads, which `datetime::ulid_time` now calls (its own copy of the
  alphabet and of the check is gone). The pattern takes the 32 capitals of
  Crockford's alphabet only: no lowercase, no I, L, O or U, no
  trailing newline (`\Z`), and any of the 32 first, so a valid text can
  name 50 bits of milliseconds that `ulid` never writes.

`ulid.jsonl` (`tests/contract/gen_ulid.py`, about 144 KB, not gated: the
same bytes under 3.11 and 3.13) drives `generate_ulid` itself with
`time.time` and `secrets.token_bytes` patched on the module: 2,028 pairs
(time 0, 1, 2^48 - 1 and past it, bytes all 0x00 and all 0xFF, each single
bit, each 5-bit group at every position taking every value over both
fills, and 400 pairs of a fixed congruential walk), 255 clock floats at
the truncation edges (each side of `m / 1000`, `(m + 0.5) / 1000`,
`(m + 1) / 1000`, negatives, `2^63`, infinities, NaN) and 669 texts
(every one of 24 bad characters at each of the 26 places, lengths 0, 25
and 27, lowercase, each first character). `ulid_order_and_round_trip`
checks that text order is the order of `(ms, bytes)` and that `ulid_ms`
reads back the milliseconds, over 3,004 rule-built pairs.

## HEAD and refs from a git directory's files

`githead` is the resolution half of `src/bettermemory/githead.py` (line
numbers at v9's a983e3bb): HEAD and the refs its chain names, read from
the files of a git directory the caller has already found. It runs no git
and reads every file through a trait the caller gives (`RefFs`).

### Cut

`githead.py` is two modules in one file. Discovery and the signature
(`stamp` 211, `_check_environment` 243 to `_located` 669, `signature`
1129, `_ref_stamps` 1097) walk directories, read `.git` files, check
owners, ceilings and file systems, read the environment and stat for
change detection: that is discovery's unit and is not ported here.
Resolution (692-1089) takes a `GitDir` (`gitdir`, `commondir`) and reads
files only: that is this unit. It borrows from `gitfile_rule.py` the
reader `_read` (1083), `_REF_LIMIT` (62, 64 KiB), `_SPACE` (68,
`b" \t\n\r"`) and `_Unsettled` (1078), and from `_location_probe.py`
`path_missing` (100). `head_commit` (1042) peels a tag through the object
reader, which is not this unit: native returns the hash to peel as an
outcome (`HeadCommit::Peel`).

### Inputs

- Files under the git directory: `HEAD`, and a loose ref whose name starts
  with `refs/bisect/`, `refs/rewritten/` or `refs/worktree/`
  (`_PER_WORKTREE`, 157).
- Files under the common directory: every other loose ref, and
  `packed-refs`.
- Nothing else. The reference reads no `config`, so neither
  `extensions.refStorage` nor `extensions.objectFormat` nor the git
  version is an input; no environment variable, no clock. A `reftable`
  repository is met only through its files: its `HEAD` is
  `ref: refs/heads/.invalid`, whose name fails the name rule, so every
  answer declines and the caller asks git. Any other ref storage is the
  same: whatever its HEAD holds is read by the files rule and, where it
  is not a valid files-backend HEAD, declined.
- Platform: the reference has no branch of its own on the platform here;
  the file system answers differently. `_read` opens with `O_NOFOLLOW`
  where the OS has it (POSIX: a symbolic link fails with ELOOP; Windows:
  followed), and a missing directory in the path is `NotADirectoryError`
  (ENOTDIR) on POSIX but `FileNotFoundError` on Windows. Native takes
  those as the file system's answers: `RefFs` reports `NotADirectory`, or
  `Missing` the way Windows does, and `MemFs` takes a `Flavour` so both
  answers are tested on every platform. Case folding and Windows reserved
  names are not considered by the reference (a name is opened as given).

### The rule

- `head_bytes` (692): `HEAD` read with no follow of a link, at most
  `_REF_LIMIT + 1` bytes; any error, or more than 64 KiB, is None. A
  symbolic link (git's oldest HEAD) is None on POSIX.
- `_valid_refname` (705): no `..`, no `@{`, no trailing `.`, no byte of
  0x00-0x1f, 0x7f, space, `~^:?*[\`; every `/`-component non-empty, not
  starting with `.`, not ending in `.lock`. Bytes 0x80-0xff pass. A
  one-component name passes this function, but `_parse` requires `refs/`.
- `_parse` (719): trailing `_SPACE` stripped. `ref:` then leading
  `_SPACE` stripped, a name under `refs/` that passes the rule: symbolic.
  Else 40 or 64 lowercase hex digits and nothing else: direct. Anything
  else declines (`_Unsettled`): uppercase hex, text after a hash, a hash
  of another length, `ref:` to a one-level name such as `HEAD`.
- `_lookup` (909): a loose file (no follow) at `gitdir/name` for a
  per-worktree name, else `commondir/name`. Missing (`path_missing`) or a
  directory: a per-worktree name declines, any other reads `packed-refs`.
  Any other error raises (declines); `NotADirectoryError` is not caught,
  so on POSIX a file where a directory of the name should be declines
  while on Windows it reads as missing. Over 64 KiB declines, else
  `_parse`. The loose file wins over `packed-refs`.
- `_packed` (855): `commondir/packed-refs` read (following links) to at
  most `_PACKED_LIMIT + 1` (64 MiB + 1) bytes. Missing: no record. Empty:
  no record. Declines when: longer than 64 MiB; not ending in `\n`; a
  first byte `#` not followed by `# pack-refs with:`; not the file shape
  (`_PACKED_SHAPES`: an optional header line, then records of exactly 40
  or exactly 64 lowercase hex, a space, a name of bytes outside
  0x00-0x20, 0x7f and `~^:?*[\`, `\n`, each followed by at most one `^`
  peeled line of a hash as long; all records one length); any of `..`,
  `@{`, `//`, `/.`, ` .`, `/\n`, `.lock/`, `.lock\n`, `.\n` anywhere in
  the file (`_BAD_NAME`); a header whose traits include `sorted` with
  record names not strictly rising bytewise (`_packed_in_order`, which
  also counts the name after a hex-run-and-space at any line start). Then
  the lines ending ` name\n`: a second one, or one whose text before the
  space is not a full hash, declines; one is the answer. CRLF files fail
  the shape. The verdict caches (`_PACKED_READS`, `_PACKED_STAMPED`) only
  memoise; they are not ported (no answer depends on them).
- `_resolve` (932): HEAD parsed; each symbolic step is a read; at the
  fifth read (`_MAX_READS`, 154, HEAD's included) a further step
  declines, so a chain of HEAD and four refs answers and a fifth ref
  declines. A cycle declines by the same bound. Returns (name, hash),
  (name, None) unborn, (None, hash) detached.
- `head_sha` (1022): `_resolve`'s hash where HEAD is detached or ends
  under `refs/heads/`; None otherwise and on every decline or OSError.
- `head_ref` (1080): the name the chain ends at, decoded as UTF-8; None
  when detached, on any decline, and on a name that is not UTF-8
  (`ValueError`).
- `head_unborn` (952): None when `head_bytes` is None or `_parse` refuses
  HEAD. Follows the chain as `_resolve`; past the bound None. Where a
  `_lookup` declines (`_Unsettled`, not an OSError): False when a loose
  entry that is not a directory exists for the name (`_loose_ref`,
  1011, lstat) or `packed_names` holds it, else None. An ended chain is
  True when the ref has no hash, False otherwise; detached False. Any
  OSError or ValueError: None.
- `packed_names` (994): `packed-refs` read to `_PACKED_LIMIT + 1` bytes;
  True when some line ends ` <name>` with an optional `\r` before its end
  (a line is `[^\n]*`, `re.MULTILINE`); False on any error.
- `head_commit` (1042): `head_sha` if it answers; else `_resolve` and, for
  a named ref with a hash, the object peel through `gitobjects` up to
  `_PEEL_DEPTH` (8) tags. Native: `Answer(hash)`, `Peel(hash)` or `None`.

### Bounds and declines

64 KiB + 1 read of `HEAD` and of each loose ref; 64 MiB + 1 read of
`packed-refs` (the trait is asked for at most that many bytes, so a file
that claims more costs no more); five reads per chain. The work on
`packed-refs` is linear in its length (the shape, the bad-name scan, the
order check and the needle search are each one pass). The reference has
no "ask git" call in this half: every decline is None (`_Unsettled`,
`OSError`, `ValueError`) and the caller runs `git symbolic-ref -q HEAD`
or `git rev-parse`. Native names that outcome `Decline` with no reason,
as the reference words none.

### The port

`src/githead.rs` and `src/githead/fs.rs`. `RefFs` has two methods:
`read(path, limit, follow) -> Result<Vec<u8>, FsError>` (at most
`limit + 1` bytes; `FsError` is `Missing`, `NotADirectory`,
`IsADirectory` or `Other`) and `lstat_is_dir(path)`. `StdFs` checks the
kind before it opens (so a FIFO never blocks), refuses a link with
`follow` false on unix only, and maps `NotFound`, `NotADirectory` and
`IsADirectory`. `MemFs` is paths to `File`, `Dir`, `Link` or `Special`,
with a `Flavour` (`Posix`, `Windows`) for the two answers that differ.
Entry points take `(&dyn RefFs, &GitDir)`: `head_bytes`, `resolve`,
`lookup`, `head_sha`, `head_ref`, `head_unborn`, `packed_names`,
`head_commit` (`HeadCommit::Answer`, `Peel` or `Decline`), plus `parse`,
`valid_refname`, `packed_reads` and `full_hash`. Not ported: discovery,
the signature and `stamp`, the object peel, and the verdict caches.

### Fixture and tests

`tests/contract/gen_githead.py` writes each case as real files in the
scratch world and runs the reference. `githead.jsonl` (914 cases, 572
declined; about 211 KB; not gated: regular files, directories and missing
files only, ASCII names every file system stores as given, no name that
differs from another only by case; the same bytes under Python 3.11 and
3.13) is compared under both flavours, since the reference reads those
cases alike everywhere. `githead_posix.jsonl` (63 cases, 19 declined; about 22 KB;
POSIX only) holds links, FIFOs, ENOTDIR, names with `<>|"`, non-ASCII
and not UTF-8, Windows device names and long names; it is compared on
unix with the POSIX flavour, and `githead_not_posix` records the gap as
class os elsewhere. The cases are built by rule: each HEAD prefix and
whitespace, each hash length and alphabet as HEAD and as a loose ref,
names at the edges of the name rule, every header against every record
set of `packed-refs` (peeled, unsorted, CRLF, duplicate, truncated, bad
names, both hash lengths), loose against packed, chains of 2 to 8 reads
and cycles, the worktree layouts and per-worktree prefixes, reftable and
`objectFormat` configs, and each file at its bound and one past (64 KiB
for `HEAD` and a loose ref, 64 MiB for `packed-refs`, as recipes).
`githead_robustness` serves files claiming 1 GB, a 50 MB `packed-refs`,
cycles and a chain of 1000 refs (counting reads: never more than five),
NULs and bytes that are not UTF-8, a file system failing every call with
each error, and the Windows flavour's answers to a link and ENOTDIR; and
checks three hostile `packed-refs` shapes take at most 30 times as long
for 10 times the size (best of five, plus 50 ms). `githead_stdfs`
writes a git directory in a scratch directory of its own.

### What only a Windows run can confirm

The reference's Windows answers come from CPython's `os.open`, `os.lstat`
and the Win32 errors behind them; no fixture holds them, since the
generator cannot run the reference on Windows from here. Native follows
them by reading:

- A loose ref that is a symbolic link to a directory: `os.open` follows
  it and raises PermissionError, `_is_directory(follow=False)` sees a
  link, and the error rises, so `_lookup` declines rather than reading
  `packed-refs`. `MemFs`'s Windows flavour and `StdFs` on Windows answer
  `Other` there (a directory link's junction kin is not modelled).
- A name Windows refuses (`<>|"`): CPython raises FileNotFoundError
  (ERROR_INVALID_NAME), read as missing, so `packed-refs` answers. `StdFs`
  maps `ErrorKind::InvalidFilename` to `Missing` on Windows only (on
  POSIX it is ENAMETOOLONG, which the reference does not read as
  missing).
- Path bytes that are not UTF-8: CPython raises ValueError and the
  reference declines; `StdFs` on Windows and the Windows flavour answer
  `Other`. A NUL in a path is ValueError on every platform: `Other`.
- Not modelled by the Windows flavour: case folding, the stripping of
  trailing dots and spaces, reserved names such as `NUL` and `CON`, and
  link loops (winerror 1921, whose exception class is unconfirmed; the
  flavour answers `Other`).
- What Windows CI runs: `golden_githead` over `githead.jsonl` with the
  Windows flavour of `MemFs`, `githead_robustness`, and `githead_stdfs`,
  whose `cfg(windows)` tests create a directory link (and skip with a
  set-aside where the runner refuses link creation) and open a name
  with `<>|"`. `githead_posix.jsonl` is not read there.

### Where the reference and git 2.54 differ

Checked apart from the fixture: each case's files laid into a fresh
`git init` repository (neutral configuration; worktree layouts, FIFOs and
the 64 MiB files skipped), and every answer the reference gives held to
`git symbolic-ref -q HEAD` and `git rev-parse --verify -q HEAD`. Of 905
cases, 44 differ. 33 are hashes of 64 digits in a SHA-1 repository,
which the reference returns as written by design (`githead.py:76`). The
other 11, which native reproduces:

- A `packed-refs` header of `# pack-refs with:` with no space after the
  colon (`# pack-refs with:\n`, `# pack-refs with:sorted\n`): git dies
  ("unexpected line"); the reference answers (`githead.py:889` checks the
  prefix without the space, and `_packed_shape`, 747, admits it).
- A loose ref beside a `packed-refs` git cannot read (a bad line, a
  directory, a link through a file): `git rev-parse --verify HEAD` dies,
  since it reads `packed-refs` for the other spellings of `HEAD` it
  checks; the reference answers from the loose file (`_lookup`, 917-929,
  reads `packed-refs` only when there is no loose file).
- A `config` naming `extensions.refStorage = reftable` beside files-backend
  refs: git reads no files there; the reference reads no config
  (`head_ref`, 1080). A real reftable repository's `HEAD` declines.

## A location capture's vocabulary

Read at ececad8c. The pure part is about 1,150 lines of reference logic,
so it was split; parts (a) and (b) below are built (`src/capture/`),
part (c) is not.

- `location_git.FileSeen` (1188-1198) and `Capture` (1201-1385): the
  capture's fields with their defaults. `to_token` (1268-1322) writes
  `v` (`_location_probe.TOKEN_VERSION`, 1) then 28 keys in a fixed order;
  keys become `[device, inode, birth]` or null, tuples become lists,
  `manifest` `{"name", "entries"}`. `kept_apart`, `tree_recorded`,
  `config_files` and `config_sources` are not in the token. Pure.
- `from_token` (1324-1385) with helpers `_text` to `_manifest_from`
  (1388-1451): refuses (None) anything not a mapping or whose `v` is not
  the version; a missing `cwd` or `state` is a refusal, every other key
  defaults. Counts are bounded (`ref_tips` 64, `remotes`/`aliases` 256,
  `stamps` 8, `files` 64, `file_errors` 64, manifest entries 4,096).
  Python's rules leak in and must be ported as such: `shallow`,
  `evicted`, `boundary`, `file_tracked` take any value's truth; stamp
  numbers go through `int()` (`True`, `1.9`, `"7"` are accepted);
  `_key` takes `str()` of any value, so `[[[]], 1, 1]` is the key
  `("[[]]", "1", "1")`; hashes are lowercased before matching. Pure,
  and the same under Python 3.11 to 3.14 except how deep `str()` may
  nest (below).
- `location.remote_key` (378-405) with `_SCHEME_URL`/`_SCP_URL`/`_DRIVE`
  (366-370): the hosted branch (`host/rest`, lowercased, `/` stripped,
  one `.git` dropped) is pure. The local branch is not: `_local`
  (408-411) calls `os.path.realpath`, which reads the file system, and
  `_file_url_path` (422-459) reads `os.path.sep` and Python's
  `url2pathname`, which changed in 3.14. No `insteadOf` is applied here
  (aliases arrive already keyed in the capture). Proposed: port the
  hosted branch and the classification (hosted or local, and the
  pre-realpath path for a local one); leave realpath to a reading unit.
- `membership_records.ObjectKey` (288-332): three text fields, `compare`
  and `on_any_device`; it has no text form or parse of its own (the
  token's three-item list is its only spelling). Pure.
- `location.to_location` (1355-1411): field-by-field copy into
  `membership_records.Location` (447-539) with `frozenset`s for remote
  and alias keys; `history` is an opaque reader, left out. Pure.
- `_paths.norm`, `inside`, `basename` (22-58): separators read off the
  spelling, never the host. Pure, no flavour needed.
- `membership_lines` (660 lines) and `_printable.printable`/`quoted`
  (19-130): every `line_*` is a function of its arguments. Pure, and on
  its own near the guide.

Proposed split: (a) capture types, token both ways, `ObjectKey`,
`to_location`, `_paths` (about 420 lines); (b) `remote_key`'s pure part
(about 50 lines, many fixture cases), may ride with (a); (c)
`_printable` and `membership_lines` (about 780 lines).

Findings (reproduced at 3.13.13): `Capture.from_token` raises
`OverflowError` for a stamp number that is a non-finite float (JSON
`Infinity` as `json.loads` reads by default), and `RecursionError` for a
key item nested about 100,000 deep (`str()` in `_key`, location_git.py
1430); neither is in its `except` (1384), so a hostile token raises
where a refusal is documented.

### Built: parts (a) and (b)

`capture` (`src/capture/mod.rs`): `Capture`, `FileSeen`, `Stamp`,
`Manifest`, `State`, `Kind`, `Key`; `Capture::to_token(&self) -> PyValue`
(the dict, keys in `TOKEN_KEYS` order) and `Capture::from_token(&PyValue)
-> Result<Option<Capture>, PyError>`: `Ok(None)` where the reference
returns None, `Err(OverflowError | RecursionError)` where it raises (the
findings above, pinned in the fixture, not smoothed over). Strings are
code points (`PyStr`, a lone surrogate kept), ints canonical decimal text.
Python's rules are ported where they show: `bool()` of any value for the
flags, `int()` (`pyvalue::py_int`, with its 4,300-digit limit) for stamp
and manifest numbers, indexing a str or a dict where a list was meant,
`1`, `True` and `1.0` all being the version, and `str()` of any key item
(`capture::str_of`). `str_of` raises ValueError (a refusal) for an int
past 4,300 digits, as `str()` does; `pyvalue::py_str` and `repr` apply
no such limit, and a general fix there would touch every caller that
spells a `PyValue::Int` (the JSON writer, `repr`, the log payload), so it
is left to its own change. It raises RecursionError past the port's own
depth limit, 9,999 levels at a top-level key and 9,997 inside ``files``
(`STR_DEPTH_TOP`, `STR_DEPTH_FILES`): what CPython 3.13 does on Linux and
macOS on a fresh thread, not a parity claim, since its C limit is 3,000
on Windows, less on ARM64 and debug builds, and a caller deeper in C has
less. The reference's RecursionError here is a property of the
interpreter build and its stack, not of the reference: it differs
between Python 3.13 and 3.14, and under 3.14 between Linux (where CI saw
`from_token` return None for a key item 100,000 deep) and macOS (where
it raises). It is pinned only in the gated file. Native answers
RecursionError at its own fixed depth on every platform; the boundary is
a native test. `config_files` (stamped files) is the file-reading unit's and is
not carried; `kept_apart`, `tree_recorded` and `config_sources` are
fields but not token keys, as in the reference.

`capture::remote::remote_key(Option<&[u32]>) -> Option<RemoteKey>`: the
hosted branch exactly (`_SCHEME_URL` and `_SCP_URL` as `re` matches them,
`[a-z]` under IGNORECASE taking U+0130, U+0131, U+017F and U+212A, `\d`
any Unicode digit, `str.strip`, `str.lower`), and the decision that a URL
is local: `RemoteKey::Local { text, file_url }` carries the stripped URL
unchanged, which the golden test compares with what the reference hands
`_local` and whether it called `_file_url_path`. The local key itself is the file-reading unit's: `_local`
joins with the host's `os.path` and calls `realpath`, and
`_file_url_path` reads the host's separator and
`urllib.request.url2pathname`, which changed in Python 3.14.

`capture::paths::{norm, inside, basename}`: `_paths`, separators read off
the spelling, so no flavour. `capture::location`: `ObjectKey` with
`compare` (`Sameness`) and `on_any_device`, `ProjectFileRef`,
`Location` (its `frozenset`s as `BTreeSet`s, `born`) and `to_location`;
the `history` reader and `Location`'s properties that read `_same_path`
are the resolving unit's.

Fixtures (`tests/contract/gen_capture_vocabulary.py`):
`capture_vocabulary.jsonl` (204,366 bytes, not gated; checked identical
under 3.11, 3.12 and 3.13) holds 72 captures built through `Capture` and
read back, 803 hostile tokens (among them every refused field paired
with every field that raises OverflowError, which pins the order
`from_token` reads in, `int()` and `str()` of floats, texts and ints at
the digit limit, and nesting 50 to 400 deep), 8 `to_location` cases,
1,303 URLs, 25 paths (each against all 25 for `inside`) and 56 key
pairs. `capture_vocabulary_python.jsonl` (17,918 bytes, gated on the
Python release) holds the 119 tokens whose answer is RecursionError.
Tests: `golden_capture_vocabulary` (class i and ii) and
`capture_vocabulary_robustness`
(5: every key with every wrong type, 100,000-deep nesting on a 512 KiB
thread, 10 MB texts and URLs, the native depth and digit limits at their
boundaries).

Left for later: part (c) is built in "Text made safe to show" below,
less `line_restated`; the local
branch of `remote_key`, `_file_url_path`, the capture's discovery, its
cache and `checkout_token`, which read files.

## The plain record writers

The public record writers of `store_records.py` (read at faa1b32e), ported
as functions on `WritableStore`, each running one `Transaction`
(`src/store/writers.rs`). Built: `put_memory`, `tombstone`,
`delete_tombstone` (part A, below). Designed, not built: `write`,
`update`, `mark_verified`, `record_corroboration`, `_active_or_raise`
(part B, after the provider gains its mint in another unit). Every writer below runs inside one
`_transaction`, so every row it appends is followed at commit by the
returned marks its puts and removals name (`_flush_returns`, one wall
clock read when anything is named and the `returned` table exists), and
the head moves once. Every row's `ts` is the log clock, read once per row.

Clocks, as the reference reads them. `utcnow` (`models.py:879`) is
`datetime.now(timezone.utc)`, imported by `store_records`; `tombstone`'s
`datetime.now` is read in `store_records`; both are the wall clock, and
`_fixed.py` patches both onto the `wall` ticker. `generate_ulid`
(`models.py:56-64`) reads `time.time()` once (milliseconds, masked to 48
bits) and ten bytes of `secrets.token_bytes`: a third read of the system
clock, separate from `utcnow`, so in production an id's milliseconds and
its `created` can differ; `_fixed.py` replaces the mint whole (counted
ULIDs under `01M`). The provider must therefore gain `utcnow()` (default:
`wall_now`) and `ulid()` (default: `mint::ulid(mint::millis(system
time), random bytes)`; `Fixed` and the fixture: the counted mint), with
`Fixed`, `System` and `Clocks` unchanged in what they already answer.

Reads per call (wall / mint), before the marks' stamp: `write` 1 / 1;
`update` 1 / 0, after the snapshot check; `mark_verified` 1 / 0, after the
check; `record_corroboration` 1 / 0, none when the session already
corroborated; `tombstone` 1 / 0 without `now`, 0 with it; `put_memory`
and `delete_tombstone` 0 / 0.

- `put_memory(memory, provenance=None, filename=None, session=None,
  home=None, standing=None) -> Memory` (114-154, 157-199). Refuses a
  provenance outside `{imported, local, synced}` (`_WRITABLE_PROVENANCE`,
  `store_schema.py:142`) before any transaction: ValueError "provenance
  must be one of ['imported', 'local', 'synced'], got {p!r}" (139-142, a
  Python `repr` of caller text). In the transaction: a `tombstones` row
  for the id refuses with TombstonedError "memory {id} was removed:
  {removed_reason}; restore it to bring it back" (172-179). An existing
  `memories` row supplies the provenance when none is given (`or`: an
  empty string also falls back) and the filename when None (183-185).
  Appends one `memory_put` `{**marks, "memory": record_payload(memory),
  "provenance": provenance or "local", "filename": filename}` (186-193).
  Returns the caller's memory unchanged. `home` / `standing` set: refused
  here as not ported (`_home_row`, `_standing_row` are another unit).
- `tombstone(memory_id, reason, session=None, now=None) ->
  TombstonedMemory` (500-547). Reads the row through `_MEMORY_SELECT`
  (101-104, the attestation by `log_mac`); none: a tombstone row refuses
  with TombstonedError "memory {id} was removed: {reason}" (no restore
  hint), else NotFoundError(id). The record read (`_row_to_memory`), the
  removal time `now or datetime.now(utc)`, the tombstone built from every
  record field plus `removed`, `removed_reason`, `removed_session=session`.
  Appends `memory_delete {"id"}` then `tombstone_put {"tombstone":
  record_payload(dead), "provenance", "filename", "links":
  json.loads(links_json), "corroborations", "last_corroborated"}` (the
  last three from the row's columns as stored). Returns the tombstone.
  No id check: an invalid id is simply not found.
- `delete_tombstone(memory_id, session=None) -> None` (744-760):
  `_require_row` NotFoundError(id) unless a `tombstones` row; then
  `tombstone_delete {"id"}`.
- `write(content, scopes, confidence=medium, source=explicit, origin,
  category, claims, links, actor, session, home, standing) -> Memory`
  (767-808): one `utcnow` for `created` and `updated`, one
  `generate_ulid`, `body = content.strip() + "\n"` (Python `str.strip`:
  Unicode White_Space), `actor` a dict validated as Actor, links each a
  MemoryLink or validated as one, `claims` copied or `[]`; the Memory
  constructor validates (pydantic ValidationError); then `put_memory(...,
  provenance="local")`.
- `update(memory, force=False, preserve_verification=False, session=None,
  standing=None) -> Memory` (811-859): `_active_or_raise`; unless `force`,
  a stored `updated` unequal to the snapshot's raises
  ConcurrentUpdateError "memory {id} was updated concurrently (your
  snapshot is stale; stored updated={isoformat})" (`store_schema.py`
  1038-1052). Carries `updated=utcnow()`, the stored corroboration
  rollup and, with `preserve_verification`, the stored verification
  fields, claims and `verified_head`; `model_copy(update=...)` (no
  validation) then `Memory.model_validate(dump)` as a check only: the
  record written and returned is the unvalidated copy. Then `put_memory`
  (nested: the tombstone check again, provenance and filename kept).
  Supersedes links changed name records whose returned marks are written
  or dropped at commit.
- `mark_verified(memory_id, verified_paths, verified_commits,
  verified_versions, verified_absent_paths, verified_path_targets,
  verified_absent_path_targets, claims, verified_head,
  expected_last_verified_at, expected_updated, check_expected, session)
  -> Memory` (862-951). Before the transaction, `verified_head` is
  stripped and lowercased and must be 40 or 64 hex: ValueError
  "verified_head must be a full commit hash (40 or 64 hex characters),
  got {head!r}" (the normalised value's repr). Then `_active_or_raise`;
  with `check_expected`, a stored `last_verified_at` unequal to the
  expected one (None included) or, when `expected_updated` is given, an
  unequal `updated` raises ConcurrentUpdateError. One `utcnow`; each list
  given replaces the stored one; a target map is set (or set to None)
  when it or its paths list is given; `verified_head` is always written
  (None clears it). Copy, validate as a check, read provenance and
  filename, append `memory_put` then `verification_put {memory_id,
  machine_id: None, verified_at: _iso(now), verified_head, the four
  lists, each non-None target map}`. Returns the copy.
- `record_corroboration(memory_id, session=None) -> Memory | None`
  (966-999): `_active_or_raise`; with a session, `CORROBORATION_CHECK_SQL`
  (89-95) with the needle `canonical_payload({"corroboration": id})[1:-1]`
  returns None with nothing written when found (the transaction keeps no
  row, the head does not move); else `corroborations + 1`,
  `last_corroborated=utcnow()`, `_put_memory` with the mark
  `{"corroboration": id}` (the tombstone check cannot fire: the record is
  active under the lock).
- `_active_or_raise(memory_id)` (1011-1026): not a valid ULID:
  MemoryNotFoundError "invalid id: {id!r}"; an active row: the record;
  a tombstone: TombstonedError "memory {id} was removed: {reason}"; else
  MemoryNotFoundError "no memory with id {id}".

Refused as not ported, nothing written: `home` and `standing`
arguments, `put_tombstone`, `restore`, `restore_trimmed` and relink,
`rename_scope`, the prunes, `batch()`, and what a transaction refuses.

Mechanisms this needs beyond the transaction: (1) the provider's
`utcnow` and `ulid`; (2) a caller-side record input surface: building a
Memory from `write`'s arguments (actor and links as values, Python
`strip`), and `model_copy` followed by a validation whose result is
dropped, so the record written may differ from the validated one; (3)
`verification_put`'s stamp and the target-map rule. Split proposed:
part A `put_memory`, `write`, `tombstone`, `delete_tombstone`
(114-199, 500-547, 744-808; mechanisms 1 and the `write` half of 2);
part B `update`, `mark_verified`, `record_corroboration`,
`_active_or_raise` (811-1026; the copy-and-check half of 2, and 3).

### Part A, built

- `WritableStore::put_memory(provider, &Memory, Put { provenance,
  filename, session, home, standing }) -> Done<Memory>`: `provenance` is
  any Python value a caller can pass (`Option<&PyValue>`): a list or dict
  (also inside a tuple) is TypeError "unhashable type: 'list'", anything
  but a writable label ValueError with the crate's `repr`. `home` or
  `standing` given: `WriterNotPorted::Home` / `Standing`, before the
  transaction. A stored empty label falls back to `local` (the `or`
  chain as written); a blob label is `WriterNotPorted::Blob`.
- `WritableStore::tombstone(provider, id, reason, session, now:
  Option<DateTime>) -> Done<Tombstone>`: the wall clock through the
  transaction's `wall_now` (added to `tx.rs`), once, after the row is
  found, only when `now` is None.
- `WritableStore::delete_tombstone(provider, id, session) -> Done<()>`.
- `RecordWriteError`: `Raised { class, message }` (the reference's
  exception; `text()` is `str(exc)`, a KeyError subclass's being its
  message's `repr`), `Record` (the stored row raised in `_row_to_memory`),
  `NotPorted`, `Write` (the transaction's errors). `Done { value,
  committed }` carries the transaction's rows and head move.
  `*_hooked` forms (hidden) take a boundary hook for the kill test.

- Order of refusals: `home` and `standing` are refused as not ported
  where the reference would first write them, after the tombstone check
  and the record's payload (`_put_memory` 172-192) and before any row, so
  a call the reference refuses for another reason (a bad label, a removed
  id, a target map) gets that reason. A `standing` answer reads the wall
  clock once before the transaction, as `_standing_row` stamps it
  (`store_records.py:221`); `_standing_row`'s own checks (202-213) are not
  ported, so an answer it refuses over a removed id gets TombstonedError
  natively.
- `validate_path_targets` (`models.py` 1157-1175), reached through
  `record_payload` by both writers, is reproduced with its texts: "{field}
  capped at 64 entries (got N)" (reachable only by a hand-built native
  record: the model caps first) and "{field} must cover every legacy
  attestation". `record_payload` raises nothing else for a validated
  record.
- Not reproduced, by design (hand-edited rows): a stored provenance that
  is a BLOB gives `native:not_ported` (`WriterNotPorted::Blob`) where the
  reference raises ValueError `unknown provenance "b'local'"`; in
  `tombstone`, a row with `corroborations='x'`, `links_json=5` or
  `last_corroborated='garbage'` refuses with the reference's class but
  not the interpreter's text.

Tests: `golden_store_records.rs` (`store_records.jsonl`, 13 cases, 73
calls, classes ii: Unicode and pydantic-core gates, POSIX only as
`store_tx.jsonl`; `store_records_not_posix.rs` records the gap);
`store_records_cross.rs` (class i, both directions);
`store_records_kills.rs` (120 kills, POSIX only); `store_records_robustness.rs`.

## Text made safe to show, and the membership lines

Read at 851c171d. `_printable.py` (128 lines) and `membership_lines.py`
(660 lines). Nothing in either reads the machine: no clock, terminal
width, locale, environment variable or file. Every function is a function
of its arguments; the only data consulted is the Unicode database, through
`printable` and `quoted`.

### Design

`_printable`:

- `printable(text: str) -> str` (19-43): each character of category Cc,
  Cf, Cs, Zl or Zp (`unicodedata.category`), the tab excepted, is written
  as `ch.encode("unicode_escape")`: `\n`, `\r`, `\xNN` below U+0100,
  `\uNNNN`, `\UNNNNNNNN`, lowercase hex; a lone surrogate is `\udNNN`.
  Everything else is kept. No truncation (there is no length bound in
  either module).
- `_quote_like(ch, base)` (92-97): U+030B and U+030E are quote-like unless
  `base` (the last character written that is not a mark) is
  `str.isalnum`; otherwise a character is quote-like if it is in
  `_QUOTE_LIKE` (`_QUOTATION_MARKS` and `_QUOTE_CONFUSABLES`, 38 code
  points, 51-78) or of category Pi or Pf.
- `quoted(text: str) -> str` (100-125): `json.dumps(printable(text),
  ensure_ascii=False)` (escapes `"`, `\` and C0 controls), then each
  quote-like character of that, its quotes included, as `json.dumps(ch)`
  without its quotes (`\uNNNN`, a surrogate pair for an astral one);
  `base` becomes the last character written unless its category starts
  with M.

Unicode data consulted: `unicodedata.category` (both), `str.isalnum` (the
base of U+030B/U+030E). Not consulted: `str.isprintable`, `str.isspace`,
east-asian width, normalisation.

`membership_lines` (callers pass `str` for paths, names, ids and keys,
`str(Path)` where they hold a path; `int` counts; tuples of `str`):

- `folder_said(folder: str | None, ref: str | None)` (75-92): the lookup
  command only when `ref` matches `[A-Za-z0-9_]+` (ASCII); the display
  part only when `folder` is not None. `run_in` (95-97): `in` and that.
- `_named(name, project_id)` (105-108): `quoted(name) (printable(id))`.
- `_answer(question, answer, target=None, *, said=False)` (111-126):
  `id=` only when `question` is truthy (an empty id is left out),
  `target=` likewise, `said=` a fixed text.
- L1 `line_first_sight(root, candidates: [(name, id)], evidence, *,
  unwritten, filed, question)` (129-157); L2 `line_new(root, name,
  project_id, *, previous: [(checkout, name, id)], unwritten)` (160-186);
  L3 `line_file_contradiction` (189-204); L4 `line_joined(root, name,
  project_id, codes)` (207-214: `WORDS[c] for c in sorted(codes) if c in
  WORDS`, code-point order, duplicates kept); `line_alias_joined`
  (217-237); `line_alias_held(..., where: (machine, path), shown)`
  (240-271: `shown` less a leading `"bettermemory: "`, through
  `printable`); L5 `line_remote_diverged` (274-291); L6 `line_legacy(...,
  memories, episodes, ..., held, ..., recorded=None)` (294-318: `recorded`
  None reads `memories`); L7 `line_subproject` (321-341); L8
  `line_unreadable` (344-350); L9 `line_deferred` (353-360); L10
  `line_import(name, source, candidates: [(name, id, codes)], *,
  question)` (363-382: each candidate's codes sorted and joined by `+`,
  the first candidate's id the join's target, none when there is none);
  `line_unresolved` (385-404).
- `REASON_SAID`, `REASON_UNNAMED`, `line_restated` (407-594): a question
  shown again from its row, branching on the kinds of
  `membership_records`, with sentence rewrites for another checkout.
- The lines without a number (597-660): `line_folder_project`,
  `line_new_folder`, `line_boundary_added`, `line_boundary_dropped`,
  `line_across_boundary` (its `named` already `_named` spellings, through
  `printable`), `line_content_crowded`.

No line takes a reference object (a resolution, a proposal, a location):
every argument is text, a count or a tuple of text, so the native
functions take `&[u32]` (a `PyStr`), `i64` and three small structs for
the tuples (below).

### Cut

Built: `_printable` whole (19-125) and `membership_lines` 66-405 and
597-660, about 470 lines of logic. Left for the next unit:
`REASON_SAID`, `REASON_UNNAMED` and `line_restated` (407-594). The cut is
by dependency rather than top-down: every other function is a flat
template over `quoted`, `printable`, `_named`, `_answer` and `run_in`;
`line_restated` alone carries the reason table (which a Python test holds
equal to every reason the package opens a question with), the question
kinds and the "here"/"this" rewrites, and is the one function whose
callers pass a stored row.

### Built

`printable` (`src/printable.rs`): `printable(&[u32]) -> PyStr`,
`quoted(&[u32]) -> PyStr`, `printable_into`/`quoted_into` (appending),
`unprintable(cp)`, `QUOTE_LIKE` (sorted) and `QUOTE_COMBINING`; the
crate's Unicode 15.1.0 `category` and `is_alnum`.

`membership_lines` (`src/membership_lines.rs`): `folder_said`, `run_in`,
`named`, `answer`, `line_first_sight`, `line_new`,
`line_file_contradiction`, `line_joined`, `line_alias_joined`,
`line_alias_held` (`where` as `machine`, `path`), `line_remote_diverged`,
`line_legacy`, `line_subproject`, `line_unreadable`, `line_deferred`,
`line_import`, `line_unresolved`, `line_folder_project`,
`line_new_folder`, `line_boundary_added`, `line_boundary_dropped`,
`line_across_boundary`, `line_content_crowded`, and `WORDS`. An optional
the reference tests by truth is `Option<&[u32]>` with `Some(empty)` read
as absent; one it tests against None is not. Counts are `i64` (Python's
are unbounded; the callers' are row counts). Plain structs, the
reference's tuples: `Candidate { name, id }` (L1), `Previous { checkout,
name, id }` (L2), `ImportCandidate { name, id, codes }` (L10).

### Fixtures and tests

`tests/contract/gen_membership_lines.py`, two generators:

- `membership_lines.jsonl` (176,872 bytes, not gated, class i): the
  tables (`_QUOTE_LIKE`, `_QUOTE_COMBINING`, `WORDS`); `printable` and
  `quoted` of each of 372 single code points (Latin-1 whole: C0, DEL, C1,
  NBSP, soft hyphen; bidirectional controls and isolates, zero-width
  characters, variation selectors, U+2028/2029, non-characters, private
  use, tags, the four surrogate bounds, the quote marks); 152 texts each
  (each quote mark after a letter, a space and itself; U+030B/U+030E on 25
  bases; hostile names; lengths 0 to 1,024; four long recipes); and 409
  line cases, each argument varied one at a time from a base (empty,
  hostile, absent, plurals 0, 1, 2, 3 and 10, 60 candidates, 200 names,
  ints at the i64 bounds). Its texts use only code points whose category
  and `isalnum` are the same from Unicode 14 to 16; identical under Python
  3.11 (Unicode 14.0.0) and 3.13.
- `membership_lines_unicode.jsonl` (43,001 bytes, gated on Unicode,
  class ii): the first, middle and last code point of each category, alone
  and before U+030B; and every code point U+0000 to U+10FFFF in 68 blocks
  through `printable`, `quoted`, and `quoted` with U+030B after each
  (SHA-256). It differs under 3.11.

Tests: `golden_membership_lines` (`membership_lines_every_case`: 1,457
cases, class i; `membership_lines_unicode_every_case`: 462, class ii);
`membership_lines_robustness` (5, class i: 1 MiB texts with exact output
lengths; at 64 KiB, 256 KiB and 1 MiB the output of `printable_into` and
`quoted_into` is the length the per-character rule gives and the buffer's
capacity at most twice it, which checks linear work without a clock; every code point once with no Cc/Cf/Cs/Zl/Zp or quote-like
character surviving, million-mark combining runs, 100,000 names,
candidates and codes with lengths linear by count).

Finding (reproduced at 3.13.13, pinned in both fixtures): `quoted`'s
`base` is the last character *written* (`_printable.py` 122-124), so
after an escape it is the escape's last hex digit or letter, which is
alphanumeric. A U+030B or U+030E right after an escaped quote, an escaped
control or another escaped U+030E is kept raw, drawn as a double mark over
that digit: `quoted("”̋")` is `"”̋"`, and `quoted(" " +
"̎" * 3)` escapes the first mark and keeps the other two. The value
still cannot close its own quote (the `"` is always escaped), but a
double-quote-like mark appears raw where the rule meant to escape it.

Known and reproduced (a reviewer's, being filed; pinned in
`membership_lines.jsonl` as the reference behaves, every code point
involved stable from Unicode 14 to 16):

- `quoted` is not injective: a control character and the ASCII spelling of
  its escape print the same (`quoted("\n")` and `quoted("\\n")` are both
  `"\\n"`; likewise ESC and `\x1b`, U+202E and `\u202e`).
- Characters drawn like a double quote but outside `_QUOTE_LIKE` and not
  Pi or Pf come through raw: U+275D, U+275E, U+1F676 to U+1F678, U+2057,
  U+2034, U+05F3.
- Invisible characters not of category Cc, Cf, Cs, Zl or Zp come through
  raw: U+3164, U+115F, U+FFA0, U+2800, U+034F, U+17B4.

### Not ported, or not expressible natively

- Counts are `i64`. The reference prints any Python int with `str()`, and
  a bool as `True`/`False`. The real callers pass `int(...)`, `len(...)`
  or a constant, but a count read from an edited store can exceed i64: the
  native caller must then refuse the value (or clamp it and say so) before
  calling, since the line cannot spell it; a bool is never a count there.
- `line_import`'s codes: the reference iterates whatever it is given, and
  `membership_queries` passes what stored JSON holds, so a stored string
  `"HO"` gives `H+O`. The native function takes a list of texts; the
  native caller must turn a stored string into its characters (one text
  each) before calling.
- None where a str is expected raises (TypeError or AttributeError) in the
  reference; the native signatures take `&[u32]` there, so that call
  cannot be written.

## The store's row writers

`store::home` (`src/store/home.rs`) and `store::tables`
(`src/store/tables.rs`): a record's home, a standing answer, the episode
journal's writers and the verification, conflict, import and quarantine
writers. Reference read at faa1b32e; line numbers are that commit's.

### The design (what each reads, decides, appends, mints, returns)

Every refusal below is `WriteError::Refused { class, text }` with the
reference's class and `str(exc)`, raised before anything is appended (or,
inside a transaction, rolled back with it). `NotFoundError` is a
`KeyError`, so its text is the key's `repr` (`'x'`, or `('m', None)` for a
verification), reproduced with the crate's `repr`.

- `Store._home_row(record_id, kind, home)` (`store.py` 1459-1497) is
  `home::home_row(tx, record_id, kind, Option<&Home>) ->
  Result<Option<PyValue>, WriteError>`. No home: None, nothing read. A
  project that is not a non-empty `str`: `ValueError` "a home names a
  project (or global)"; then a rule likewise: "a home names the rule that
  decided it" (the project is checked first). Reads `SELECT project,
  checkout, rule FROM homes WHERE record_id = ?` through
  `tx.connection()`. A row equal as a Python tuple (column values against
  the caller's, `==`): None, nothing written. Any other row: `ValueError`
  "{id} already has a home; a home is written once". Else the row
  `{record_id, kind, project, checkout, rule, decided}`, `decided` the
  wall clock read once (`datetime.now` in `store`).
  `home::put_home(tx, &row, session)` appends `home_put {"home": row}`.
- `_standing_row(memory, answer)` (`store_records.py` 202-222) is
  `home::standing_row(tx, record_id, body, &Answer) -> Result<PyValue, _>`.
  A probability that is a `bool` or not an `int`/`float`, not finite or
  outside 0 to 1: `ValueError` "a standing answer is a probability from 0
  to 1" (an `int` too large for a float: `OverflowError` "int too large to
  convert to float", from `float()`); a falsy source or giver:
  `ValueError` "a standing answer names its source and who gave it". The
  row `{record_id, probability: float(p), body_sha256: sha256 of the
  stored body's UTF-8, source, answered_by, answered}`, `answered` the
  wall clock read once, after the checks. `home::put_standing_row` appends
  `standing_put {"standing": row}`.
- `put_standing(record_id, answer, session=)` (225-239) is
  `WritableStore::put_standing(&str, &Answer, session, provider) ->
  (row, Committed)`: one transaction; `get_memory` under the lock
  (`home::get_memory`: `_MEMORY_SELECT ... WHERE id = ?`, the row read by
  `records::rows::memory`; none: `NotFoundError(id)`, checked before the
  answer), the row, `standing_put`. Returns the row.
- `put_episode(episode, session=, home=)` (`store_journal.py` 44-59):
  `WritableStore::put_episode(&Episode, session, Option<&Home>, provider)
  -> (Episode, Committed)`: one transaction; `_home_row(id, "episode",
  home)`, then `episode_put {"episode": model_dump(mode="json")}`, then
  `home_put` when a row was decided. Returns the episode.
- `delete_episode(id, session=)` (87-90): `_require_row(episodes, id)`
  (`NotFoundError(id)`), then `episode_delete {"id"}`. Returns None.
- `write_episode(*, session_id, body, scopes, takeaway, swarm_id, origin,
  now, home)` (96-122): `EpisodeArgs` plus `now: Option<DateTime>` and
  the home. A falsy body, or a `str` body blank after `strip()`:
  `ValueError` "episode body must be a non-empty string"; a truthy body
  that is not a `str`: `AttributeError` "'int' object has no attribute
  'strip'" (nothing minted or read in either case). Then, in the
  constructor's argument order: the mint once (`generate_ulid`), the wall
  clock once unless `now` is given (`utcnow`), the body stripped plus a
  newline, `list(scopes or [])` (a `str` gives its characters, a dict its
  keys, any other truthy non-iterable `TypeError` "'int' object is not
  iterable"), the takeaway stripped when truthy (a truthy non-`str`:
  `AttributeError`), then `Episode(...)` (a ValidationError's first line,
  "1 validation error for Episode"). Then `put_episode` with
  `session=session_id`. Returns the episode.
- `write_floor(*, session_id, origin, now, home)` (125-146): the mint,
  the wall clock unless `now`, the floor episode (`is_floor`, its fixed
  body), `put_episode`.
- `put_verification(memory_id, *, verified_at, ...)` (`store_tables.py`
  36-72): `Verification`; the payload `{memory_id, machine_id,
  verified_at: verified_at.isoformat(), verified_head, the four lists}`
  plus each target map when given (`dict(targets)`); `verification_put`.
  No check of its own: the fold refuses what the payload cannot hold.
- `delete_verification(memory_id, *, machine_id=)` (111-133): a row whose
  `COALESCE(machine_id, '')` matches, else `NotFoundError((memory_id,
  machine_id))`; `verification_delete`.
- `put_conflict(record)` (137-145): the record's `_CONFLICT_COLUMNS`
  (`record.get`, None when absent; a record that is not a dict:
  `AttributeError` "'list' object has no attribute 'get'"); a falsy `id`,
  `a_id`, `b_id`, in that order: `ValueError` "a conflict needs {name}",
  before the transaction; `conflict_put`. `delete_conflict` (157-162).
- `put_import` / `delete_import` (165-196), `put_quarantine` /
  `delete_quarantine` (199-236): the payload as given, one mutation; the
  deletes `_require_row` first.

Every table writer and episode writer is one transaction on the
`WritableStore` and returns what the reference returns with the
`Committed` rows and head. No writer here names a record for the
`returned` settle (only memory kinds do).

The clocks and the mint, per call (what the fixture replays exactly):
`home` (a decided row) reads the wall clock once; `put_standing` once;
`write_episode` and `write_floor` mint one id and read the wall clock
once unless `now` is given, plus once more for a decided home; every
appended row reads the log clock once, and the head's move once more. The
provider's mint is `Provider::ulid` (`src/store/create.rs`): by default
`create::system_ulid()` (the system clock's seconds through
`create::ulid_millis`, which is `mint::millis`: wrapped at 48 bits, an
infinite time the reference's OverflowError and NaN its ValueError, as an
error naming the class; then ten OS bytes through `mint::ulid`, as `generate_ulid`
reads `time.time()` apart from both clocks); `Fixed` mints the constant
id of millisecond 0 and ten zero bytes (the create and open fixtures never
mint); `Clocks` passes to its inner provider. `utcnow` is the wall clock
(`wall_now`), the same read: it has no method of its own.

`home_put` and `standing_put` are written only through these functions
(`Transaction::mutate_home_or_standing`, crate-private); a direct
`Transaction::mutate("home_put", ...)` is still refused as not ported. In
the reference `_mutate` is private and its only callers with these kinds
are `put_memory`, `put_episode` and `put_standing`: no public caller
reaches it otherwise.

Another writer calls them inside its own transaction: `let row =
home::home_row(&mut tx, id, "memory", home)?;` before its record's rows,
then `home::put_home(&mut tx, &row, session)?` after them (as
`_put_memory` does), and `home::standing_row(&mut tx, &id, &body,
&answer)?` / `home::put_standing_row`. Run in a `tx.scope(..)`, a refusal
rolls back that scope only.

Not ported, refused with nothing written: `prune_episode_sessions`
(chunked, its own unit), and whatever a transaction refuses (membership
kinds, a native-path origin, a rekeyed store). Not expressible:
`write_episode` and `write_floor` take `now` as `Option<DateTime>`, so a
falsy or non-datetime `now`, which the reference passes through `now or
utcnow()` and then pydantic, cannot be given natively.

A refusal that quotes a type (`'int' object has no attribute 'strip'`,
`... 'get'`, `... is not iterable`) spells it as CPython's messages do
(`tp_name`): `NoneType`, `bool`, `int`, `float`, `str`, `list`, `tuple`,
`dict`, `PurePosixPath`, and `datetime.datetime` for a datetime (where
`type(x).__name__` is `datetime`), each checked against the reference.

Reference findings (pinned in the fixture, not changed): (1) a home whose
checkout is not a `str` (an int, say) is stored by the `TEXT` column as
text, so the same home written again compares unequal and is refused as
"already has a home" (`store.py` 1480-1491; case `homes`, steps 12-15).
(2) `put_verification` takes any item types in its lists
(`store_tables.py` 54-62; `verified_paths=[1, 2]` writes). (3) a standing
row's source and giver are not type-checked by `_standing_row`
(`store_records.py` 214); a `bool` or `int` giver is stored as given,
and a list, float or other giver is refused only by the fold
(`TypeError`), after the wall clock was read and the row appended (case
`type_names`).

Pinned natively only, since the fixture's JSON cannot carry them: a NaN
or infinite probability (`ValueError` "a standing answer is a probability
from 0 to 1", the reference's text, established by running it), and
arguments nested 9,998 and 100,000 deep. For those the reference was run
at both depths in a child process (it did not crash for any shape tried):
`session_id`, `scopes`, `swarm_id` and `origin` give pydantic's "1
validation error for Episode", `body` and `takeaway` `.strip()`'s
AttributeError, a probability the range ValueError, and every value that
reaches the payload's encoder (a home's checkout, a standing source or
giver, any quarantine, import, conflict or verification field) is
RecursionError, with nothing written. `PyValue`'s `Clone`, `PartialEq` and
`Debug` are iterative (`src/pyvalue.rs`), so no copy or comparison on the
way overflows the stack.

### Tests

| test | what | class |
| --- | --- | --- |
| `golden_store_rows_written.rs` | `store_rows_written.jsonl` (9 cases, 153 calls, 167 KB; Unicode and pydantic-core gated, POSIX only, as `store_tx.jsonl` is and for its reasons): per call the returned value or the refusal, every clock reading and minted id read once, the order of all reads (one sequence of `log`, `wall` and `mint` per call; swapping the mint and the wall read in `write_episode` or `write_floor` fails it), the rows byte for byte, the whole state's digest, the head, FTS5 hits; a refold per case | ii |
| `store_rows_written_not_posix.rs` | the fixture's gap elsewhere | os |
| `store_rows_written_cross.rs` | native writes every writer's rows; the reference verifies, refolds and reads them, writes on top; native verifies and continues; the reverse from a store the reference wrote | i |
| `store_rows_written_kills.rs` | 120 kills (every provider read of four calls by abort and exit, 60 timed), WAL and rollback journal, both builds verify, the head never ahead | i |
| `store_rows_written_robustness.rs` | 18 hostile values in every argument, ids that are not ids, an 8 MiB body, `homes` and `standing` altered under the writer: typed errors with nothing written, no panic; each type's name in each refusal that quotes one, by text; `create::ulid_millis` wrapping at 48 bits and refusing an infinite or NaN time as the reference; NaN and infinite probabilities by text; every argument position nested 9,998 and 100,000 deep on a 512 KiB stack: the reference's refusal, nothing written, the store verifies | i |

The fixture's cases: homes (new, the same again, another, each refusal,
a non-`str` checkout); standing at 0, 1, between, out of range, a `str`,
None, `bool`s, a 401-digit int, falsy source and giver, a missing record,
over bodies needing the strip and newline; `put_episode` with and without
a home, the same home again, another home, a refused home;
`delete_episode` present and missing; `write_episode` with empty, blank,
None, 0, `int` and list bodies, with and without `now` and a home,
takeaways, scopes as a `str` and an `int`, validation refusals;
`write_floor`; verifications put, re-put, per machine, deleted, missing;
conflicts, imports and quarantine put, re-put, deleted, missing, refused;
rollback-journal mode; the `homes` and `standing` tables dropped; every
type as a body, scopes, a takeaway, a conflict record and a standing giver.

## The rule a .git entry is followed by

`src/bettermemory/gitfile_rule.py` (1,537 lines, read at v9's 6000b993)
decides whether a folder's `.git` entry (a directory, a `gitdir:` file or
a link to either) is followed. A wrong follow keys a folder on another
checkout's git directory, so "native follows where the reference refuses"
is the most serious difference this port can have. It is ported unit by
unit; unit 1 is built.

### Map, by function

Machine facts are in brackets: [os.path] the host's path module, [env]
the environment, [git] the installed git through `_git_is_at_least` or
`_reads_compat`, [clock/threads] `location_reads`, [stat] stat fields
past kind and identity.

| Lines | Function | Reads / depends on |
|---|---|---|
| 57-87 | constants: `_GITFILE_LIMIT` 1 MiB, `_REF_LIMIT`, `_READ_CHUNK` 64 KiB, `_HEADREF_READ` 255, `_SPACE`, `_HEX_PREFIX`, `_SF_DATALESS`, `_WINDOWS_RECALL`, `UNREAD_TREE`, `_RECORDED_BUDGET` 0.5 s | none |
| 90-97 | `_gitfile_target` | a blocking `open()` of the `.git` file; `_parse_gitfile` |
| 100-114 | `_parse_gitfile` | bytes; [os.path] isabs, join, dirname, normpath |
| 117-127 | `_small_text` | a blocking `open()` |
| 130-144 | `_names_here` | `os.stat` twice, `samestat`, `path_missing`; [os.path] |
| 147-219 | `gitfile_refusal`, `_refusal` | `_common_of`, `_commondir`, `tree_format`, `_small_text` (a worktree's `gitdir`), `_names_folder`, `_tree_refusal`, `_working_tree_of`, `_same_folder`, `_recorded_reading`, `gitdir_pin.recorded` (the pin records) |
| 222-288 | `TreeFormat`, the format's names and extension tables | none |
| 291-355 | `tree_format` | `_git_common`, `_common_format`, `gitconfig.read_git_config` (`config.worktree`), `_take`, `_dies`, `gitconfig.spaced`; [git] 2.45 |
| 358-382 | `_Format`, `_common_format` | `read_git_config`, `_judge_format` |
| 385-419 | `object_format_at`, `object_format` | `_common_format` / `git_entries`, `_judge_format` |
| 422-547 | `_judge_format`, `_reads_compat`, `_git_is_at_least`, `_extension_reads` | `git_int`, `git_bool`; [git] |
| 550-573 | `reads_worktree_config` | `_common_format`; [git] 2.42 |
| 576-608 | `_take`, `_dies`, `FormatUnknown` | none |
| 611-699 | `_tree_refusal`, `own_refusal`, `inside_reading`, `inside_refusal` | `tree_format`, `_names_here`; [os.path] |
| 702-785 | `_recorded_refusal`, `_recorded_reading`, `_spells`, `_read_recorded` | `bounded_read` [clock/threads], `os.path.normcase`, `realpath`, `_same_folder`, `_entry_names` |
| 788-860 | `earliest_naming`, `_view`, `names_git_dir` | `bounded_reads` [clock/threads], `realpath`, `_entry_names` |
| 863-905 | `_entry_names` | `lstat`, `_is_link` [stat: reparse tag], `_working_tree_of`, `stat`, `_placeholder` [stat: flags, file attributes], `open()`, `_parse_gitfile`, `_names_here`, `path_gone` |
| 908-934 | `_names_folder`, `_same_folder` | `lstat`, `stat`, `samestat`, `path_missing` |
| 937-982 | `_JUNCTION_TAG`, `_is_link`, `_working_tree_of`, `_tree_of` | [stat], `scandir`, `realpath` |
| 985-1059 | `_common_of`, `_commondir`, `_not_registered`, `_foreign_commondir`, `_registered`, `_is_git_directory` | [env] `GIT_COMMON_DIR`, a blocking `open()`, `realpath`, `samefile`, `isdir`, `lexists` |
| 1062-1070 | `_placeholder` | [stat] |
| 1078-1164 | `_Unsettled`, `_read` (non-blocking, `O_NOFOLLOW`), `_is_directory`, `_real`, `_real_to_last` | `os.open`, `fstat`, `os.read`, `realpath(strict=True)`, `path_missing` |
| 1167-1204 | `_valid_headref`, `_headref_content` | `lstat`, `readlink`, `_read`, `_placeholder` |
| 1207-1287 | `_git_common`, `_git_directory`, `_git_found`, `_git_reads`, `_git_read` | `_read`, `_real_to_last`, `os.access(X_OK)` |
| 1295-1388 | `Entry`, `judge` | everything above |
| 1391-1537 | `Reading`, `reading_at`, `_entry_reading`, `discovery_reading`, `discovery_refusal`, `toplevel_agrees` | `judge`, `_git_reads`, `_foreign_commondir`, `inside_reading`, `realpath`, the walk to the root |

`_location_probe.py` 60-106 (`path_gone`, `path_missing`) and
`location_reads.py` (all of it: threads, a clock, a process-wide board)
are leaves under it.

### The cut

1. **Leaves (built).** 57-127, 235-288, 398-419, 422-547 (the facts as
   data), 576-596, 1197-1204; `_location_probe` 60-106;
   `location_reads` with the bound as data. About 330 lines of logic.
2. **git's reading of a git directory and the identity helpers.**
   1062-1287 and 130-144, 908-982. Needs from `Fs`: an open with
   `O_NOFOLLOW`, a stat that follows links (kind and identity), the
   placeholder bits (`SF_DATALESS`, the Windows recall attributes, the
   junction tag), `readlink`, `realpath` (strict and not), `access(X_OK)`,
   a directory listing; and an errno beside `PyErr::Os` (ELOOP and
   ENAMETOOLONG read as gone; see the probe below). About 330 lines.
3. **The format from files, and the trust of a `commondir`.** 222-232,
   291-395, 550-573, 599-699, 985-1059. Reads through
   `gitconfig::file::read_git_config` (built); `GIT_COMMON_DIR` as an
   argument. About 330 lines.
4. **Records and the follow.** 147-219, 702-905: the pin records as data,
   the bounded reads of unit 1 for the recorded trees. About 330 lines.
5. **Judge and discovery.** 1295-1537. About 250 lines.

Each later unit has one fixture of real trees (POSIX) and, where its
answer is a function of bytes, a small ungated one.

### Unit 1, as built

`src/gitfile_rule.rs` with `bounded`, `format`, `hostpath` and `probe`:

- `parse_gitfile(data, dotgit, flavour)`, `gitfile_target(fs, dotgit,
  flavour)`, `small_text(fs, path)`, `headref_content(content)` and the
  constants. Reads go through `fsdata::Fs` (open, fstat, read), at most
  `GITFILE_LIMIT + 1` bytes.
- `format::judge_format(path, entries, &GitFacts)` and
  `format::object_format(data, &GitFacts)`. `GitFacts { release,
  reads_compat }` stands for `_git_is_at_least` and `_reads_compat`.
- `probe::path_gone`, `probe::path_missing` over `Raised` (a ValueError,
  an OSError with class, errno and Windows code, or another exception).
  `Raised::from(PyErr)` maps a plain `PyErr::Os` to an unknown errno
  ("cannot tell"). The reference reads ELOOP and ENAMETOOLONG as gone,
  so unit 2 must carry the errno before it reads stat errors.
- `bounded::{Answers, Hung, Read, bounded_read, bounded_reads}`. Whether
  a path answers is data: `StdFs` and `MemFs` always answer, and `Hung`
  names paths that do not. No clock, timer or thread decides an answer.
  A hung path is unanswered for every read of it. The reference's `what`
  is not taken, since it only shares a flight. On the machine, `StdFs`
  blocks wherever the OS blocks, so a later unit that reads recorded
  trees on disk must bound those reads itself.
- `hostpath`: `posixpath` on bytes, and CPython 3.13's pure-Python
  `ntpath` over code points (bytes that do not decode are kept,
  `surrogateescape`). The stack form of `normpath` gives the same answer
  as the reference's in-place deletes, in linear time.

Nothing was added to `fsdata.rs`. `Answers` is a separate trait in
`bounded`, so the other unit's edits to `Fs` (`realpath`) merge without
conflict.

**Where native and the reference part on purpose:** the reference's
`_gitfile_target` and `_small_text` open with a blocking `open()`, so a
FIFO waits for a writer. Native's open does not block, and a FIFO or a
socket reads as None, as a file that cannot be read does. Neither
fixture holds a FIFO.

**Windows rules that rest on reading only:** the `ntpath` parse is held
to the reference on every host (the module's `os` swapped for `ntpath`).
Two readings are left out of the fixture because the reference itself
changes with the release: `ntpath.isabs` of a target with one leading
separator (absolute before 3.13), and `normpath` of `\\?\` and `\\.\`
paths (kept whole before 3.12; on Windows 3.12+ `normpath` is
`nt._path_normpath`). For these native follows 3.13's pure-Python
functions. `str.lower` of a drive is applied one code point at a time.
The real Windows read (open of a directory refused) is held only in
`MemFs`'s Windows flavour and the robustness test.

### Findings about the reference (reproduced, pinned; native agrees)

Checked against git 2.54.0 (`git rev-parse --absolute-git-dir` in a
scratch folder, neutral config). The reference follows a `.git` file
that git dies on, or that git reads as another path:

- `gitfile_rule.py:106-109` strips Python whitespace on both sides of
  the text and of the target. git reads only `gitdir: ` (one space) at
  offset 0 and strips only trailing CR/LF. So `gitdir:x`, `  gitdir: x`,
  a leading newline, `gitdir:\tx`, `gitdir:  x`, a trailing space, tab,
  VT or NBSP, and junk after a newline (`x\ny` names `x\ny` in both)
  name a path to the reference where git dies or reads another path.
- `:113-114` normalizes `..` lexically (`normpath`); git resolves it
  through the file system. `gitdir: ../w/../x` with no `w` dies in git
  and names `x` here.
- `:106` keeps a NUL (git cuts the path there), and replaces bytes that
  are not UTF-8 with U+FFFD, so it names a path git does not name.
- In each case the follow still needs git's pointers back from the
  target, so none is a follow of another checkout by itself. They are
  differences in which path is judged.
- `:117-127` (`_small_text`, used at 200 for a worktree's `gitdir`)
  and `:93` open with a blocking `open()`. A planted pointer whose
  target's `gitdir` is a FIFO hangs the reader until a writer comes.
- `:1005` (`_commondir`) reads `_GITFILE_LIMIT` bytes with no `+ 1`, so
  a `commondir` over 1 MiB is cut and used, not refused (unit 3).

### Tests

`golden_gitfile_leaves.rs` (`gitfile_leaves.jsonl`, ungated, 387 cases:
236 parses, 20 HEADs, 76 formats, 48 errors, 7 bounded reads, class i),
`golden_gitfile_leaves_posix.rs` (`gitfile_leaves_posix.jsonl`, 32 trees,
through `MemFs` and `StdFs`, class i on unix; `gitfile_leaves_not_posix.rs`
records the gap as os), `gitfile_leaves_robustness.rs` (9 tests).

## Not built yet

The store's writes past `create`, the writing open's plain states and
the write transaction (`mutate`, `begin`, its `returned` marks),
`put_memory`, `tombstone`, `delete_tombstone` and the row writers
(`store::home`, `store::tables`): the rest of the public record API
(`write`, `update`, `mark_verified`, `record_corroboration`,
`put_tombstone`, `restore` with relink, `rename_scope`, the prunes, `batch`,
provenance, and `put_memory`'s homes and standing answers), the membership kinds, schema steps, rekey, reclaim, the origin's settling, a
restore's rows, re-spelling the index), membership's writes and
the caller's resolution (its admission is `store::caller`), and each hit's snippet, signals and drift report,
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
