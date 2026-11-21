//! The telemetry recorder's row classes (`events.py`), its first unit: the
//! event kinds, the class of a row by its kind, the audit rows among them
//! and the fields a state row keeps with telemetry off.
//!
//! A row's fields are the Python `dict` the handler passes, as
//! [`PyValue`] pairs in insertion order. The reference calls
//! `str.endswith` on a key wherever it looks for override evidence, so a
//! key that is not a `str` raises `AttributeError` there
//! ([`EventsError::AttributeError`]) and is skipped everywhere else. The
//! query redaction `state_fields` applies to a `probe_query` still in text
//! (`redact_query`) belongs to a later unit and is passed in.

use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// What the reference raises out of this unit: `AttributeError`, from
/// `.endswith` on a field key that is not a `str`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventsError {
    AttributeError,
}

/// `HARNESS_STAMPED_KINDS`, sorted.
pub const HARNESS_STAMPED_KINDS: &[&str] = &["search", "show", "update", "use", "verify", "write"];

/// `HARNESS_SESSION_FIELD`.
pub const HARNESS_SESSION_FIELD: &str = "harness_session";

/// `AUDIT_KINDS`.
pub const AUDIT_KINDS: &[&str] = &["standing_guard"];

/// `OVERRIDE_EVIDENCE_KINDS`, sorted.
pub const OVERRIDE_EVIDENCE_KINDS: &[&str] = &["update", "write"];

/// `_ACKNOWLEDGED_SUFFIX`.
pub const ACKNOWLEDGED_SUFFIX: &str = "_acknowledged";

/// `AUDIT_STATUSES`.
pub const AUDIT_STATUSES: &[&str] = &["policy_refused"];

/// `OVERRIDABLE_REFUSALS`, in the reference's order: status, hatch.
pub const OVERRIDABLE_REFUSALS: &[(&str, &str)] = &[
    ("credential_warning", "acknowledge_credential"),
    ("duplicate", "force"),
    ("previously_removed", "force"),
    ("scope_mismatch", "acknowledge_scope_mismatch"),
    ("transient_warning", "acknowledge_transient"),
    ("truncation_warning", "acknowledge_truncation"),
    ("user_claim_warning", "acknowledge_user_claim"),
];

/// `STATE_STAMP_FIELDS`, sorted.
pub const STATE_STAMP_FIELDS: &[&str] = &[
    "attribution",
    "harness_session",
    "session_id",
    "triggered_from",
    "worktree_root",
];

/// `_REDACTED_TEXT_FIELDS`, sorted.
pub const REDACTED_TEXT_FIELDS: &[&str] = &["probe_query", "query"];

/// `_QUERY_PREVIEW_CHARS`.
pub const QUERY_PREVIEW_CHARS: usize = 32;

/// `_QUERY_HASH_PREFIX`.
pub const QUERY_HASH_PREFIX: usize = 16;

/// `STATE_WRITE_ATTEMPTS`.
pub const STATE_WRITE_ATTEMPTS: u32 = 2;

