//! Every query of `githead_discovery.jsonl` (class i, POSIX only):
//! `find_gitdir`, `gitdir_at`, `locate`, `_located`, `_entry_above`,
//! `probed`, `stamp`, `discovery_moved` and `across_filesystems` against
//! `githead::discovery` over the case's tree in memory (`MemFs`, owners
//! and devices as the case states them), and on disk (`StdFs`, the
//! owners the system reports) for every case that names no other owner,
//! user or device.
//!
//! A walk that leaves its case goes on to the file system's root. In
//! memory nothing is above the case; on disk the answer is the fixture's
//! wherever no folder above the scratch directory holds a `.git` or a
//! `HEAD` entry, which `clear_above` checks first.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::StdFs;
use common::githead_discovery::{
    answers, entries, env_of, euid_of, foreign_of, text, world, Given, Owned, ME, MEM_ROOT,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

const FILE: &str = "githead_discovery.jsonl";

fn cases() -> (Vec<Value>, usize) {
    let lines = jsonl(FILE);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty(), "{FILE}: no case");
    let queries = lines[0]["queries"].as_u64().expect("a count of queries");
    (cases, usize::try_from(queries).unwrap())
}

fn compare(got: &[Value], case: &Value, i: usize, failures: &mut Vec<String>) -> usize {
    let theirs = case["q"].as_array().unwrap();
    assert_eq!(got.len(), theirs.len());
    for (mine, theirs) in got.iter().zip(theirs) {
        if mine != theirs {
            failures.push(format!("#{i}: native {mine} reference {theirs}"));
        }
    }
    theirs.len()
}

#[test]
fn githead_discovery_every_case_in_memory() {
    let (cases, queries) = cases();
    let ledger = Ledger::new("golden_githead_discovery", FILE);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, case) in cases.iter().enumerate() {
        let fs = world(case);
        let owners = Owned {
            fs: &fs,
            foreign: foreign_of(case, MEM_ROOT),
            owner: ME,
        };
        let given = Given {
            fs: &fs,
            owners: &owners,
            euid: euid_of(case, ME),
            env: env_of(case, MEM_ROOT),
            root: MEM_ROOT,
        };
        checked += compare(&answers(&given, case), case, i, &mut failures);
    }
    assert!(checked > 0, "no query checked");
    assert_eq!(checked, queries, "checked {checked} of {queries} queries");
    ledger.settle(checked, &failures);
}

struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt;
        fn open_up(dir: &std::path::Path) {
            let Ok(list) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in list.flatten() {
                let path = entry.path();
                if std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir()) {
                    let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
                    open_up(&path);
                }
            }
        }
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn clear_above(base: &std::path::Path) {
    for dir in base.ancestors() {
        for name in [".git", "HEAD"] {
            let entry = dir.join(name);
            assert!(
                std::fs::symlink_metadata(&entry).is_err(),
                "{} exists above the scratch directory: run this test with a \
                 temporary directory (TMPDIR) outside any repository",
                entry.display()
            );
        }
    }
}

fn write_tree(case: &Value, root: &std::path::Path) -> bool {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let real = root.to_str().unwrap();
    for (rel, _) in entries(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
    }
    for (rel, value) in entries(case, "f") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, text(value, real)).unwrap();
    }
    for (rel, to) in entries(case, "l") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        symlink(to.as_str().unwrap().replace("<root>", real), path).unwrap();
    }
    for (rel, _) in entries(case, "p") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let made = std::process::Command::new("mkfifo")
            .arg(&path)
            .status()
            .is_ok_and(|s| s.success());
        if !made {
            return false;
        }
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    }
    for (rel, m) in entries(case, "m") {
        let mode = u32::from_str_radix(m.as_str().unwrap(), 8).unwrap();
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode)).unwrap();
    }
    true
}

/// The cases a disk can hold as written: no other owner, user or device.
fn on_disk_case(case: &Value) -> bool {
    ["o", "euid", "mnt"].iter().all(|k| case.get(*k).is_none())
}

#[test]
fn githead_discovery_every_plain_case_on_disk() {
    use std::os::unix::fs::MetadataExt;
    let (cases, _) = cases();
    let ledger = Ledger::new("golden_githead_discovery_stdfs", FILE);
    let base = std::env::temp_dir().join(format!("bm-ghdisc-{}", std::process::id()));
    std::fs::create_dir(&base).unwrap();
    let base = Scratch(std::fs::canonicalize(&base).unwrap());
    clear_above(&base.0);
    let me = std::fs::metadata(&base.0).unwrap().uid();
    assert_ne!(me, 0, "run as a user: root's owners are not the fixture's");
    let mut failures = Vec::new();
    let mut checked = 0;
    let mut expected = 0;
    for (i, case) in cases.iter().enumerate().filter(|(_, c)| on_disk_case(c)) {
        expected += case["q"].as_array().unwrap().len();
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        if !write_tree(case, &dir) {
            failures.push(format!("#{i}: mkfifo could not run"));
            continue;
        }
        let root = dir.to_str().unwrap();
        let given = Given {
            fs: &StdFs,
            owners: &StdFs,
            euid: me,
            env: env_of(case, root),
            root,
        };
        checked += compare(&answers(&given, case), case, i, &mut failures);
    }
    drop(base);
    assert!(checked > 0, "no query checked on disk");
    if failures.is_empty() {
        assert_eq!(checked, expected);
    }
    ledger.settle(checked.max(failures.len()), &failures);
}
