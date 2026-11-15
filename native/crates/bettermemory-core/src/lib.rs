//! The core of bettermemory 9's native build: the parts of the Python
//! reference that need no database, reproduced exactly.
//!
//! - [`json`]: `json.dumps` as the reference's call sites spell values
//!   (the log payload's canonical text among them) and `json.loads`;
//! - [`float_repr`]: Python's `float.__repr__`;
//! - [`datetime`]: the datetime spellings and readings;
//! - [`unicode`]: normalization, case mapping and the character classes,
//!   over tables generated from the reference Python's Unicode 15.1.0;
//! - [`lexicon`]: the search tokenizer and its fingerprint;
//! - [`log`]: the row MAC, the key ring and `verify_chain`;
//! - [`search`]: `search.search` over records in memory, bit for bit;
//! - [`store`]: a store file read without writing to it: the version
//!   contract, the tables, the key ring from a keys directory, the chain
//!   verified on disk, the FTS5 prefilter.

#![forbid(unsafe_code)]

pub mod datetime;
pub mod float_repr;
pub mod json;
pub mod lexicon;
pub mod log;
pub mod pyvalue;
pub mod search;
pub mod store;
pub mod unicode;
