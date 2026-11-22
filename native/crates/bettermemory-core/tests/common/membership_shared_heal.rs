//! The spelling of `membership_shared_heal.jsonl`
//! (`tests/contract/gen_membership_shared_heal.py`): the recording store
//! of `membership_core` given a `membership_log` that answers the case's
//! rows, checkout rows spelled against the header's (`$k`), sightings
//! decoded, and each case run against the native model.

#![allow(dead_code)]

use std::collections::BTreeSet;

use bettermemory_core::capture::location::{Location, ProjectFileRef};
use bettermemory_core::capture::{Kind, Stamp, State};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::LogRow;
use bettermemory_core::membership::shared_heal::MembershipLog;
use bettermemory_core::membership::{
    Error, LivenessSeam, Membership, MembershipStore, Options, Query, Written,
};
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::{self, cps};

use super::common::membership_core::{raised, tables_of, Fake};
use super::common::membership_rows::{dec, enc, field, int, list, name};

/// `_LogStub`: the recording store and the case's log rows.
pub struct Heal {
    pub inner: Fake,
    pub log: Vec<LogRow>,
}

impl MembershipStore for Heal {
    type Error = PyStr;
    fn in_batch(&self) -> bool {
        self.inner.in_batch()
    }
    fn batch<T>(&self, f: &mut dyn FnMut() -> Result<T, Error<PyStr>>) -> Result<T, Error<PyStr>> {
        self.inner.batch(f)
    }
    fn put_membership(&self, k: &[u32], r: &PyValue, s: Option<&[u32]>) -> Result<Written, PyStr> {
        self.inner.put_membership(k, r, s)
    }
    fn delete_membership(
        &self,
        k: &[u32],
        r: &PyValue,
        s: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        self.inner.delete_membership(k, r, s)
    }
    fn record_membership(
        &self,
        k: &[u32],
        p: &PyValue,
        s: Option<&[u32]>,
    ) -> Result<Written, PyStr> {
        self.inner.record_membership(k, p, s)
    }
    fn membership_rows(&self, table: &str, q: &Query) -> Result<Vec<PyValue>, PyStr> {
        self.inner.membership_rows(table, q)
    }
    fn next_membership_id(&self, table: &str, prefix: &str) -> Result<PyStr, PyStr> {
        self.inner.next_membership_id(table, prefix)
    }
    fn next_membership_number(&self, table: &str, column: &str) -> Result<i64, PyStr> {
        self.inner.next_membership_number(table, column)
    }
}

impl MembershipLog for Heal {
    fn membership_log(
        &self,
        kinds: Option<&[&str]>,
        checkout: Option<&[u32]>,
    ) -> Result<Vec<LogRow>, PyStr> {
        let kinds = kinds.map_or(PyValue::None, |ks| {
            PyValue::List(ks.iter().map(|k| PyValue::str(k)).collect())
        });
        let checkout = checkout.map_or(PyValue::None, |c| PyValue::Str(c.to_vec()));
        self.inner.0.borrow_mut().log.push(PyValue::List(vec![
            PyValue::str("log"),
            kinds,
            checkout,
        ]));
        Ok(self.log.clone())
    }
}

pub type Model = Membership<Heal>;

/// `{"$k": [base, diff]}` written out as the `$d` it stands for.
pub fn expand(v: &PyValue, bases: &PyValue) -> PyValue {
    match v {
        PyValue::List(items) => PyValue::List(items.iter().map(|x| expand(x, bases)).collect()),
        PyValue::Dict(items) => {
            if let Some(k) = v.get("$k").filter(|_| items.len() == 1) {
                let k = list(k);
                let base = list(field(field(bases, &name(&k[0])), "$d"));
                let pairs = base
                    .iter()
                    .map(|p| {
                        let p = list(p);
                        let col = name(&p[0]);
                        let val = k[1].get(&col).unwrap_or(&p[1]).clone();
                        PyValue::List(vec![p[0].clone(), val])
                    })
                    .collect();
                return PyValue::Dict(vec![(PyValue::str("$d"), PyValue::List(pairs))]);
            }
            PyValue::Dict(
                items
                    .iter()
                    .map(|(a, b)| (a.clone(), expand(b, bases)))
                    .collect(),
            )
        }
        other => other.clone(),
    }
}

