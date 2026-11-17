//! Every case of `gitfile_leaves_posix.jsonl` (POSIX only: links and
//! mode-000 files): `_gitfile_target` and `_small_text` of the case's
//! tree, read through `MemFs` and again written to a scratch directory and
//! read through `StdFs`.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, Fs, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::gitfile_rule::{gitfile_target, small_text};
use common::gitconfig_file::{content, modes, target, world, ROOT};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "gitfile_leaves_posix.jsonl";

fn cases() -> Vec<Value> {
    let lines = jsonl(FILE);
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

/// The answer as recorded: the root relabelled, then spelled.
fn out(data: Option<Vec<u8>>, root: &[u8]) -> Value {
    let Some(data) = data else {
        return Value::Null;
    };
    let mut relabelled = Vec::with_capacity(data.len());
    let mut rest = &data[..];
    while !rest.is_empty() {
        if rest.starts_with(root) && !root.is_empty() {
            relabelled.extend_from_slice(ROOT.as_bytes());
            rest = &rest[root.len()..];
        } else {
            relabelled.push(rest[0]);
            rest = &rest[1..];
        }
    }
    if relabelled.len() > 256 {
        let digest: String = Sha256::digest(&relabelled)
            .iter()
            .map(|x| format!("{x:02x}"))
            .collect();
        return json!({"n": relabelled.len(), "sha256": digest});
    }
    json!(b(&relabelled))
}

fn answers<F: Fs>(fs: &F, root: &str, case: &Value) -> Value {
    let path = target(case, root);
    json!({
        "gt": out(gt(fs, &path, Flavour::Posix), root.as_bytes()),
        "st": out(st(fs, &path).map(String::into_bytes), root.as_bytes()),
    })
}

fn shown(case: &Value) -> String {
    let mut c = case.clone();
    c["f"] = Value::Null;
    c.to_string().chars().take(300).collect()
}

/// `_gitfile_target` of an absolute path, with `/` as the working
/// directory (never consulted).
fn gt<F: Fs + ?Sized>(fs: &F, path: &[u8], flavour: Flavour) -> Option<Vec<u8>> {
    let ctx = Ctx {
        fs,
        flavour,
        cwd: b"/",
    };
    gitfile_target(&ctx, path).expect("an absolute path is ported")
}

/// `_small_text` of an absolute path.
fn st<F: Fs + ?Sized>(fs: &F, path: &[u8]) -> Option<String> {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    small_text(&ctx, path).expect("an absolute path is ported")
}

#[test]
fn gitfile_leaves_posix_every_case_in_memory() {
    let cases = cases();
    let ledger = Ledger::new("golden_gitfile_leaves_posix", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let fs = world(case, Flavour::Posix);
        let got = answers(&fs, ROOT, case);
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
/// counter and the clock in its name, made with `create_dir`, which fails
/// where the name is taken.
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

fn write_tree(case: &Value, root: &std::path::Path) {
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
            let path = root.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            symlink(to.as_str().unwrap(), path).unwrap();
        }
    }
    // Deepest first, so a directory is still searchable while its entries
    // are set.
    for (rel, mode) in modes(case).into_iter().rev() {
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode)).unwrap();
    }
}

#[test]
fn gitfile_leaves_posix_every_case_on_disk() {
    let cases = cases();
    let ledger = Ledger::new("golden_gitfile_leaves_posix_stdfs", FILE);
    let base = Scratch(fresh_dir("bm-gfleaves"));
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = Scratch(base.0.join(i.to_string()));
        std::fs::create_dir(&dir.0).unwrap();
        write_tree(case, &dir.0);
        let root = dir.0.to_str().expect("a UTF-8 scratch path").to_owned();
        let got = answers(&StdFs, &root, case);
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
