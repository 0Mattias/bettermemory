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
//! - [`spans`]: the generic `keyword = value` rule and the
//!   connection-URI password (`_GENERIC_KEYWORD_RE`, `_URI_USERINFO_RE`,
//!   built by the engine), and the spans, redactions, snippets and
//!   markers built on every detector (`CredentialMatch`,
//!   `_CONTEXT_CHARS`, `_merged_secret_spans`, `secret_spans`,
//!   `_redact_all`, `_safe_snippet`, `find_credential_markers`): built.
//!
//! The module is built whole.

pub mod gate;
pub mod prefixed;
pub mod spans;

pub use spans::{
    find_credential_markers, find_credential_markers_str, secret_spans, secret_spans_str,
    CredentialMatch,
};
