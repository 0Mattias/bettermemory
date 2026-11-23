//! Unit membership-3 of `membership.py`: the history readings (`_holds`,
//! `_held`, `_rewritten`, `_most_specific`, `_specific`, `_independent`,
//! 1208-1315).
//!
//! `loc.history` is an argument: the location record carries no reader.
//! Where the reference asks `isinstance(history, IndependentReader)`, the
//! caller passes the same reader as `git` (or None). The established map APIs
//! retain key order. The tally uses the ordered reads because first discovery
//! and the source sets' iteration order can reach boundary messages.

use std::collections::{BTreeMap, BTreeSet, HashSet};

use super::{column, str_of, Membership, MembershipStore, Result2};
use crate::capture::location::Location;
use crate::membership_records::records::{
    bounded, HistoryReader, IndependentReader, NeedIndependent, Specific,
};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::lower;

/// `membership_records.WALK_LIMIT`: the commits `_rewritten` reads at most.
pub const WALK_LIMIT: usize = 512;

/// Python's truth of a column value.
fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(t) | PyValue::Path(t) => !t.is_empty(),
        PyValue::Tuple(x) | PyValue::List(x) => !x.is_empty(),
        PyValue::Dict(x) => !x.is_empty(),
        PyValue::DateTime(_) => true,
    }
}

/// The four history-object queries made by the tally. Providers that can raise
/// return the original store/host error before the next query is made.
pub trait TallyHistory<E> {
    fn holds(&self, commits: &[PyStr]) -> Result2<BTreeSet<PyStr>, E>;
    fn fingerprint(&self, commit: &[u32]) -> Result2<Option<PyStr>, E>;
    fn parents(&self, commit: &[u32]) -> Result2<Vec<PyStr>, E>;
    fn holds_objects(&self, ids: &[PyStr]) -> Result2<BTreeSet<PyStr>, E>;
}

/// Adapt an existing infallible native history provider.
pub struct NativeHistory<'a>(pub &'a dyn HistoryReader);
impl<E> TallyHistory<E> for NativeHistory<'_> {
    fn holds(&self, commits: &[PyStr]) -> Result2<BTreeSet<PyStr>, E> {
        Ok(self.0.holds(commits))
    }
    fn fingerprint(&self, commit: &[u32]) -> Result2<Option<PyStr>, E> {
        Ok(self.0.fingerprint(commit))
    }
    fn parents(&self, commit: &[u32]) -> Result2<Vec<PyStr>, E> {
        Ok(self.0.parents(commit))
    }
    fn holds_objects(&self, ids: &[PyStr]) -> Result2<BTreeSet<PyStr>, E> {
        Ok(self.0.holds_objects(ids))
    }
}

/// Observed iteration order at an input boundary that still has the Python sets.
/// Without it, the native set uses codepoint order. Keys only order evidence that
/// actually exists: stale, missing or repeated hints never change its membership.
#[derive(Clone, Debug, Default)]
pub struct HistoryOrder {
    pub commits: Vec<PyStr>,
    pub projects_by_commit: BTreeMap<PyStr, Vec<PyStr>>,
    pub projects_by_fingerprint: BTreeMap<PyStr, Vec<PyStr>>,
}

pub(crate) fn ordered_members<'a>(values: &'a BTreeSet<PyStr>, order: &[PyStr]) -> Vec<&'a PyStr> {
    if order.is_empty() {
        return values.iter().collect();
    }
    let mut seen = BTreeSet::new();
    let mut result = Vec::with_capacity(values.len());
    for v in order {
        if let Some(key) = values.get(v) {
            if seen.insert(key) {
                result.push(key);
            }
        }
    }
    result.extend(values.iter().filter(|v| !seen.contains(v)));
    result
}

// The tally consumes the first appearance of each project.
fn take_in_order<V>(mut map: BTreeMap<PyStr, V>, order: Vec<PyStr>) -> Vec<(PyStr, V)> {
    order
        .into_iter()
        .map(|key| {
            let value = map.remove(&key).expect("project was encountered");
            (key, value)
        })
        .collect()
}

impl<S: MembershipStore> Membership<S> {
    /// `_holds(loc, commits)` (1208-1212): the commits asked once each, in
    /// sorted order.
    pub fn holds(history: Option<&dyn HistoryReader>, commits: &[PyStr]) -> BTreeSet<PyStr> {
        match history {
            Some(h) if !commits.is_empty() => {
                let asked: BTreeSet<&PyStr> = commits.iter().collect();
                let asked: Vec<PyStr> = asked.into_iter().cloned().collect();
                h.holds(&asked)
            }
            _ => BTreeSet::new(),
        }
    }

