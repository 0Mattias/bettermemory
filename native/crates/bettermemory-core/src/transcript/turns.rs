//! `InterruptedTurn`, `interrupted_turns`, `owner_words`, `CompactMarker`
//! and `compact_marker` (`transcript.py` 259-450).

use std::num::NonZeroUsize;

use super::stop::{get, is_str, Pending};
use super::{prompt_id, row, Opener, OwnerPrompt, ReverseLines, INTERRUPT_OPENING};
use crate::datetime::{self, DateTime};
use crate::pyvalue::{PyError, PyStr, PyValue};

/// `InterruptedTurn`: a turn the owner interrupted.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InterruptedTurn {
    pub prompt_id: Option<PyStr>,
    pub prompt: Option<PyStr>,
    pub reply: Option<PyStr>,
    pub model: Option<PyStr>,
    pub at: DateTime,
}

/// `CompactMarker`: a `compact_boundary` row's key and timestamp.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompactMarker {
    pub key: PyStr,
    pub at: DateTime,
}

/// `parse_event_ts(row.get("timestamp"))`: None for an absent key, None
/// or any value that is not a `str`, as `parse_event_ts` declines them.
fn stamp_of(fields: &[(PyValue, PyValue)]) -> Option<DateTime> {
    match get(fields, "timestamp") {
        Some(PyValue::Str(s)) => datetime::parse_event_ts(s),
        _ => None,
    }
}

/// The instant of an aware datetime; a naive one cannot be compared with
/// the aware stamps `parse_event_ts` answers, which is Python's TypeError.
fn instant(dt: &DateTime) -> Result<i128, PyError> {
    dt.instant_us().ok_or(PyError::TypeError)
}

/// `text.lstrip()` of `str`: Python's whitespace (`str.isspace`).
fn lstrip(text: &[u32]) -> &[u32] {
    let start = text
        .iter()
        .position(|&c| !crate::unicode::is_space(c))
        .unwrap_or(text.len());
    &text[start..]
}

fn starts_with(text: &[u32], prefix: &str) -> bool {
    let p: Vec<u32> = prefix.chars().map(u32::from).collect();
    text.starts_with(&p)
}

/// The turn being read, newest row first.
struct Turn {
    current: Option<PyStr>,
    in_turn: bool,
    interrupted: bool,
    prompt: Option<PyStr>,
    newest: Option<DateTime>,
    reply: Option<PyStr>,
    model: Option<PyStr>,
}

impl Turn {
    /// `close()`.
    fn close(&self, out: &mut Vec<InterruptedTurn>) {
        if let (true, true, Some(at), Some(prompt)) =
            (self.interrupted, self.in_turn, self.newest, &self.prompt)
        {
            out.push(InterruptedTurn {
                prompt_id: self.current.clone(),
                prompt: Some(prompt.clone()),
                reply: self.reply.clone(),
                model: self.model.clone(),
                at,
            });
        }
    }
}

/// `interrupted_turns(path, since=..., chunk_bytes=..., budget=...)`: the
/// interrupted turns stamped after `since`, oldest first. Err for the
/// RecursionError of a line nested past the JSON decoder's depth, and
/// TypeError when `since` is naive and a user row carries a stamp.
pub fn interrupted_turns<O: Opener, P: OwnerPrompt + ?Sized>(
    opener: &O,
    owner: &P,
    path: &[u8],
    since: &DateTime,
    chunk_bytes: NonZeroUsize,
    budget: i64,
) -> Result<Vec<InterruptedTurn>, PyError> {
    let lines = ReverseLines::new(opener, path, chunk_bytes, budget, None);
    let mut out = Vec::new();
    let mut turn = Turn {
        current: None,
        in_turn: false,
        interrupted: false,
        prompt: None,
        newest: None,
        reply: None,
        model: None,
    };
    let mut pending = Pending::default();
    for line in lines {
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
        let stamp = stamp_of(fields);
        if let Some(s) = &stamp {
            if instant(s)? <= instant(since)? {
                break;
            }
        }
        let row_id = prompt_id(fields);
        if !turn.in_turn || row_id != turn.current || row_id.is_none() {
            turn.close(&mut out);
            turn = Turn {
                current: row_id,
                in_turn: true,
                interrupted: false,
                prompt: None,
                newest: None,
                reply: pending.text.take(),
                model: pending.model.take(),
            };
        } else {
            if turn.reply.is_none() {
                turn.reply = pending.text.take();
            }
            if turn.model.is_none() {
                turn.model = pending.model.take();
            }
        }
        pending = Pending::default();
        if !owner.is_submitted(&value) {
            continue;
        }
        if turn.newest.is_none() {
            turn.newest = stamp;
        }
        if let Some(text) = owner.submitted_text(&value) {
            if starts_with(lstrip(&text), INTERRUPT_OPENING) {
                turn.interrupted = true;
                continue;
            }
        }
        if turn.prompt.is_none() {
            turn.prompt = owner.owner_prompt(&value, true);
        }
    }
    turn.close(&mut out);
    out.reverse();
    Ok(out)
}

