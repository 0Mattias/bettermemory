//! Every case of `hook_stop.jsonl`: `run_audit` over the recording fakes
//! of `gen_hook_stop` (`common::hook_stop`), whose log of calls and answer
//! must be the reference's. A raising case's row holds the length its log
//! shares with the raise-free case over the same arguments (`same`) and
//! the rest.

mod common;

use common::hook_stop::run_row;
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

fn is_raise_base(row: &Value) -> bool {
    let a = &row["a"];
    row["w"].get("raise").is_none()
        && a.get("dry_run").is_none()
        && a["store"].is_null()
        && a["config"].is_null()
        && a["state"].is_null()
}

#[test]
fn hook_stop_every_case() {
    let file = "hook_stop.jsonl";
    let lines = jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(rows.len() as u64));
    let base: Vec<Value> = rows.iter().find(|r| is_raise_base(r)).unwrap()["log"]
        .as_array()
        .unwrap()
        .clone();
    let ledger = Ledger::new("golden_hook_stop", file);
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        let (log, answer) = run_row(&row["a"], &row["w"]);
        let mut want: Vec<Value> = match row["same"].as_u64() {
            Some(n) => base[..n as usize].to_vec(),
            None => Vec::new(),
        };
        want.extend(row["log"].as_array().unwrap().iter().cloned());
        if log != want {
            failures.push(format!(
                "#{i}: native log {}\nreference log {}",
                Value::from(log),
                Value::from(want)
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
