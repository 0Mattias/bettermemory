//! Every case of `restore_rows.jsonl` and `restore_rows_python.jsonl`: the
//! created store opened for writing natively, the case's log rows
//! inserted and its restore record written, then each step's call
//! (`settle`, `append_missing`, `append_for`) under its hook. The answer
//! or the class raised, the warnings (format and arguments), the rows
//! appended (every column, byte for byte), the record's text after, the
//! head file's text, whether the connection is left in a transaction and
//! the clock readings equal the reference's. A case with a
//! `readonly_keys` step is left out as root, as the generator leaves it
//! out there. POSIX only, as `store_open_upkeep.jsonl` is;
//! `restore_rows_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;
mod restore_rows;

use std::os::unix::fs::MetadataExt;

use common::parity::Ledger;
use common::stores::scratch;
use restore_rows::{open_case, run_step, STORE_ID};

/// Whether this process runs as root, where a mode does not refuse.
fn as_root() -> bool {
    let dir = scratch("whoami");
    std::fs::metadata(&dir.0).is_ok_and(|m| m.uid() == 0)
}

fn every_case(file: &str, test: &str) {
    let lines = common::jsonl(file);
    let header = &lines[0];
    assert_eq!(header["store_id"], STORE_ID);
    assert_eq!(header["engine_version"], common::opened::ENGINE);
    let root = as_root();
    let mut total = 0;
    let mut failures = Vec::new();
    for case in &lines[1..] {
        let steps = case["steps"].as_array().expect("steps");
        if root && steps.iter().any(|s| s["hook"] == "readonly_keys") {
            continue;
        }
        let label = case["case"].as_str().expect("case");
        let mut opened = open_case(
            "restore-rows",
            case["rows"].as_array().expect("rows"),
            &case["record"],
            case["lock"].as_bool().expect("lock"),
        );
        for (i, step) in steps.iter().enumerate() {
            let differs = run_step(&mut opened, step, &format!("{label} step {i}"));
            if !differs.is_empty() {
                failures.push(differs.join("\n"));
            }
            total += 1;
        }
        opened.store.close().expect("close");
    }
    assert!(total > 0, "{file}: no case checked");
    let in_file: usize = lines[1..]
        .iter()
        .map(|c| c["steps"].as_array().map_or(0, Vec::len))
        .sum();
    assert!(
        root || total == in_file,
        "{file}: checked {total} of {in_file} steps"
    );
    eprintln!("{file}: {total} steps checked");
    Ledger::new(test, file).settle(total, &failures);
}

#[test]
fn every_step_settles_as_the_reference() {
    every_case("restore_rows.jsonl", "every_step_settles_as_the_reference");
}

#[test]
fn every_nested_step_raises_as_the_reference() {
    every_case(
        "restore_rows_python.jsonl",
        "every_nested_step_raises_as_the_reference",
    );
}