/// `owner_words(path, chars=..., chunk_bytes=..., budget=...)`: the newest
/// `chars` code points of the owner's words, oldest first, joined by blank
/// lines. Err only for the RecursionError the reference raises.
pub fn owner_words<O: Opener, P: OwnerPrompt + ?Sized>(
    opener: &O,
    owner: &P,
    path: &[u8],
    chars: i64,
    chunk_bytes: NonZeroUsize,
    budget: i64,
) -> Result<PyStr, PyError> {
    if chars <= 0 || !opener.is_file(path) {
        return Ok(Vec::new());
    }
    let mut pieces: Vec<PyStr> = Vec::new();
    let mut held: i64 = 0;
    for line in ReverseLines::new(opener, path, chunk_bytes, budget, None) {
        if !line.windows(6).any(|w| w == b"\"user\"") {
            continue;
        }
        let Some(value) = row(&line)? else {
            continue;
        };
        let Some(text) = owner.owner_prompt(&value, false) else {
            continue;
        };
        if text.is_empty() {
            continue;
        }
        held += text.len() as i64 + if pieces.is_empty() { 0 } else { 2 };
        pieces.push(text);
        if held >= chars {
            break;
        }
    }
    pieces.reverse();
    let mut joined: PyStr = Vec::new();
    for (i, piece) in pieces.iter().enumerate() {
        if i > 0 {
            joined.extend([10, 10]);
        }
        joined.extend_from_slice(piece);
    }
    let keep = usize::try_from(chars)
        .unwrap_or(usize::MAX)
        .min(joined.len());
    Ok(joined.split_off(joined.len() - keep))
}

/// `compact_marker(path, near=..., window=..., chunk_bytes=...,
/// budget=...)`: the newest `compact_boundary` row stamped within
/// `window_us` microseconds (a `timedelta`) of `near`. Err for the
/// RecursionError of a nested line, and TypeError when `near` is naive and
/// a boundary row carries a stamp.
pub fn compact_marker<O: Opener>(
    opener: &O,
    path: &[u8],
    near: &DateTime,
    window_us: i128,
    chunk_bytes: NonZeroUsize,
    budget: i64,
) -> Result<Option<CompactMarker>, PyError> {
    let lines = ReverseLines::new(
        opener,
        path,
        chunk_bytes,
        budget,
        Some(b"compact_boundary".as_slice()),
    );
    for line in lines {
        let Some(value) = row(&line)? else {
            continue;
        };
        let PyValue::Dict(fields) = &value else {
            continue;
        };
        if !is_str(get(fields, "type"), "system")
            || !is_str(get(fields, "subtype"), "compact_boundary")
        {
            continue;
        }
        let Some(stamp) = stamp_of(fields) else {
            continue;
        };
        if (instant(&stamp)? - instant(near)?).abs() > window_us {
            continue;
        }
        let key = match get(fields, "uuid") {
            Some(PyValue::Str(u)) if !u.is_empty() => u.clone(),
            // A parsed stamp is a str: `str()` of it is itself.
            _ => match get(fields, "timestamp") {
                Some(PyValue::Str(s)) => s.clone(),
                _ => Vec::new(),
            },
        };
        return Ok(Some(CompactMarker { key, at: stamp }));
    }
    Ok(None)
}
