//! Every case of `location_git_readers.jsonl`: synthetic histories through
//! `location_git::reachable_among` and `walk`, and the digest over the
//! generator's seeded random histories.

mod common;

use common::location_git::{reach_of, sweep, walk_of};
use common::parity::Ledger;
use serde_json::json;

const FILE: &str = "location_git_readers.jsonl";

#[test]
fn location_git_readers_every_case() {
    let lines = common::jsonl(FILE);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new("golden_location_git_readers", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = match case["fn"].as_str().unwrap() {
            "reach" => reach_of(case),
            "walk" => walk_of(case),
            "sweep" => {
                let count = case["count"].as_u64().unwrap() as usize;
                assert!(count > 0);
                json!(sweep(case["seed"].as_u64().unwrap(), count))
            }
            other => panic!("a row of kind {other}"),
        };
        let want = if case["fn"] == "sweep" {
            &case["h"]
        } else {
            &case["a"]
        };
        if &got != want {
            failures.push(format!(
                "#{i} {}: native {got} reference {want}",
                case["note"]
            ));
        }
    }
    ledger.settle(cases.len(), &failures);
}
