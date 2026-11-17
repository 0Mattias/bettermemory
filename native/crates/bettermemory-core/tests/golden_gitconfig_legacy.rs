//! Every case of `gitconfig_legacy.jsonl` and `gitconfig_legacy_python.jsonl`
//! (trees holding a link loop), POSIX only: the legacy remote names, each
//! remote read in turn, the default branch, `origin`'s stamps and the files
//! read with their stamps, against `gitconfig::legacy`, over the case's
//! tree in memory (`MemFs`) and written to a scratch directory (`StdFs`).
//! A stamp case's change (`ch`) is made on disk only, where `unchanged`
//! is compared; in memory every other field is.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, StdFs};
use bettermemory_core::gitconfig::legacy::{
    default_branch, file_stamp, legacy_names, legacy_remote, legacy_remote_stamps, unchanged,
    Against, Files, Halt, Legacy, Stamp, UNSTATED,
};
use bettermemory_core::gitconfig::tree::ConfigValues;
use bettermemory_core::gitconfig::Name;
use common::gitconfig_file::content;
use common::gitconfig_tree::spelled;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const PLAIN: &str = "gitconfig_legacy.jsonl";
const LOOPS: &str = "gitconfig_legacy_python.jsonl";
const MEM_ROOT: &str = "/<scratch>";

fn cases(file: &str) -> Vec<Value> {
    let lines = jsonl(file);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    cases
}

fn unspelled(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => common::unhex(text),
    }
}

fn url(data: &[u8]) -> Value {
    if data.len() > 120 {
        let digest: String = Sha256::digest(data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        return json!({"n": data.len(), "sha256": digest});
    }
    json!(spelled(data))
}

fn stamp(s: Option<Stamp>) -> Value {
    match s {
        None => Value::Null,
        Some(s) if s == UNSTATED => json!("u"),
        Some(s) => json!([format!("{:o}", s[4]), s[2] as u64]),
    }
}

fn obj<'a>(case: &'a Value, key: &str) -> Vec<(&'a String, &'a Value)> {
    case.get(key)
        .and_then(Value::as_object)
        .map(|m| m.iter().collect())
        .unwrap_or_default()
}

fn list<'a>(case: &'a Value, key: &str) -> Vec<&'a str> {
    case.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|v| v.as_str().unwrap()).collect())
        .unwrap_or_default()
}

/// Every path the case makes with its mode: 644 for a file or FIFO, 755
/// for a folder and each file's parents, then the case's own.
fn all_modes(case: &Value) -> Vec<(String, u32)> {
    let mut made: Vec<(String, u32)> = Vec::new();
    let mut set = |rel: &str, mode: u32, keep: bool| match made.iter_mut().find(|(r, _)| r == rel) {
        Some(entry) => {
            if !keep {
                entry.1 = mode;
            }
        }
        None => made.push((rel.to_owned(), mode)),
    };
    let parents = |rel: &str, set: &mut dyn FnMut(&str, u32, bool)| {
        let parts: Vec<&str> = rel.split('/').collect();
        for i in 1..parts.len() {
            set(&parts[..i].join("/"), 0o755, true);
        }
    };
    for (rel, _) in obj(case, "f") {
        parents(rel, &mut set);
        set(rel, 0o644, false);
    }
    for rel in list(case, "d") {
        parents(rel, &mut set);
        set(rel, 0o755, false);
    }
    for (rel, _) in obj(case, "l") {
        parents(rel, &mut set);
    }
    for rel in list(case, "p") {
        parents(rel, &mut set);
        set(rel, 0o644, false);
    }
    for (rel, mode) in obj(case, "m") {
        set(
            rel,
            u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap(),
            false,
        );
    }
    made.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    made
}

