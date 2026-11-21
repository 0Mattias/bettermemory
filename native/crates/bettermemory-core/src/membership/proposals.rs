//! The proposals of the model (`membership.py` 1349-1487): the open
//! ones read, one read by id, one put, opened (one per checkout and
//! question), answered and restated.

use std::collections::HashMap;

use super::{key_error, Error, Membership, MembershipStore, Query, Result2};
use crate::location_witness::py_eq;
use crate::membership_records::records::{Proposal, ANSWERED, OPEN};
use crate::membership_rows::{jsonable, proposal_from, proposal_row};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// `line: str | Callable[[str], str]`: the question's line, or the
/// function of the question's id that builds it.
pub enum Line<'a> {
    Text(PyStr),
    Of(&'a dyn Fn(&[u32]) -> PyStr),
}

/// The keywords of `open_proposal` and `_open_proposal`. `evidence` is the
/// mapping's items, in its order.
pub struct Opening<'a> {
    pub checkout: Option<PyStr>,
    pub kind: PyStr,
    pub candidates: Vec<PyStr>,
    pub evidence: Vec<(PyValue, PyValue)>,
    pub reason: PyStr,
    pub line: Line<'a>,
    pub distinct: Option<PyStr>,
}

fn opt(v: &Option<PyStr>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |t| PyValue::Str(t.clone()))
}

/// `mapping.get(key)`: the value of the last item whose key is the str.
fn get<'v>(items: &'v [(PyValue, PyValue)], key: &[u32]) -> Option<&'v PyValue> {
    items
        .iter()
        .rev()
        .find(|(k, _)| matches!(k, PyValue::Str(t) if t == key))
        .map(|(_, v)| v)
}

fn opt_eq(a: Option<&PyValue>, b: Option<&PyValue>) -> bool {
    py_eq(a.unwrap_or(&PyValue::None), b.unwrap_or(&PyValue::None))
}

/// Python's `==` on two evidence values, dict keys looked up by hash at
/// every depth: a str key equals only a str key, so the str keys of each
/// dict are indexed once (the last of equal keys kept) and only the
/// other keys are searched in turn. A nested dict of n str keys then
/// costs O(n), as in the reference. Lists and tuples go item by item,
/// any other pair through `py_eq`.
fn evidence_eq(a: &PyValue, b: &PyValue) -> bool {
    let mut work = vec![(a, b)];
    while let Some((a, b)) = work.pop() {
        match (a, b) {
            (PyValue::List(x), PyValue::List(y)) | (PyValue::Tuple(x), PyValue::Tuple(y)) => {
                if x.len() != y.len() {
                    return false;
                }
                work.extend(x.iter().zip(y.iter()));
            }
            (PyValue::Dict(x), PyValue::Dict(y)) => {
                if x.len() != y.len() {
                    return false;
                }
                let mut index: HashMap<&[u32], &PyValue> = HashMap::new();
                let mut others: Vec<&(PyValue, PyValue)> = Vec::new();
                for item in y {
                    match &item.0 {
                        PyValue::Str(t) => {
                            index.insert(t.as_slice(), &item.1);
                        }
                        _ => others.push(item),
                    }
                }
                for (k, v) in x {
                    let found = match k {
                        PyValue::Str(t) => index.get(t.as_slice()).copied(),
                        _ => others
                            .iter()
                            .rev()
                            .find(|(ky, _)| py_eq(k, ky))
                            .map(|(_, w)| w),
                    };
                    match found {
                        Some(w) => work.push((v, w)),
                        None => return false,
                    }
                }
            }
            _ => {
                if !py_eq(a, b) {
                    return false;
                }
            }
        }
    }
    true
}

/// The dataclass's `==`: field by field, the evidence and the signature
/// by Python's `==`.
pub fn proposal_eq(a: &Proposal, b: &Proposal) -> bool {
    let sig = |p: &Proposal| p.signature.clone().map_or(PyValue::None, PyValue::Tuple);
    fields_eq(a, b) && py_eq(&sig(a), &sig(b))
}

/// Every field of the dataclass's `==` but the signature.
fn fields_eq(a: &Proposal, b: &Proposal) -> bool {
    a.id == b.id
        && a.checkout == b.checkout
        && a.kind == b.kind
        && a.candidates == b.candidates
        && evidence_eq(&a.evidence, &b.evidence)
        && a.reason == b.reason
        && a.line == b.line
        && a.state == b.state
        && a.answer == b.answer
        && a.answered_by == b.answered_by
        && a.opened == b.opened
}

impl<S: MembershipStore> Membership<S> {
    /// `_open_proposals(checkout_id)` (1349-1357).
    pub fn open_proposals(&self, checkout_id: Option<&[u32]>) -> Result2<Vec<Proposal>, S::Error> {
        let mut where_ = vec![(cps("state"), PyValue::str(OPEN))];
        if let Some(k) = checkout_id {
            where_.push((cps("checkout"), PyValue::Str(k.to_vec())));
        }
        let rows = self.read(
            "proposals",
            &Query {
                where_: Some(where_),
                ..Query::default()
            },
        )?;
        rows.iter()
            .map(|r| proposal_from(r).map_err(Error::Raised))
            .collect()
    }

