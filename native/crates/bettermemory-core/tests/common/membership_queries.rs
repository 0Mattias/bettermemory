//! The steps of `tests/contract/gen_membership_queries.py` over the
//! recording store of `gen_membership_core` (`Fake`), given the store's
//! `declared_scopes` and a scripted root project file per view path.

use std::collections::HashMap;
use std::io;

use bettermemory_core::membership::queries::{HomeRow, ScopeReader};
use bettermemory_core::membership::Error;
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::caller::{Resolution, Val};

use super::membership_core::{raised, Fake, Model};
use super::membership_rows::{dec, enc, field, list, name};

impl ScopeReader for Fake {
    fn declared_scopes(&self, ids: &[PyStr]) -> Result<HashMap<PyStr, Vec<PyStr>>, PyStr> {
        let mut st = self.0.borrow_mut();
        let asked = PyValue::List(ids.iter().map(|i| PyValue::Str(i.clone())).collect());
        st.log
            .push(PyValue::List(vec![PyValue::str("scopes"), asked]));
        let n = st.calls.entry("scopes".to_string()).or_insert(0);
        *n += 1;
        let n = *n;
        if st
            .fail
            .as_ref()
            .is_some_and(|(m, at)| m == "scopes" && *at == n)
        {
            return Err(bettermemory_core::unicode::cps("injected"));
        }
        let mut out = HashMap::new();
        for r in st.tables.get("scoped").into_iter().flatten() {
            let (Some(PyValue::Str(id)), Some(PyValue::List(scopes))) =
                (r.get("id"), r.get("scopes"))
            else {
                continue;
            };
            if ids.contains(id) {
                let scopes = scopes
                    .iter()
                    .map(|s| bettermemory_core::pyvalue::py_str(s).expect("str"))
                    .collect();
                out.insert(id.clone(), scopes);
            }
        }
        Ok(out)
    }
}

/// The scripted root project files: a view's path to what it holds.
pub fn files_of(spelled: &PyValue) -> HashMap<PyStr, PyValue> {
    match spelled {
        PyValue::Dict(items) => items
            .iter()
            .map(|(k, v)| match k {
                PyValue::Str(k) => (k.clone(), v.clone()),
                other => panic!("not a path: {other:?}"),
            })
            .collect(),
        other => panic!("not files: {other:?}"),
    }
}

/// The closure the port reads a view's project file through.
pub fn reader(
    files: &HashMap<PyStr, PyValue>,
) -> impl Fn(&[u32]) -> Result<Option<PyStr>, io::Error> + '_ {
    move |view: &[u32]| match files.get(view) {
        None => Ok(None),
        Some(PyValue::Str(t)) => match name(&PyValue::Str(t.clone())).as_str() {
            "missing" | "error" => Ok(None),
            "valueerror" => Err(io::Error::new(io::ErrorKind::InvalidData, "malformed")),
            _ => Err(io::Error::other("os")),
        },
        Some(held) => match field(held, "id") {
            PyValue::Str(id) => Ok(Some(id.clone())),
            other => panic!("not an id: {other:?}"),
        },
    }
}

fn val(v: &Val) -> PyValue {
    match v {
        Val::Null => PyValue::None,
        Val::Text(t) => PyValue::Str(t.clone()),
        other => panic!("not text: {other:?}"),
    }
}

fn strs(v: &[PyStr]) -> PyValue {
    PyValue::List(v.iter().map(|s| PyValue::Str(s.clone())).collect())
}

/// `_home(h)`.
pub fn home_out(h: &HomeRow) -> PyValue {
    let mut named: Vec<PyStr> = h
        .home
        .named
        .iter()
        .map(|n| match n {
            Val::Text(s) => s.clone(),
            other => panic!("not a name: {other:?}"),
        })
        .collect();
    named.sort();
    let s = PyValue::str;
    PyValue::Dict(vec![
        (s("record_id"), PyValue::Str(h.record_id.clone())),
        (s("kind"), PyValue::Str(h.kind.clone())),
        (s("project"), val(&h.home.project)),
        (s("checkout"), h.checkout.clone()),
        (s("rule"), PyValue::Str(h.rule.clone())),
        (s("decided"), PyValue::Str(h.decided.clone())),
        (
            s("recorded_project"),
            PyValue::Str(h.recorded_project.clone()),
        ),
        (s("chain"), strs(&h.home.chain)),
        (s("tenant"), val(&h.home.tenant)),
        (s("unresolved"), PyValue::Bool(h.home.unresolved)),
        (s("named"), strs(&named)),
    ])
}

