//! The steps of `tests/contract/gen_membership_proposals.py`, driven over
//! the native `Membership` and `gen_membership_core`'s recording store.

use bettermemory_core::machine::{MachineFacts, MachineIdentity};
use bettermemory_core::membership::proposals::{Line, Opening};
use bettermemory_core::membership::Error;
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

use super::membership_core::{model, raised, tables_of, tables_sha, Model};
use super::membership_rows::{dec, enc, field, int, list, name};

fn ostr(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a str or null: {other:?}"),
    }
}

fn st(v: &PyValue) -> PyStr {
    ostr(v).expect("a str")
}

fn facts(a: &PyValue) -> Option<MachineFacts> {
    if a == &PyValue::None {
        return None;
    }
    let id_file = match field(a, "id_file") {
        PyValue::List(x) => Some((st(&x[0]), st(&x[1]), ostr(&x[2]))),
        _ => None,
    };
    Some(MachineFacts {
        hardware: ostr(field(a, "hardware")),
        hostname: ostr(field(a, "hostname")),
        id_file,
    })
}

pub fn identity(a: &PyValue) -> MachineIdentity {
    MachineIdentity {
        id: st(field(a, "id")),
        facts: facts(field(a, "facts")).expect("facts"),
        predecessor: ostr(field(a, "predecessor")),
        minted: true,
        cloned: false,
        facts_updated: false,
        recorded: facts(field(a, "recorded")),
    }
}

fn items(v: &PyValue) -> Vec<(PyValue, PyValue)> {
    match &dec(v) {
        PyValue::Dict(items) => items.clone(),
        other => panic!("not a dict: {other:?}"),
    }
}

fn prefixed(prefix: &PyStr) -> impl Fn(&[u32]) -> PyStr + '_ {
    move |q: &[u32]| {
        let mut out = prefix.clone();
        out.extend_from_slice(q);
        out
    }
}

/// One answer of `_answer`.
pub fn answer(m: &mut Model, op: &str, a: &PyValue) -> Result<PyValue, Error<PyStr>> {
    let row = |p| rows::proposal_row(p).map_err(Error::Raised);
    Ok(match op {
        "record_machine" => {
            m.record_machine(&identity(a))?;
            PyValue::None
        }
        "machines" => PyValue::List(
            m.machines()?
                .into_iter()
                .map(|x| {
                    PyValue::List(vec![
                        PyValue::Str(x.id),
                        x.facts,
                        x.predecessor.map_or(PyValue::None, PyValue::Str),
                        PyValue::Str(x.minted),
                    ])
                })
                .collect(),
        ),
        "open_proposals" => {
            let got = m.open_proposals(ostr(a).as_deref())?;
            PyValue::List(got.iter().map(row).collect::<Result<_, _>>()?)
        }
        "proposal" => row(&m.proposal(&st(a))?)?,
        "put_proposal" => {
            let pr = rows::proposal_from(&dec(a)).map_err(Error::Raised)?;
            m.put_proposal(&pr)?;
            PyValue::None
        }
        "open_proposal" | "open_signed" => {
            let prefix = match field(a, "line") {
                l @ PyValue::Dict(_) => Some(st(field(l, "of"))),
                _ => None,
            };
            let f = prefix.as_ref().map(prefixed);
            let line = match &f {
                Some(f) => Line::Of(f),
                None => Line::Text(st(field(a, "line"))),
            };
            let o = Opening {
                checkout: ostr(field(a, "checkout")),
                kind: st(field(a, "kind")),
                candidates: list(field(a, "candidates")).iter().map(st).collect(),
                evidence: items(field(a, "evidence")),
                reason: st(field(a, "reason")),
                line,
                distinct: a.get("distinct").and_then(ostr),
            };
            let got = if op == "open_proposal" {
                m.open_proposal(&o)?
            } else {
                let sig = match &dec(field(a, "signature")) {
                    PyValue::Tuple(x) | PyValue::List(x) => Some(x.clone()),
                    _ => None,
                };
                m.open_proposal_with(&o, sig)?
            };
            row(&got)?
        }
        "close" => {
            let pr = rows::proposal_from(&dec(field(a, "pr"))).map_err(Error::Raised)?;
            let said = a.get("said").and_then(ostr);
            m.close(
                &pr,
                &st(field(a, "answer")),
                &st(field(a, "by")),
                said.as_deref(),
            )?;
            PyValue::None
        }
        "restate" => {
            let got = m.restate_proposal(
                &st(field(a, "id")),
                &st(field(a, "line")),
                &items(field(a, "evidence")),
            )?;
            row(&got)?
        }
        other => panic!("no op {other}"),
    })
}

/// One step: `{"o", "log", "rows"}`.
pub fn step(m: &mut Model, s: &PyValue) -> (PyValue, PyValue, PyValue) {
    let op = name(field(s, "op"));
    let a = s.get("a").cloned().unwrap_or(PyValue::None);
    let start = m.store().0.borrow().log.len();
    let o = match answer(m, &op, &a) {
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

/// The model a sequence line builds.
pub fn model_of(l: &PyValue) -> Model {
    let fail = match field(l, "fail") {
        PyValue::List(f) => Some((name(&f[0]), int(&f[1]) as usize)),
        _ => None,
    };
    let outer = field(l, "outer") == &PyValue::Bool(true);
    model(field(l, "init"), tables_of(field(l, "tables")), fail, outer)
}

/// `_sha(_spelled(stub.tables))` after a sequence.
pub fn after(m: &Model) -> PyValue {
    PyValue::Str(cps(&tables_sha(&m.store().0.borrow())))
}
