//! The asking half of `membership_shared.py` (ENG-215), unit 2b of the
//! module's cut: `_ask_which`, `_retire_into`, `_asked_at`,
//! `_asked_here`, `_unseen` and `_asked_plan`, as methods of
//! [`Membership`]. `_retire_into` reads `retired_into` (unit 2a), so it
//! is bound on [`MembershipLog`]; the others need the store alone.
//!
//! The questions about another project are those of `_SUBJECT`'s kinds;
//! each is keyed by `_asks` (unit 1), whose AttributeError for an
//! evidence that is not a dict passes through.

use std::collections::{BTreeMap, BTreeSet};

use super::shared_heal::MembershipLog;
use super::{key_error, raise, Error, Membership, MembershipStore, Query, Result2};
use crate::capture::paths::norm;
use crate::datetime::{parse_event_ts, DateError};
use crate::membership_lines::{line_restated, Named, Raised};
use crate::membership_records::records::{Checkout, Proposal, DUPLICATE_CHECKOUT, OPEN};
use crate::membership_rows::proposal_from;
use crate::membership_shared::{asks, RowError, ASKED_HERE, SETTLED, SUBJECT};
use crate::pyvalue::{py_str, PyStr, PyValue};
use crate::unicode::cps;

use super::proposals::{Line, Opening};

/// `RETIRE_AFTER` (`membership_records.py` 144): `timedelta(days=180)`,
/// in microseconds.
const RETIRE_AFTER_US: i128 = 180 * 86_400 * 1_000_000;

/// `RETIRE_AFTER` in microseconds, for the tests that pin it.
pub fn retire_after_us() -> i128 {
    RETIRE_AFTER_US
}

fn row_error<E>(e: RowError) -> Error<E> {
    match e {
        RowError::Key(k) => match key_error::<(), E>(&cps(k)) {
            Err(e) => e,
            Ok(()) => unreachable!("key_error raises"),
        },
        RowError::Py(p) => Error::Raised(Raised {
            class: p.name(),
            message: String::new(),
        }),
        RowError::AttributeError => Error::Raised(Raised {
            class: "AttributeError",
            message: String::new(),
        }),
    }
}

fn asks_of<E>(pr: &Proposal) -> Result2<(PyStr, PyStr), E> {
    asks(pr).map_err(row_error)
}

fn subject(kind: &[u32]) -> bool {
    SUBJECT.iter().any(|(k, _)| cps(k) == kind)
}

/// `sorted(_SUBJECT)`, as the `column_in` of a read.
fn subject_kinds() -> Vec<PyValue> {
    let mut kinds: Vec<PyStr> = SUBJECT.iter().map(|(k, _)| cps(k)).collect();
    kinds.sort();
    kinds.into_iter().map(PyValue::Str).collect()
}

/// `SETTLED + to`.
fn settled(to: &[u32]) -> PyStr {
    let mut out = cps(SETTLED);
    out.extend_from_slice(to);
    out
}

/// `{**row, key: value}`: an existing key keeps its place.
fn set_key(row: &mut PyValue, key: &str, value: PyValue) {
    if let PyValue::Dict(items) = row {
        let k = PyValue::str(key);
        match items.iter_mut().find(|(x, _)| *x == k) {
            Some(item) => item.1 = value,
            None => items.push((k, value)),
        }
    }
}

impl<S: MembershipStore> Membership<S> {
    fn canon_set(&mut self, ids: &[PyStr]) -> Result2<BTreeSet<PyStr>, S::Error> {
        let mut out = BTreeSet::new();
        for c in ids {
            out.insert(self.canon(c)?);
        }
        Ok(out)
    }

    /// The open questions of `_SUBJECT`'s kinds, as rows.
    fn subject_rows(&self) -> Result2<Vec<PyValue>, S::Error> {
        self.read(
            "proposals",
            &Query {
                where_: Some(vec![(cps("state"), PyValue::str(OPEN))]),
                column_in: Some((cps("kind"), subject_kinds())),
                ..Query::default()
            },
        )
    }

    /// `_ask_which(self, later, first)` (545-564): the duplicate question
    /// on `later`, one per checkout.
    pub fn ask_which(&mut self, later: &Checkout, first: &Checkout) -> Result2<(), S::Error> {
        let theirs = self.canon(&first.project)?;
        let mine = self.canon(&later.project)?;
        let mut named = Vec::new();
        for x in [&theirs, &mine] {
            named.push(Named {
                name: Some(self.project(x)?.name),
                id: x.clone(),
            });
        }
        let kind = cps(DUPLICATE_CHECKOUT);
        let line = |q: &[u32]| line_restated(q, &kind, &kind, &named, false, None, None);
        let o = Opening {
            checkout: Some(later.id.clone()),
            kind: kind.clone(),
            candidates: vec![theirs.clone(), mine.clone()],
            evidence: vec![(PyValue::str("first"), PyValue::Str(first.id.clone()))],
            reason: kind.clone(),
            line: Line::Of(&line),
            distinct: None,
        };
        self.open_proposal_with(&o, None)?;
        Ok(())
    }

