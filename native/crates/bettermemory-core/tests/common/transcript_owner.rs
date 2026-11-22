//! A plain `OwnerPrompt` for the transcript kills and robustness tests:
//! a row is submitted and the owner's when its `message.content` is a
//! string, which is its text; no row has text blocks. It counts the
//! `owner_prompt` calls.

use std::cell::Cell;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::transcript::OwnerPrompt;

#[derive(Default)]
pub struct Plain {
    pub asked: Cell<usize>,
}

fn field<'v>(value: &'v PyValue, key: &str) -> Option<&'v PyValue> {
    let PyValue::Dict(fields) = value else {
        return None;
    };
    fields.iter().find_map(|(k, v)| match k {
        PyValue::Str(s) if s.iter().copied().eq(key.chars().map(u32::from)) => Some(v),
        _ => None,
    })
}

fn content(row: &PyValue) -> Option<PyStr> {
    match field(row, "message").and_then(|m| field(m, "content")) {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

impl OwnerPrompt for Plain {
    fn is_submitted(&self, row: &PyValue) -> bool {
        content(row).is_some()
    }

    fn owner_prompt(&self, row: &PyValue, _cut: bool) -> Option<PyStr> {
        self.asked.set(self.asked.get() + 1);
        content(row)
    }

    fn submitted_text(&self, row: &PyValue) -> Option<PyStr> {
        content(row)
    }

    fn text_blocks(&self, _content: &PyValue) -> Option<PyStr> {
        None
    }
}
