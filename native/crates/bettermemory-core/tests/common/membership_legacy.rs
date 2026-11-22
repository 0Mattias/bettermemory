//! The steps of `tests/contract/gen_membership_legacy.py` over the
//! recording store of `gen_membership_core` (`Fake`): the legacy
//! questions settled, the questions shown, a directory read against a
//! checkout's root and the record counts.

use std::collections::{BTreeMap, HashMap};

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::membership::legacy::ViewPath;
use bettermemory_core::membership::Error;
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::{py_str, PyStr, PyValue};
use bettermemory_core::store::caller::Resolution;
use bettermemory_core::unicode::cps;

use super::membership_core::{raised, tables_of, Model};
use super::membership_queries::reader;
use super::membership_rows::{enc, field, list, name};

/// `_VIEWS`: what the scripted `view_path` reads a directory as.
const VIEWS: [(&str, &str); 3] = [
    ("/w/link", "/w/one/sub"),
    ("/w/up", "/w"),
    ("C:/link", "C:\\W\\One"),
];

/// The scripted `location.view_path`.
pub fn view_path(dir: &[u32]) -> Option<PyStr> {
    Some(
        VIEWS
            .iter()
            .find(|(k, _)| cps(k) == dir)
            .map_or_else(|| dir.to_vec(), |(_, v)| cps(v)),
    )
}

/// The named tables of the header line.
pub fn named(header: &PyValue) -> BTreeMap<String, PyValue> {
    match field(header, "tables") {
        PyValue::Dict(items) => items.iter().map(|(k, v)| (name(k), v.clone())).collect(),
        other => panic!("not tables: {other:?}"),
    }
}

/// The tables a sequence starts from.
pub fn tables(named: &BTreeMap<String, PyValue>, seq: &PyValue) -> BTreeMap<String, Vec<PyValue>> {
    tables_of(&named[&name(field(seq, "tables"))])
}

fn text(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a str: {other:?}"),
    }
}

fn strs(v: &[PyStr]) -> PyValue {
    PyValue::List(v.iter().map(|s| PyValue::Str(s.clone())).collect())
}

fn pair((m, e): (i64, i64)) -> PyValue {
    PyValue::List(vec![
        PyValue::int(i128::from(m)),
        PyValue::int(i128::from(e)),
    ])
}

fn flavour(a: &PyValue) -> Flavour {
    match a.get("nt") {
        Some(PyValue::Bool(true)) => Flavour::Windows,
        _ => Flavour::Posix,
    }
}

/// `_answer(m, op, a)`, not yet spelled.
pub fn answer(
    m: &mut Model,
    op: &str,
    a: &PyValue,
    files: &HashMap<PyStr, PyValue>,
) -> Result<PyValue, Error<PyStr>> {
    let file = reader(files);
    let proposals = |got: Vec<_>| {
        PyValue::List(
            got.iter()
                .map(|p| rows::proposal_row(p).expect("a row"))
                .collect(),
        )
    };
    Ok(match op {
        "settle" => proposals(m.settle_legacy_questions()?),
        "pending" => proposals(m.pending(&file)?),
        "first" => {
            let res = Resolution {
                outcome: cps("silent"),
                project: None,
                checkout: text(field(a, "checkout")),
                rule: cps("r"),
                proposal: None,
                candidates: Vec::new(),
                witnesses: Vec::new(),
                chain: Vec::new(),
                tenant: None,
                lines: list(field(a, "lines"))
                    .iter()
                    .map(|l| text(l).expect("a line"))
                    .collect(),
                rows: Vec::new(),
                remint_token: false,
            };
            strs(&m.first_output(&res, &file)?)
        }
        "counts" => pair(m.record_counts(&text(a).expect("an id"))?),
        "by_project" => {
            let ids: Vec<PyStr> = list(a).iter().map(|x| py_str(x).expect("str")).collect();
            let mut got = m.records_by_project(&ids)?;
            got.sort_by(|x, y| x.0.cmp(&y.0));
            PyValue::List(
                got.into_iter()
                    .map(|(k, c)| PyValue::List(vec![PyValue::Str(k), pair(c)]))
                    .collect(),
            )
        }
        "below" | "declared" => {
            let k = m.checkout(&text(field(a, "k")).expect("an id"))?;
            let dir = text(field(a, "dir"));
            let f = flavour(a);
            if op == "below" {
                let canonical = field(a, "canonical") == &PyValue::Bool(true);
                let view: Option<ViewPath> = if canonical { None } else { Some(&view_path) };
                let dir = dir.expect("a directory");
                match m.below_root_in(f, &k, &dir, view)? {
                    None => PyValue::None,
                    Some(b) => PyValue::Bool(b),
                }
            } else {
                let action = text(field(a, "action")).expect("an action");
                PyValue::Bool(m.declared_below_in(f, &action, dir.as_deref(), &k)?)
            }
        }
        other => panic!("no op {other}"),
    })
}

/// One step: what it answered (spelled), the store calls and the rows.
pub fn step(
    m: &mut Model,
    st: &PyValue,
    files: &HashMap<PyStr, PyValue>,
) -> (PyValue, PyValue, PyValue) {
    let op = name(field(st, "op"));
    let a = st.get("a").cloned().unwrap_or(PyValue::None);
    let start = m.store().0.borrow().log.len();
    let o = match answer(m, &op, &a, files) {
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