/// One kept field of a state kind: its name and, when its readers read
/// only some keys inside it, those keys (`KeptFields`' None is `None`).
pub type Kept = (&'static str, Option<&'static [&'static str]>);

const fn whole(name: &'static str) -> Kept {
    (name, None)
}

/// `STATE_FIELDS`: every state kind with its kept fields, each in the
/// reference's order (`_kept`: the plain names, then the ones kept by
/// their keys). The keys of a field kept by keys are sorted.
pub const STATE_FIELDS: &[(&str, &[Kept])] = &[
    ("episode_handoff", &[]),
    ("episode_write", &[]),
    ("miss_ack", &[whole("event_id")]),
    (
        "prompt_recall",
        &[
            whole("agent_id"),
            whole("checkout"),
            whole("event_id"),
            whole("harness"),
            whole("segment"),
            whole("shown_ids"),
        ],
    ),
    (
        "review_issued",
        &[
            whole("door"),
            whole("parent_review_id"),
            whole("record"),
            whole("related"),
            whole("review_id"),
            whole("roster"),
            whole("subject"),
        ],
    ),
    ("review_unanswered", &[whole("harness"), whole("root_ids")]),
    ("scope_disable", &[whole("scope")]),
    ("scope_enable", &[whole("scope")]),
    ("search", &[whole("returned")]),
    (
        "search_miss",
        &[whole("event_id"), ("top_hits", Some(&["id"]))],
    ),
    (
        "segment_opened",
        &[
            whole("agent_id"),
            whole("brief_event_id"),
            whole("harness"),
            whole("marker"),
            whole("marker_absent"),
            whole("opened_at"),
            whole("segment"),
            whole("withdrawn"),
        ],
    ),
    (
        "session_brief",
        &[
            whole("agent_id"),
            whole("checkout"),
            whole("event_id"),
            whole("harness"),
            whole("ids"),
            whole("marker"),
            whole("marker_absent"),
            whole("segment"),
        ],
    ),
    ("show", &[whole("id")]),
    ("silent_miss_cutoff", &[whole("cutoff_ts")]),
    (
        "standing_guard",
        &[whole("door"), whole("said_checked"), whole("standing")],
    ),
    (
        "turn_audited",
        &[
            whole("no_signal_reason"),
            whole("prompt_id"),
            whole("repeat"),
            whole("settled_count"),
            whole("verdict"),
            ("probe_query", Some(&["hash", "len"])),
        ],
    ),
    (
        "update",
        &[
            whole("control_token_kinds"),
            whole("credential_kinds"),
            whole("forced"),
            whole("id"),
            whole("markers"),
            whole("policy_flags"),
            whole("policy_keys"),
            whole("policy_managed"),
            whole("review_id"),
            whole("status"),
        ],
    ),
    (
        "use",
        &[
            whole("agent_id"),
            whole("auto"),
            whole("harness"),
            whole("ids"),
            whole("outcome"),
            whole("segment"),
            whole("shown_event_id"),
            whole("surface"),
        ],
    ),
    (
        "verify",
        &[
            whole("id"),
            whole("status"),
            whole("verified_commits"),
            whole("verified_head"),
        ],
    ),
    (
        "write",
        &[
            whole("control_token_kinds"),
            whole("credential_kinds"),
            whole("forced"),
            whole("id"),
            whole("markers"),
            whole("mismatch_kinds"),
            whole("policy_flags"),
            whole("policy_keys"),
            whole("policy_managed"),
            whole("review_id"),
            whole("status"),
        ],
    ),
];

/// `MEASUREMENT_KINDS`, sorted.
pub const MEASUREMENT_KINDS: &[&str] = &[
    "answer_standing",
    "capture_grade",
    "conflict_scan",
    "conflict_verdict",
    "list_tombstones",
    "recall_grade",
    "recall_held",
    "remove",
    "rename_scope",
    "restore",
    "review_grade",
    "use_token_expired",
];

/// `STATE_KINDS`: the keys of [`STATE_FIELDS`].
pub fn state_kinds() -> impl Iterator<Item = &'static str> {
    STATE_FIELDS.iter().map(|(kind, _)| *kind)
}

/// `RowClass`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RowClass {
    State,
    Measurement,
}

impl RowClass {
    /// The member's value.
    pub fn value(self) -> &'static str {
        match self {
            RowClass::State => "state",
            RowClass::Measurement => "measurement",
        }
    }
}

/// `ROW_KINDS`, in the reference's order.
pub fn row_kinds() -> [(RowClass, Vec<&'static str>); 2] {
    [
        (RowClass::State, state_kinds().collect()),
        (RowClass::Measurement, MEASUREMENT_KINDS.to_vec()),
    ]
}

fn is(text: &[u32], word: &str) -> bool {
    text.len() == word.chars().count()
        && text
            .iter()
            .zip(word.chars())
            .all(|(&c, w)| c == u32::from(w))
}

fn among(text: &[u32], words: &[&str]) -> bool {
    words.iter().any(|w| is(text, w))
}

fn kept_of(kind: &[u32]) -> Option<&'static [Kept]> {
    STATE_FIELDS
        .iter()
        .find(|(k, _)| is(kind, k))
        .map(|(_, kept)| *kept)
}

/// `str.endswith(_ACKNOWLEDGED_SUFFIX)` of a key, or the `AttributeError`
/// a key that is not a `str` raises.
fn acknowledged(key: &PyValue) -> Result<bool, EventsError> {
    let PyValue::Str(name) = key else {
        return Err(EventsError::AttributeError);
    };
    let suffix = cps(ACKNOWLEDGED_SUFFIX);
    Ok(name.ends_with(&suffix))
}

/// Python's `bool(value)`. A `PurePosixPath` and a `datetime` are true.
pub fn truthy(value: &PyValue) -> bool {
    match value {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(text) => text != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) => !s.is_empty(),
        PyValue::DateTime(_) | PyValue::Path(_) => true,
        PyValue::Tuple(v) | PyValue::List(v) => !v.is_empty(),
        PyValue::Dict(v) => !v.is_empty(),
    }
}

/// `dict.get` by a `str` key: the last entry, as `json::loads` and
/// `PyValue::get` keep the last of a repeated key.
fn get<'a>(fields: &'a [(PyValue, PyValue)], name: &str) -> Option<&'a PyValue> {
    fields.iter().rev().find_map(|(k, v)| match k {
        PyValue::Str(s) if is(s, name) => Some(v),
        _ => None,
    })
}

