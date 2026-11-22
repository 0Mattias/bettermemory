//! The spelling of `membership_shared_ask.jsonl`
//! (`tests/contract/gen_membership_shared_ask.py`): the recording store
//! of `membership_shared_heal` over the case's tables (the header's
//! `world` with the case's `x` in place, or its own `tables`), a liveness
//! answering `live` for the case's paths, the case's clock, and each case
//! run against the native model.

#![allow(dead_code)]

use bettermemory_core::datetime::DateTime;
use bettermemory_core::membership::{Error, LivenessSeam, Membership, Options};
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

use super::common::membership_core::{raised, tables_of, Fake};
use super::common::membership_rows::{enc, field, int, list, name};
use super::heal::{self, Heal};

pub type Model = Membership<Heal>;

/// The case's tables, spelled: its own, or the world with `x` in place.
pub fn tables(case: &PyValue, world: &PyValue) -> PyValue {
    if let Some(t) = case.get("tables") {
        return t.clone();
    }
    let x = field(case, "x");
    let mut items = match world {
        PyValue::Dict(items) => items.clone(),
        _ => panic!("a world"),
    };
    if let PyValue::Dict(extra) = x {
        for (k, v) in extra {
            match items.iter_mut().find(|(a, _)| a == k) {
                Some(item) => item.1 = v.clone(),
                None => items.push((k.clone(), v.clone())),
            }
        }
    }
    PyValue::Dict(items)
}

/// `_clock(spec)`.
pub fn clock(spec: Option<&PyValue>) -> DateTime {
    let (text, minutes) = match spec {
        Some(s) => {
            let s = list(s);
            let m = match &s[1] {
                PyValue::None => None,
                v => Some(int(v)),
            };
            (name(&s[0]), m)
        }
        None => ("2026-03-04T05:06:07".to_string(), Some(0)),
    };
    let mut t = text;
    if let Some(m) = minutes {
        let sign = if m < 0 { '-' } else { '+' };
        let m = m.abs();
        t.push_str(&format!("{sign}{:02}:{:02}", m / 60, m % 60));
    }
    DateTime::from_isoformat(&cps(&t)).expect("a datetime")
}

/// The model over the case, as `_run` builds it.
pub fn model(case: &PyValue, world: &PyValue) -> Model {
    let spelled = tables(case, world);
    let mut with_log = PyValue::Dict(vec![(PyValue::str("tables"), spelled.clone())]);
    if let (PyValue::Dict(items), Some(l)) = (&mut with_log, case.get("log")) {
        items.push((PyValue::str("log"), l.clone()));
    }
    let log = heal::model(&with_log).store().log.clone();
    let live: Vec<PyStr> = case
        .get("live")
        .map(|l| list(l).iter().map(|p| cps(&name(p))).collect())
        .unwrap_or_default();
    let dt = clock(case.get("clock"));
    Membership::new(
        Heal {
            inner: Fake::new(tables_of(&spelled), None, false),
            log,
        },
        cps("M1"),
        Options {
            liveness: LivenessSeam::Given(Box::new(move |_: &Checkout, v: &View| {
                cps(if live.contains(&v.path) {
                    "live"
                } else {
                    "gone"
                })
            })),
            token_writer: None,
            now: Box::new(move || dt),
            session: None,
        },
    )
}

fn arg(m: &Model, case: &PyValue, key: &str) -> Option<Checkout> {
    match case.get(key) {
        None | Some(PyValue::None) => None,
        Some(id) => Some(m.checkout(&cps(&name(id))).expect("an argument")),
    }
}

fn ids(cs: &[Checkout]) -> PyValue {
    PyValue::List(cs.iter().map(|c| PyValue::Str(c.id.clone())).collect())
}

/// What the model answers for a case, its arguments read first.
pub fn answer(m: &mut Model, case: &PyValue) -> Result<PyValue, Error<PyStr>> {
    let k = arg(m, case, "k");
    let first = arg(m, case, "first");
    let idle = arg(m, case, "idle");
    let kept = arg(m, case, "kept");
    let settled = arg(m, case, "settled");
    let pr = case
        .get("pr")
        .map(|q| m.proposal(&cps(&name(q))).expect("a question"));
    m.store().inner.0.borrow_mut().log.clear();
    let f = name(field(case, "f"));
    let opt = |v: Option<PyStr>| v.map_or(PyValue::None, PyValue::Str);
    Ok(match f.as_str() {
        "ask_which" => {
            m.ask_which(k.as_ref().expect("k"), first.as_ref().expect("first"))?;
            PyValue::None
        }
        "retire_into" => {
            let by = cps(&name(field(case, "by")));
            m.retire_into(
                idle.as_ref().expect("idle"),
                kept.as_ref().expect("kept"),
                &by,
            )?;
            PyValue::None
        }
        "asked_at" => opt(m.asked_at(
            pr.as_ref().expect("pr"),
            idle.as_ref().expect("idle"),
            kept.as_ref().expect("kept"),
        )?),
        "asked_here" => {
            m.asked_here(k.as_ref().expect("k"))?;
            PyValue::None
        }
        "unseen" => PyValue::Bool(m.unseen(k.as_ref().expect("k"))?),
        "asked_plan" => {
            let projects: Vec<PyStr> = list(field(case, "projects"))
                .iter()
                .map(|p| cps(&name(p)))
                .collect();
            ids(&m.asked_plan(&projects, k.as_ref(), settled.as_ref())?)
        }
        other => panic!("no function {other}"),
    })
}

/// The case's answer and calls, spelled as the file spells them.
pub fn run(case: &PyValue, world: &PyValue) -> (PyValue, PyValue) {
    let mut m = model(case, world);
    let o = match answer(&mut m, case) {
        Ok(v) => enc(&v),
        Err(e) => raised(&e),
    };
    let calls = enc(&PyValue::List(m.store().inner.0.borrow().log.clone()));
    (o, calls)
}
