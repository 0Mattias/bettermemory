//! The core of bettermemory 9's native build: the parts of the Python
//! reference that need no database, reproduced exactly.
//!
//! - [`config`]: the settings: the config file's loader, the managed
//!   file's pins, and `config check|show|path --json`;
//! - [`json`]: `json.dumps` as the reference's call sites spell values
//!   (the log payload's canonical text among them) and `json.loads`;
//! - [`float_repr`]: Python's `float.__repr__`;
//! - [`datetime`]: the datetime spellings and readings;
//! - [`unicode`]: normalization, case mapping and the character classes,
//!   over tables generated from the reference Python's Unicode 15.1.0;
//! - [`lexicon`]: the search tokenizer and its fingerprint;
//! - [`log`]: the row MAC, the key ring and `verify_chain`;
//! - [`search`]: `search.search` over records in memory, bit for bit;
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
//! - [`printable`] and [`membership_lines`]: text made safe to show, and
//!   the lines shown about which project a folder belongs to;
//! - [`origin_remote`]: a remote URL read into the triple `origin`
//!   compares, with `urllib.parse.urlparse` as CPython 3.13.13 has it;
//! - [`credentials`]: the write gate's credential detectors, its first
//!   unit (the vendor-prefixed shapes);
//! - [`control_tokens`]: the chat-template control tokens a text may not
//!   carry;
//! - [`origin_gitfiles`]: the repository files `origin` reads in-process,
//!   its first unit (the pure decoders);
//! - [`checkout_token`]: the checkout token, its first unit (the text);
//! - [`gitfile_rule`]: the rule a `.git` entry is followed by, up to the
//!   follow of an entry and its pin records.

#![forbid(unsafe_code)]

pub mod capture;
pub mod checkout_token;
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
pub mod json;
pub mod lexicon;
pub mod log;
pub mod membership_lines;
pub mod origin_gitfiles;
pub mod origin_remote;
pub mod printable;
pub mod pyvalue;
pub mod records;
pub mod search;
pub mod store;
pub mod toml;
pub mod unicode;
