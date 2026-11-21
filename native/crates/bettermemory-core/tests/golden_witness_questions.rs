//! Every case of `witness_questions.jsonl` (not gated) against
//! `witness_questions`.

mod common;
#[allow(dead_code)]
mod witness;

use bettermemory_core::witness_questions as wq;
use common::parity::Ledger;
use serde_json::Value;
use witness::{asked_text, checkout, dumps, key, load, location, opt_text, text};

fn check(row: &Value) {
    let o = &row["o"];
    match row["f"].as_str().unwrap() {
        "token" => {
            let answers = load(&row["a"]);
            let got = wq::AnsweredToken::new(&answers, row["p"].as_bool().unwrap())
                .call(&checkout(&row["k"]));
            match got {
                Ok(None) => assert_eq!(o["r"], Value::Null),
                Ok(Some((t, s))) => assert_eq!(Value::Array(vec![opt_text(&t), text(&s)]), o["r"]),
                Err(q) => assert_eq!(Value::String(dumps(&q)), o["need"]),
            }
        }
        "located" => {
            let answers = load(&row["a"]);
            let loc = location(row);
            let mut asked = Vec::new();
            let held: Vec<_> = row["m"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| (witness::cps(c[0].as_str().unwrap()), key(&c[1])))
                .collect();
            let mut model = |t: &[u32]| {
                asked.push(text(t));
                held.clone()
            };
            let got = wq::AnsweredToken::new(&answers, true).located(&loc, &mut model);
            let out = vec![
                opt_text(&got.token),
                opt_text(&got.token_written),
                text(&got.token_status),
                Value::Array(asked),
            ];
            assert_eq!(Value::Array(out), *o);
        }
        "remint" => {
            let (cap, mut calls) = (witness::capture(&row["cap"]), Vec::new());
            let w = &row["w"];
            let mut stub = |g: &[u32], policy: bool, bare: bool, r: Option<_>| {
                calls.push(witness::call(g, policy, bare, r));
                (
                    w[0].as_str().map(str::to_owned),
                    w[1].as_str().unwrap().to_owned(),
                )
            };
            let got = wq::remint_answer(&cap, &load(&row["q"]), &mut stub).unwrap();
            assert_eq!(Value::String(dumps(&got)), o[0]);
            assert_eq!(Value::Array(calls), o[1]);
        }
        "refused" => {
            let got = wq::remint_refused(&load(&row["q"]), &load(&row["a"]));
            assert_eq!(Value::Bool(got), *o);
        }
        "liveness" => {
            let live = match wq::AnsweredLiveness::new(&load(&row["a"])) {
                Ok(l) => l,
                Err(e) => return assert_eq!(o["raise"], e.name()),
            };
            let view = witness::view(row["v"].as_str().unwrap());
            match live.call(&checkout(&row["k"]), &view) {
                Ok(s) => assert_eq!(text(&s), o["r"]),
                Err(q) => assert_eq!(Value::String(dumps(&q)), o["need"]),
            }
        }
        "views" => match wq::asked_views(&load(&row["q"])) {
            Ok(views) => assert_eq!(Value::String(asked_text(&views)), o["r"]),
            Err(e) => assert_eq!(o["raise"], e.name()),
        },
        other => panic!("unknown row kind {other}"),
    }
}

fn run(test: &str, file: &str) {
    let rows = common::jsonl(file);
    let mut ledger = Ledger::new(test, file);
    for (n, row) in rows.iter().enumerate() {
        ledger.case(format!("row {n}: {row}"), || check(row));
    }
    ledger.finish(rows.len());
}

#[test]
fn witness_questions_every_case() {
    run("golden_witness_questions", "witness_questions.jsonl");
}
