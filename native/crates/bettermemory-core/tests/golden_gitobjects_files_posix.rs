//! Every case of `gitobjects_files_posix.jsonl` (POSIX only) against
//! `gitobjects::files`: the alternates walk over links (relative,
//! absolute and C-quoted entries, cycles, the depth limit, a NUL raw and
//! quoted), FIFOs, modes and eviction. Each case is asked over its tree
//! in memory and on disk.

#![cfg(unix)]

mod common;

use common::gitobjects_files as gf;
use common::parity::Ledger;
use serde_json::Value;

fn check(case: &Value, (a, s): (Value, Value), on: &str) {
    let name = case["name"].as_str().unwrap();
    assert_eq!(a, case["a"], "{name} ({on}): the answers");
    assert_eq!(s, case["s"], "{name} ({on}): the state");
}

#[test]
fn gitobjects_files_posix_every_case() {
    let lines = common::jsonl("gitobjects_files_posix.jsonl");
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, cases.len());
    let mut ledger = Ledger::new(
        "golden_gitobjects_files_posix",
        "gitobjects_files_posix.jsonl",
    );
    for case in cases {
        ledger.case(case["name"].as_str().unwrap(), || {
            check(case, gf::ask_in_memory(case), "in memory");
            check(case, on_disk(case), "on disk");
        });
    }
    ledger.finish(cases.len());
}

fn on_disk(case: &Value) -> (Value, Value) {
    use bettermemory_core::fsdata::StdFs;
    let tmp = std::env::temp_dir().join(format!(
        "bm-gobj-files-posix-{}-{}",
        std::process::id(),
        case["name"].as_str().unwrap().len()
    ));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).unwrap();
    let real = std::fs::canonicalize(&tmp).unwrap();
    let root = {
        use std::os::unix::ffi::OsStrExt;
        real.as_os_str().as_bytes().to_vec()
    };
    gf::disk_tree(case, &root);
    let mut fs = gf::Claims::new(StdFs);
    fs.claims = gf::claims_of(case, &root);
    let answer = gf::ask(&fs, &root, case);
    gf::undo_modes(case, &root);
    std::fs::remove_dir_all(&tmp).unwrap();
    answer
}
