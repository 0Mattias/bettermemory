//! Every case of `location_git_readers_posix.jsonl`, POSIX only: the ref
//! tips, the content manifest and the head, over each tree in memory
//! (`fsdata::MemFs` for the walks, `githead::MemFs` for the head) and on
//! disk (`fsdata::StdFs`, `githead::StdFs`).

#![cfg(unix)]

mod common;

use bettermemory_core::capture::Kind;
use bettermemory_core::fsdata::{Flavour, Fs, PyErr, StdFs};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::githead::{self, RefFs};
use bettermemory_core::location_git::{manifest, ref_tips, resolve_head, Git};
use bettermemory_core::pyvalue::PyStr;
use common::location_git::{disk_tree, entries, mem_tree, ref_tree, show_manifest, Entry};
use common::parity::Ledger;
use serde_json::{json, Value};
use std::os::unix::ffi::OsStrExt;

const FILE: &str = "location_git_readers_posix.jsonl";

struct Root;

impl Process for Root {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        Ok(vec![u32::from(b'/')])
    }
}

fn git(git_dir: Vec<u8>, common: Vec<u8>) -> Git {
    Git {
        dotgit: None,
        git_dir,
        common_dir: common,
        root: None,
        kind: Kind::Primary,
    }
}

/// One case's answer over the given file systems, `base` the case's folder.
fn answer<F: Fs>(case: &Value, fs: &F, refs: &dyn RefFs, base: &[u8]) -> Value {
    let at = |rel: &str| [base, rel.as_bytes()].concat();
    match case["fn"].as_str().unwrap() {
        "tips" => json!(ref_tips(fs, &at("/g"))),
        "manifest" => {
            let mut root = at("/proj");
            if case.get("slash").is_some() {
                root.push(b'/');
            }
            show_manifest(&manifest(fs, Flavour::Posix, &Root, &root).expect("a manifest"))
        }
        "head" => {
            let gd = if case.get("w").is_some() {
                at("/g/worktrees/w")
            } else {
                at("/g")
            };
            let want = case.get("peel").and_then(Value::as_str).map(str::to_owned);
            let answer = case["a"][0].as_str().map(str::to_owned);
            let mut asked = None;
            let mut peel = |id: &str| {
                asked = Some(id.to_owned());
                answer.clone()
            };
            let (sha, branch) = resolve_head(refs, &git(gd, at("/g")), &mut peel);
            assert_eq!(asked, want, "the peel asked");
            json!([sha, branch])
        }
        other => panic!("a row of kind {other}"),
    }
}

fn folder(case: &Value) -> &'static str {
    if case["fn"] == "manifest" {
        "/proj"
    } else {
        "/g"
    }
}

fn cases() -> Vec<Value> {
    let lines = common::jsonl(FILE);
    assert_eq!(lines[0]["cases"].as_u64(), Some(lines.len() as u64 - 1));
    lines[1..].to_vec()
}

#[test]
fn location_git_readers_posix_every_case_in_memory() {
    let cases = cases();
    let ledger = Ledger::new("golden_location_git_readers_posix", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let items: Vec<Entry> = entries(&case["t"]);
        let fs = mem_tree(&items, format!("/s{}", folder(case)).as_bytes());
        let refs = ref_tree(
            &items
                .iter()
                .filter(|e| !matches!(e, Entry::Locked(_)))
                .cloned()
                .collect::<Vec<_>>(),
            b"s/g",
        );
        let base: &[u8] = if case["fn"] == "head" { b"s" } else { b"/s" };
        let got = answer(case, &fs, &refs, base);
        if got != case["a"] {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                case["note"], case["a"]
            ));
        }
    }
    ledger.settle(cases.len(), &failures);
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
                if std::fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) {
                    continue;
                }
                let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
                if path.is_dir() {
                    open_up(&path);
                }
            }
        }
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// The generator's `refuse_root`: root reads mode-000 entries, so the
/// rows of unreadable entries hold only for another user.
fn refuse_root(base: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    let probe = base.join("probe");
    std::fs::create_dir(&probe).unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o000)).unwrap();
    let listed = std::fs::read_dir(&probe).is_ok();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::remove_dir(&probe).unwrap();
    assert!(
        !listed,
        "a mode-000 folder can be listed here (running as root?)"
    );
}

#[test]
fn location_git_readers_posix_every_case_on_disk() {
    let cases = cases();
    let ledger = Ledger::new("golden_location_git_readers_posix_stdfs", FILE);
    let dir = std::env::temp_dir().join(format!("bm-lgread-{}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let base = Scratch(std::fs::canonicalize(&dir).unwrap());
    refuse_root(&base.0);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let here = base.0.join(i.to_string());
        disk_tree(&entries(&case["t"]), &here.join(&folder(case)[1..]));
        let got = answer(case, &StdFs, &githead::StdFs, here.as_os_str().as_bytes());
        if got != case["a"] {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                case["note"], case["a"]
            ));
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}