fn world(case: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
    fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
    for rel in list(case, "d") {
        fs.insert(&at(rel), Node::Dir);
    }
    for (rel, data) in obj(case, "f") {
        fs.insert(&at(rel), Node::File(content(data)));
    }
    for (rel, to) in obj(case, "l") {
        fs.insert(
            &at(rel),
            Node::Link(to.as_str().unwrap().as_bytes().to_vec()),
        );
    }
    for rel in list(case, "p") {
        fs.insert(&at(rel), Node::Fifo);
    }
    for (rel, mode) in all_modes(case) {
        fs.set_mode(&at(&rel), mode);
    }
    fs
}

/// The native answer; `change` makes the stamp case's change.
fn answer<F: Fs>(fs: &F, case: &Value, root: &str, change: &dyn Fn(&str)) -> Value {
    let label = |bytes: &[u8]| -> String {
        let text = bytes
            .windows(root.len())
            .position(|w| w == root.as_bytes())
            .map_or_else(
                || bytes.to_vec(),
                |at| [&bytes[..at], b"<scratch>", &bytes[at + root.len()..]].concat(),
            );
        spelled(&text)
    };
    let k = &case["k"];
    let mut config = ConfigValues::default();
    if let Some(values) = k.get("db").and_then(Value::as_array) {
        let name = Name {
            section: "init".into(),
            subsection: None,
            key: "defaultbranch".into(),
        };
        let values = values.iter().map(|v| v.as_str().map(unspelled)).collect();
        config.values.push((name, values));
    }
    let test_name = k.get("tn").and_then(Value::as_str).map(str::as_bytes);
    let default = default_branch(test_name, [&config]);
    let against = Against {
        resets: k["rs"].as_bool(),
        default: default.clone(),
    };
    let dir = root.as_bytes();
    let mut files: Files = Vec::new();
    let mut out = json!({});
    match legacy_names(fs, Flavour::Posix, dir, &mut files) {
        Err(Halt::Declines(why)) => out["nm"] = json!({"d": label(&why)}),
        Err(Halt::NotPorted) => panic!("a POSIX stamp is held"),
        Ok(names) => {
            out["nm"] = json!(names.iter().map(|n| spelled(n)).collect::<Vec<_>>());
            let mut read = Vec::new();
            for name in &names {
                let got = legacy_remote(fs, Flavour::Posix, dir, name, &mut files, &against);
                let shown = match got {
                    Err(Halt::Declines(why)) => {
                        read.push(json!([spelled(name), {"d": label(&why)}]));
                        break;
                    }
                    Err(Halt::NotPorted) => panic!("a POSIX stamp is held"),
                    Ok(Legacy::Unknown) => {
                        read.push(json!([spelled(name), "unknown"]));
                        break;
                    }
                    Ok(Legacy::Dies) => Value::Null,
                    Ok(Legacy::Urls(urls)) => {
                        json!(urls.iter().map(|u| url(u)).collect::<Vec<_>>())
                    }
                };
                read.push(json!([spelled(name), shown]));
            }
            out["rm"] = json!(read);
        }
    }
    out["db"] = default
        .as_deref()
        .map_or(Value::Null, |d| json!(spelled(d)));
    let st = legacy_remote_stamps(fs, Flavour::Posix, dir, b"origin").expect("held");
    out["st"] = json!(st.iter().map(|s| stamp(*s)).collect::<Vec<_>>());
    out["fl"] = json!(files
        .iter()
        .map(|(p, s)| json!([label(p), stamp(*s)]))
        .collect::<Vec<_>>());
    if case.get("ch").is_some() {
        let x = format!("{root}/x").into_bytes();
        let mut stamped = files.clone();
        stamped.push((x.clone(), file_stamp(fs, &x).expect("held")));
        change(root);
        out["u"] = json!(unchanged(fs, &stamped).expect("held"));
    }
    out
}

fn in_memory(test: &str, file: &str) {
    let cases = cases(file);
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = answer(&world(case), case, MEM_ROOT, &|_| {});
        let mut want = case["r"].clone();
        let mut got = got;
        if let Some(w) = want.as_object_mut() {
            w.remove("u");
        }
        if let Some(g) = got.as_object_mut() {
            g.remove("u");
        }
        if got != want {
            failures.push(format!("#{i}: native {got} reference {want}"));
        }
    }
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitconfig_legacy_every_case_in_memory() {
    in_memory("golden_gitconfig_legacy", PLAIN);
}

