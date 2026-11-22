//! Every case of `builder_schema.jsonl` (not gated) against `builder`:
//! each hand-written tree after `strip_titles`, after `fold_nullable` and
//! after both, compared as values with every object's key order
//! (`tests/contract/gen_builder_schema.py`).

mod common;

use bettermemory_core::builder::{fold_nullable, strip_titles};
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

/// The tree under `key`, or under `instead` when the generator left `key`
/// out as equal to it.
fn load(row: &Value, key: &str, instead: &str) -> PyValue {
    let text = row.get(key).unwrap_or(&row[instead]);
    json::loads(&unicode::cps(text.as_str().expect("a JSON text"))).expect("valid JSON")
}

fn check(row: &Value, label: &str) {
    let tree = load(row, "tree", "tree");
    let mut strip = tree.clone();
    strip_titles(&mut strip);
    assert_eq!(strip, load(row, "strip", "tree"), "{label}: strip");
    let mut fold = tree.clone();
    fold_nullable(&mut fold);
    assert_eq!(fold, load(row, "fold", "tree"), "{label}: fold");
    let mut both = tree;
    strip_titles(&mut both);
    fold_nullable(&mut both);
    let after_strip = if row.get("strip").is_some() {
        "strip"
    } else {
        "tree"
    };
    assert_eq!(both, load(row, "both", after_strip), "{label}: both");
}

#[test]
fn builder_schema_every_case() {
    let lines = jsonl("builder_schema.jsonl");
    let mut ledger = Ledger::new("golden_builder_schema", "builder_schema.jsonl");
    for (i, row) in lines.iter().enumerate() {
        let label = format!("row {i}");
        ledger.case(label.clone(), || check(row, &label));
    }
    ledger.finish(lines.len());
}
