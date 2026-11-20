//! `origin_gitfiles_machine.jsonl` (class i, POSIX only): `_read_prefix`
//! and `relative_to_root` over each case's tree in memory (`MemFs`) and
//! written to a scratch directory (`StdFs`). And
//! `origin_gitfiles_darwin.jsonl` (class iii, gated on the platform):
//! `_spelled_as_git` and `relative_to_root` over a tree on disk, asked by
//! names that differ from the stored ones by case and Unicode form.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, Refused, StdFs};
use bettermemory_core::origin_gitfiles::machine::{
    read_prefix, relative_to_root, spelled_as_git, NotRelative,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const MACHINE: &str = "origin_gitfiles_machine.jsonl";
const DARWIN: &str = "origin_gitfiles_darwin.jsonl";
const MEM_ROOT: &str = "/<scratch>";

fn cases_of(file: &str) -> Vec<Value> {
    let lines = common::jsonl(file);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty(), "{file}: no case");
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

fn bytes_answer(data: &[u8]) -> Value {
    if data.len() <= 64 {
        return json!({"a": b(data)});
    }
    let digest: String = Sha256::digest(data)
        .iter()
        .map(|x| format!("{x:02x}"))
        .collect();
    json!({"z": data.len(), "h": digest})
}

/// One query's answer through `fs`, the case's directory being `root`.
fn answer<F: Fs>(fs: &F, q: &Value, root: &str, darwin: bool) -> Value {
    let path = |v: &Value| replaced(&unspelled(v.as_str().unwrap()), b"<root>", root.as_bytes());
    let spell = |p: &[u8]| json!(b(&replaced(p, root.as_bytes(), b"<root>")));
    let mut out = serde_json::Map::new();
    if let Some(pair) = q.get("rel") {
        out.insert("rel".into(), pair.clone());
        match relative_to_root(fs, &path(&pair[0]), &path(&pair[1])) {
            Ok(rel) => {
                out.insert("a".into(), spell(&rel));
            }
            Err(NotRelative::ValueError(m)) => {
                out.insert("e".into(), json!("ValueError"));
                out.insert("m".into(), spell(&m));
            }
            Err(NotRelative::NotPorted) => panic!("samestat is ported on POSIX"),
        }
    } else if let Some(pre) = q.get("pre") {
        out.insert("pre".into(), pre.clone());
        let skip = pre[1].as_u64().unwrap() as usize;
        let length = pre[2].as_u64().unwrap() as usize;
        let got = fs
            .open(&path(&pre[0]))
            .map_err(Refused::Raised)
            .and_then(|mut f| {
                if skip > 0 {
                    fs.read(&mut f, skip).map_err(Refused::Raised)?;
                }
                read_prefix(fs, &mut f, length)
            });
        match got {
            Ok(data) => out.extend(bytes_answer(&data).as_object().unwrap().clone()),
            Err(Refused::Raised(class)) => {
                out.insert("e".into(), json!(class.name()));
            }
            Err(Refused::NotOffered) => panic!("rewind is offered"),
        }
    } else if let Some(p) = q.get("sp") {
        out.insert("sp".into(), p.clone());
        match spelled_as_git(fs, &path(p), darwin) {
            Ok(Some(spelled)) => {
                out.insert("a".into(), spell(&spelled));
            }
            Ok(None) => {
                out.insert("n".into(), json!(true));
            }
            Err(Refused::Raised(class)) => {
                out.insert("e".into(), json!(class.name()));
            }
            Err(Refused::NotOffered) => panic!("kernel_path is offered on macOS"),
        }
    } else {
        panic!("unknown query {q}");
    }
    Value::Object(out)
}

fn world(case: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
    fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
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
        let target = to.as_str().unwrap().replace("<root>", MEM_ROOT);
        fs.insert(&at(rel), Node::Link(target.into_bytes()));
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

struct Scratch(std::path::PathBuf);

impl Scratch {
    fn new(stem: &str) -> Scratch {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("{stem}-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).expect("a new scratch directory");
        Scratch(std::fs::canonicalize(&dir).expect("a resolved scratch directory"))
    }
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

/// The case's tree under `root`; false where a FIFO cannot be made.
fn write_tree(case: &Value, root: &std::path::Path) -> bool {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let mode = |p: &std::path::Path, m: u32| {
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap()
    };
    for (rel, _) in entries(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
    }
    for (rel, text) in entries(case, "f") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, text.as_str().unwrap()).unwrap();
    }
    let real = root.to_str().unwrap();
    for (rel, to) in entries(case, "l") {
        symlink(to.as_str().unwrap().replace("<root>", real), root.join(rel)).unwrap();
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
    for (rel, m) in entries(case, "m") {
        mode(
            &root.join(rel),
            u32::from_str_radix(m.as_str().unwrap(), 8).unwrap(),
        );
    }
    true
}

/// Where a mode-000 folder can be searched (root), the fixture's answers
/// are not this process's.
fn running_as_root(base: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    let probe = base.join("probe");
    std::fs::create_dir_all(probe.join("in")).unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o000)).unwrap();
    let seen = std::fs::metadata(probe.join("in")).is_ok();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::remove_dir_all(&probe).unwrap();
    seen
}

fn mismatches<F: Fs>(fs: &F, case: &Value, root: &str, darwin: bool) -> Vec<String> {
    case["q"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|q| {
            let got = answer(fs, q, root, darwin);
            (&got != q).then(|| format!("native {got} reference {q}"))
        })
        .collect()
}

#[test]
fn machine_every_query_in_memory() {
    let cases = cases_of(MACHINE);
    let ledger = Ledger::new("golden_origin_gitfiles_machine", MACHINE);
    let mut failures = Vec::new();
    let mut total = 0;
    for case in &cases {
        total += case["q"].as_array().unwrap().len();
        failures.extend(mismatches(&world(case), case, MEM_ROOT, false));
    }
    assert!(total > 0, "no query checked");
    ledger.settle(total, &failures);
}

#[test]
fn machine_every_query_on_disk() {
    let cases = cases_of(MACHINE);
    let scratch = Scratch::new("bm-ogf-machine");
    if running_as_root(&scratch.0) {
        eprintln!("SKIPPED: a mode-000 folder is searchable here (root)");
        return;
    }
    let mut failures = Vec::new();
    let mut total = 0;
    for (i, case) in cases.iter().enumerate() {
        let root = scratch.0.join(i.to_string());
        std::fs::create_dir(&root).unwrap();
        assert!(write_tree(case, &root), "mkfifo is on PATH");
        total += case["q"].as_array().unwrap().len();
        failures.extend(mismatches(&StdFs, case, root.to_str().unwrap(), false));
    }
    assert!(total > 0, "no query checked");
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn darwin_every_query_on_disk() {
    let cases = cases_of(DARWIN);
    let mut ledger = Ledger::new("golden_origin_gitfiles_darwin", DARWIN);
    let scratch = Scratch::new("bm-ogf-darwin");
    if running_as_root(&scratch.0) {
        eprintln!("SKIPPED: a mode-000 folder is searchable here (root)");
        return;
    }
    // The fixture asks by names the scratch must fold, as the generator
    // checks before it writes them.
    if cfg!(target_os = "macos") {
        let probe = scratch.0.join("Fold");
        std::fs::create_dir(&probe).unwrap();
        let folds = scratch.0.join("fold").is_dir();
        std::fs::remove_dir(&probe).unwrap();
        if !folds {
            eprintln!("SKIPPED: the scratch file system is case-sensitive");
            return;
        }
    }
    let mut looked = 0;
    let mut in_file = 0;
    for (i, case) in cases.iter().enumerate() {
        let root = scratch.0.join(i.to_string());
        std::fs::create_dir(&root).unwrap();
        assert!(write_tree(case, &root), "the tree is written");
        let real = root.to_str().unwrap();
        for q in case["q"].as_array().unwrap() {
            in_file += 1;
            looked += 1;
            let got = answer(&StdFs, q, real, cfg!(target_os = "macos"));
            assert!(
                !ledger.differs_fatally(format!("{q}"), &got == q),
                "native {got} reference {q}"
            );
        }
    }
    assert_eq!(looked, in_file);
    ledger.finish(in_file);
}
