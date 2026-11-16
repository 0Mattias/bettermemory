//! Every query of `fs_realpath.jsonl` (class i) and
//! `fs_realpath_python.jsonl` (class ii, Python: trees holding a link
//! loop), POSIX only: `os.path.realpath` and `os.path.abspath` against
//! `fsdata::realpath` over the case's tree in memory (`MemFs`), and again
//! over the same tree written to a scratch directory (`StdFs`).

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::realpath::{abspath, realpath, Unresolved};
use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, PyErr, StdFs};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const PLAIN: &str = "fs_realpath.jsonl";
const LOOPS: &str = "fs_realpath_python.jsonl";
/// The case's directory in memory.
const MEM_ROOT: &str = "/<scratch>";

fn cases(file: &str) -> Vec<Value> {
    let lines = jsonl(file);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    cases
}

fn b(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&c| (0x20..0x7f).contains(&c) && c != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).expect("ASCII"));
    }
    common::hex(data)
}

fn unspelled(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => common::unhex(text),
    }
}

fn replaced(data: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut rest = data;
    while !rest.is_empty() {
        if rest.starts_with(from) {
            out.extend_from_slice(to);
            rest = &rest[from.len()..];
        } else {
            out.push(rest[0]);
            rest = &rest[1..];
        }
    }
    out
}

fn entries<'a>(case: &'a Value, key: &str) -> Vec<(&'a str, &'a Value)> {
    match case.get(key) {
        Some(Value::Object(map)) => map.iter().map(|(k, v)| (k.as_str(), v)).collect(),
        Some(Value::Array(list)) => list
            .iter()
            .map(|v| (v.as_str().unwrap(), &Value::Null))
            .collect(),
        _ => Vec::new(),
    }
}

fn target_of(value: &Value, root: &str) -> String {
    value.as_str().unwrap().replace("<root>", root)
}

/// The case's answers through `fs`, its directory being `root`.
fn answers<F: Fs>(fs: &F, case: &Value, root: &str) -> Value {
    let label = |answer: Result<Vec<u8>, Unresolved>| match answer {
        Ok(path) => json!(b(&replaced(&path, root.as_bytes(), b"<scratch>"))),
        Err(Unresolved::Raised(PyErr::Value)) => json!({"raised": "ValueError"}),
        Err(other) => json!({"native": format!("{other:?}")}),
    };
    let queries = case["q"].as_array().unwrap();
    Value::Array(
        queries
            .iter()
            .map(|q| {
                let query = unspelled(q["p"].as_str().unwrap());
                let path = replaced(&query, b"<root>", root.as_bytes());
                json!({
                    "p": q["p"],
                    "r": label(realpath(fs, Flavour::Posix, &path, root.as_bytes())),
                    "a": label(abspath(Flavour::Posix, &path, root.as_bytes())),
                })
            })
            .collect(),
    )
}

fn world(case: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
    fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
    fs.insert(b"/dev/null", Node::Device);
    for (rel, text) in entries(case, "f") {
        fs.insert(
            &at(rel),
            Node::File(text.as_str().unwrap().as_bytes().to_vec()),
        );
    }
    for (rel, _) in entries(case, "d") {
        fs.insert(&at(rel), Node::Dir);
    }
    for (rel, to) in entries(case, "l") {
        fs.insert(&at(rel), Node::Link(target_of(to, MEM_ROOT).into_bytes()));
    }
    for (rel, _) in entries(case, "p") {
        fs.insert(&at(rel), Node::Fifo);
    }
    for (rel, mode) in entries(case, "m") {
        fs.set_mode(
            &at(rel),
            u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap(),
        );
    }
    fs
}

fn in_memory(test: &str, file: &str) {
    let cases = cases(file);
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = answers(&world(case), case, MEM_ROOT);
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}

#[test]
fn fs_realpath_every_case_in_memory() {
    in_memory("golden_fs_realpath", PLAIN);
}

#[test]
fn fs_realpath_loops_in_memory() {
    in_memory("golden_fs_realpath_loops", LOOPS);
}

struct Scratch(std::path::PathBuf);

/// A new directory of this test's own: the process id and a process-wide
/// counter in its name, made with `create_dir`, which fails where the
/// name is taken; resolved, so no link above it is in an answer.
fn fresh_dir(stem: &str) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("{stem}-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
}

impl Drop for Scratch {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt;
        fn open_up(dir: &std::path::Path) {
            let Ok(entries) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in entries.flatten() {
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

fn write_tree(case: &Value, root: &std::path::Path) -> bool {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let real = root.to_str().unwrap();
    for (rel, _) in entries(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
    }
    for (rel, text) in entries(case, "f") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text.as_str().unwrap()).unwrap();
    }
    for (rel, to) in entries(case, "l") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        symlink(target_of(to, real), path).unwrap();
    }
    for (rel, _) in entries(case, "p") {
        let made = std::process::Command::new("mkfifo")
            .arg(root.join(rel))
            .status()
            .is_ok_and(|s| s.success());
        if !made {
            return false;
        }
    }
    let mut modes = entries(case, "m");
    modes.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    for (rel, mode) in modes {
        let bits = u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap();
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(bits)).unwrap();
    }
    true
}

fn on_disk(test: &str, file: &str) {
    let cases = cases(file);
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir("bm-fsreal"));
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        if !write_tree(case, &dir) {
            failures.push(format!("#{i}: mkfifo could not run"));
            continue;
        }
        let got = answers(&StdFs, case, dir.to_str().unwrap());
        if got != case["q"] {
            failures.push(format!("#{i}: native {got} reference {}", case["q"]));
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}

#[test]
fn fs_realpath_every_case_on_disk() {
    on_disk("golden_fs_realpath_stdfs", PLAIN);
}

#[test]
fn fs_realpath_loops_on_disk() {
    on_disk("golden_fs_realpath_loops_stdfs", LOOPS);
}