fn opt_text(v: Option<&PyValue>) -> Option<PyStr> {
    match v {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

/// `_loc(spec)`.
pub fn location(spec: &PyValue) -> Location {
    let stamps = spec
        .get("s")
        .map(|s| {
            list(s)
                .iter()
                .map(|x| {
                    let x = list(x);
                    Stamp {
                        name: cps(&name(&x[0])),
                        numbers: [1, 2, 3, 4].map(|i| int(&x[i]).to_string()),
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    let files = spec
        .get("files")
        .map(|f| {
            list(f)
                .iter()
                .map(|f| {
                    let f = list(f);
                    let dir = cps(&name(&f[1]));
                    let mut path = dir.clone();
                    path.extend(cps("/bettermemory.toml"));
                    ProjectFileRef {
                        id: cps(&name(&f[0])),
                        name: cps("n"),
                        dir,
                        path,
                        boundary: false,
                        object_key: None,
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    Location {
        machine: cps("M1"),
        cwd: cps("/w/r"),
        state: State::Repo,
        root: Some(cps("/w/r")),
        kind: Some(Kind::Primary),
        git_dir: None,
        object_key: None,
        common_object_key: None,
        root_object_key: None,
        head: None,
        branch: None,
        ref_tips: Vec::new(),
        remote_keys: BTreeSet::new(),
        alias_keys: BTreeSet::new(),
        shallow: false,
        token: opt_text(spec.get("t")),
        token_written: opt_text(spec.get("w")),
        token_status: cps("present"),
        stamps,
        ticks: match spec.get("x") {
            Some(PyValue::Bool(b)) => Some(*b),
            _ => None,
        },
        files,
        file_tracked: None,
        evicted: false,
        detail: Vec::new(),
        manifest: None,
        session: None,
    }
}

/// The model over the case's tables and log, as `_run` builds it.
pub fn model(case: &PyValue) -> Model {
    let tables = tables_of(case.get("tables").unwrap_or(&PyValue::Dict(Vec::new())));
    let log = case
        .get("log")
        .map(|l| {
            list(l)
                .iter()
                .enumerate()
                .map(|(i, r)| {
                    let r = list(r);
                    LogRow {
                        seq: i as i64 + 1,
                        ts: "t".into(),
                        session: None,
                        kind: name(&r[0]),
                        payload: unicode::to_string(match &r[1] {
                            PyValue::Str(s) => s,
                            other => panic!("a payload: {other:?}"),
                        })
                        .expect("a payload"),
                        prev_mac: String::new(),
                        mac: String::new(),
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    let dt = DateTime::from_isoformat(&cps("2026-03-04T05:06:07+00:00")).expect("a datetime");
    Membership::new(
        Heal {
            inner: Fake::new(tables, None, false),
            log,
        },
        cps("M1"),
        Options {
            liveness: LivenessSeam::Default {
                one: Box::new(|_: &Checkout, _: &View| cps("live")),
                many: Box::new(|views: &[(Checkout, View)]| vec![None; views.len()]),
            },
            token_writer: None,
            now: Box::new(move || dt),
            session: None,
        },
    )
}

fn checkout_arg(case: &PyValue, key: &str) -> Result<Checkout, Error<PyStr>> {
    rows::checkout_from(&dec(field(case, key)), &[]).map_err(Error::Raised)
}

fn strs(v: impl IntoIterator<Item = PyStr>) -> PyValue {
    PyValue::List(v.into_iter().map(PyValue::Str).collect())
}

/// What the model answers for an expanded case, before spelling.
pub fn answer(m: &mut Model, case: &PyValue) -> Result<PyValue, Error<PyStr>> {
    let f = name(field(case, "f"));
    Ok(match f.as_str() {
        "carriers" => {
            let tokens: Vec<PyStr> = list(field(case, "tokens"))
                .iter()
                .map(|t| cps(&name(t)))
                .collect();
            PyValue::List(
                m.carriers(&tokens)?
                    .iter()
                    .map(rows::checkout_row)
                    .collect(),
            )
        }
        "restamped" => {
            let k = checkout_arg(case, "k")?;
            let loc = location(field(case, "loc"));
            let how = cps(&name(field(case, "how")));
            rows::checkout_row(&m.restamped(k, &loc, &how)?)
        }
        "healed" | "chosen" => {
            let k = checkout_arg(case, "k")?;
            let later = checkout_arg(case, "later")?;
            let loc = location(field(case, "loc"));
            if f == "healed" {
                PyValue::str(m.healed(&k, &later, &loc)?)
            } else {
                PyValue::Bool(m.chosen(&k, &later, &loc)?)
            }
        }
        "retired_into" => {
            let k = checkout_arg(case, "k")?;
            m.retired_into(&k)?.map_or(PyValue::None, PyValue::Str)
        }
        "folded" => strs(m.folded(&cps(&name(field(case, "checkout"))))?),
        other => panic!("no function {other}"),
    })
}

/// The case's answer and calls, spelled as the file spells them.
pub fn run(case: &PyValue) -> (PyValue, PyValue) {
    let mut m = model(case);
    let o = match answer(&mut m, case) {
        Ok(v) => enc(&v),
        Err(e) => raised(&e),
    };
    let calls = enc(&PyValue::List(m.store().inner.0.borrow().log.clone()));
    (o, calls)
}
