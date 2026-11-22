//! Every case of `review_answers.jsonl` against `review::answers`: the
//! formats, `parse_answers` and `parse_update_answers` with each teaching
//! error, `to_event` and `is_zero_attestation`, `check_answered_ids`,
//! `check_named`, and the seeded sweep's digest.

mod common;

use std::collections::HashSet;

use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::review::*;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn text(t: &[u32]) -> String {
    unicode::to_string(t).expect("no surrogate")
}

fn load(raw: &Value) -> Option<PyValue> {
    raw.as_str()
        .map(|t| json::loads(&unicode::cps(t)).expect("the row's JSON"))
}

type Read = Result<Option<ReviewAnswers>, AnswerFault>;

fn read(kind: &str, raw: Option<&PyValue>) -> Read {
    if kind == "p" {
        parse_answers(raw)
    } else {
        parse_update_answers(raw)
    }
}

fn answer(got: Read) -> Value {
    match got {
        Err(AnswerFault::Teach(m)) => json!({"e": text(&m)}),
        Err(AnswerFault::Py(e)) => json!({"x": e.name()}),
        Ok(None) => Value::Null,
        Ok(Some(a)) => json!({
            "ev": text(&json::dumps(&a.to_event(), DumpOptions::DEFAULT).expect("event")),
            "rid": a.review_id.as_deref().map(text),
            "z": a.is_zero_attestation(),
        }),
    }
}

fn ids(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .expect("ids")
        .iter()
        .map(|x| unicode::cps(x.as_str().expect("id")))
        .collect()
}

fn check(case: &Value) -> Value {
    let kind = if case["u"] == json!(true) { "u" } else { "p" };
    let raw = load(&case["raw"]);
    let got = match read(kind, raw.as_ref()) {
        Ok(got) => got.expect("answers"),
        Err(AnswerFault::Teach(m)) => return json!({"e": text(&m)}),
        Err(e) => panic!("{e:?}"),
    };
    let out = if case["k"] == "ids" {
        check_answered_ids(&got, &ids(&case["allowed"])).map(|()| Value::Null)
    } else {
        let set = |v: &Value| ids(v).into_iter().collect::<HashSet<_>>();
        let req = case["req"].as_bool().expect("req");
        check_named(&got, &set(&case["roster"]), &set(&case["shown"]), req)
            .map(|n| Value::Array(n.iter().map(|i| json!(text(i))).collect()))
    };
    match out {
        Ok(v) => v,
        Err(AnswerFault::Teach(m)) => json!({"e": text(&m)}),
        Err(e) => panic!("{e:?}"),
    }
}

/// The generator's `_Lcg`.
struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
    fn pick<'a>(&mut self, items: &'a [Value]) -> &'a Value {
        let n = self.next() as usize % items.len();
        &items[n]
    }
}

fn sweep(case: &Value) -> Value {
    let table = |k: &str| case[k].as_array().expect("table").clone();
    let (ids, labs) = (table("ids"), table("labs"));
    let keyed = [
        ("unsafe", table("scores")),
        ("secret", table("scores")),
        ("standing", table("scores")),
        ("named", table("named")),
        ("said", table("said")),
        ("review_id", table("rids")),
    ];
    let extra = table("extra");
    let mut g = Lcg(case["seed"].as_u64().expect("seed"));
    let mut digest = Sha256::new();
    let n = case["n"].as_u64().expect("n");
    for _ in 0..n {
        let kind = if g.next().is_multiple_of(2) { "p" } else { "u" };
        let mut rel = Vec::new();
        for _ in 0..g.next() % 4 {
            let id = g.pick(&ids).as_str().expect("id").to_owned();
            let lab = g.pick(&labs).as_str().expect("label");
            rel.push(format!("{id}: {lab}"));
        }
        let mut parts = vec![format!("\"relation\": {{{}}}", rel.join(", "))];
        for (key, t) in &keyed {
            if let Some(v) = g.pick(t).as_str() {
                parts.push(format!("\"{key}\": {v}"));
            }
        }
        if let Some(v) = g.pick(&extra).as_str() {
            parts.push(v.to_owned());
        }
        let raw = json::loads(&unicode::cps(&format!("{{{}}}", parts.join(", ")))).expect("json");
        let spelled = match read(kind, Some(&raw)) {
            Ok(None) => "N".to_owned(),
            Err(AnswerFault::Teach(m)) => format!("E{}", text(&m)),
            Err(AnswerFault::Py(e)) => format!("X{}", e.name()),
            Ok(Some(a)) => {
                let ev = text(&json::dumps(&a.to_event(), DumpOptions::DEFAULT).expect("event"));
                let rid = a.review_id.as_deref().map_or("None".to_owned(), text);
                let z = if a.is_zero_attestation() {
                    "True"
                } else {
                    "False"
                };
                format!("O{ev}|{rid}|{z}")
            }
        };
        digest.update(spelled.as_bytes());
        digest.update(b"\n");
    }
    json!(digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>())
}

#[test]
fn review_answers_every_case() {
    let name = "review_answers.jsonl";
    let lines = jsonl(name);
    let mut ledger = Ledger::new("review_answers_every_case", name);
    for (i, case) in lines.iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            let k = case["k"].as_str().expect("kind");
            if k == "formats" {
                assert_eq!(case["answer"], json!(answer_format()));
                assert_eq!(case["named"], json!(NAMED_FORMAT));
                assert_eq!(case["update"], json!(update_answer_format()));
                return;
            }
            let mut got = case.clone();
            let (key, value) = match k {
                "p" | "u" => ("r", answer(read(k, load(&case["raw"]).as_ref()))),
                "ids" | "named" => ("r", check(case)),
                "sweep" => ("sha256", sweep(case)),
                k => panic!("unknown kind {k}"),
            };
            got[key] = value;
            assert_eq!(got, *case, "{name} case {i}");
        });
    }
    ledger.finish(lines.len());
}