    /// The witness rows indexed by the key `pick` reads off a row, each
    /// project canonical and not excluded. `_held` reads the project
    /// first and every row's commit; `_rewritten` reads the fingerprint
    /// first and skips a row without one.
    fn witness_index(
        &mut self,
        exclude: &[PyStr],
        key_first: bool,
        pick: impl Fn(&PyValue) -> Result2<Option<PyStr>, S::Error>,
    ) -> Result2<BTreeMap<PyStr, BTreeSet<PyStr>>, S::Error> {
        let mut index: BTreeMap<PyStr, BTreeSet<PyStr>> = BTreeMap::new();
        let excluded: HashSet<&[u32]> = exclude.iter().map(Vec::as_slice).collect();
        for row in self.witness_rows(None)? {
            let mut key = None;
            if key_first {
                key = pick(&row)?;
                if key.is_none() {
                    continue;
                }
            }
            let project = str_of(column(&row, "project")?)?;
            let pid = self.canon(&project)?;
            if excluded.contains(pid.as_slice()) {
                continue;
            }
            if !key_first {
                key = pick(&row)?;
            }
            if let Some(key) = key {
                index.entry(key).or_default().insert(pid);
            }
        }
        Ok(index)
    }

    /// `_held(loc, exclude)` (1213-1232): {project: its witness commits
    /// this repository holds}.
    pub fn held(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        exclude: Option<&PyStr>,
    ) -> Result2<BTreeMap<PyStr, BTreeSet<PyStr>>, S::Error> {
        self.held_excluding(loc, history, exclude.map_or(&[], std::slice::from_ref))
    }

    /// The same reading with an arbitrary collection excluded before any history query.
    pub fn held_excluding(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        exclude: &[PyStr],
    ) -> Result2<BTreeMap<PyStr, BTreeSet<PyStr>>, S::Error> {
        let reader = history.map(NativeHistory);
        Ok(self
            .held_in_order(
                loc,
                reader.as_ref().map(|h| h as &dyn TallyHistory<S::Error>),
                exclude,
                &HistoryOrder::default(),
            )?
            .into_iter()
            .collect())
    }

    /// The same evidence in order of first discovery, preserving observed set iteration.
    pub fn held_in_order(
        &mut self,
        loc: &Location,
        history: Option<&dyn TallyHistory<S::Error>>,
        exclude: &[PyStr],
        order: &HistoryOrder,
    ) -> Result2<Vec<(PyStr, BTreeSet<PyStr>)>, S::Error> {
        let Some(history) = history.filter(|_| loc.born()) else {
            return Ok(Vec::new());
        };
        let index = self.witness_index(exclude, false, |row| {
            Ok(Some(lower(&str_of(column(row, "commit_id")?)?)))
        })?;
        let mut projects = Vec::new();
        let mut out: BTreeMap<PyStr, BTreeSet<PyStr>> = BTreeMap::new();
        if index.is_empty() {
            return Ok(Vec::new());
        }
        let keys: Vec<PyStr> = index.keys().cloned().collect();
        let got = history.holds(&keys)?;
        for sha in ordered_members(&got, &order.commits) {
            if let Some(ids) = index.get(sha) {
                let observed = order
                    .projects_by_commit
                    .get(sha)
                    .map_or(&[][..], Vec::as_slice);
                for pid in ordered_members(ids, observed) {
                    out.entry(pid.clone())
                        .or_insert_with(|| {
                            projects.push(pid.clone());
                            BTreeSet::new()
                        })
                        .insert(sha.clone());
                }
            }
        }
        Ok(take_in_order(out, projects))
    }

    /// `_rewritten(loc, exclude)` (1233-1263): {project: commits read from
    /// HEAD that carry one of its witness fingerprints}.
    pub fn rewritten(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        exclude: Option<&PyStr>,
    ) -> Result2<BTreeMap<PyStr, i64>, S::Error> {
        self.rewritten_excluding(loc, history, exclude.map_or(&[], std::slice::from_ref))
    }

    /// The same reading with an arbitrary collection excluded before any history query.
    pub fn rewritten_excluding(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        exclude: &[PyStr],
    ) -> Result2<BTreeMap<PyStr, i64>, S::Error> {
        let reader = history.map(NativeHistory);
        Ok(self
            .rewritten_in_order(
                loc,
                reader.as_ref().map(|h| h as &dyn TallyHistory<S::Error>),
                exclude,
                &HistoryOrder::default(),
            )?
            .into_iter()
            .collect())
    }

