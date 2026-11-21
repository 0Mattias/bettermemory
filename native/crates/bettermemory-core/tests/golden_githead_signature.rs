//! Every query of `githead_signature.jsonl` (class i, POSIX only):
//! `entry_above` and `signature` against `githead::signature`, on disk
//! (`StdFs`) for every case that names no other owner or user, and in
//! memory (`MemFs`, owners as the case states them) for every case that
//! rewrites no file: a file in memory keeps its stamp through a rewrite,
//! where a disk gives it a new inode and ctime.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, Fs, StdFs};
use bettermemory_core::gitconfig::legacy::Stamp;
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Host, Owners};
use bettermemory_core::githead::signature::{entry_above, signature, First, Signature};
use common::githead_discovery::{
    entries, env_of, euid_of, foreign_of, replaced, text, unspelled, world, Owned, ME, MEM_ROOT,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "githead_signature.jsonl";

fn cases() -> (Vec<Value>, usize) {
    let lines = jsonl(FILE);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty(), "{FILE}: no case");
    let queries = lines[0]["queries"].as_u64().expect("a count of queries");
    (cases, usize::try_from(queries).unwrap())
}

fn b(data: &[u8]) -> Value {
    if data
        .iter()
        .all(|&c| (0x20..0x7f).contains(&c) && c != b'\\')
    {
        return json!(format!("={}", std::str::from_utf8(data).unwrap()));
    }
    json!(common::hex(data))
}

fn spelled(data: &[u8], root: &str) -> Value {
    b(&replaced(data, root.as_bytes(), b"<scratch>"))
}

fn head(data: Option<&Vec<u8>>) -> Value {
    match data {
        None => Value::Null,
        Some(d) if d.len() <= 256 => b(d),
        Some(d) => json!({"n": d.len(), "sha256": common::hex(&Sha256::digest(d))}),
    }
}

fn shown(got: Option<Stamp>, stamps: &mut Vec<Stamp>) -> Value {
    let Some(got) = got else {
        return Value::Null;
    };
    stamps.push(got);
    let eq = stamps.iter().position(|s| *s == got).unwrap();
    let mut out = json!({"t": got[4] >> 12, "eq": eq});
    if got[4] >> 12 == 0o10 {
        out["s"] = json!(got[2]);
    }
    out
}

fn sig_value(
    sig: Signature,
    root: &str,
    stamps: &mut Vec<Stamp>,
    sigs: &mut Vec<Signature>,
) -> Value {
    sigs.push(sig.clone());
    let eq = sigs.iter().position(|s| *s == sig).unwrap();
    let g = match &sig.first {
        First::Outside => Value::Null,
        First::GitDir(d) => spelled(d, root),
        First::Undetermined(_) => json!("u"),
    };
    let env: Vec<Value> = [&sig.git_dir, &sig.git_work_tree, &sig.ceilings]
        .iter()
        .map(|v| v.as_ref().map_or(Value::Null, |v| spelled(v, root)))
        .collect();
    // In the reference's order: config, HEAD, the refs, packed-refs.
    let config = shown(sig.config, stamps);
    let head_file = shown(sig.head_file, stamps);
    let refs = sig.refs.map_or(Value::Null, |r| {
        Value::Array(r.into_iter().map(|s| shown(s, stamps)).collect())
    });
    let packed = shown(sig.packed, stamps);
    json!({
        "g": g,
        "h": head(sig.head.as_ref()),
        "c": config,
        "hf": head_file,
        "r": refs,
        "pk": packed,
        "env": env,
        "eq": eq,
    })
}

fn rewrite(path: &str) {
    let data = std::fs::read(path).unwrap();
    let lock = format!("{path}.lock");
    std::fs::write(&lock, data).unwrap();
    std::fs::rename(&lock, path).unwrap();
}

