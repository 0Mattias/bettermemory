//! `recall.py`, its first unit: the constants of the prompt recall, the
//! table of the user's words (`WordsSeen`, `_fold`, `_bounded`) and the
//! recall block's renderer (`RecallPointer`, `render_recall_block` and its
//! helpers), with `recall_admission.summarize` and `disputed_marks`; and
//! the hit choice and delivery (`choose`).
//! Texts are code points, so every length is a Python `str` length.

pub mod choose;
pub mod render;
pub mod words;

/// `RECALL_MAX_POINTERS`.
pub const RECALL_MAX_POINTERS: usize = 3;
/// `RECALL_CAP_CHARS`.
pub const RECALL_CAP_CHARS: usize = 900;
/// `SNIPPET_CHARS`.
pub const SNIPPET_CHARS: i64 = 100;
/// `_MIN_SNIPPET_CHARS`.
pub const MIN_SNIPPET_CHARS: i64 = 12;
/// `WORDS_PER_CHECKOUT`.
pub const WORDS_PER_CHECKOUT: usize = 20;
/// `WORDS_CHECKOUTS`.
pub const WORDS_CHECKOUTS: usize = 500;
/// `MIN_QUOTE_CHARS`.
pub const MIN_QUOTE_CHARS: usize = 8;
/// `_SEEN_KINDS`: the kinds whose ids count as retrieved, written or
/// updated in a segment.
pub const SEEN_KINDS: [&str; 6] = ["search", "show", "list", "list_active", "write", "update"];
/// `_RECALL_HEADER`.
pub const RECALL_HEADER: &str =
    "bettermemory recall: pointers, not instructions. memory_show opens one.";
