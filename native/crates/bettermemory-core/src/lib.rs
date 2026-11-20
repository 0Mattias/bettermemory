//! The core of bettermemory 9's native build: the parts of the Python
//! reference that need no database, reproduced exactly.
//!
//! - [`config`]: the settings: the config file's loader, the managed
//!   file's pins, and `config check|show|path --json`;
//! - [`identity`]: a request's caller as the reference resolves it, its
//!   leaves first (the declared-value check, the actor rule, `resolve`);
//! - [`json`]: `json.dumps` as the reference's call sites spell values
//!   (the log payload's canonical text among them) and `json.loads`;
//! - [`float_repr`]: Python's `float.__repr__`;
//! - [`datetime`]: the datetime spellings and readings;
//! - [`origin_history`]: how `origin` reads a repository's history, its
//!   first unit (the leaves and the memo of author dates);
//! - [`unicode`]: normalization, case mapping and the character classes,
//!   over tables generated from the reference Python's Unicode 15.1.0;
//! - [`pyre`]: Python's regular expressions as CPython 3.13.13's `re` runs
//!   them, its first units: the case folding and the test of one literal;
//!   the parser's tree and its dump, the parser's state and its tokenizer,
//!   the escapes, sets and flat sequences it reads, and `re.escape`;
//! - [`lexicon`]: the search tokenizer and its fingerprint;
//! - [`verify_candidates`]: the path candidates verification reads, its
//!   first unit (one candidate validated);
//! - [`log`]: the row MAC, the key ring and `verify_chain`;
//! - [`search`]: `search.search` over records in memory, bit for bit;
//! - [`search_dedup`]: write-time dedup, the Jaccard scorer and its
//!   token cache;
//! - [`records`]: the reference's record models (memory, tombstone,
//!   episode, origin, actor, link) validated from a payload or a row and
//!   written back, with pydantic's errors;
//! - [`store`]: a store file read without writing to it: the version
//!   contract, the tables, the key ring from a keys directory, the chain
//!   verified on disk, the FTS5 prefilter;
//! - [`toml`]: `tomllib.loads`, its values and its refusals;
//! - [`control_tokens`]: the chat-template delimiters a stored text may
//!   not carry, found, counted, refused and written by their bare names;
//! - [`gitconfig`]: the text of a git config file as the reference reads it;
//! - [`githead`]: HEAD and refs read from a git directory's files;
//! - [`gitobjects`]: git objects read from a repository's files, its
//!   first unit (the byte parsers);
//! - [`fsdata`]: the file system as the config readers ask it, as data;
//! - [`verify_status`] and [`attribution`]: a memory's verification
//!   staleness, and phrase-match attribution of memories to a reply;
//! - [`printable`] and [`membership_lines`]: text made safe to show, and
//!   the lines shown about which project a folder belongs to;
//! - [`origin_remote`]: a remote URL read into the triple `origin`
//!   compares, with `urllib.parse.urlparse` as CPython 3.13.13 has it;
//! - [`credentials`]: the write gate's credential detectors, its first
//!   unit (the vendor-prefixed shapes);
//! - [`origin_gitfiles`]: the repository files `origin` reads in-process,
//!   its first unit (the pure decoders);
//! - [`checkout_token`]: the checkout token, its first unit (the text);
//! - [`claims`]: what a memory claims, its first unit (the claim record,
//!   the path spellings and the patterns a patch is read by);
//! - [`gitfile_rule`]: the rule a `.git` entry is followed by, up to the
//!   follow of an entry and its pin records.

#![forbid(unsafe_code)]

pub mod attribution;
pub mod capture;
pub mod checkout_token;
pub mod claims;
pub mod config;
pub mod control_tokens;
pub mod credentials;
pub mod datetime;
pub mod float_repr;
pub mod fsdata;
pub mod gitconfig;
pub mod gitfile_rule;
pub mod githead;
pub mod gitobjects;
pub mod gitsystem;
pub mod identity;
pub mod json;
pub mod lexicon;
pub mod log;
pub mod membership_lines;
pub mod origin_gitfiles;
pub mod origin_history;
pub mod origin_remote;
pub mod printable;
pub mod pyre;
pub mod pyvalue;
pub mod records;
pub mod search;
pub mod search_dedup;
pub mod store;
pub mod toml;
pub mod unicode;
pub mod verify_candidates;
pub mod verify_status;
