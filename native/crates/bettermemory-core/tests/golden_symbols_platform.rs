//! Every case of `symbols_platform.jsonl` (class iii, the platform: links,
//! a FIFO, a mode-000 file and names that differ only in case, which
//! macOS folds), POSIX only: the tree written to a scratch directory and
//! read through `StdFs`. Where the file was made on this host (CI's parity
//! job makes it anew), the host's answers are the reference's.

#![cfg(unix)]

mod common;

use std::os::unix::fs::PermissionsExt;

use bettermemory_core::fsdata::{Flavour, StdFs};
use common::jsonl;
use common::parity::Ledger;
use common::symbols::{content, report_json, run, Replay};
use serde_json::Value;

const FILE: &str = "symbols_platform.jsonl";

struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn fresh_dir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("bm-symbols-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
}

fn write_tree(case: &Value, here: &std::path::Path) {
    if let Some(Value::Array(dirs)) = case.get("d") {
        for d in dirs {
            std::fs::create_dir_all(here.join(d.as_str().unwrap())).unwrap();
        }
    }
    for (rel, spec) in case["f"].as_object().unwrap() {
        let path = here.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, content(spec)).unwrap();
    }
    if let Some(Value::Object(links)) = case.get("l") {
        for (rel, target) in links {
            let target = target.as_str().unwrap();
            let target = match target.strip_prefix('@') {
                Some(rest) => format!("{}{rest}", here.display()),
                None => target.to_owned(),
            };
            let path = here.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::os::unix::fs::symlink(target, path).unwrap();
        }
    }
    if let Some(Value::Array(fifos)) = case.get("p") {
        for rel in fifos {
            let status = std::process::Command::new("mkfifo")
                .arg(here.join(rel.as_str().unwrap()))
                .status()
                .expect("mkfifo runs");
            assert!(status.success());
        }
    }
    if let Some(Value::Object(modes)) = case.get("m") {
        for (rel, mode) in modes {
            let mode = u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap();
            std::fs::set_permissions(here.join(rel), std::fs::Permissions::from_mode(mode))
                .unwrap();
        }
    }
}

fn unlock(case: &Value, here: &std::path::Path) {
    if let Some(Value::Object(modes)) = case.get("m") {
        for rel in modes.keys() {
            let _ =
                std::fs::set_permissions(here.join(rel), std::fs::Permissions::from_mode(0o600));
        }
    }
}

#[test]
fn symbols_platform_every_case() {
    let lines = jsonl(FILE);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty());
    let ledger = Ledger::new("golden_symbols_platform", FILE);
    let mut failures = Vec::new();
    let base = Scratch(fresh_dir());
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        write_tree(case, &dir);
        let here = dir.to_str().unwrap().to_owned();
        let names = Replay::of(case);
        let got = run(case, &StdFs, Flavour::Posix, &here, &names).map(|r| report_json(&r));
        unlock(case, &dir);
        if got.as_ref() != Ok(&case["r"]) || *names.misses.borrow() != 0 {
            failures.push(format!("#{i}: native {got:?} reference {}", case["r"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}
