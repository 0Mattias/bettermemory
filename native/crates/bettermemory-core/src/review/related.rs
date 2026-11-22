//! The related-set records of `review.py` and the text one related entry
//! shows: `RelatedEntry`, `RelatedSet`, `_SENTENCE_BREAK_RE`,
//! `related_text`, `related_entry` and `_write_sentences`.

use std::collections::HashSet;
use std::sync::OnceLock;

use super::RELATED_TEXT_CHARS;
use crate::datetime::DateError;
use crate::models_text::{snippet_window, strip};
use crate::pyre::regex::Regex;
use crate::pyvalue::{PyStr, PyValue};
use crate::records::Memory;
use crate::search_dedup::raw_content_token_set;
use crate::store::digest::claim_sized;

/// `RelatedEntry`: one stored statement a review shows. `updated` is the
/// ISO date, ASCII.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RelatedEntry {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub updated: String,
    pub claim_sized: bool,
    pub text: PyStr,
}

impl RelatedEntry {
    /// `to_dict()`, in the reference's key order.
    pub fn to_dict(&self) -> PyValue {
        PyValue::Dict(vec![
            (PyValue::str("id"), PyValue::Str(self.id.clone())),
            (
                PyValue::str("scopes"),
                PyValue::List(self.scopes.iter().cloned().map(PyValue::Str).collect()),
            ),
            (PyValue::str("updated"), PyValue::str(&self.updated)),
            (PyValue::str("claim_sized"), PyValue::Bool(self.claim_sized)),
            (PyValue::str("text"), PyValue::Str(self.text.clone())),
        ])
    }
}

/// What `RelatedSet` reads of the cue code's `SupersessionReport`: the
/// memory ids of its `supersedes` and its `conflicts`, each in report
/// order. The supersession unit converts its report into this shape.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CueReport {
    pub supersedes: Vec<PyStr>,
    pub conflicts: Vec<PyStr>,
}

/// `RelatedSet`: the entries in review order and the cue report, None
/// when `behavior.write_supersession` is off.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RelatedSet {
    pub entries: Vec<RelatedEntry>,
    pub report: Option<CueReport>,
}

impl RelatedSet {
    /// `ids`.
    pub fn ids(&self) -> Vec<PyStr> {
        self.entries.iter().map(|e| e.id.clone()).collect()
    }

    /// `cue_supersedes`: every id the cue code links.
    pub fn cue_supersedes(&self) -> Vec<PyStr> {
        self.report
            .as_ref()
            .map_or_else(Vec::new, |r| r.supersedes.clone())
    }

    /// `cue_conflicts`: every id the cue code files.
    pub fn cue_conflicts(&self) -> Vec<PyStr> {
        self.report
            .as_ref()
            .map_or_else(Vec::new, |r| r.conflicts.clone())
    }

    /// `only(ids)`: narrowed to `ids`, in review order.
    pub fn only<'a>(&self, ids: impl IntoIterator<Item = &'a [u32]>) -> RelatedSet {
        let keep: HashSet<&[u32]> = ids.into_iter().collect();
        RelatedSet {
            entries: self
                .entries
                .iter()
                .filter(|e| keep.contains(e.id.as_slice()))
                .cloned()
                .collect(),
            report: self.report.clone(),
        }
    }

    /// `plus(entries)`: `entries` after the set's own, each id once.
    pub fn plus(&self, entries: impl IntoIterator<Item = RelatedEntry>) -> RelatedSet {
        let mut have: HashSet<PyStr> = self.entries.iter().map(|e| e.id.clone()).collect();
        let mut out = self.entries.clone();
        for entry in entries {
            if have.insert(entry.id.clone()) {
                out.push(entry);
            }
        }
        RelatedSet {
            entries: out,
            report: self.report.clone(),
        }
    }
}

/// `_SENTENCE_BREAK_RE`'s pattern text, compiled with no flags.
pub const SENTENCE_BREAK_PATTERN: &str = r"(?<=[.!?])\s+|\n+";

fn sentence_break() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new_str(SENTENCE_BREAK_PATTERN, 0).expect("the engine builds _SENTENCE_BREAK_RE")
    })
}

/// `related_text(content, body)`: the stripped body whole up to
/// `RELATED_TEXT_CHARS`, else a window from the first sentence holding a
/// shared content token (the head when none does).
pub fn related_text(content: &[u32], body: &[u32]) -> PyStr {
    let text = strip(body);
    if text.len() <= RELATED_TEXT_CHARS {
        return text.to_vec();
    }
    let ours = raw_content_token_set(content);
    let theirs = raw_content_token_set(text);
    let shared: HashSet<&String> = ours.intersection(&theirs).collect();
    let mut start = 0;
    if !shared.is_empty() {
        // No budget is given, so the only failure is memory, which the
        // plain call ends as an allocation failure ends.
        let breaks = sentence_break()
            .finditer(text, 0, isize::MAX)
            .expect("finditer without a budget");
        let mut position = 0usize;
        let spans = breaks
            .iter()
            .map(|m| m.span(0).map(|(a, b)| (a as usize, b as usize)))
            .chain(std::iter::once(None));
        for brk in spans {
            let end = brk.map_or(text.len(), |(a, _)| a);
            let piece = text.get(position..end).unwrap_or(&[]);
            if raw_content_token_set(piece)
                .iter()
                .any(|t| shared.contains(t))
            {
                start = position;
                break;
            }
            match brk {
                None => break,
                Some((_, b)) => position = b,
            }
        }
    }
    snippet_window(text, start as i64, RELATED_TEXT_CHARS as i64 - 3)
}

/// Why `related_entry` answers no entry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RelatedError {
    /// `updated.astimezone(timezone.utc)` raised.
    Date(DateError),
    /// A naive `updated`, which the reference reads as the machine's
    /// local time: not ported.
    NaiveUpdated,
}

/// `related_entry(content, memory)`.
pub fn related_entry(content: &[u32], memory: &Memory) -> Result<RelatedEntry, RelatedError> {
    let f = &memory.fields;
    let utc = f
        .updated
        .to_utc()
        .ok_or(RelatedError::NaiveUpdated)?
        .map_err(RelatedError::Date)?;
    Ok(RelatedEntry {
        id: f.id.clone(),
        scopes: f.scopes.clone(),
        updated: format!("{:04}-{:02}-{:02}", utc.year, utc.month, utc.day),
        claim_sized: claim_sized(&f.body),
        text: related_text(content, &f.body),
    })
}

/// `_write_sentences(content)`: the pieces `_SENTENCE_BREAK_RE.split`
/// leaves, stripped, that hold a content token, each once, in order.
pub fn write_sentences(content: &[u32]) -> Vec<PyStr> {
    let parts = sentence_break()
        .split(content, 0)
        .expect("split without a budget");
    let mut seen: HashSet<PyStr> = HashSet::new();
    let mut out = Vec::new();
    for part in parts.into_iter().flatten() {
        let p = strip(&part).to_vec();
        if !p.is_empty() && !raw_content_token_set(&p).is_empty() && seen.insert(p.clone()) {
            out.push(p);
        }
    }
    out
}
