//! `owner_prompt.py` but `_ENVELOPES` and `probe_text`: the one
//! definition of an owner prompt, judged by a row's marks first and its
//! text second.
//!
//! `probe_text` removes the envelope blocks with lazy patterns under
//! `DOTALL|IGNORECASE`, which wait for the regular-expression engine;
//! until then every function that reads it takes it as `probe`
//! (`probe(text, cut)`). Rows are `PyValue`s as `json.loads` gives them,
//! and a row is a dict (the reference's `row.get`).

use crate::machine::truthy;
use crate::pyvalue::{PyStr, PyValue};
use crate::transcript::OwnerPrompt;
use crate::unicode::{self, is_space};

/// `PROMPT_CUT_CHARS`.
pub const PROMPT_CUT_CHARS: usize = 2_000;

/// `ENVELOPE_TAGS`.
pub const ENVELOPE_TAGS: [&str; 3] = ["system-reminder", "pasted_content", "artifact-view-context"];

/// `INTERRUPT_OPENING`.
pub const INTERRUPT_OPENING: &str = "[Request interrupted";

/// `HARNESS_OPENINGS`.
pub const HARNESS_OPENINGS: [&str; 11] = [
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<agent-message",
    "<cross-session-message",
    "<side-sessions-event",
    "<scheduled-task",
    "Another Claude session sent a message",
    INTERRUPT_OPENING,
];

/// `COMPOSED_OPENINGS`: `<` and each envelope tag, then the harness
/// openings.
pub const COMPOSED_OPENINGS: [&str; 14] = [
    "<system-reminder",
    "<pasted_content",
    "<artifact-view-context",
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<agent-message",
    "<cross-session-message",
    "<side-sessions-event",
    "<scheduled-task",
    "Another Claude session sent a message",
    INTERRUPT_OPENING,
];

/// `HUMAN_ORIGIN`.
pub const HUMAN_ORIGIN: &str = "human";

/// `QUEUED_COMMAND`.
pub const QUEUED_COMMAND: &str = "queued_command";

/// `probe_text(text, cut=cut)`, supplied by the caller.
pub type Probe<'a> = &'a dyn Fn(&[u32], bool) -> PyStr;

fn is_str(value: Option<&PyValue>, text: &str) -> bool {
    matches!(value, Some(PyValue::Str(s)) if *s == unicode::cps(text))
}

/// `owner_text(text, cut=cut)`.
pub fn owner_text(text: &[u32], cut: bool, probe: Probe) -> Option<PyStr> {
    let probed = probe(text, cut);
    let start = probed.iter().position(|&c| !is_space(c))?;
    let rest = &probed[start..];
    let composed = COMPOSED_OPENINGS
        .iter()
        .any(|opening| rest.starts_with(&unicode::cps(opening)));
    if composed {
        None
    } else {
        Some(probed)
    }
}

/// `text_blocks(content)`.
pub fn text_blocks(content: &PyValue) -> Option<PyStr> {
    let PyValue::List(blocks) = content else {
        return None;
    };
    let parts: Vec<&PyStr> = blocks
        .iter()
        .filter(|block| matches!(block, PyValue::Dict(_)) && is_str(block.get("type"), "text"))
        .filter_map(|block| match block.get("text") {
            Some(PyValue::Str(text)) if !text.is_empty() => Some(text),
            _ => None,
        })
        .collect();
    if parts.is_empty() {
        return None;
    }
    let mut out = Vec::new();
    for (i, part) in parts.iter().enumerate() {
        if i > 0 {
            out.push(u32::from('\n'));
        }
        out.extend_from_slice(part);
    }
    Some(out)
}

/// `is_submitted(row)`.
pub fn is_submitted(row: &PyValue) -> bool {
    if !is_str(row.get("type"), "user") {
        return false;
    }
    // `PyValue::get` answers None off a dict, as the reference's
    // `message.get(...) if isinstance(message, dict) else None`.
    let content = row.get("message").and_then(|m| m.get("content"));
    match content {
        Some(PyValue::Str(_)) => true,
        Some(PyValue::List(blocks)) => !blocks.iter().any(|block| {
            matches!(block, PyValue::Dict(_)) && is_str(block.get("type"), "tool_result")
        }),
        _ => false,
    }
}

/// `submitted_text(row)`.
pub fn submitted_text(row: &PyValue) -> Option<PyStr> {
    if !is_submitted(row) {
        return None;
    }
    match row.get("message").and_then(|m| m.get("content")) {
        Some(PyValue::Str(text)) => Some(text.clone()),
        Some(content) => text_blocks(content),
        None => None,
    }
}

fn mark(row: &PyValue, key: &str) -> bool {
    row.get(key).is_some_and(truthy)
}

/// `_subagents(row)`.
fn subagents(row: &PyValue) -> bool {
    mark(row, "isSidechain") || mark(row, "agentId")
}

/// `queued_text(row)`.
pub fn queued_text(row: &PyValue) -> Option<PyStr> {
    if !is_str(row.get("type"), "attachment") || subagents(row) {
        return None;
    }
    let attachment = row.get("attachment")?;
    if !matches!(attachment, PyValue::Dict(_)) || !is_str(attachment.get("type"), QUEUED_COMMAND) {
        return None;
    }
    if mark(attachment, "isMeta") {
        return None;
    }
    match attachment.get("prompt") {
        Some(PyValue::Str(prompt)) if !unicode::py_strip(prompt).is_empty() => Some(prompt.clone()),
        _ => None,
    }
}

/// `owner_prompt(row, cut=cut)`.
pub fn owner_prompt(row: &PyValue, cut: bool, probe: Probe) -> Option<PyStr> {
    if mark(row, "isMeta") || mark(row, "isCompactSummary") || subagents(row) {
        return None;
    }
    match row.get("turnOrigin") {
        None | Some(PyValue::None) => {}
        origin if is_str(origin, HUMAN_ORIGIN) => {}
        Some(_) => return None,
    }
    owner_text(&submitted_text(row)?, cut, probe)
}

/// The module as `transcript.py` calls it, with `probe_text` supplied.
pub struct Reference<'a> {
    pub probe: Probe<'a>,
}

impl OwnerPrompt for Reference<'_> {
    fn is_submitted(&self, row: &PyValue) -> bool {
        is_submitted(row)
    }

    fn owner_prompt(&self, row: &PyValue, cut: bool) -> Option<PyStr> {
        owner_prompt(row, cut, self.probe)
    }

    fn submitted_text(&self, row: &PyValue) -> Option<PyStr> {
        submitted_text(row)
    }

    fn text_blocks(&self, content: &PyValue) -> Option<PyStr> {
        text_blocks(content)
    }
}