fn answers<F: Fs, O: Owners>(
    fs: &F,
    owners: &O,
    euid: u32,
    root: &str,
    case: &Value,
) -> Vec<Value> {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &git,
    };
    let host = Host { euid, owners };
    let env = env_of(case, root);
    let (mut stamps, mut sigs) = (Vec::new(), Vec::new());
    let mut out = Vec::new();
    for q in case["q"].as_array().unwrap() {
        let query = unspelled(q["p"].as_str().unwrap());
        let colon = query.iter().position(|&c| c == b':').unwrap();
        let (op, raw) = (&query[..colon], &query[colon + 1..]);
        let args: Vec<Vec<u8>> = raw
            .split(|&c| c == b'|')
            .map(|a| replaced(a, b"<root>", root.as_bytes()))
            .collect();
        let flags: &[u8] = args.get(1).map_or(b"", Vec::as_slice);
        let v = match op {
            b"E" => entry_above(&ctx, &env, &args[0]).map_or(json!("declined"), |v| json!(v)),
            b"G" => match signature(&rule, &host, &env, &args[0], !flags.contains(&b'n')) {
                Ok(sig) => sig_value(sig, root, &mut stamps, &mut sigs),
                Err(class) => json!({"raised": class}),
            },
            b"W" => {
                rewrite(&format!("{root}/{}", std::str::from_utf8(raw).unwrap()));
                Value::Null
            }
            other => panic!("op {other:?}"),
        };
        out.push(json!({"p": q["p"], "v": v}));
    }
    out
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

fn rewrites(case: &Value) -> bool {
    case["q"]
        .as_array()
        .unwrap()
        .iter()
        .any(|q| q["p"].as_str().unwrap().starts_with("=W:"))
}

#[test]
fn githead_signature_every_case_in_memory() {
    let (cases, _) = cases();
    let ledger = Ledger::new("golden_githead_signature", FILE);
    let mut failures = Vec::new();
    let (mut checked, mut expected) = (0, 0);
    for (i, case) in cases.iter().enumerate().filter(|(_, c)| !rewrites(c)) {
        expected += case["q"].as_array().unwrap().len();
        let fs = world(case);
        let owners = Owned {
            fs: &fs,
            foreign: foreign_of(case, MEM_ROOT),
            owner: ME,
        };
        let got = answers(&fs, &owners, euid_of(case, ME), MEM_ROOT, case);
        checked += compare(&got, case, i, &mut failures);
    }
    assert!(checked > 0, "no query checked");
    assert_eq!(checked, expected);
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

fn write_tree(case: &Value, root: &std::path::Path) {
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
    let mut modes = entries(case, "m");
    modes.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    for (rel, m) in modes {
        let mode = u32::from_str_radix(m.as_str().unwrap(), 8).unwrap();
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode)).unwrap();
    }
}

#[test]
fn githead_signature_every_plain_case_on_disk() {
    use std::os::unix::fs::MetadataExt;
    let (cases, queries) = cases();
    let ledger = Ledger::new("golden_githead_signature_stdfs", FILE);
    let base = std::env::temp_dir().join(format!("bm-ghsig-{}", std::process::id()));
    std::fs::create_dir(&base).unwrap();
    let base = Scratch(std::fs::canonicalize(&base).unwrap());
    for dir in base.0.ancestors() {
        for name in [".git", "HEAD"] {
            assert!(
                std::fs::symlink_metadata(dir.join(name)).is_err(),
                "a {name} entry stands above the scratch directory"
            );
        }
    }
    let me = std::fs::metadata(&base.0).unwrap().uid();
    assert_ne!(
        me, 0,
        "run as a user: root reads what the fixture's user may not"
    );
    let mut failures = Vec::new();
    let (mut checked, mut expected, mut all) = (0, 0, 0);
    for (i, case) in cases.iter().enumerate() {
        all += case["q"].as_array().unwrap().len();
        if ["o", "euid", "mnt"].iter().any(|k| case.get(*k).is_some()) {
            continue;
        }
        expected += case["q"].as_array().unwrap().len();
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        write_tree(case, &dir);
        let got = answers(&StdFs, &StdFs, me, dir.to_str().unwrap(), case);
        checked += compare(&got, case, i, &mut failures);
    }
    drop(base);
    assert_eq!(all, queries, "the header's count of queries");
    assert!(checked > 0, "no query checked on disk");
    assert_eq!(checked, expected);
    ledger.settle(checked, &failures);
}