    /// `_asked_at(self, pr, idle, kept)` (638-665): None where `pr` is
    /// closed as settled; else the checkout it is asked at from then on.
    pub fn asked_at(
        &mut self,
        pr: &Proposal,
        idle: &Checkout,
        kept: &Checkout,
    ) -> Result2<Option<PyStr>, S::Error> {
        if !subject(&pr.kind) {
            return Ok(None);
        }
        let named = self.canon_set(&pr.candidates)?;
        if named.contains(&self.canon(&kept.project)?) {
            return Ok(Some(kept.id.clone()));
        }
        let mut ranked = Vec::new();
        for q in &named {
            for k in self.checkouts_of(q)? {
                if k.id != idle.id {
                    ranked.push(k);
                }
            }
        }
        ranked.sort_by(|a, b| {
            (a.machine != self.machine, &a.id).cmp(&(b.machine != self.machine, &b.id))
        });
        for k in &ranked {
            if !self.unseen(k)? {
                return Ok(Some(k.id.clone()));
            }
        }
        Ok(Some(idle.id.clone()))
    }

    /// `_unseen(self, k)` (710-729): no sighting shows the questions
    /// open on `k`.
    pub fn unseen(&mut self, k: &Checkout) -> Result2<bool, S::Error> {
        if k.retired {
            return Ok(true);
        }
        if k.machine == self.machine {
            return Ok(self.live_view(k).is_none());
        }
        let mut newest: Option<i128> = None;
        for v in &k.views {
            if let Some(us) = parse_event_ts(&v.last_seen).and_then(|t| t.instant_us()) {
                newest = Some(newest.map_or(us, |n| n.max(us)));
            }
        }
        let Some(newest) = newest else {
            return Ok(false);
        };
        let cutoff = match (self.now)().minus_micros(RETIRE_AFTER_US) {
            Ok(c) => c,
            Err(DateError::OverflowError) => {
                return raise("OverflowError", "date value out of range".into())
            }
            Err(DateError::ValueError) => return raise("ValueError", String::new()),
        };
        match cutoff.instant_us() {
            Some(c) => Ok(newest < c),
            None => raise(
                "TypeError",
                "can't compare offset-naive and offset-aware datetimes".into(),
            ),
        }
    }

    /// `_asked_here(self, k)` (668-707): the questions of `k`'s project
    /// open where no sighting shows them, moved to `k`.
    pub fn asked_here(&mut self, k: &Checkout) -> Result2<(), S::Error> {
        let pid = self.canon(&k.project)?;
        let rows = self.subject_rows()?;
        let mut asked: Option<BTreeSet<(PyStr, PyStr)>> = None;
        for r in &rows {
            let pr = proposal_from(r).map_err(Error::Raised)?;
            if pr.checkout.as_ref() == Some(&k.id)
                || !self.canon_set(&pr.candidates)?.contains(&pid)
            {
                continue;
            }
            let at = match pr.checkout.as_ref().filter(|c| !c.is_empty()) {
                None => None,
                Some(c) => match self.checkout(c) {
                    Ok(at) => Some(at),
                    Err(Error::Raised(Raised {
                        class: "KeyError", ..
                    })) => None,
                    Err(e) => return Err(e),
                },
            };
            if let Some(at) = &at {
                if !self.unseen(at)? {
                    continue;
                }
            }
            if asked.is_none() {
                let mut set = BTreeSet::new();
                for x in self.open_proposals(Some(&k.id))? {
                    set.insert(asks_of(&x)?);
                }
                asked = Some(set);
            }
            let key = asks_of(&pr)?;
            let set = asked.as_mut().expect("filled above");
            if set.contains(&key) {
                self.close(&pr, &settled(&k.id), &cps(ASKED_HERE), None)?;
            } else {
                self.put_proposal(&Proposal {
                    checkout: Some(k.id.clone()),
                    ..pr
                })?;
                set.insert(key);
            }
        }
        Ok(())
    }

