//! Every case of `location_reads.jsonl` against `location_reads`: scripted
//! calls of `bounded_read` and `bounded_reads` over one board, each call's
//! answer and the reads it started compared.

mod common;

use bettermemory_core::location_reads::{READ_HANDOFF, READ_LANES, READ_SLOTS};
use common::jsonl;
use common::location_reads::run_case;
use common::parity::Ledger;
use serde_json::json;

#[test]
fn location_reads_every_case() {
    let mut lines = jsonl("location_reads.jsonl");
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    assert_eq!(
        header["consts"],
        json!([format!("{READ_HANDOFF}"), READ_LANES, READ_SLOTS])
    );
    let ledger = Ledger::new("golden_location_reads", "location_reads.jsonl");
    let mut failures = Vec::new();
    for case in &lines {
        let got = run_case(case);
        let want = case["want"].as_array().unwrap();
        if got.len() != want.len() || got.iter().zip(want).any(|(g, w)| g != w) {
            failures.push(format!(
                "{}: native {got:?}\n reference {want:?}",
                case["name"]
            ));
        }
    }
    assert!(!lines.is_empty());
    ledger.settle(lines.len(), &failures);
}
