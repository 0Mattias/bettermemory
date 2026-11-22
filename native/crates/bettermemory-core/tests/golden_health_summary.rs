//! Every case of `health_summary.jsonl` against `health_summary`:
//! `health_summary`, `section_names` and `health_section`, each answer or
//! exception (class, and text for KeyError and ValueError).

mod common;

use bettermemory_core::health_summary::{health_section, health_summary, section_names};
use bettermemory_core::pyvalue::PyValue;
use common::parity::Ledger;
use common::{jsonl, neutral, text_field};

#[test]
fn health_summary_every_case() {
    let name = "health_summary.jsonl";
    let lines = jsonl(name);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    assert_eq!(count + 1, lines.len(), "{name}: header count");
    let ledger = Ledger::new("golden_health_summary", name);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in lines[1..].iter().enumerate() {
        let report = neutral(&row["report"]);
        let got = match row["op"].as_str().expect("op") {
            "summary" => health_summary(&report),
            "names" => section_names(&report).map(PyValue::List),
            _ => health_section(&report, &text_field(row, "name")),
        };
        let same = match (&got, row.get("error")) {
            (Ok(out), None) => *out == neutral(&row["out"]),
            (Err(e), Some(class)) => {
                e.name == class.as_str().expect("class")
                    && match &e.text {
                        Some(t) => *t == text_field(row, "text"),
                        None => row.get("text").is_none() && row.get("text_cps").is_none(),
                    }
            }
            _ => false,
        };
        if !same {
            failures.push(format!("row {i} {row}: native {got:?}"));
        }
        checked += 1;
    }
    assert!(checked > 0 && checked == count, "{name}: {checked} checked");
    ledger.settle(count, &failures);
}
