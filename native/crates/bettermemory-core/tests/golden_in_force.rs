//! Every case of `in_force.jsonl` against `in_force::in_force`: each
//! answer and every call made on the store and the admission, in order,
//! and the sweep's count and SHA-256 over the same drawn cases.

mod common;
#[path = "in_force_world/mod.rs"]
mod world;

use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use world::{answer, draw, Case};

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|x| x.as_str().expect("a text").to_owned())
        .collect()
}

fn case_of(row: &Value) -> Case {
    let links = row["links"]
        .as_object()
        .expect("links")
        .iter()
        .map(|(t, rows)| {
            let rows = rows
                .as_array()
                .expect("rows")
                .iter()
                .map(|r| {
                    let r = strs(r);
                    (r[0].clone(), r[1].clone(), r[2].clone())
                })
                .collect();
            (t.clone(), rows)
        })
        .collect();
    Case {
        links,
        admit: (!row["admit"].is_null()).then(|| strs(&row["admit"])),
        held: strs(&row["held"]),
        ids: strs(&row["ids"]),
    }
}

#[test]
fn in_force_every_case() {
    let name = "in_force.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    let count = header["count"].as_u64().expect("count") as usize;
    assert_eq!(count + 2, lines.len(), "{name}: header count");
    let ledger = Ledger::new("golden_in_force", name);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in lines[1..=count].iter().enumerate() {
        let got = answer(&case_of(row));
        let want = json!([row["out"], row["calls"]]);
        if got != want {
            failures.push(format!("row {i} {row}: native {got}"));
        }
        checked += 1;
    }
    assert!(checked > 0 && checked == count, "{name}: {checked} checked");

    let sweep = &lines[count + 1];
    assert_eq!(sweep["sweep"], "lcg64");
    let swept = sweep["count"].as_u64().expect("count") as usize;
    assert_eq!(Some(swept as u64), header["swept"].as_u64());
    let cases = draw(sweep["seed"].as_u64().expect("seed"), swept);
    assert!(
        !cases.is_empty() && cases.len() == swept,
        "{name}: sweep count"
    );
    let mut h = Sha256::new();
    for case in &cases {
        let text = serde_json::to_string(&answer(case)).expect("JSON");
        h.update(text.as_bytes());
        h.update(b"\n");
    }
    let got: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    if got != sweep["sha256"].as_str().expect("sha256") {
        failures.push(format!("sweep: native digest {got}"));
    }
    ledger.settle(count + 1, &failures);
}
