//! `_row`, `_prompt_id`, `StopTurn`, `_Pending` and `stop_turn`
//! (`transcript.py` 131-258).

use std::num::NonZeroUsize;

use super::{Opener, OwnerPrompt, ReverseLines};
use crate::json;
use crate::pyvalue::{PyError, PyStr, PyValue};

/// `dict.get(key)` of a row's string key.
fn get<'v>(dict: &'v [(PyValue, PyValue)], key: &str) -> Option<&'v PyValue> {
    dict.iter().find_map(|(k, v)| match k {
        PyValue::Str(s) if s.iter().copied().eq(key.chars().map(u32::from)) => Some(v),
        _ => None,
    })
}

fn is_str(value: Option<&PyValue>, text: &str) -> bool {
    matches!(value, Some(PyValue::Str(s)) if s.iter().copied().eq(text.chars().map(u32::from)))
}

/// `_row(line)`: the line's JSON object, None for a blank line, a broken
/// one or any other value. The line is decoded as UTF-8 with
/// `errors="replace"`. RecursionError is no ValueError: it is raised
/// through, as the reference raises it out of the walk.
pub fn row(line: &[u8]) -> Result<Option<PyValue>, PyError> {
    // `bytes.strip()`: ASCII whitespace.
    if line
        .iter()
        .all(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
    {
        return Ok(None);
    }
    let text: PyStr = String::from_utf8_lossy(line)
        .chars()
        .map(u32::from)
        .collect();
    match json::loads(&text) {
        Ok(value @ PyValue::Dict(_)) => Ok(Some(value)),
        Ok(_) => Ok(None),
        Err(PyError::RecursionError) => Err(PyError::RecursionError),
        Err(_) => Ok(None),
    }
}

/// `_prompt_id(row)`: the row's `promptId` when it is a nonempty string.
pub fn prompt_id(row: &[(PyValue, PyValue)]) -> Option<PyStr> {
    match get(row, "promptId") {
        Some(PyValue::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// `StopTurn`: the turn a Stop ends, as the transcript holds it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StopTurn {
    pub prompt: Option<PyStr>,
    pub reply: Option<PyStr>,
    pub model: Option<PyStr>,
}

/// `_Pending`: the newest text and model among the assistant rows read
/// since the last user row.
#[derive(Default)]
struct Pending {
    text: Option<PyStr>,
    model: Option<PyStr>,
}

impl Pending {
    fn take<P: OwnerPrompt + ?Sized>(&mut self, row: &[(PyValue, PyValue)], owner: &P) {
        let Some(PyValue::Dict(message)) = get(row, "message") else {
            return;
        };
        if self.model.is_none() {
            if let Some(PyValue::Str(m)) = get(message, "model") {
                if !m.is_empty() {
                    self.model = Some(m.clone());
                }
            }
        }
        if self.text.is_none() {
            let absent = PyValue::None;
            let content = get(message, "content").unwrap_or(&absent);
            self.text = owner.text_blocks(content);
        }
    }
}

/// `stop_turn(path, prompt_id, chunk_bytes=..., budget=...)`: the turn
/// the Stop names, newest row first; Ok(None) when the transcript does not
/// say what the turn's prompt was. Err only for the RecursionError of a
/// line nested past the JSON decoder's depth, which the reference raises.
pub fn stop_turn<O: Opener, P: OwnerPrompt + ?Sized>(
    opener: &O,
    owner: &P,
    path: &[u8],
    prompt_id_arg: Option<&[u32]>,
    chunk_bytes: NonZeroUsize,
    budget: i64,
) -> Result<Option<StopTurn>, PyError> {
    let mut lines = ReverseLines::new(opener, path, chunk_bytes, budget, None);
    let mut target: Option<PyStr> = prompt_id_arg.map(<[u32]>::to_vec);
    let mut in_turn = false;
    let mut submitted = false;
    let mut pending = Pending::default();
    let mut reply: Option<PyStr> = None;
    let mut model: Option<PyStr> = None;
    let mut broke = false;
    for line in lines.by_ref() {
        let Some(value) = row(&line)? else {
            continue;
        };
        let PyValue::Dict(fields) = &value else {
            continue;
        };
        let kind = get(fields, "type");
        if is_str(kind, "assistant") {
            pending.take(fields, owner);
            continue;
        }
        if !is_str(kind, "user") {
            continue;
        }
        let row_id = prompt_id(fields);
        if !in_turn {
            if target.is_some() && row_id != target {
                pending = Pending::default(); // a newer turn's rows
                continue;
            }
            target = row_id;
            in_turn = true;
        } else if row_id != target {
            broke = true; // an older turn: this one began after it
            break;
        }
        if reply.is_none() {
            reply = pending.text.take();
        }
        if model.is_none() {
            model = pending.model.take();
        }
        pending = Pending::default();
        if !owner.is_submitted(&value) {
            continue;
        }
        submitted = true;
        if let Some(prompt) = owner.owner_prompt(&value, true) {
            return Ok(Some(StopTurn {
                prompt: Some(prompt),
                reply,
                model,
            }));
        }
        if target.is_none() {
            broke = true; // without ids, a submitted row ends its turn
            break;
        }
    }
    if !broke && in_turn && !lines.reached_start {
        return Ok(None); // the budget ended inside the turn: undecided
    }
    if !submitted {
        return Ok(None);
    }
    Ok(Some(StopTurn {
        prompt: None,
        reply,
        model,
    }))
}
