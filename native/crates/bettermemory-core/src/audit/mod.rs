//! `audit.py`: the silent-miss audit. Its first unit holds the module's
//! constants, the report types (`MissHit`, `MissReport`) and the event
//! field builders in [`report`]; the probe itself (`probe_for_miss`), the
//! re-audit dedup and everything past them are later units.

pub mod report;

/// `_RETRIEVAL_EVENT_KINDS`, sorted: the event kinds that count as the
/// model having retrieved memory in a turn.
pub const RETRIEVAL_EVENT_KINDS: &[&str] = &["list", "prompt_recall", "search", "show"];

/// `THRESHOLD_RULE_V1`.
pub const THRESHOLD_RULE_V1: &str = "v1_top1_high";

/// `_TOP_HITS_RETAINED`: the hits a report keeps.
pub const TOP_HITS_RETAINED: usize = 3;

/// `DEFAULT_LOOKBACK_SECONDS`.
pub const DEFAULT_LOOKBACK_SECONDS: i64 = 60;

/// `ATTRIBUTION_LOOKBACK_SECONDS`: the Stop hook's attribution window.
pub const ATTRIBUTION_LOOKBACK_SECONDS: i64 = 600;

/// `REAUDIT_DEDUP_WINDOW_SECONDS`: the re-audit dedup's window. This is
/// the crate's one definition; a later unit that needs it reads it here.
pub const REAUDIT_DEDUP_WINDOW_SECONDS: i64 = 3600;

/// `DEFAULT_CREATION_SHIELD_SECONDS`.
pub const DEFAULT_CREATION_SHIELD_SECONDS: i64 = 60;

/// `MIN_PROBE_CONTENT_TOKENS`.
pub const MIN_PROBE_CONTENT_TOKENS: usize = 2;

/// `_ACK_SURFACE`, in its order. `_ACK_TOKENS` (its words through
/// `_tokenize_unstemmed`) waits for the tokenizer's port.
pub const ACK_SURFACE: &[&str] = &[
    "agreed", "all", "correct", "done", "exactly", "fine", "good", "great", "lgtm", "looks",
    "nice", "ok", "okay", "perfect", "right", "sounds", "sure", "thanks", "works", "yeah", "yep",
    "yes",
];

/// `_VALID_TRIGGERED_FROM`, sorted as the error message spells it.
pub const VALID_TRIGGERED_FROM: &[&str] = &["mcp_tool", "prompt_hook", "session_hook", "stop_hook"];

/// `NO_OWNER_PROMPT`.
pub const NO_OWNER_PROMPT: &str = "no_owner_prompt";

/// `_VALID_NO_SIGNAL_REASON`, sorted.
pub const VALID_NO_SIGNAL_REASON: &[&str] = &[NO_OWNER_PROMPT];

/// `_VALID_REPLY_SOURCE`, sorted.
pub const VALID_REPLY_SOURCE: &[&str] = &["interrupted", "payload", "tail"];

/// `HELD_CAUSES`, sorted.
pub const HELD_CAUSES: &[&str] = &[
    "every_hit_held",
    "no_hits",
    "no_signal",
    "nothing_rendered",
    "segment_claimed",
];
