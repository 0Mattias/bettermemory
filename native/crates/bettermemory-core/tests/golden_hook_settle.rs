//! Every case of `hook_settle.jsonl`: each settlement step over the
//! recording fakes of `gen_hook_settle` (`common::hook_settle`), whose log
//! of calls and answer must be the reference's.

mod common;

use common::hook_settle::run_row;
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

#[test]
fn hook_settle_every_case() {
    let file = "hook_settle.jsonl";
    let lines = jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(rows.len() as u64));
    let ledger = Ledger::new("golden_hook_settle", file);
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        let (log, answer) = run_row(row["fn"].as_str().unwrap(), &row["s"], &row["w"]);
        let want = row["log"].as_array().unwrap();
        if &log != want {
            failures.push(format!(
                "#{i}: native log {}\nreference log {}",
                Value::from(log),
                Value::from(want.clone())
            ));
            continue;
        }
        if answer.get("r") != row.get("r") || answer.get("x") != row.get("x") {
            failures.push(format!(
                "#{i}: native {answer:?} reference r {:?} x {:?}",
                row.get("r"),
                row.get("x")
            ));
        }
    }
    assert!(!rows.is_empty(), "{file}: no case checked");
    ledger.settle(rows.len(), &failures);
}
