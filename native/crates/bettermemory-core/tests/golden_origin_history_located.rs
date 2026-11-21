//! Every query of `origin_history_located.jsonl` and
//! `origin_history_located_modes.jsonl` (class i, POSIX only):
//! `located_key`, `shallow_file`, `cuts`, `cuts_in` and `history_guard`
//! against `origin_history::located` over the case's tree on disk, the
//! steps (`w:` writes, `e:` variables) taken in order.
//!
//! A guard is checked by equality alone: the index of the first guard of
//! the case equal to it.

#![cfg(unix)]

mod common;

use common::jsonl;
use common::origin_history_located::{answer, write_tree, Scratch, State};
use common::parity::Ledger;

fn check(file: &str, tag: &str) {
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let queries = usize::try_from(lines[0]["queries"].as_u64().unwrap()).unwrap();
    assert!(!cases.is_empty(), "{file}: no case");
    let ledger = Ledger::new(&format!("golden_origin_history_located_{tag}"), file);
    let base = Scratch::new(tag);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        write_tree(case, &dir);
        let root = dir.to_str().unwrap();
        let mut state = State::new();
        for q in case["q"].as_array().unwrap() {
            let query = q["p"].as_str().unwrap();
            let got = answer(query, root, &mut state);
            if got != q["v"] {
                failures.push(format!("#{i} {query}: native {got} reference {}", q["v"]));
            }
            checked += 1;
        }
    }
    drop(base);
    assert!(checked > 0, "no query checked");
    assert_eq!(checked, queries, "checked {checked} of {queries} queries");
    ledger.settle(checked, &failures);
}

#[test]
fn origin_history_located_every_query() {
    check("origin_history_located.jsonl", "plain");
}

#[test]
fn origin_history_located_modes_every_query() {
    use std::os::unix::fs::MetadataExt;
    let probe = Scratch::new("whoami");
    let me = std::fs::metadata(&probe.0).unwrap().uid();
    assert_ne!(me, 0, "run as a user: root reads mode-000 files");
    drop(probe);
    check("origin_history_located_modes.jsonl", "modes");
}
