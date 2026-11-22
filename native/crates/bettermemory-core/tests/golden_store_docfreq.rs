//! Every case of `store_docfreq.jsonl` against `store::docfreq`: each
//! case's operations run on the stand-in for the store's side
//! (`common::docfreq_source`), each query through a fresh memo, its
//! answer, the calls it made to the store's side, the stores the memo
//! keeps and the terms the queried store's entry keeps compared.

mod common;

use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::docfreq::{
    DocActor, DocFreqError, DocFreqMemo, Ranked, DOC_FREQ_STORES, DOC_FREQ_TERMS,
};
use common::docfreq_source::Double;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn py_json(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Int(t) => serde_json::from_str(t).unwrap(),
        PyValue::Str(cps) => {
            Value::String(cps.iter().map(|&c| char::from_u32(c).unwrap()).collect())
        }
        PyValue::List(items) => Value::Array(items.iter().map(py_json).collect()),
        other => panic!("unexpected scope item {other:?}"),
    }
}

pub fn short(events: &[String], bound: usize) -> Value {
    if events.len() <= bound {
        return json!(events);
    }
    let text = serde_json::to_string(events).unwrap();
    let sha: String = Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    json!({"n": events.len(), "sha": sha})
}

fn admits(spec: &Value, scopes: &[PyValue], home: Option<&str>, actor: Option<&DocActor>) -> bool {
    let client = actor.and_then(|a| a.client.as_deref());
    match spec[0].as_str().unwrap() {
        "all" => true,
        "none" => false,
        "scope" => {
            let want = spec[1].as_str().unwrap();
            scopes.iter().any(|s| py_json(s) == json!(want))
        }
        "home" => home == spec[1].as_str(),
        _ => client == spec[1].as_str(),
    }
}

fn store_of(d: &mut Vec<Double>, schema: &[Value], n: usize) -> usize {
    match d.iter().position(|x| x.number == n) {
        Some(i) => i,
        None => {
            d.push(Double::new(n, schema));
            d.len() - 1
        }
    }
}

fn query(memo: &DocFreqMemo, double: &Double, op: &[Value]) -> Value {
    let kind = op[0].as_str().unwrap();
    let (terms, spec): (Vec<String>, &Value) = if kind == "qn" {
        let prefix = op[2].as_str().unwrap();
        let count = op[3].as_u64().unwrap();
        (
            (0..count).map(|i| format!("{prefix}{i:04}")).collect(),
            &op[4],
        )
    } else {
        (
            op[2]
                .as_array()
                .unwrap()
                .iter()
                .map(|t| t.as_str().unwrap().to_owned())
                .collect(),
            &op[3],
        )
    };
    if kind == "qr" {
        *double.hook.borrow_mut() = Some(op[4].clone());
    }
    double.trace.borrow_mut().clear();
    let mut admit = |scopes: &[PyValue], home: Option<&str>, actor: Option<&DocActor>| {
        let args = json!([
            Value::Array(scopes.iter().map(py_json).collect()),
            home,
            actor.and_then(|a| a.client.clone()),
            actor.and_then(|a| a.model.clone()),
        ]);
        double.trace.borrow_mut().push(format!("a:{args}"));
        admits(spec, scopes, home, actor)
    };
    let answer = match memo.ranking_statistics(double, &terms, &mut admit) {
        Ok(Ranked::Nothing) => Value::Null,
        Ok(Ranked::Uncached) => json!("uncached"),
        Ok(Ranked::Found((size, body, scope, average))) => json!([
            size,
            body.iter().map(|(t, n)| json!([t, n])).collect::<Vec<_>>(),
            scope.iter().map(|(t, n)| json!([t, n])).collect::<Vec<_>>(),
            average.map(bettermemory_core::float_repr::repr),
        ]),
        Err(DocFreqError::Py(PyError::ValueError)) => json!("ValueError"),
        Err(other) => json!(format!("{other:?}")),
    };
    *double.hook.borrow_mut() = None;
    let kept: Vec<usize> = memo
        .keys()
        .iter()
        .map(|(p, _)| p.rsplit('s').next().unwrap().parse().unwrap())
        .collect();
    let terms_kept = memo
        .entry(&(
            format!("/stores/s{}", double.number),
            format!("id{}", double.number),
        ))
        .map(|e| e.terms())
        .unwrap_or_default();
    let trace = double.trace.borrow().clone();
    json!({"r": answer, "t": short(&trace, 60), "k": kept, "m": short(&terms_kept, 20)})
}

pub fn run_case(memo: &DocFreqMemo, schema: &[Value], ops: &[Value]) -> Vec<Value> {
    memo.clear();
    let mut doubles: Vec<Double> = Vec::new();
    let mut out = Vec::new();
    for op in ops {
        let op = op.as_array().unwrap();
        let kind = op[0].as_str().unwrap();
        if kind == "clear" {
            memo.clear();
            continue;
        }
        let i = store_of(&mut doubles, schema, op[1].as_u64().unwrap() as usize);
        if matches!(kind, "q" | "qn" | "qr") {
            out.push(query(memo, &doubles[i], op));
        } else {
            doubles[i].apply(op);
        }
    }
    out
}

#[test]
fn store_docfreq_every_case() {
    let name = "store_docfreq.jsonl";
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    assert!(!lines.is_empty());
    assert_eq!(header["bounds"], json!([DOC_FREQ_STORES, DOC_FREQ_TERMS]));
    let schema = header["schema"].as_array().unwrap().clone();
    let ledger = Ledger::new("golden_store_docfreq", name);
    let memo = DocFreqMemo::new();
    let mut failures = Vec::new();
    let mut queries = 0;
    for case in &lines {
        let got = run_case(&memo, &schema, case["ops"].as_array().unwrap());
        let want = case["out"].as_array().unwrap();
        assert_eq!(got.len(), want.len(), "{}", case["case"]);
        for (j, (g, w)) in got.iter().zip(want).enumerate() {
            queries += 1;
            if g != w {
                failures.push(format!(
                    "{} query {j}: native {g}\n reference {w}",
                    case["case"]
                ));
                break;
            }
        }
    }
    assert!(queries > 0);
    ledger.settle(lines.len(), &failures);
}