    /// `proposal(proposal_id)` (1358-1365): KeyError with the id's repr.
    pub fn proposal(&self, proposal_id: &[u32]) -> Result2<Proposal, S::Error> {
        let rows = self.read(
            "proposals",
            &Query {
                where_: Some(vec![(cps("id"), PyValue::Str(proposal_id.to_vec()))]),
                ..Query::default()
            },
        )?;
        match rows.first() {
            None => key_error(proposal_id),
            Some(r) => proposal_from(r).map_err(Error::Raised),
        }
    }

    /// `_put_proposal(pr)` (1371-1373).
    pub fn put_proposal(&mut self, pr: &Proposal) -> Result2<(), S::Error> {
        let row = proposal_row(pr).map_err(Error::Raised)?;
        self.put("proposal", &row)
    }

    /// `_open_proposal(...)` (1374-1425): an open one of the kind on the
    /// checkout (with `distinct`, whose evidence holds an equal value
    /// there) is returned as it is; else the id is minted, then the
    /// evidence made plain, the line built, the stamp taken, the row put
    /// and `proposal_opened` recorded.
    pub fn open_proposal_with(
        &mut self,
        o: &Opening<'_>,
        signature: Option<Vec<PyValue>>,
    ) -> Result2<Proposal, S::Error> {
        if let Some(k) = &o.checkout {
            for existing in self.open_proposals(Some(k))? {
                if existing.kind == o.kind
                    && o.distinct.as_ref().is_none_or(|d| {
                        let had = match &existing.evidence {
                            PyValue::Dict(items) => get(items, d),
                            _ => None,
                        };
                        opt_eq(had, get(&o.evidence, d))
                    })
                {
                    return Ok(existing);
                }
            }
        }
        let qid = self
            .store
            .next_membership_id("proposals", "Q")
            .map_err(Error::Store)?;
        let evidence = jsonable(&PyValue::Dict(o.evidence.clone())).map_err(Error::Raised)?;
        let line = match &o.line {
            Line::Text(t) => t.clone(),
            Line::Of(f) => f(&qid),
        };
        let opened = self.stamp()?;
        let pr = Proposal {
            id: qid,
            checkout: o.checkout.clone(),
            kind: o.kind.clone(),
            candidates: o.candidates.clone(),
            evidence,
            reason: o.reason.clone(),
            line,
            state: cps(OPEN),
            answer: None,
            answered_by: None,
            signature,
            opened,
        };
        self.put_proposal(&pr)?;
        let candidates = o.candidates.iter().cloned().map(PyValue::Str).collect();
        self.event(
            "proposal_opened",
            vec![
                ("proposal", PyValue::Str(pr.id.clone())),
                ("checkout", opt(&o.checkout)),
                ("kind", PyValue::Str(o.kind.clone())),
                ("candidates", PyValue::List(candidates)),
                ("reason", PyValue::Str(o.reason.clone())),
                ("evidence", pr.evidence.clone()),
            ],
        )?;
        Ok(pr)
    }

    /// `_close(pr, answer, by, said=)` (1426-1444): nothing for a
    /// proposal not open as given; else the stored row answered and
    /// `proposal_answered` naming the given one's checkout.
    pub fn close(
        &mut self,
        pr: &Proposal,
        answer: &[u32],
        by: &[u32],
        said: Option<&[u32]>,
    ) -> Result2<(), S::Error> {
        if pr.state != cps(OPEN) {
            return Ok(());
        }
        let mut current = self.proposal(&pr.id)?;
        current.state = cps(ANSWERED);
        current.answer = Some(answer.to_vec());
        current.answered_by = Some(by.to_vec());
        self.put_proposal(&current)?;
        let mut payload = vec![
            ("proposal", PyValue::Str(pr.id.clone())),
            ("checkout", opt(&pr.checkout)),
            ("answer", PyValue::Str(answer.to_vec())),
            ("by", PyValue::Str(by.to_vec())),
        ];
        if let Some(s) = said {
            payload.push(("said", PyValue::Str(s.to_vec())));
        }
        self.event("proposal_answered", payload)
    }

    /// `open_proposal(...)` (1445-1472): `_open_proposal` in one call,
    /// with no signature.
    pub fn open_proposal(&mut self, o: &Opening<'_>) -> Result2<Proposal, S::Error> {
        self.call(|m| m.open_proposal_with(o, None))
    }

    /// `restate_proposal(proposal_id, line=, evidence=)` (1473-1487): an
    /// answered one as it is; an open one with the line and the plain
    /// evidence, put only when it differs by the dataclass's `==` (the
    /// signature, the same object on both sides, always equal).
    pub fn restate_proposal(
        &mut self,
        proposal_id: &[u32],
        line: &[u32],
        evidence: &[(PyValue, PyValue)],
    ) -> Result2<Proposal, S::Error> {
        self.call(|m| {
            let pr = m.proposal(proposal_id)?;
            if pr.state != cps(OPEN) {
                return Ok(pr);
            }
            let mut restated = pr.clone();
            restated.line = line.to_vec();
            restated.evidence =
                jsonable(&PyValue::Dict(evidence.to_vec())).map_err(Error::Raised)?;
            // `dataclasses.replace` hands the restated proposal the same
            // signature tuple, and a tuple equals itself by identity even
            // when it holds a NaN, so the signature never differs here.
            if !fields_eq(&restated, &pr) {
                m.put_proposal(&restated)?;
            }
            Ok(restated)
        })
    }
}
