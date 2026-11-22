//! The steps of `tests/contract/gen_membership_touch.py` driven over the
//! native `Membership` and the recording store of `membership_core`: a
//! liveness of one state per view path, a token writer of one write per
//! checkout id, the generator's fixed clock and its location specs.

use std::collections::BTreeMap;

use bettermemory_core::capture::location::Location;
use bettermemory_core::capture::State;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::membership::touch::foreign;
use bettermemory_core::membership::{Error, LivenessSeam, Membership, Options, TokenWriter};
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

use super::membership_core::{checkout_out, raised, Fake, Model};
use super::membership_rows::{enc, field, int, list, name};

fn text(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a str or null: {other:?}"),
    }
}

fn a_str(v: &PyValue) -> PyStr {
    text(v).expect("a str")
}

/// The states by path (`live`).
pub fn states(spec: &PyValue) -> BTreeMap<PyStr, PyStr> {
    match spec {
        PyValue::Dict(items) => items.iter().map(|(k, v)| (a_str(k), a_str(v))).collect(),
        _ => BTreeMap::new(),
    }
}

/// The writes by checkout id (`writer`), None for no writer.
pub fn writer(spec: &PyValue) -> Option<TokenWriter> {
    let PyValue::Dict(items) = spec else {
        return None;
    };
    let writes: BTreeMap<PyStr, Option<(Option<PyStr>, PyStr)>> = items
        .iter()
        .map(|(k, v)| {
            let w = match v {
                PyValue::None => None,
                v => {
                    let v = list(v);
                    Some((text(&v[0]), a_str(&v[1])))
                }
            };
            (a_str(k), w)
        })
        .collect();
    Some(Box::new(move |k: &Checkout| {
        writes.get(&k.id).cloned().flatten()
    }))
}

/// The clock of the header (`now`).
pub fn clock(spec: &PyValue) -> DateTime {
    let now = list(spec);
    let mut dt = DateTime::from_isoformat(&cps(&name(&now[0]))).expect("a datetime");
    dt.offset_us = Some(int(&now[1]) as i64 * 60_000_000);
    dt
}

/// The model of one sequence.
pub fn model(
    tables: BTreeMap<String, Vec<PyValue>>,
    fail: Option<(String, usize)>,
    live: BTreeMap<PyStr, PyStr>,
    token_writer: Option<TokenWriter>,
    now: DateTime,
) -> Model {
    Membership::new(
        Fake::new(tables, fail, false),
        cps("M1"),
        Options {
            liveness: LivenessSeam::Given(Box::new(move |_: &Checkout, v: &View| {
                live.get(&v.path).cloned().unwrap_or_else(|| cps("live"))
            })),
            token_writer,
            now: Box::new(move || now),
            session: Some(cps("S1")),
        },
    )
}

/// `_loc(spec)`.
pub fn location(spec: &PyValue) -> Location {
    let t = |k: &str| spec.get(k).and_then(text);
    Location {
        machine: t("machine").unwrap_or_else(|| cps("M1")),
        cwd: t("cwd").unwrap_or_else(|| cps("/w")),
        state: State::Repo,
        root: t("root"),
        kind: None,
        git_dir: None,
        object_key: None,
        common_object_key: None,
        root_object_key: None,
        head: None,
        branch: None,
        ref_tips: Vec::new(),
        remote_keys: Default::default(),
        alias_keys: Default::default(),
        shallow: false,
        token: None,
        token_written: None,
        token_status: cps("n/a"),
        stamps: Vec::new(),
        ticks: None,
        files: Vec::new(),
        file_tracked: None,
        evicted: false,
        detail: Vec::new(),
        manifest: None,
        session: None,
    }
}

/// `_answer(m, op, a)`.
pub fn answer(m: &mut Model, op: &str, a: &PyValue) -> Result<PyValue, Error<PyStr>> {
    Ok(match op {
        "touch" => {
            let a = list(a);
            let k = m.checkout(&a_str(&a[0]))?;
            checkout_out(&m.touch_view(k, &location(&a[1]))?)
        }
        "name" => PyValue::Str(m.checkout_name(&m.checkout(&a_str(a))?)),
        "give" => {
            let k = m.checkout(&a_str(a))?;
            m.give_token(&k)?;
            PyValue::None
        }
        "written" => {
            let a = list(a);
            m.token_written(&a_str(&a[0]), text(&a[1]).as_deref(), &a_str(&a[2]))?;
            PyValue::None
        }
        "foreign" => {
            let a = list(a);
            let k = m.checkout(&a_str(&a[0]))?;
            PyValue::Bool(foreign(&k, text(&a[1]).as_deref(), &a_str(&a[2]))?)
        }
        other => panic!("op {other}"),
    })
}

/// One step: `(o, log, rows, remint)`.
pub fn step(m: &mut Model, st: &PyValue) -> [PyValue; 4] {
    let op = name(field(st, "op"));
    let start = m.store().0.borrow().log.len();
    let o = match answer(m, &op, field(st, "a")) {
        Ok(v) => enc(&v),
        Err(e) => raised(&e),
    };
    let log = enc(&PyValue::List(m.store().0.borrow().log[start..].to_vec()));
    let rows = PyValue::List(
        m.rows()
            .iter()
            .map(|&n| PyValue::int(i128::from(n)))
            .collect(),
    );
    let remint = m.remint().cloned().map_or(PyValue::None, PyValue::Str);
    [o, log, rows, remint]
}
