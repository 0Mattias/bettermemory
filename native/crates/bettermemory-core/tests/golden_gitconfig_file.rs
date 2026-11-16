//! Every case of `gitconfig_file.jsonl` (POSIX only: links, FIFOs, mode
//! 000, directories opened): `read_git_config`, `_config_bytes` (plain
//! and gentle), the open's kind, `access` and `lstat`, against the native
//! readers over the case's tree in memory, and again over the same tree
//! written to a scratch directory and read through `StdFs`.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, StdFs};
use common::gitconfig_file::{answers, content, modes, relabel, same, target, world, ROOT};
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

const FILE: &str = "gitconfig_file.jsonl";

fn cases() -> Vec<Value> {
    let lines = jsonl(FILE);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    cases
}

fn shown(case: &Value) -> String {
    let mut c = case.clone();
    c["f"] = Value::Null;
    c.to_string().chars().take(300).collect()
}

#[test]
fn gitconfig_file_every_case_in_memory() {
    let cases = cases();
    let ledger = Ledger::new("golden_gitconfig_file", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let fs = world(case, Flavour::Posix);
        let got = answers(
            &fs,
            &target(case, ROOT),
            same(case, ROOT).as_deref(),
            &relabel(ROOT),
        );
        if got != case["r"] {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                shown(case),
                case["r"]
            ));
        }
    }
    ledger.settle(cases.len(), &failures);
}

struct Scratch(std::path::PathBuf);

/// A new directory of this test's own: the process id, a process-wide
/// counter and the clock in its name, made with `create_dir`, which
/// fails where the name is taken.
fn fresh_dir(stem: &str) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("a clock")
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("{stem}-{}-{n}-{nanos}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    dir
}

impl Drop for Scratch {
    fn drop(&mut self) {
        unlock(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn unlock(dir: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            let is_dir = std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir());
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
            if is_dir {
                unlock(&path);
            }
        }
    }
}

fn mkfifo(path: &std::path::Path) -> bool {
    std::process::Command::new("mkfifo")
        .arg(path)
        .status()
        .is_ok_and(|s| s.success())
}

fn write_tree(case: &Value, root: &std::path::Path) -> bool {
    use std::os::unix::fs::{symlink, PermissionsExt};
    for d in case
        .get("d")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        std::fs::create_dir_all(root.join(d.as_str().unwrap())).unwrap();
    }
    for (rel, data) in case["f"].as_object().unwrap() {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content(data)).unwrap();
    }
    if let Some(hard) = case.get("h").and_then(Value::as_object) {
        for (rel, existing) in hard {
            std::fs::hard_link(root.join(existing.as_str().unwrap()), root.join(rel)).unwrap();
        }
    }
    if let Some(links) = case.get("l").and_then(Value::as_object) {
        for (rel, to) in links {
            symlink(to.as_str().unwrap(), root.join(rel)).unwrap();
        }
    }
    for p in case
        .get("p")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if !mkfifo(&root.join(p.as_str().unwrap())) {
            return false;
        }
    }
    // Deepest first, so a directory is still searchable while its
    // entries are set.
    for (rel, mode) in modes(case).into_iter().rev() {
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode)).unwrap();
    }
    true
}

#[test]
fn gitconfig_file_every_case_on_disk() {
    let cases = cases();
    let ledger = Ledger::new("golden_gitconfig_file_stdfs", FILE);
    let base = Scratch(fresh_dir("bm-gcfile"));
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = Scratch(base.0.join(i.to_string()));
        std::fs::create_dir(&dir.0).unwrap();
        if !write_tree(case, &dir.0) {
            failures.push(format!("#{i}: mkfifo could not run"));
            continue;
        }
        let root = dir.0.to_str().expect("a UTF-8 scratch path").to_owned();
        let got = answers(
            &StdFs,
            &target(case, &root),
            same(case, &root).as_deref(),
            &relabel(&root),
        );
        if got != case["r"] {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                shown(case),
                case["r"]
            ));
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}
