//! The guard on a change to a standing memory, `handlers/_guard.py` whole:
//! `read_words`, `record`, `Held`, `check` and `guard`, with the hints.
//!
//! Everything the guard asks of the store, the standing question, the
//! caller and the prompt hook's words comes through [`GuardDeps`], in the
//! reference's order: `guarded_ids` only for a non-empty list, `asking`
//! only when something is guarded, the caller only once `asking` says yes.

use std::collections::HashSet;

use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::{cps, is_space};

/// `_HINT`.
pub const HINT: &str = "Not changed: it would replace, remove, demote, re-scope or edit a \
standing memory, quoted in `standing`, which directs future work until the user changes it. \
Leave it as it is; or, when the user changed it in this conversation, re-send this same call \
with said=<the user's words as they typed them>.";

/// `_RULE_S_HINT`.
pub const RULE_S_HINT: &str = "Not changed: the memory states a rule, and the supersedes links \
this call adds would take each memory under `standing` out of force. A rule replaces stored \
memories only on the user's words: if the user gave it in this conversation, re-send this same \
call with said=<the user's words as they typed them>; otherwise leave those links out.";

/// `_UNSEEN` with `{name}` filled in.
pub fn unseen(name: &[u32]) -> PyStr {
    let mut m = name.to_vec();
    m.extend(cps(
        " is not in any prompt the hook saw in this checkout (the last twenty, word for word, \
whitespace and case folded, eight characters or more): quote the user's words as they typed them.",
    ));
    m
}

/// What the guard reads from the handler's collaborators.
pub trait GuardDeps {
    /// The caller `caller_anchoring` resolves, opaque here.
    type Caller;
    /// What `caller_anchoring` raises, passed through unchanged.
    type Error;
    /// `standing.guarded_ids(store, ids)`.
    fn guarded_ids(&mut self, ids: &[PyStr]) -> Vec<PyStr>;
    /// `standing.asking()`.
    fn asking(&mut self) -> bool;
    /// `caller_anchoring(store, config)`'s caller, with its
    /// `resolution.checkout`.
    fn caller(&mut self) -> Result<(Option<PyStr>, Self::Caller), Self::Error>;
    /// `standing.refusal_rows(store, ids, Admission.of(REVIEW_STRATA, caller))`.
    fn refusal_rows(&mut self, ids: &[PyStr], caller: &Self::Caller) -> PyValue;
    /// `recall.WORDS.seen(store.path, checkout)`.
    fn words_seen(&mut self, checkout: &[u32]) -> bool;
    /// `recall.WORDS.holds(store.path, checkout, quote)`.
    fn words_hold(&mut self, checkout: &[u32], quote: &[u32]) -> bool;
    /// `recorder.record(kind, **fields)`.
    fn record_row(&mut self, kind: &str, fields: Vec<(&'static str, PyValue)>);
}

/// What `check` raises: the caller's error, or `read_words`' `ValueError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GuardError<E> {
    Caller(E),
    Value(PyStr),
}

/// `Words`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Words {
    pub quote: PyStr,
    pub checked: bool,
}

/// `read_words(deps, caller, said, name=name)`, the caller given by its
/// checkout.
pub fn read_words<D: GuardDeps + ?Sized>(
    deps: &mut D,
    checkout: Option<&[u32]>,
    said: Option<&[u32]>,
    name: &[u32],
) -> Result<Option<Words>, PyStr> {
    let Some(said) = said else {
        return Ok(None);
    };
    // `" ".join(said.split())`.
    let words: Vec<&[u32]> = said
        .split(|&c| is_space(c))
        .filter(|w| !w.is_empty())
        .collect();
    let quote = words.join(&[u32::from(b' ')][..]);
    if quote.is_empty() {
        let mut m = name.to_vec();
        m.extend(cps(
            " quotes the user's words that make this change; it cannot be empty",
        ));
        return Err(m);
    }
    let Some(checkout) = checkout.filter(|c| deps.words_seen(c)) else {
        return Ok(Some(Words {
            quote,
            checked: false,
        }));
    };
    if deps.words_hold(checkout, &quote) {
        return Ok(Some(Words {
            quote,
            checked: true,
        }));
    }
    Err(unseen(name))
}

