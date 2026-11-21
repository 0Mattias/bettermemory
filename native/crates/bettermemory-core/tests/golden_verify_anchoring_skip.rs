//! Every line of `verify_anchoring_skip.jsonl` (POSIX only):
//! `gitfile_rule.object_format` over config texts, and `_sparse_enabled`,
//! `_parts_below`, `_skip_index` with its memo and `_absent_by_sparsity`
//! over each world in memory, a stamp that moves during a read made by
//! rewriting the file just before its first open.

#![cfg(unix)]

mod common;

use bettermemory_core::gitfile_rule::format::object_format;
use common::jsonl;
use common::parity::Ledger;
use common::verify_anchoring_skip::{answers, git, moving_world};
use serde_json::{json, Value};

const FILE: &str = "verify_anchoring_skip.jsonl";

#[test]
fn verify_anchoring_skip_every_line() {
    let lines = jsonl(FILE);
    let count = lines[0]["count"].as_u64().expect("a count");
    assert_eq!(lines[0]["cap"], json!(4));
    let ledger = Ledger::new("golden_verify_anchoring_skip", FILE);
    let mut failures = Vec::new();
    let mut checked = 0usize;
    let mut queries = 0usize;
    for (i, line) in lines[1..].iter().enumerate() {
        match line["k"].as_str() {
            Some("o") => {
                let text = common::unhex(line["t"].as_str().unwrap());
                let got = object_format(&text, &git())
                    .map_or(Value::Null, |n| json!(String::from_utf8(n).unwrap()));
                if got != line["o"] {
                    failures.push(format!("#{i}: native {got} reference {}", line["o"]));
                }
            }
            Some("w") => {
                let fs = moving_world(line);
                let got = answers(&fs, line);
                let theirs = line["q"].as_array().unwrap();
                assert_eq!(got.len(), theirs.len());
                for (mine, theirs) in got.iter().zip(theirs) {
                    queries += 1;
                    if mine != theirs {
                        failures.push(format!("#{i}: native {mine} reference {theirs}"));
                    }
                }
            }
            other => panic!("kind {other:?}"),
        }
        checked += 1;
    }
    assert!(checked > 0 && queries > 0, "nothing checked");
    assert_eq!(checked as u64, count, "checked {checked} of {count} lines");
    ledger.settle(checked, &failures);
}