    /// `_asked_plan(self, projects, k, settled)` (732-776): the checkouts
    /// whose views a sighting reads to place the questions about another
    /// project.
    ///
    /// The reference adds a heal's checkouts in the order of
    /// `_checkouts_by_project`'s dict, built from a set of project ids
    /// (an order that follows the string hash); here by project id.
    pub fn asked_plan(
        &mut self,
        projects: &[PyStr],
        k: Option<&Checkout>,
        settled_: Option<&Checkout>,
    ) -> Result2<Vec<Checkout>, S::Error> {
        let wanted = self.canon_set(projects)?;
        let rows = self.subject_rows()?;
        let mut held = BTreeSet::new();
        for r in &rows {
            let pr = proposal_from(r).map_err(Error::Raised)?;
            let Some(c) = pr.checkout.filter(|c| !c.is_empty()) else {
                continue;
            };
            if !self.canon_set(&pr.candidates)?.is_disjoint(&wanted) {
                held.insert(c);
            }
        }
        let mut out = if held.is_empty() {
            Vec::new()
        } else {
            let ids = held.into_iter().map(PyValue::Str).collect();
            self.checkouts_where(None, Some((cps("id"), ids)))?
        };
        if let (Some(k), Some(s)) = (k, settled_) {
            let mut named: BTreeSet<PyStr> = BTreeSet::new();
            for pr in self.open_proposals(Some(&s.id))? {
                let cs = self.canon_set(&pr.candidates)?;
                if subject(&pr.kind) && !cs.contains(&self.canon(&k.project)?) {
                    named.extend(cs);
                }
            }
            if !named.is_empty() {
                let ids: Vec<PyStr> = named.into_iter().collect();
                let machine = self.machine.clone();
                let got: BTreeMap<PyStr, Vec<Checkout>> =
                    self.checkouts_by_project(&ids, Some(&machine))?;
                out.extend(got.into_values().flatten());
            }
        }
        let skip: Vec<&PyStr> = [k, settled_].into_iter().flatten().map(|x| &x.id).collect();
        Ok(out
            .into_iter()
            .filter(|c| c.machine == self.machine && !c.retired && !skip.contains(&&c.id))
            .collect())
    }
}

impl<S: MembershipLog> Membership<S> {
    /// `_retire_into(self, idle, kept, by)` (567-635): `idle` folded into
    /// `kept` and retired; nothing for one retired into another already.
    pub fn retire_into(
        &mut self,
        idle: &Checkout,
        kept: &Checkout,
        by: &[u32],
    ) -> Result2<(), S::Error> {
        if self.retired_into(idle)?.is_some() {
            return Ok(());
        }
        let homes = self.read(
            "homes",
            &Query {
                where_: Some(vec![(cps("checkout"), PyValue::Str(idle.id.clone()))]),
                ..Query::default()
            },
        )?;
        for row in homes {
            let Some(rule) = row.get("rule") else {
                return key_error(&cps("rule"));
            };
            let mut rule = py_str(rule).map_err(|e| {
                Error::Raised(Raised {
                    class: e.name(),
                    message: String::new(),
                })
            })?;
            rule.push(u32::from(b'|'));
            rule.extend_from_slice(by);
            let mut row = row.clone();
            set_key(&mut row, "checkout", PyValue::Str(kept.id.clone()));
            set_key(&mut row, "rule", PyValue::Str(rule));
            self.put("home", &row)?;
        }
        let paths: BTreeSet<PyStr> = kept
            .views_on(&idle.machine)
            .iter()
            .map(|v| norm(&v.path))
            .collect();
        for v in idle.views_on(&idle.machine) {
            if paths.contains(&norm(&v.path)) {
                let key = PyValue::Dict(vec![
                    (PyValue::str("checkout"), PyValue::Str(idle.id.clone())),
                    (PyValue::str("machine"), PyValue::Str(v.machine.clone())),
                    (PyValue::str("path"), PyValue::Str(v.path.clone())),
                ]);
                self.delete("checkout_view", &key)?;
            }
        }
        let mut asked: BTreeMap<PyStr, BTreeSet<(PyStr, PyStr)>> = BTreeMap::new();
        for pr in self.open_proposals(Some(&idle.id))? {
            let place = self.asked_at(&pr, idle, kept)?;
            if place.as_ref() == Some(&idle.id) {
                continue;
            }
            if let Some(w) = &place {
                if !asked.contains_key(w) {
                    let mut set = BTreeSet::new();
                    for x in self.open_proposals(Some(w))? {
                        set.insert(asks_of(&x)?);
                    }
                    asked.insert(w.clone(), set);
                }
            }
            let Some(w) = place else {
                self.close(&pr, &settled(&kept.id), by, None)?;
                continue;
            };
            let key = asks_of(&pr)?;
            if asked[&w].contains(&key) {
                let to = if w.is_empty() { &kept.id } else { &w };
                self.close(&pr, &settled(to), by, None)?;
            } else {
                self.put_proposal(&Proposal {
                    checkout: Some(w.clone()),
                    ..pr
                })?;
                asked.get_mut(&w).expect("filled above").insert(key);
            }
        }
        self.put_checkout(Checkout {
            retired: true,
            ..idle.clone()
        })?;
        self.event(
            "checkout_retired",
            vec![
                ("checkout", PyValue::Str(idle.id.clone())),
                ("into", PyValue::Str(kept.id.clone())),
                ("evidence", PyValue::Str(by.to_vec())),
            ],
        )
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn retire_after_is_180_days() {
        assert_eq!(super::RETIRE_AFTER_US, 180 * 24 * 3600 * 1_000_000);
    }
}
