# bettermemory native build

The Rust build of bettermemory 9. It reproduces the Python reference in
`src/bettermemory` exactly, so a store and its signed log written by one
open unchanged in the other. The reference behaviour is pinned by the golden
files in `tests/contract/golden`, which `tests/contract/gen_*.py` generate
from the Python code; every test here reads them from there.

Nothing here is wired into the Python product, packaged or run in CI yet.

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

Dependencies: `sha2`, `subtle` (constant-time MAC comparison) and `zeroize`
(key bytes are wiped when a `Key` drops); the tests also use `serde_json`
to read the golden files.

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
`BM_REFERENCE_PYTHON` or the repository's `.venv/bin/python`.

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

## Regenerating the Unicode tables

`crates/bettermemory-core/src/unicode/tables.rs` is generated from the
reference Python's own Unicode data (3.13, Unicode 15.1.0) and committed:

```sh
../.venv/bin/python tools/gen_unicode_tables.py          # write
../.venv/bin/python tools/gen_unicode_tables.py --check  # compare, exit 1 on a difference
```

The generator refuses any other Unicode version. Its output depends only on
that data, so the same interpreter writes the same bytes.

## Not built yet

The store over SQLite (rows, schema steps, FTS5 and its `bm25()`, the
search pool it draws, admission and the hit's snippet and drift report),
the daemon, the MCP tools and the hooks; reading and writing the key ring's
files on disk (the ring here holds their bytes); `datetime.astimezone` of a
naive value (the reference reads it as the machine's local time).
