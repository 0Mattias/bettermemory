//! Every case of `health_roots.jsonl` against `EstateProgress`: each
//! `ordered` answer and the state after the steps, and the cap row's
//! digest over the same attempts.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_roots::EstateProgress;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{jsonl, text_field};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|x| text_field(x, "v"))
        .collect()
}

fn opt(row: &Value, name: &str) -> Option<PyStr> {
    if row.get(name).is_some_and(Value::is_null) {
        None
    } else {
        Some(text_field(row, name))
    }
}

fn iso(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(text)).expect("an isoformat")
}

fn run(row: &Value) -> bool {
    let mut p = EstateProgress {
        cursor: opt(row, "start_cursor"),
        retry_root: opt(row, "start_retry"),
        ..EstateProgress::default()
    };
    let mut outs = Vec::new();
    for step in row["steps"].as_array().expect("steps") {
        match step[0].as_str().expect("op") {
            "o" => outs.push(p.ordered(&texts(&step[1]))),
            "a" => p.attempt(
                text_field(&step[1], "v"),
                iso(step[2].as_str().expect("iso")),
            ),
            _ => p.retry_root = Some(text_field(&step[1], "v")),
        }
    }
    let want_outs: Vec<Vec<PyStr>> = row["out"]
        .as_array()
        .expect("out")
        .iter()
        .map(texts)
        .collect();
    let checked: Vec<(PyStr, String)> = p
        .checked
        .iter()
        .map(|(r, w)| (r.clone(), w.isoformat()))
        .collect();
    let want_checked: Vec<(PyStr, String)> = row["checked"]
        .as_array()
        .expect("checked")
        .iter()
        .map(|c| {
            (
                text_field(&c[0], "v"),
                c[1].as_str().expect("iso").to_owned(),
            )
        })
        .collect();
    outs == want_outs
        && p.cursor == opt(row, "cursor")
        && p.retry_root == opt(row, "retry_root")
        && checked == want_checked
        && Some(p.caller_first) == row["caller_first"].as_bool()
}

fn root(i: u64, modulo: u64) -> PyStr {
    unicode::cps(&format!("r{}", i % modulo))
}

#[test]
fn health_roots_every_case() {
    let name = "health_roots.jsonl";
    let lines = jsonl(name);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    assert_eq!(count + 2, lines.len(), "{name}: header count");
    let ledger = Ledger::new("golden_health_roots", name);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in lines[1..=count].iter().enumerate() {
        if !run(row) {
            failures.push(format!("row {i} {row}"));
        }
        checked += 1;
    }
    assert!(checked > 0 && checked == count, "{name}: {checked} checked");

    let cap = &lines[count + 1];
    let n = cap["cap"].as_u64().expect("cap");
    let modulo = cap["mod"].as_u64().expect("mod");
    assert!(n > 0);
    let base = iso(cap["base"].as_str().expect("base"));
    let mut p = EstateProgress::default();
    for i in 0..n {
        let when = base.minus_micros(-(i as i128) * 1_000_000).expect("a time");
        p.attempt(root(i, modulo), when);
    }
    let s = |t: &[u32]| unicode::to_string(t).expect("ASCII");
    let mut lines_out: Vec<String> = p
        .checked
        .iter()
        .map(|(r, w)| json!([s(r), w.isoformat()]).to_string().replace(",", ", "))
        .collect();
    lines_out.push(json!(p.cursor.as_deref().map(s)).to_string());
    let all: Vec<PyStr> = (0..modulo).map(|i| root(i, modulo)).collect();
    let order: Vec<String> = p.ordered(&all).iter().map(|r| s(r)).collect();
    lines_out.push(
        serde_json::to_string(&order)
            .expect("JSON")
            .replace(",", ", "),
    );
    let digest: String = Sha256::digest(lines_out.join("\n").as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    if digest != cap["sha256"].as_str().expect("sha256") {
        failures.push(format!("cap: native digest {digest}"));
    }
    ledger.settle(count + 1, &failures);
}