/// `record(deps, door, records, words)`: the `standing_guard` row.
pub fn record<D: GuardDeps + ?Sized>(deps: &mut D, door: &[u32], records: &[PyStr], words: &Words) {
    deps.record_row(
        "standing_guard",
        vec![
            ("door", PyValue::Str(door.to_vec())),
            (
                "standing",
                PyValue::List(records.iter().cloned().map(PyValue::Str).collect()),
            ),
            ("said", PyValue::Str(words.quote.clone())),
            ("said_checked", PyValue::Bool(words.checked)),
        ],
    );
}

/// `Held`: a change let through on the user's words, its row written once
/// the change commits.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Held {
    pub door: PyStr,
    pub records: Vec<PyStr>,
    pub words: Words,
}

impl Held {
    /// `Held.write`.
    pub fn write<D: GuardDeps + ?Sized>(&self, deps: &mut D) {
        record(deps, &self.door, &self.records, &self.words);
    }
}

/// What `check` returns: the refused_standing response, or the held row.
pub type Checked = (Option<PyValue>, Option<Held>);

/// `check(deps, door, records, said, rule_s=rule_s)`.
pub fn check<D: GuardDeps + ?Sized>(
    deps: &mut D,
    door: &[u32],
    records: &[PyStr],
    said: Option<&[u32]>,
    rule_s: Option<&[PyStr]>,
) -> Result<Checked, GuardError<D::Error>> {
    let own = if records.is_empty() {
        Vec::new()
    } else {
        deps.guarded_ids(records)
    };
    // `dict.fromkeys(rule_s or [])`: first occurrence kept.
    let mut seen: HashSet<&[u32]> = own.iter().map(|s| s.as_slice()).collect();
    let mut extra: Vec<PyStr> = Vec::new();
    for id in rule_s.unwrap_or(&[]) {
        if seen.insert(id) {
            extra.push(id.clone());
        }
    }
    let guarded: Vec<PyStr> = own.iter().chain(&extra).cloned().collect();
    if guarded.is_empty() || !deps.asking() {
        return Ok((None, None));
    }
    let (checkout, caller) = deps.caller().map_err(GuardError::Caller)?;
    let words =
        read_words(deps, checkout.as_deref(), said, &cps("said")).map_err(GuardError::Value)?;
    if let Some(words) = words {
        let held = Held {
            door: door.to_vec(),
            records: guarded,
            words,
        };
        return Ok((None, Some(held)));
    }
    let rows = deps.refusal_rows(&guarded, &caller);
    let mut hints: Vec<&str> = Vec::new();
    if !own.is_empty() {
        hints.push(HINT);
    }
    if !extra.is_empty() {
        hints.push(RULE_S_HINT);
    }
    let mut response = vec![
        (PyValue::str("status"), PyValue::str("refused_standing")),
        (PyValue::str("standing"), rows),
        (PyValue::str("hint"), PyValue::str(&hints.join(" "))),
    ];
    if !extra.is_empty() {
        response.push((PyValue::str("standing_write"), PyValue::Bool(true)));
    }
    Ok((Some(PyValue::Dict(response)), None))
}

/// `guard(deps, door, records, said)`: the refusal, or None with the row
/// written when the change rests on the user's words.
pub fn guard<D: GuardDeps + ?Sized>(
    deps: &mut D,
    door: &[u32],
    records: &[PyStr],
    said: Option<&[u32]>,
) -> Result<Option<PyValue>, GuardError<D::Error>> {
    let (refusal, held) = check(deps, door, records, said, None)?;
    if let Some(held) = held {
        held.write(deps);
    }
    Ok(refusal)
}
