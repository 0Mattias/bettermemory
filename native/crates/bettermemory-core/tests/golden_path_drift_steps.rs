//! Every case of `path_drift_steps.jsonl` against
//! `path_drift_steps::check_body_candidates`: the buckets after the pass
//! and every closure call in order, under recorded existence, route and
//! key answers.

mod common;

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};

use bettermemory_core::path_drift_steps::{check_body_candidates, Buckets};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().to_owned())
        .collect()
}

fn check(row: &Value) {
    let candidates: Vec<(String, bool, bool)> = row["c"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            (
                c[0].as_str().unwrap().to_owned(),
                c[1].as_u64().unwrap() == 1,
                c[2].as_u64().unwrap() == 1,
            )
        })
        .collect();
    let set = |k: &str| strs(&row[k]).into_iter().collect::<HashSet<_>>();
    let (verified, absent, existing, routes) = (set("v"), set("x"), set("e"), set("rt"));
    let table: HashMap<String, String> = row["k"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
        .collect();
    let log = RefCell::new(Vec::<Value>::new());
    let mut b = Buckets {
        seen: set("s"),
        ..Buckets::default()
    };
    check_body_candidates(
        &candidates,
        &verified,
        &absent,
        &mut b,
        &mut |p| {
            log.borrow_mut().push(json!(["e", p]));
            existing.contains(p)
        },
        &mut |p| {
            log.borrow_mut().push(json!(["rt", p]));
            routes.contains(p)
        },
        &mut |p| {
            log.borrow_mut().push(json!(["k", p]));
            table.get(p).cloned().unwrap_or_else(|| p.to_owned())
        },
    );
    let mut seen: Vec<String> = b.seen.into_iter().collect();
    seen.sort();
    let got = json!([
        b.checked,
        b.missing,
        b.claim_anchored,
        b.verified,
        b.expected_absent,
        b.dropped_as_route,
        seen
    ]);
    assert_eq!(got, row["r"], "buckets of {:?}", row["c"]);
    assert_eq!(
        json!(log.into_inner()),
        row["log"],
        "calls of {:?}",
        row["c"]
    );
}

#[test]
fn path_drift_steps_every_case() {
    let name = "path_drift_steps.jsonl";
    let all = jsonl(name);
    let mut ledger = Ledger::new("path_drift_steps_every_case", name);
    for (i, row) in all[1..].iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check(row));
    }
    assert_eq!(all[0]["cases"], json!(all.len() - 1), "{name} header");
    ledger.finish(all.len() - 1);
}
