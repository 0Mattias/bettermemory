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
//! - [`gitconfig`]: the text of a git config file as the reference reads it;
//! - [`githead`]: HEAD and refs read from a git directory's files;
//! - [`fsdata`]: the file system as the config readers ask it, as data.

#![forbid(unsafe_code)]

pub mod capture;
pub mod config;
pub mod datetime;
pub mod float_repr;
pub mod fsdata;
pub mod gitconfig;
pub mod githead;
pub mod json;
pub mod lexicon;
pub mod log;
pub mod pyvalue;
pub mod records;
pub mod search;
pub mod store;
pub mod toml;
pub mod unicode;