#[test]
fn gitconfig_legacy_loops_in_memory() {
    in_memory("golden_gitconfig_legacy_loops", LOOPS);
}

struct Scratch(std::path::PathBuf);

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
    for rel in list(case, "d") {
        std::fs::create_dir_all(root.join(rel)).unwrap();
    }
    for (rel, data) in obj(case, "f") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content(data)).unwrap();
    }
    for (rel, to) in obj(case, "l") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        symlink(to.as_str().unwrap(), path).unwrap();
    }
    for rel in list(case, "p") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let made = std::process::Command::new("mkfifo")
            .arg(&path)
            .status()
            .is_ok_and(|s| s.success());
        if !made {
            return false;
        }
    }
    for (rel, mode) in all_modes(case) {
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode)).unwrap();
    }
    true
}

/// The stamp case's change to `x`, as the generator makes it.
fn change(case: &Value, root: &str) {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let Some(op) = case.get("ch").and_then(Value::as_str) else {
        return;
    };
    let x = std::path::Path::new(root).join("x");
    let mode = |p: &std::path::Path, m: u32| {
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap();
    };
    match op {
        "none" => {}
        "size" => {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new().append(true).open(&x).unwrap();
            f.write_all(b"y").unwrap();
        }
        "mtime" => {
            let f = std::fs::OpenOptions::new().write(true).open(&x).unwrap();
            let at = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1);
            f.set_times(std::fs::FileTimes::new().set_accessed(at).set_modified(at))
                .unwrap();
        }
        "replace" => {
            let new = std::path::Path::new(root).join("x.new");
            let meta = std::fs::metadata(&x).unwrap();
            std::fs::write(&new, std::fs::read(&x).unwrap()).unwrap();
            mode(&new, meta.permissions().mode() & 0o7777);
            let f = std::fs::OpenOptions::new().write(true).open(&new).unwrap();
            f.set_times(
                std::fs::FileTimes::new()
                    .set_accessed(meta.accessed().unwrap())
                    .set_modified(meta.modified().unwrap()),
            )
            .unwrap();
            drop(f);
            std::fs::rename(&new, &x).unwrap();
        }
        "mode" => mode(&x, 0o600),
        "remove" => {
            let meta = std::fs::symlink_metadata(&x).unwrap();
            if meta.is_dir() {
                std::fs::remove_dir_all(&x).unwrap();
            } else {
                std::fs::remove_file(&x).unwrap();
            }
        }
        "create" => {
            std::fs::write(&x, b"URL: u\n").unwrap();
            mode(&x, 0o644);
        }
        "entry" => std::fs::write(x.join("new"), b"").unwrap(),
        "hardlink" => std::fs::hard_link(&x, std::path::Path::new(root).join("x.link")).unwrap(),
        "write" => {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new().append(true).open(&x).unwrap();
            f.write_all(b"y").unwrap();
        }
        "retarget" => {
            std::fs::remove_file(&x).unwrap();
            symlink("b", &x).unwrap();
        }
        other => panic!("no change {other}"),
    }
}

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

fn on_disk(test: &str, file: &str) {
    use std::os::unix::fs::PermissionsExt;
    let cases = cases(file);
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir("bm-gclegacy"));
    refuse_root(&base.0);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        if !write_tree(case, &dir) {
            failures.push(format!("#{i}: mkfifo could not run"));
            continue;
        }
        let root = dir.to_str().unwrap();
        let got = answer(&StdFs, case, root, &|r| change(case, r));
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitconfig_legacy_every_case_on_disk() {
    on_disk("golden_gitconfig_legacy_stdfs", PLAIN);
}

#[test]
fn gitconfig_legacy_loops_on_disk() {
    on_disk("golden_gitconfig_legacy_loops_stdfs", LOOPS);
}
