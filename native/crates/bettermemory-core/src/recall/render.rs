//! The recall block (recall.py 1268-1372) and, from
//! `recall_admission.py` (126-160), `summarize` and `disputed_marks`.

use std::collections::{HashMap, HashSet};

use super::words::{join_words, occurrences};
use super::{MIN_SNIPPET_CHARS, RECALL_CAP_CHARS, RECALL_HEADER, SNIPPET_CHARS};
use crate::pyvalue::PyStr;
use crate::unicode::{self, cps};

const ELLIPSIS: u32 = 0x2026;

/// `RecallPointer`: one delivered candidate as the block renders it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RecallPointer {
    pub id: PyStr,
    pub summary: PyStr,
    pub snippet: PyStr,
    pub provenance: Option<PyStr>,
    pub remote_stamp: bool,
    pub trust_unavailable: bool,
    pub disputed: Option<PyStr>,
}

/// `recall_admission.summarize(body, limit)`: whitespace collapsed, cut
/// to `limit` code points with an ellipsis when longer.
pub fn summarize(body: &[u32], limit: i64) -> PyStr {
    let text = join_words(body);
    if (text.len() as i64) <= limit {
        return text;
    }
    let keep = limit.saturating_sub(1).max(0) as usize;
    let mut cut = rstrip(&text[..keep.min(text.len())]).to_vec();
    cut.push(ELLIPSIS);
    cut
}

/// `recall_admission.disputed_marks(pairs)`: `{memory id: mark}` in the
/// dict's order (the order each member is first named).
pub fn disputed_marks(pairs: &[(PyStr, PyStr, PyStr)]) -> Vec<(PyStr, PyStr)> {
    let mut by_member: Vec<(PyStr, Vec<PyStr>)> = Vec::new();
    let mut index: HashMap<PyStr, usize> = HashMap::new();
    let mut listed_once: HashSet<(usize, PyStr)> = HashSet::new();
    for (a, b, pair) in pairs {
        for member in [a, b] {
            let at = *index.entry(member.clone()).or_insert_with(|| {
                by_member.push((member.clone(), Vec::new()));
                by_member.len() - 1
            });
            if listed_once.insert((at, pair.clone())) {
                by_member[at].1.push(pair.clone());
            }
        }
    }
    by_member
        .into_iter()
        .map(|(member, mut ids)| {
            ids.sort();
            let more = ids.len() - 1;
            let mut mark = cps("[disputed: ");
            mark.extend_from_slice(&ids[0][..ids[0].len().min(15)]);
            if more > 0 {
                mark.extend(cps(&format!(" +{}", more.min(99))));
            }
            mark.push(u32::from(b']'));
            (member, mark)
        })
        .collect()
}

fn rstrip(t: &[u32]) -> &[u32] {
    let end = t
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .map_or(0, |i| i + 1);
    &t[..end]
}

fn strip_by(t: &[u32], drop: impl Fn(u32) -> bool) -> &[u32] {
    let Some(a) = t.iter().position(|&c| !drop(c)) else {
        return &[];
    };
    let b = t.iter().rposition(|&c| !drop(c)).map_or(a, |i| i + 1);
    &t[a..b]
}

/// `text in other`.
fn contains(other: &[u32], text: &[u32]) -> bool {
    text.is_empty() || !occurrences(text, other).is_empty()
}

/// `_annotation(pointer)`.
pub fn annotation(pointer: &RecallPointer) -> PyStr {
    if pointer.trust_unavailable {
        return cps("[provenance: unknown, key absent]");
    }
    match &pointer.provenance {
        Some(p) if *p != cps("local") => {
            let mut out = cps("[provenance: ");
            out.extend_from_slice(p);
            if pointer.remote_stamp {
                out.extend(cps(", unverified here"));
            }
            out.push(u32::from(b']'));
            out
        }
        _ => Vec::new(),
    }
}

/// `_core(text)`: the ellipses stripped from both ends, then whitespace.
pub fn core(text: &[u32]) -> &[u32] {
    strip_by(strip_by(text, |c| c == ELLIPSIS), unicode::is_space)
}

/// `_one_excerpt(pointer, snippet)`.
pub fn one_excerpt(
    pointer: &RecallPointer,
    snippet: Option<&[u32]>,
) -> (RecallPointer, Option<PyStr>) {
    if let Some(s) = snippet.filter(|s| !s.is_empty()) {
        if contains(&pointer.summary, core(s)) {
            return (pointer.clone(), None);
        }
        if contains(s, core(&pointer.summary)) {
            let mut replaced = pointer.clone();
            replaced.summary = s.to_vec();
            return (replaced, None);
        }
    }
    (pointer.clone(), snippet.map(<[u32]>::to_vec))
}

/// `_recall_line(pointer, snippet)`.
pub fn recall_line(pointer: &RecallPointer, snippet: Option<&[u32]>) -> PyStr {
    let mut line = pointer.id.clone();
    line.push(0x20);
    line.extend_from_slice(&pointer.summary);
    let note = annotation(pointer);
    if !note.is_empty() {
        line.push(0x20);
        line.extend(note);
    }
    if let Some(d) = pointer.disputed.as_ref().filter(|d| !d.is_empty()) {
        line.push(0x20);
        line.extend_from_slice(d);
    }
    if let Some(s) = snippet.filter(|s| !s.is_empty()) {
        line.extend(cps(" | "));
        line.extend_from_slice(s);
    }
    line
}

fn render(kept: &[RecallPointer], snippets: &[Option<PyStr>]) -> PyStr {
    let mut text = cps(RECALL_HEADER);
    for (p, s) in kept.iter().zip(snippets) {
        text.push(u32::from(b'\n'));
        text.extend(recall_line(p, s.as_deref()));
    }
    text
}

/// `render_recall_block(pointers)`: the block and the ids it shows, or
/// an empty text and no ids when nothing fits under `RECALL_CAP_CHARS`.
pub fn render_recall_block(pointers: &[RecallPointer]) -> (PyStr, Vec<PyStr>) {
    let mut kept = Vec::with_capacity(pointers.len());
    let mut snippets: Vec<Option<PyStr>> = Vec::with_capacity(pointers.len());
    for pointer in pointers {
        let cut = summarize(&pointer.snippet, SNIPPET_CHARS);
        let (p, s) = one_excerpt(pointer, (!cut.is_empty()).then_some(&cut[..]));
        kept.push(p);
        snippets.push(s);
    }
    let mut text = render(&kept, &snippets);
    for index in (0..kept.len()).rev() {
        if text.len() <= RECALL_CAP_CHARS {
            break;
        }
        let overflow = (text.len() - RECALL_CAP_CHARS) as i64;
        let Some(snippet) = snippets[index].take() else {
            continue;
        };
        let target = snippet.len() as i64 - overflow;
        snippets[index] = (target >= MIN_SNIPPET_CHARS).then(|| summarize(&snippet, target));
        text = render(&kept, &snippets);
    }
    while !kept.is_empty() && text.len() > RECALL_CAP_CHARS {
        kept.pop();
        snippets.pop();
        text = render(&kept, &snippets);
    }
    if kept.is_empty() {
        return (Vec::new(), Vec::new());
    }
    let ids = kept.into_iter().map(|p| p.id).collect();
    (text, ids)
}