    /// The same evidence in order of first discovery, preserving observed set iteration.
    pub fn rewritten_in_order(
        &mut self,
        loc: &Location,
        history: Option<&dyn TallyHistory<S::Error>>,
        exclude: &[PyStr],
        order: &HistoryOrder,
    ) -> Result2<Vec<(PyStr, i64)>, S::Error> {
        let Some(history) = history.filter(|_| loc.born()) else {
            return Ok(Vec::new());
        };
        let index = self.witness_index(exclude, true, |row| {
            let fp = column(row, "fingerprint")?;
            Ok(if truthy(fp) { Some(str_of(fp)?) } else { None })
        })?;
        let mut projects = Vec::new();
        let mut out: BTreeMap<PyStr, i64> = BTreeMap::new();
        if index.is_empty() {
            return Ok(Vec::new());
        }
        let mut seen: BTreeSet<PyStr> = BTreeSet::new();
        let mut todo: Vec<PyStr> = match &loc.head {
            Some(h) if !h.is_empty() => vec![h.clone()],
            _ => loc.ref_tips.clone(),
        };
        while seen.len() < WALK_LIMIT {
            let Some(sha) = todo.pop() else { break };
            if seen.contains(&sha) {
                continue;
            }
            seen.insert(sha.clone());
            if let Some(fp) = history.fingerprint(&sha)?.filter(|f| !f.is_empty()) {
                if let Some(ids) = index.get(&fp) {
                    let observed = order
                        .projects_by_fingerprint
                        .get(&fp)
                        .map_or(&[][..], Vec::as_slice);
                    for pid in ordered_members(ids, observed) {
                        *out.entry(pid.clone()).or_insert_with(|| {
                            projects.push(pid.clone());
                            0
                        }) += 1;
                    }
                }
            }
            todo.extend(history.parents(&sha)?);
        }
        Ok(take_in_order(out, projects))
    }

    /// `_most_specific(loc, held)` (1264-1268).
    pub fn most_specific(
        &self,
        history: Option<&dyn HistoryReader>,
        git: Option<&dyn IndependentReader>,
        held: &BTreeMap<PyStr, BTreeSet<PyStr>>,
    ) -> Result<Vec<PyStr>, NeedIndependent> {
        Ok(self.specific(history, git, held)?.best)
    }

    /// `_specific(loc, held)` (1269-1296): the most specific line of tied
    /// projects. `git` is `history` where it is an `IndependentReader`.
    pub fn specific(
        &self,
        history: Option<&dyn HistoryReader>,
        git: Option<&dyn IndependentReader>,
        held: &BTreeMap<PyStr, BTreeSet<PyStr>>,
    ) -> Result<Specific, NeedIndependent> {
        let shas: BTreeSet<PyStr> = held.values().flatten().cloned().collect();
        let shas: Vec<PyStr> = shas.into_iter().collect();
        let Some(history) = history else {
            return Ok(Specific {
                best: held.keys().cloned().collect(),
                held: shas.len() as i64,
                top: shas.into_iter().collect(),
                bounded: false,
            });
        };
        let lowered: BTreeSet<PyStr> = shas.iter().map(|s| lower(s)).collect();
        let lowered: Vec<PyStr> = lowered.into_iter().collect();
        let named = self.independent(git, &lowered)?;
        let (top, cut): (BTreeSet<PyStr>, bool) = match named {
            Some(named) => (
                shas.iter()
                    .filter(|s| named.contains(&lower(s)))
                    .cloned()
                    .collect(),
                false,
            ),
            None => {
                let (reach, said) = history.reachable_among(&shas);
                let top = shas
                    .iter()
                    .filter(|s| reach.get(&lower(s)).is_none_or(BTreeSet::is_empty))
                    .cloned()
                    .collect();
                (top, bounded(said))
            }
        };
        let best = held
            .iter()
            .filter(|(_, ws)| ws.iter().any(|w| top.contains(w)))
            .map(|(p, _)| p.clone())
            .collect();
        Ok(Specific {
            best,
            top,
            held: shas.len() as i64,
            bounded: cut,
        })
    }

    /// `_independent(history, shas)` (1297-1315): git's reading for this
    /// resolution, None for the traversal, or `NeedIndependent`.
    pub fn independent(
        &self,
        git: Option<&dyn IndependentReader>,
        shas: &[PyStr],
    ) -> Result<Option<BTreeSet<PyStr>>, NeedIndependent> {
        if shas.len() < 2 {
            return Ok(Some(shas.iter().cloned().collect()));
        }
        let Some(asked) = self.ancestry.as_ref().filter(|_| git.is_some()) else {
            return Ok(None);
        };
        if let Some(answer) = asked.get(shas) {
            return Ok(answer.clone());
        }
        if !asked.is_empty() {
            return Ok(None);
        }
        Err(NeedIndependent {
            commits: shas.to_vec(),
        })
    }
}
