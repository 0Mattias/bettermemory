//! `_response_links.py`: the store's link rows recovered from a loaded
//! candidate snapshot (`candidate_link_rows`).

use std::collections::{HashMap, HashSet};

use crate::pyvalue::PyStr;
use crate::records::Memory;

/// One edge: the link type's value, the other memory's id and the note.
pub type Edge = (&'static str, PyStr, Option<PyStr>);

/// Per wanted id, in the order the ids first came: its outbound and its
/// inbound edges.
pub type LinkRows = Vec<(PyStr, (Vec<Edge>, Vec<Edge>))>;

/// `candidate_link_rows`: the rows of every wanted id from the snapshot's
/// links alone. An exact duplicate (source, type, target, note) is kept
/// once; each list is stably sorted by (type, id), so ties keep their
/// order in the snapshot. A repeated wanted id keeps its first place.
pub fn candidate_link_rows<'a>(
    memories: impl IntoIterator<Item = &'a Memory>,
    ids: impl IntoIterator<Item = &'a PyStr>,
) -> LinkRows {
    let mut out: LinkRows = Vec::new();
    let mut index: HashMap<PyStr, usize> = HashMap::new();
    for id in ids {
        if !index.contains_key(id) {
            index.insert(id.clone(), out.len());
            out.push((id.clone(), (Vec::new(), Vec::new())));
        }
    }
    let mut seen: HashSet<(PyStr, &'static str, PyStr, Option<PyStr>)> = HashSet::new();
    for memory in memories {
        let source = &memory.fields.id;
        for link in &memory.links {
            let target = &link.target_id;
            let from = index.get(source).copied();
            let to = index.get(target).copied();
            if from.is_none() && to.is_none() {
                continue;
            }
            let kind = link.link_type.value();
            let key = (source.clone(), kind, target.clone(), link.note.clone());
            if !seen.insert(key) {
                continue;
            }
            if let Some(i) = from {
                out[i].1 .0.push((kind, target.clone(), link.note.clone()));
            }
            if let Some(i) = to {
                out[i].1 .1.push((kind, source.clone(), link.note.clone()));
            }
        }
    }
    for (_, (outbound, inbound)) in &mut out {
        outbound.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
        inbound.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    }
    out
}
