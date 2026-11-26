//! Every script of response_drift.jsonl over a real `DriftPass`: each
//! effect in order with its budget scope, each answer, the pass's kept
//! maps and the memo's keys in order.
mod common;
mod response_drift_script;

use common::parity::Ledger;
use response_drift_script::replay;
use serde_json::json;

const FILE: &str = "response_drift.jsonl";

#[test]
fn golden_response_drift() {
    let rows = common::jsonl(FILE);
    let mut ledger = Ledger::new("golden_response_drift", FILE);
    assert_eq!(rows[0]["count"], rows.len() - 1);
    assert!(rows.len() > 1, "no case");
    for case in &rows[1..] {
        let got = replay(case);
        let expected = json!([case["calls"], case["results"]]);
        let label = case["label"].as_str().unwrap();
        assert!(
            !ledger.differs_fatally(label, got == expected),
            "{label}\n{got}\n{expected}"
        );
    }
    ledger.finish(rows.len() - 1);
}
