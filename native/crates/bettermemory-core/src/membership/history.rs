//! Unit membership-3 of `membership.py`: the history readings (`_holds`,
//! `_held`, `_rewritten`, `_most_specific`, `_specific`, `_independent`,
//! 1208-1315).
//!
//! `loc.history` is an argument: the location record carries no reader.
//! Where the reference asks `isinstance(history, IndependentReader)`, the
//! caller passes the same reader as `git` (or None). The maps answer in
//! key order: the reference's dicts are built in the order of sets, which
//! no caller reads unsorted.

use std::collections::{BTreeMap, BTreeSet};

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
        exclude: Option<&PyStr>,
        key_first: bool,
        pick: impl Fn(&PyValue) -> Result2<Option<PyStr>, S::Error>,
    ) -> Result2<BTreeMap<PyStr, BTreeSet<PyStr>>, S::Error> {
        let mut index: BTreeMap<PyStr, BTreeSet<PyStr>> = BTreeMap::new();
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
            if exclude == Some(&pid) {
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
        if !loc.born() || history.is_none() {
            return Ok(BTreeMap::new());
        }
        let index = self.witness_index(exclude, false, |row| {
            Ok(Some(lower(&str_of(column(row, "commit_id")?)?)))
        })?;
        let mut out: BTreeMap<PyStr, BTreeSet<PyStr>> = BTreeMap::new();
        if index.is_empty() {
            return Ok(out);
        }
        let keys: Vec<PyStr> = index.keys().cloned().collect();
        for sha in Self::holds(history, &keys) {
            for pid in index.get(&sha).into_iter().flatten() {
                out.entry(pid.clone()).or_default().insert(sha.clone());
            }
        }
        Ok(out)
    }

    /// `_rewritten(loc, exclude)` (1233-1263): {project: commits read from
    /// HEAD that carry one of its witness fingerprints}.
    pub fn rewritten(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        exclude: Option<&PyStr>,
    ) -> Result2<BTreeMap<PyStr, i64>, S::Error> {
        let Some(history) = history.filter(|_| loc.born()) else {
            return Ok(BTreeMap::new());
        };
        let index = self.witness_index(exclude, true, |row| {
            let fp = column(row, "fingerprint")?;
            Ok(if truthy(fp) { Some(str_of(fp)?) } else { None })
        })?;
        let mut out: BTreeMap<PyStr, i64> = BTreeMap::new();
        if index.is_empty() {
            return Ok(out);
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
            if let Some(fp) = history.fingerprint(&sha).filter(|f| !f.is_empty()) {
                for pid in index.get(&fp).into_iter().flatten() {
                    *out.entry(pid.clone()).or_default() += 1;
                }
            }
            todo.extend(history.parents(&sha));
        }
        Ok(out)
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
