//! The eval family's model, unit eval_model-3: the tool-usage tables.
//! `_TOOL_EVENT_KIND_TO_TOOL` is a table of `(kind, tool)` pairs in the
//! reference's insertion order with its lookup, `TOOLS_WITHOUT_TELEMETRY`
//! is empty, the two side-effect rosters (`frozenset`s, read only by
//! membership) are tables sorted by code point, and `ToolUsageRow` is the
//! rollup's row with its `to_dict`.
//!
//! eval-3's `ADMIN_RECORDED_EVENT_KINDS` (`_KNOWN - _IN_SESSION`) and
//! `is_admin_recorded_event` are not built here: they read
//! [`KNOWN_SIDE_EFFECT_KINDS`] and [`is_in_session_side_effect_kind`].

use crate::health_model::{dict, int, opt_float};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// `_TOOL_EVENT_KIND_TO_TOOL`: each event kind a tool records and the
/// tool, in the reference's insertion order (18 pairs: the seven memory
/// tools, `episode` twice, `memory_admin` nine times).
pub const TOOL_EVENT_KIND_TO_TOOL: [(&str, &str); 18] = [
    ("search", "memory_search"),
    ("show", "memory_show"),
    ("write", "memory_write"),
    ("update", "memory_update"),
    ("remove", "memory_remove"),
    ("verify", "memory_verify"),
    ("use", "memory_record_use"),
    ("episode_write", "episode"),
    ("episode_handoff", "episode"),
    ("restore", "memory_admin"),
    ("list_tombstones", "memory_admin"),
    ("rename_scope", "memory_admin"),
    ("scope_disable", "memory_admin"),
    ("scope_enable", "memory_admin"),
    ("miss_ack", "memory_admin"),
    ("conflict_scan", "memory_admin"),
    ("conflict_verdict", "memory_admin"),
    ("answer_standing", "memory_admin"),
];

/// `_TOOL_EVENT_KIND_TO_TOOL.get(kind)`: the tool of an event kind given
/// as code points, None for a kind the map does not hold.
pub fn tool_of_kind(kind: &[u32]) -> Option<&'static str> {
    TOOL_EVENT_KIND_TO_TOOL
        .iter()
        .find(|(k, _)| spells(kind, k))
        .map(|&(_, tool)| tool)
}

/// `TOOLS_WITHOUT_TELEMETRY`: empty on the nine-tool surface.
pub const TOOLS_WITHOUT_TELEMETRY: [&str; 0] = [];

/// `_KNOWN_SIDE_EFFECT_KINDS`, sorted by code point: the kinds recorded
/// as side effects of other tools or by the hooks, not as tool calls.
pub const KNOWN_SIDE_EFFECT_KINDS: [&str; 13] = [
    "capture_grade",
    "prompt_recall",
    "recall_grade",
    "recall_held",
    "review_grade",
    "review_unanswered",
    "search_miss",
    "segment_opened",
    "session_brief",
    "silent_miss_cutoff",
    "standing_guard",
    "turn_audited",
    "use_token_expired",
];

/// `_IN_SESSION_SIDE_EFFECT_KINDS`, sorted by code point: the subset of
/// [`KNOWN_SIDE_EFFECT_KINDS`] recorded inside a live client session.
pub const IN_SESSION_SIDE_EFFECT_KINDS: [&str; 9] = [
    "prompt_recall",
    "recall_held",
    "review_unanswered",
    "search_miss",
    "segment_opened",
    "session_brief",
    "standing_guard",
    "turn_audited",
    "use_token_expired",
];

/// `kind in _KNOWN_SIDE_EFFECT_KINDS` for a `str` kind.
pub fn is_known_side_effect_kind(kind: &[u32]) -> bool {
    KNOWN_SIDE_EFFECT_KINDS.iter().any(|k| spells(kind, k))
}

/// `kind in _IN_SESSION_SIDE_EFFECT_KINDS` for a `str` kind.
pub fn is_in_session_side_effect_kind(kind: &[u32]) -> bool {
    IN_SESSION_SIDE_EFFECT_KINDS.iter().any(|k| spells(kind, k))
}

fn spells(s: &[u32], text: &str) -> bool {
    s.iter().copied().eq(text.chars().map(u32::from))
}

/// `ToolUsageRow`: one row of the tool-usage rollup. `share` is None
/// when the rollup is empty; `has_telemetry` defaults to true.
#[derive(Clone, Debug, PartialEq)]
pub struct ToolUsageRow {
    pub tool: PyStr,
    pub count: i64,
    pub share: Option<f64>,
    pub has_telemetry: bool,
}

impl ToolUsageRow {
    /// `ToolUsageRow(tool, count, share)`, `has_telemetry` at its default.
    pub fn new(tool: &str, count: i64, share: Option<f64>) -> ToolUsageRow {
        ToolUsageRow {
            tool: cps(tool),
            count,
            share,
            has_telemetry: true,
        }
    }

    /// The dict in the reference's key order.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("tool", PyValue::Str(self.tool.clone())),
            ("count", int(self.count)),
            ("share", opt_float(self.share)),
            ("has_telemetry", PyValue::Bool(self.has_telemetry)),
        ])
    }
}