/// `{**_PROPOSAL, **dec(a)}` read as a proposal.
pub fn proposal(
    base: &PyValue,
    a: &PyValue,
) -> bettermemory_core::membership_records::records::Proposal {
    let (PyValue::Dict(b), PyValue::Dict(over)) = (&dec(base), &dec(a)) else {
        panic!("not a proposal")
    };
    let mut items = b.clone();
    let over = over.clone();
    for (k, v) in over {
        match items.iter_mut().find(|(x, _)| *x == k) {
            Some(slot) => slot.1 = v,
            None => items.push((k, v)),
        }
    }
    rows::proposal_from(&PyValue::Dict(items)).expect("a proposal")
}

fn ostr(v: Option<PyStr>) -> PyValue {
    v.map_or(PyValue::None, PyValue::Str)
}

fn arg(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a str: {other:?}"),
    }
}

/// `_answer(m, op, a)`, not yet spelled.
pub fn answer(
    m: &mut Model,
    op: &str,
    a: &PyValue,
    base: &PyValue,
    files: &HashMap<PyStr, PyValue>,
) -> Result<PyValue, Error<PyStr>> {
    let file = reader(files);
    let ids = |v: &PyValue| -> Vec<PyStr> {
        list(v)
            .iter()
            .map(|x| bettermemory_core::pyvalue::py_str(x).expect("str"))
            .collect()
    };
    Ok(match op {
        "home" => m
            .home(&arg(a).expect("an id"))?
            .map_or(PyValue::None, |h| home_out(&h)),
        "homes" => PyValue::List(
            m.homes(&ids(a))?
                .iter()
                .map(|(k, h)| PyValue::List(vec![PyValue::Str(k.clone()), home_out(h)]))
                .collect(),
        ),
        "admit" => {
            let caller = Resolution::from_value(&dec(field(a, "caller"))).expect("a caller");
            let adm = arg(field(a, "admission")).expect("an admission");
            let got = m.admit_recall(&caller, &ids(field(a, "hits")), &adm)?;
            let pairs = got
                .suppressed
                .iter()
                .map(|(r, w)| {
                    PyValue::Tuple(vec![PyValue::Str(r.clone()), PyValue::Str(w.clone())])
                })
                .collect();
            PyValue::Dict(vec![
                (PyValue::str("delivered"), strs(&got.delivered)),
                (PyValue::str("suppressed"), PyValue::List(pairs)),
            ])
        }
        "restated" => rows::proposal_row(&m.restated(&proposal(base, a), &file)?).expect("a row"),
        "afresh" => ostr(m.written_afresh(&proposal(base, a))?),
        "restatement" => {
            let pr = proposal(base, field(a, "pr"));
            let at = arg(field(a, "at"));
            PyValue::Str(m.restatement(&pr, at.as_deref(), &file)?)
        }
        "filed" => PyValue::Bool(m.filed_elsewhere(&proposal(base, a), &file)?),
        other => panic!("no op {other}"),
    })
}

/// One step: what it answered (spelled), the store calls and the rows.
pub fn step(
    m: &mut Model,
    st: &PyValue,
    base: &PyValue,
    files: &HashMap<PyStr, PyValue>,
) -> (PyValue, PyValue, PyValue) {
    let op = name(field(st, "op"));
    let a = st.get("a").cloned().unwrap_or(PyValue::None);
    let start = m.store().0.borrow().log.len();
    let o = match answer(m, &op, &a, base, files) {
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
    (o, log, rows)
}
