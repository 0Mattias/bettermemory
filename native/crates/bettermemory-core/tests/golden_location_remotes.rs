//! Every case of `location_remotes.jsonl`, POSIX only: the config files a
//! checkout reads for its remotes and their reads, against
//! `gitconfig::remotes::gather`, over the case's tree in memory (`MemFs`
//! for the config, `githead::MemFs` for HEAD) and written to a scratch
//! directory (`StdFs`, `githead::StdFs`).

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node, StdFs};
use bettermemory_core::gitconfig::legacy::{Stamp, UNSTATED};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{gather, Env, Inputs, Outcome, Sources};
use bettermemory_core::gitconfig::tree::{Branch, ConfigValues, Values};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::githead;
use common::gitconfig_file::content;
use common::gitconfig_tree::spelled;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "location_remotes.jsonl";
const MEM_ROOT: &str = "/<scratch>";

fn cases() -> Vec<Value> {
    let lines = jsonl(FILE);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    cases
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

/// Every path the case makes with its mode (644 a file, 755 a folder and
/// each parent), then the case's own; deepest first.
fn all_modes(case: &Value) -> Vec<(String, u32)> {
    let mut made: Vec<(String, u32)> = Vec::new();
    fn set(made: &mut Vec<(String, u32)>, rel: &str, mode: u32, keep: bool) {
        match made.iter_mut().find(|(r, _)| r == rel) {
            Some(entry) if !keep => entry.1 = mode,
            Some(_) => {}
            None => made.push((rel.to_owned(), mode)),
        }
    }
    fn parents(made: &mut Vec<(String, u32)>, rel: &str) {
        let parts: Vec<&str> = rel.split('/').collect();
        for i in 1..parts.len() {
            set(made, &parts[..i].join("/"), 0o755, true);
        }
    }
    for rel in list(case, "d") {
        parents(&mut made, rel);
        set(&mut made, rel, 0o755, false);
    }
    for (rel, _) in obj(case, "f") {
        parents(&mut made, rel);
        set(&mut made, rel, 0o644, false);
    }
    for (rel, _) in obj(case, "l") {
        parents(&mut made, rel);
    }
    for rel in list(case, "p") {
        parents(&mut made, rel);
        set(&mut made, rel, 0o644, false);
    }
    for (rel, mode) in obj(case, "m") {
        set(
            &mut made,
            rel,
            u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap(),
            false,
        );
    }
    made.sort_by_key(|(rel, _)| std::cmp::Reverse(rel.matches('/').count()));
    made
}

fn worlds(case: &Value) -> (MemFs, githead::MemFs) {
    let mut fs = MemFs::new(Flavour::Posix);
    let mut refs = githead::MemFs::new(githead::Flavour::Posix);
    let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
    // githead's MemFs names a path by its components, no leading slash.
    let rat = |rel: &str| format!("{}/{rel}", &MEM_ROOT[1..]).into_bytes();
    fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
    let modes = all_modes(case);
    let unreadable = |rel: &str| modes.iter().any(|(r, m)| r == rel && m & 0o400 == 0);
    for rel in list(case, "d") {
        fs.insert(&at(rel), Node::Dir);
        refs.insert(&rat(rel), githead::fs::Node::Dir);
    }
    for (rel, data) in obj(case, "f") {
        fs.insert(&at(rel), Node::File(content(data)));
        let node = if unreadable(rel) {
            githead::fs::Node::Special
        } else {
            githead::fs::Node::File(content(data))
        };
        refs.insert(&rat(rel), node);
    }
    for (rel, to) in obj(case, "l") {
        let target = to.as_str().unwrap().as_bytes().to_vec();
        fs.insert(&at(rel), Node::Link(target.clone()));
        refs.insert(&rat(rel), githead::fs::Node::Link(target));
    }
    for rel in list(case, "p") {
        fs.insert(&at(rel), Node::Fifo);
        refs.insert(&rat(rel), githead::fs::Node::Special);
    }
    for (rel, mode) in &modes {
        fs.set_mode(&at(rel), *mode);
    }
    (fs, refs)
}

fn stamp(s: Option<Stamp>) -> Value {
    match s {
        None => Value::Null,
        Some(s) if s == UNSTATED => json!("u"),
        Some(s) => json!([format!("{:o}", s[4]), s[2] as u64]),
    }
}

fn values(names: &Values) -> Vec<Value> {
    names
        .iter()
        .map(|(name, vals)| {
            json!([
                name.section,
                name.subsection.as_deref().map(spelled),
                name.key,
                vals.iter()
                    .map(|v| v.as_deref().map(spelled))
                    .collect::<Vec<_>>()
            ])
        })
        .collect()
}

fn answer<F: bettermemory_core::fsdata::Fs>(
    fs: &F,
    refs: &dyn githead::RefFs,
    case: &Value,
    root: &str,
) -> Value {
    let label = |bytes: &[u8]| -> String {
        let mut out = Vec::new();
        let mut rest = bytes;
        while let Some(at) = rest.windows(root.len()).position(|w| w == root.as_bytes()) {
            out.extend_from_slice(&rest[..at]);
            out.extend_from_slice(b"<scratch>");
            rest = &rest[at + root.len()..];
        }
        out.extend_from_slice(rest);
        spelled(&out)
    };
    let at = |v: &str| -> Vec<u8> {
        match v.strip_prefix("@/") {
            Some(rest) => format!("{root}/{rest}").into_bytes(),
            None => v.as_bytes().to_vec(),
        }
    };
    let k = &case["k"];
    let env_var = |name: &str| k["env"].get(name).and_then(Value::as_str).map(&at);
    let none = |_: &[u8]| None;
    let machine = Machine {
        flavour: Flavour::Posix,
        home: env_var("HOME"),
        profile: None,
        user_home: &none,
    };
    let so = &k["so"];
    let inputs = Inputs {
        machine: &machine,
        env: Env {
            git_config_nosystem: env_var("GIT_CONFIG_NOSYSTEM"),
            git_config_system: env_var("GIT_CONFIG_SYSTEM"),
            git_config_global: env_var("GIT_CONFIG_GLOBAL"),
            xdg_config_home: env_var("XDG_CONFIG_HOME"),
            git_test_default_initial_branch_name: env_var("GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME"),
        },
        sources: Sources {
            own: so["own"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| at(p.as_str().unwrap()))
                .collect(),
            system: so["system"].as_str().map(&at),
            prefix: so["prefix"].as_str().map(&at),
            hasconfig: so["hasconfig"].as_bool(),
            legacy: so.get("legacy").and_then(Value::as_bool),
            resets: so.get("resets").and_then(Value::as_bool),
            version: so.get("version").and_then(Value::as_array).map(|v| {
                let n = |i: usize| v[i].as_u64().unwrap() as u32;
                (n(0), n(1), n(2))
            }),
        },
        git: GitFacts {
            release: k["v"]
                .as_array()
                .map(|v| (v[0].as_u64().unwrap() as u32, v[1].as_u64().unwrap() as u32)),
            reads_compat: None,
        },
        cwd: root.as_bytes().to_vec(),
        asked_branch: Some(match &k["ab"] {
            Value::Null => Branch::Elsewhere,
            Value::String(s) if s == "?" => Branch::Unsettled,
            Value::String(s) => Branch::Name(s.as_bytes().to_vec()),
            other => panic!("ab {other}"),
        }),
    };
    let common_dir = at(&format!("@/{}", k["cd"].as_str().unwrap()));
    let git_dir = at(&format!("@/{}", k["gd"].as_str().unwrap()));
    let got = gather(fs, refs, &common_dir, &git_dir, &inputs).expect("ported on POSIX");
    let shown = |read: &ConfigValues| -> Value {
        let mut v = json!({"v": values(&read.values)});
        if !read.undecided.is_empty() {
            v["ud"] = json!(values(&read.undecided));
        }
        if let Some(md) = &read.maybe_dies {
            v["md"] = json!(label(md));
        }
        v
    };
    let outcome = match &got.outcome {
        Outcome::Declined(why) => json!({"d": label(why)}),
        Outcome::NoRemotes => json!("none"),
        Outcome::Reads {
            machine,
            own,
            legacy,
        } => json!({
            "m": machine.iter().map(shown).collect::<Vec<_>>(),
            "w": own.iter().map(shown).collect::<Vec<_>>(),
            "lg": legacy.iter().map(|n| spelled(n)).collect::<Vec<_>>(),
        }),
    };
    json!({
        "fl": got.files.iter().map(|(p, s)| json!([label(p), stamp(*s)])).collect::<Vec<_>>(),
        "nm": got.named.iter().map(|p| label(p)).collect::<Vec<_>>(),
        "o": outcome,
    })
}

#[test]
fn location_remotes_every_case_in_memory() {
    let cases = cases();
    let ledger = Ledger::new("golden_location_remotes", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let (fs, refs) = worlds(case);
        let got = answer(&fs, &refs, case, MEM_ROOT);
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}

struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt;
        fn open_up(dir: &std::path::Path) {
            let Ok(entries) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
                if std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir()) {
                    open_up(&path);
                }
            }
        }
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn fresh_dir() -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-locremotes-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
}

fn write_tree(case: &Value, root: &std::path::Path) {
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
        assert!(made, "mkfifo could not run");
    }
    for (rel, mode) in all_modes(case) {
        std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode)).unwrap();
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

#[test]
fn location_remotes_every_case_on_disk() {
    use std::os::unix::fs::PermissionsExt;
    let cases = cases();
    let ledger = Ledger::new("golden_location_remotes_stdfs", FILE);
    let base = Scratch(fresh_dir());
    refuse_root(&base.0);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        write_tree(case, &dir);
        let got = answer(&StdFs, &githead::StdFs, case, dir.to_str().unwrap());
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}
