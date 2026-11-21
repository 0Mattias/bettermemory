//! Python's regular expressions as CPython 3.13.13 runs them (`re` and
//! `_sre`), for the patterns the reference holds. The engine is built in
//! units (native/README.md, "Python's regular expressions"); each lives in
//! its own file here.
//!
//! - [`fold`]: the case folding `_sre` uses under IGNORECASE and the test of
//!   one literal (unit pyre-1).
//! - [`tree`], [`token`]: the parser's tree, its state and its tokenizer
//!   (unit pyre-2a).
//! - [`atom`]: the atoms the parser reads (escapes, sets, flat sequences)
//!   with `re.escape` (unit pyre-2b).
//! - [`parse`]: the repeats `_parse` reads, with their errors (unit
//!   pyre-3a); `SubPattern::getwidth` is in [`tree`].

pub mod atom;
pub mod fold;
pub mod parse;
pub mod token;
pub mod tree;

/// The interpreter whose `re` the engine reproduces. `pyre_python.jsonl`
/// pins these five values as that interpreter reports them.
pub const PYTHON: &str = "3.13.13";
/// The Unicode data of that interpreter, which the crate's tables carry.
pub const UNICODE: &str = "15.1.0";
/// `_sre.MAGIC`: the version of the compiled form `re` hands to `_sre`.
pub const SRE_MAGIC: u32 = 20230612;
/// `_sre.CODESIZE`: a code word is four bytes, so it holds any code point.
pub const SRE_CODESIZE: u32 = 4;
/// The `Lib/re` sources the engine was read from: each file's name and the
/// first 16 hex digits of the SHA-256 of its text as Python reads it
/// (`inspect.getsource`, line ends as `\n`, in UTF-8).
pub const RE_SOURCES: [(&str, &str); 5] = [
    ("__init__.py", "dbe158a677c6aaac"),
    ("_casefix.py", "1b12d9136f23db6c"),
    ("_compiler.py", "83537237a82294d0"),
    ("_constants.py", "1dbe236d34fa934e"),
    ("_parser.py", "c7a6c80b3b448f50"),
];
