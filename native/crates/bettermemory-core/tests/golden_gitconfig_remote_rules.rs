//! Every case of `gitconfig_remote_rules.jsonl`: the reference's
//! `_remote_rule` of a name and a value (its worded reason, or none),
//! `refname_valid` under each flag and `refspec_valid` as a fetch and as a
//! push refspec, against the native rules.

mod common;

use bettermemory_core::gitconfig::remote_rules::{refname_valid, refspec_valid, remote_rule};
use bettermemory_core::gitconfig::spelled;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn bytes_of(value: &Value) -> Vec<u8> {
    let text = value.as_str().expect("a byte string");
    match text.strip_prefix('=') {
        Some(plain) => plain.as_bytes().to_vec(),
        None => common::unhex(text),
    }
}

#[test]
fn gitconfig_remote_rules_every_case() {
    let lines = jsonl("gitconfig_remote_rules.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "gitconfig_remote_rules");
    let ledger = Ledger::new(
        "golden_gitconfig_remote_rules",
        "gitconfig_remote_rules.jsonl",
    );
    let mut failures = Vec::new();
    let (mut rules, mut refusals, mut refs, mut specs) = (0u64, 0u64, 0u64, 0u64);
    for (i, row) in lines[1..].iter().enumerate() {
        let row = row.as_array().expect("a row");
        let (got, want) = match row[0].as_str().expect("a kind") {
            "rule" => {
                rules += 1;
                let value = (!row[2].is_null()).then(|| bytes_of(&row[2]));
                let why = remote_rule(&spelled(&bytes_of(&row[1])), value.as_deref());
                refusals += u64::from(why.is_some());
                (json!(why), row[3].clone())
            }
            "ref" => {
                refs += 1;
                let valid = refname_valid(&bytes_of(&row[1]), row[2] == 1, row[3] == 1);
                (json!(valid), row[4].clone())
            }
            "spec" => {
                specs += 1;
                (
                    json!(refspec_valid(&bytes_of(&row[1]), row[2] == 1)),
                    row[3].clone(),
                )
            }
            other => panic!("no kind {other}"),
        };
        if got != want {
            failures.push(format!(
                "#{i} {}: native {got} reference {want}",
                Value::Array(row.clone())
            ));
        }
    }
    assert_eq!(header["rules"].as_u64(), Some(rules));
    assert_eq!(header["refusals"].as_u64(), Some(refusals));
    assert_eq!(header["refs"].as_u64(), Some(refs));
    assert_eq!(header["specs"].as_u64(), Some(specs));
    ledger.settle((rules + refs + specs) as usize, &failures);
}
