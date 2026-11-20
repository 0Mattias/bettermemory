//! The write gate's credential detectors: the port of
//! `src/bettermemory/credentials.py` (see the README's "The credential
//! detectors", which maps the module by function and gives the cut).
//!
//! - [`prefixed`]: the vendor-prefixed token shapes (`_PREFIXED_DETECTORS`,
//!   `_is_masked_token`, `_is_spent_run`, `_MIN_BODY_DIVERSITY`): built.
//! - [`gate`]: the value gate (`_is_placeholder`, `_looks_like_secret`,
//!   `_NUMBER_SUFFIX_RE`, `_DATETIME_SHAPE_RE`, `_PLACEHOLDER_VALUES`,
//!   `_PLACEHOLDER_WORDS`): built.
//!
//! The generic `keyword = value` rule and the connection-URI password,
//! whose captured values the gate judges, and the spans, snippets and
//! markers built on all of them are later units.

pub mod gate;
pub mod prefixed;
