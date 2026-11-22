//! Every case of `response_render.jsonl` (not gated) against
//! `response_render`, each answer's dicts compared in order
//! (`tests/contract/gen_response_render.py`).

mod common;

use bettermemory_core::json::loads;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Memory;
use bettermemory_core::response_render::{
    compact_origin, grouped_links, path_states, render_hit, render_show_sep, slashed,
    ClaimRejected, Fault, ParsedClaim, PathDriftLists,
};
use bettermemory_core::unicode::{self, cps};
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "response_render.jsonl";

/// A fixture value read as the generator's `json.loads` reads it.
pub fn py(v: &Value) -> PyValue {
    loads(&cps(&v.to_string())).expect("a JSON value")
}

/// The generator's stub `parse_claim`.
pub fn parse_claim(raw: &[u32]) -> ParsedClaim {
    for kind in ["path", "absent", "symbol"] {
        let mut prefix = cps(kind);
        prefix.push(':' as u32);
        if raw.starts_with(&prefix) {
            return Ok((cps(kind), raw[prefix.len()..].to_vec()));
        }
    }
    Err(ClaimRejected)
}

fn strs(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .map(|a| a.iter().map(|s| cps(s.as_str().unwrap())).collect())
        .unwrap_or_default()
}

/// The stub drift report from the case's fields.
pub fn drift(d: &Value) -> PathDriftLists {
    PathDriftLists {
        missing: strs(&d["missing"]),
        claim_anchored_missing: strs(&d["claim_anchored_missing"]),
        verified: strs(&d["verified"]),
        expected_absent: strs(&d["expected_absent"]),
        present_but_attested_absent: strs(&d["present_but_attested_absent"]),
        unconfirmed: strs(&d["unconfirmed"]),
        unconfirmed_reasons: strs(&d["unconfirmed_reasons"]),
        dropped_as_route: strs(&d["dropped_as_route"]),
        checked: strs(&d["checked"]),
        anchor: d["anchor"].as_str().map(cps),
    }
}

pub fn memory(m: &Value) -> Memory {
    Memory::from_payload(&py(m)).expect("a valid memory")
}

fn row(v: &Value) -> Vec<(PyValue, PyValue)> {
    match &py(v) {
        PyValue::Dict(d) => d.clone(),
        other => panic!("a row, got {other:?}"),
    }
}

/// `v` as the generator's `_ordered` writes it: each dict as its pairs.
fn text(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Int(t) => serde_json::from_str(t).unwrap(),
        PyValue::Float(f) => json!(f),
        PyValue::Str(s) => json!(unicode::to_string(s).unwrap()),
        PyValue::List(a) | PyValue::Tuple(a) => Value::Array(a.iter().map(text).collect()),
        PyValue::Dict(d) => json!({
            "d": d.iter().map(|(k, v)| json!([text(k), text(v)])).collect::<Vec<_>>()
        }),
        other => panic!("unexpected {other:?}"),
    }
}

fn out(r: Result<PyValue, Fault>) -> Value {
    match r {
        Ok(v) => text(&v),
        Err(e) => json!({ "raised": format!("{e:?}") }),
    }
}

fn sep(case: &Value) -> Option<char> {
    case["sep"].as_str().map(|s| s.chars().next().unwrap())
}

/// The port's answer to one case, spelled as the file spells it.
pub fn answer(case: &Value) -> Value {
    let a = &case["a"];
    let sep = sep(case);
    match case["f"].as_str().unwrap() {
        "hit" => out(render_hit(&row(a)).map(PyValue::Dict)),
        "show" => out(render_show_sep(
            &row(&a["state"]),
            &memory(&a["m"]),
            &drift(&a["d"]),
            &mut parse_claim,
            sep,
        )
        .map(PyValue::Dict)),
        "ps" => out(
            path_states(&memory(&a["m"]), &drift(&a["d"]), &mut parse_claim, sep).map(
                |(paths, claims)| {
                    PyValue::List(vec![
                        PyValue::List(
                            paths
                                .into_iter()
                                .map(|(p, f)| PyValue::List(vec![PyValue::Str(p), PyValue::Str(f)]))
                                .collect(),
                        ),
                        PyValue::List(claims.into_iter().map(PyValue::Str).collect()),
                    ])
                },
            ),
        ),
        "co" => text(&compact_origin(Some(&py(a))).map_or(PyValue::None, PyValue::Dict)),
        "gl" => out(grouped_links(&py(&a["e"]), a["end"].as_str().unwrap())),
        "sl" => text(&PyValue::Str(slashed(&cps(a["p"].as_str().unwrap()), sep))),
        other => panic!("unknown case {other}"),
    }
}

#[test]
fn response_render_every_case() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    let cases = &lines[1..];
    assert_eq!(header["count"].as_u64().unwrap() as usize, cases.len());
    assert!(!cases.is_empty());
    let mut ledger = Ledger::new("golden_response_render", FILE);
    for (i, case) in cases.iter().enumerate() {
        ledger.case(format!("#{i} {} {}", case["f"], case["a"]), || {
            assert_eq!(answer(case), case["o"], "case #{i} {}", case["a"]);
        });
    }
    ledger.finish(cases.len());
}
