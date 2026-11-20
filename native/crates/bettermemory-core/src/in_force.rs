//! What is in force at delivery: `in_force.py` whole (`InForce`,
//! `in_force`, and the walk's inner functions `newest`, `newest_head`,
//! `admitted_sources` and `head`).
//!
//! A record an admitted record `supersedes` is history and its head takes
//! its slot; the newest in-force record extending a record is named; a
//! record held out of force is never named as an extender. The store's
//! reads (`Store.inbound_links`, `standing.held_ids`) and the admission
//! (`RecordView.prime`, `admits_id`) are arguments, as traits, and are
//! called in the reference's order with the reference's arguments.
//!
//! Ids and stamps are Rust strings: the reference compares them as Python
//! `str`, by code point, which is the byte order of their UTF-8. A lone
//! surrogate in an id is not representable here; the store's ids are
//! ASCII.

use std::collections::{HashMap, HashSet};

/// `models.LinkType.SUPERSEDES.value`.
pub const SUPERSEDES: &str = "supersedes";
/// `models.LinkType.EXTENDS.value`.
pub const EXTENDS: &str = "extends";
/// `_WALKED`: the link types `inbound_links` is asked for.
pub const WALKED: [&str; 2] = [SUPERSEDES, EXTENDS];

/// One inbound link as `Store.inbound_links` answers it:
/// `(type, source id, source updated)`.
pub type Inbound = (String, String, String);

/// The store's reads `in_force` makes.
pub trait LinkReader {
    /// `Store.inbound_links(targets, types)`: per target, its inbound
    /// links of `types`, in the store's order. A target with none may be
    /// missing.
    fn inbound_links(
        &mut self,
        targets: &[String],
        types: &[&str],
    ) -> HashMap<String, Vec<Inbound>>;
    /// `standing.held_ids(store, ids)`: the held ones of `ids`.
    fn held_ids(&mut self, ids: &[String]) -> HashSet<String>;
}

/// `admission.RecordView` as `in_force` reads it.
pub trait RecordView {
    fn prime(&mut self, ids: &[String]);
    fn admits_id(&mut self, id: &str) -> bool;
}

/// `InForce`: one asked id as a surface delivers it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InForce {
    pub asked: String,
    pub id: String,
    pub replaces: Option<String>,
    pub extended_by: Option<String>,
    pub held: bool,
}

impl InForce {
    /// `InForce.history`.
    pub fn history(&self) -> bool {
        self.replaces.is_some()
    }
}

/// `dict.fromkeys(items)`: first occurrences, in order.
fn first_seen<'a>(items: impl IntoIterator<Item = &'a String>) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for item in items {
        if seen.insert(item.as_str()) {
            out.push(item.clone());
        }
    }
    out
}

struct Walk<'v, V: RecordView> {
    view: &'v mut V,
    inbound: HashMap<String, Vec<Inbound>>,
    updated: HashMap<String, String>,
    held: HashSet<String>,
}

impl<V: RecordView> Walk<'_, V> {
    /// `newest`: the greatest of `candidates` by `(updated, id)`, an id
    /// with no stamp read as the empty one.
    fn newest<'c>(&self, candidates: impl IntoIterator<Item = &'c String>) -> Option<String> {
        candidates
            .into_iter()
            .map(|rid| {
                let stamp = self.updated.get(rid).map_or("", String::as_str);
                (stamp, rid)
            })
            .max()
            .map(|(_, rid)| rid.clone())
    }

    /// `newest_head`: `newest(pool - held) or newest(pool)`. The `or` is
    /// Python's: an empty id is falsy, so it falls through to the newest
    /// of the whole pool.
    fn newest_head(&self, pool: &[String]) -> Option<String> {
        let free = self.newest(pool.iter().filter(|r| !self.held.contains(*r)));
        match free {
            Some(ref rid) if !rid.is_empty() => free,
            _ => self.newest(pool.iter()),
        }
    }

    /// `admitted_sources`: the sources of `kind` into `record_id`, in the
    /// store's order, `admits_id` asked only of those of that kind.
    fn admitted_sources(&mut self, record_id: &str, kind: &str) -> Vec<String> {
        let rows = self.inbound.get(record_id).cloned().unwrap_or_default();
        let mut out = Vec::new();
        for (k, source, _) in rows {
            if k == kind && self.view.admits_id(&source) {
                out.push(source);
            }
        }
        out
    }

    /// `head`: the supersession chain followed to its end; a cycle ends it
    /// at the record where it closes.
    fn head(&mut self, record_id: &str) -> String {
        let mut seen: HashSet<String> = HashSet::from([record_id.to_owned()]);
        let mut current = record_id.to_owned();
        loop {
            let pool = self.admitted_sources(&current, SUPERSEDES);
            match self.newest_head(&pool) {
                Some(next) if !seen.contains(&next) => {
                    seen.insert(next.clone());
                    current = next;
                }
                _ => return current,
            }
        }
    }
}

/// `in_force(store, ids, admission)`: an `InForce` for each asked id, in
/// the order of first occurrence (the reference's dict, keyed by `asked`).
pub fn in_force<S: LinkReader, V: RecordView>(
    store: &mut S,
    ids: &[String],
    view: &mut V,
) -> Vec<InForce> {
    let mut inbound: HashMap<String, Vec<Inbound>> = HashMap::new();
    let mut updated: HashMap<String, String> = HashMap::new();
    let mut updated_order: Vec<String> = Vec::new();
    let mut frontier = first_seen(ids);
    while !frontier.is_empty() {
        let mut found = store.inbound_links(&frontier, &WALKED);
        let mut sources: Vec<String> = Vec::new();
        for target in &frontier {
            let rows = found.remove(target).unwrap_or_default();
            for (_, source, stamp) in &rows {
                if updated.insert(source.clone(), stamp.clone()).is_none() {
                    updated_order.push(source.clone());
                }
                sources.push(source.clone());
            }
            inbound.insert(target.clone(), rows);
        }
        view.prime(&sources);
        let mut next = Vec::new();
        for source in first_seen(&sources) {
            if !inbound.contains_key(&source) && view.admits_id(&source) {
                next.push(source);
            }
        }
        frontier = next;
    }

    let held = if ids.is_empty() {
        HashSet::new()
    } else {
        let asked: Vec<String> = ids.iter().chain(&updated_order).cloned().collect();
        store.held_ids(&asked)
    };

    let mut walk = Walk {
        view,
        inbound,
        updated,
        held,
    };
    let mut out = Vec::new();
    for asked in first_seen(ids) {
        let target = walk.head(&asked);
        let mut extenders: HashSet<String> = HashSet::new();
        for source in walk.admitted_sources(&target, EXTENDS) {
            let h = walk.head(&source);
            if h != target {
                extenders.insert(h);
            }
        }
        let extended_by = walk.newest(extenders.iter().filter(|r| !walk.held.contains(*r)));
        out.push(InForce {
            replaces: (target != asked).then(|| asked.clone()),
            held: walk.held.contains(&target),
            asked,
            id: target,
            extended_by,
        });
    }
    out
}