/// `is_audit_row(kind, fields)`.
pub fn is_audit_row(kind: &[u32], fields: &[(PyValue, PyValue)]) -> Result<bool, EventsError> {
    if among(kind, AUDIT_KINDS) {
        return Ok(true);
    }
    if !among(kind, OVERRIDE_EVIDENCE_KINDS) {
        return Ok(false);
    }
    if let Some(PyValue::Str(status)) = get(fields, "status") {
        if among(status, AUDIT_STATUSES) || OVERRIDABLE_REFUSALS.iter().any(|(s, _)| is(status, s))
        {
            return Ok(true);
        }
    }
    if matches!(get(fields, "forced"), Some(PyValue::Bool(true))) {
        return Ok(true);
    }
    for (key, value) in fields {
        if acknowledged(key)? && truthy(value) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `row_class(kind)`.
pub fn row_class(kind: &[u32]) -> RowClass {
    if kept_of(kind).is_some() {
        RowClass::State
    } else {
        RowClass::Measurement
    }
}

/// `prunable(kind)`.
pub fn prunable(kind: &[u32]) -> bool {
    row_class(kind) == RowClass::Measurement
}

fn filtered(items: &[(PyValue, PyValue)], keys: &[&str]) -> PyValue {
    PyValue::Dict(
        items
            .iter()
            .filter(|(k, _)| matches!(k, PyValue::Str(s) if among(s, keys)))
            .cloned()
            .collect(),
    )
}

/// `_keys_of(value, keys)`: a dict with only `keys`, each dict of a list
/// so filtered (an item that is not a dict dropped), None for the rest.
pub fn keys_of(value: &PyValue, keys: &[&str]) -> PyValue {
    match value {
        PyValue::Dict(items) => filtered(items, keys),
        PyValue::List(list) => PyValue::List(
            list.iter()
                .filter_map(|item| match item {
                    PyValue::Dict(items) => Some(filtered(items, keys)),
                    _ => None,
                })
                .collect(),
        ),
        _ => PyValue::None,
    }
}

/// `state_fields(kind, fields)`, with `redact_query` passed as `redact`
/// (called on a kept-by-keys field among `_REDACTED_TEXT_FIELDS` that is
/// still a `str`). The output keeps the input's order.
pub fn state_fields(
    kind: &[u32],
    fields: &[(PyValue, PyValue)],
    redact: &mut dyn FnMut(&[u32]) -> PyValue,
) -> Result<Vec<(PyValue, PyValue)>, EventsError> {
    let kept = kept_of(kind).unwrap_or(&[]);
    let evidence = among(kind, OVERRIDE_EVIDENCE_KINDS);
    let mut out = Vec::new();
    for (name, value) in fields {
        let entry = match name {
            PyValue::Str(s) => kept.iter().find(|(n, _)| is(s, n)),
            _ => None,
        };
        if let (Some((field, keys)), PyValue::Str(_)) = (entry, name) {
            let Some(keys) = keys else {
                out.push((name.clone(), value.clone()));
                continue;
            };
            let kept_value = match value {
                PyValue::Str(text) if among(&cps(field), REDACTED_TEXT_FIELDS) => {
                    keys_of(&redact(text), keys)
                }
                _ => keys_of(value, keys),
            };
            out.push((name.clone(), kept_value));
            continue;
        }
        let stamp = matches!(name, PyValue::Str(s) if among(s, STATE_STAMP_FIELDS));
        if stamp || (evidence && acknowledged(name)?) {
            out.push((name.clone(), value.clone()));
        }
    }
    Ok(out)
}

/// `_event_id_items`: an event's id-list field as `(original_index, id)`
/// pairs. A `list` keeps its `str` elements at their indices; a lone
/// non-empty `str` is one id at slot 0; every other value is empty.
pub fn event_id_items(value: &PyValue) -> Vec<(usize, PyStr)> {
    match value {
        PyValue::List(items) => items
            .iter()
            .enumerate()
            .filter_map(|(i, v)| match v {
                PyValue::Str(s) => Some((i, s.clone())),
                _ => None,
            })
            .collect(),
        PyValue::Str(s) if !s.is_empty() => vec![(0, s.clone())],
        _ => Vec::new(),
    }
}

/// `_event_id_list`: `event_id_items` without the indices.
pub fn event_id_list(value: &PyValue) -> Vec<PyStr> {
    event_id_items(value).into_iter().map(|(_, v)| v).collect()
}

/// `recall_state._str_ids` (`recall_state.py`), one line over
/// `_event_id_items`; `recall_settle` imports it.
pub fn str_ids(value: &PyValue) -> Vec<PyStr> {
    event_id_list(value)
}
