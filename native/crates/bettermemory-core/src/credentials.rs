//! The write gate's credential detectors: the port of
//! `src/bettermemory/credentials.py` (see the README's "The credential
//! detectors", which maps the module by function and gives the cut).
//!
//! - [`prefixed`]: the vendor-prefixed token shapes (`_PREFIXED_DETECTORS`,
//!   `_is_masked_token`, `_is_spent_run`, `_MIN_BODY_DIVERSITY`): built.
//!
//! The generic `keyword = value` rule, the connection-URI password, the
//! value gate (`_looks_like_secret`) and the spans, snippets and markers
//! built on all of them are later units.

pub mod prefixed;
