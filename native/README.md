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
| `json` | `json.dumps` as each call site spells values (`log.canonical_payload` and nine more forms), `json.loads` | `canonical_json.jsonl` |
| `float_repr` | `float.__repr__` (shortest round trip, CPython's tie and layout rules) | `canonical_json.jsonl` |
| `datetime` | `datetime.fromisoformat` (CPython 3.13's C parser), `isoformat`, `str`, the `strftime` patterns, pydantic's JSON spelling, `parse_event_ts`, `store_rows._dt`, `v8._as_dt`, `origin_history.instant`/`seconds`, `review.ulid_time` | `datetime.jsonl` |
| `unicode` | NFC/NFD/NFKC/NFKD, `str.lower`/`upper`/`casefold` (final sigma included), `unicodedata.category`/`combining`, `str.isalpha` and kin, `re`'s `\w` and `\d` | `unicode.jsonl` |
| `lexicon` | `search_lexicon.tokenize`, `_tokenize_unstemmed`, `_strip_stopwords`, `fts_index_text`, `fts_match_query`, `_kebab_parts`, the diacritic fold, `tokenizer_fingerprint` | `lexicon.jsonl`, `lexicon_spelling_tables.json` |
| `log` | the row MAC (HMAC-SHA256 written on `sha2`, RFC 4231-checked), `fingerprint`, the key ring (in memory), the head and origin files, rekey rows, `verify_chain` and its report | `log_chain.jsonl` |
| `pyvalue` | the Python values these meet (a `str` may hold a lone surrogate), `int()`, `str()`, `repr()` | through the above |

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
recursion budget, not `sys.getrecursionlimit()`). The one difference left:
the reference's limit is that budget less whatever its caller's stack
already used, so it can stop lower when called deep in a stack, while the
native limit is fixed. Native never rejects what the reference accepts.

Seqs are i64, as SQLite stores them; `verify_chain` counts the expected seq
in i128, so a chain that runs to `i64::MAX` reports as the reference does,
and `next_row` after `i64::MAX` is OverflowError (where SQLite refuses the
reference's insert). `tests/robustness.rs` pins both against reports the
reference gave, and feeds every public entry point malformed text, JSON,
numbers, head and origin files and rows under debug assertions.

`log::Key` holds key bytes. Its `Debug` prints the length and a
fingerprint prefix, never the bytes, so `KeyRing`'s derived `Debug` is safe,
and no error type carries key bytes. The bytes sit in a `Zeroizing` buffer,
wiped on drop. The store unit, when it reads key files, must keep their
bytes in such a zeroize-on-drop wrapper from the read on (`Key::new` takes
the `Vec` without copying it), not in a plain `Vec<u8>` or `String`.

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

The store over SQLite (rows, schema steps, FTS5, BM25 ranking and search),
the daemon, the MCP tools and the hooks; reading and writing the key ring's
files on disk (the ring here holds their bytes); `datetime.astimezone` of a
naive value (the reference reads it as the machine's local time).
