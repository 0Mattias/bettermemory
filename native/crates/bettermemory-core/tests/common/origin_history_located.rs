//! The cases of `origin_history_located.jsonl` and
//! `origin_history_located_modes.jsonl` on disk, and the native answers
//! of `origin_history::located` in the fixtures' spelling.

#![cfg(unix)]

use std::path::{Path, PathBuf};

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host};
use bettermemory_core::githead::StdFs as RefStd;
use bettermemory_core::origin_history::{
    cuts, cuts_in, history_guard, located_key, shallow_file, Cut, Guard, HistoryEnv, HISTORY_ENV,
};
use serde_json::{json, Value};

/// `_b`: `=` and the text where it is printable ASCII without a
/// backslash, else hex.
pub fn b(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&c| (0x20..0x7f).contains(&c) && c != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).unwrap());
    }
    super::hex(data)
}

fn spelled(data: &[u8], root: &str) -> Value {
    let text = String::from_utf8_lossy(data).replace(root, "<scratch>");
    json!(b(text.as_bytes()))
}

fn text(value: &Value) -> Vec<u8> {
    match value {
        Value::Object(map) => {
            let mut out = map
                .get("p")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let n = usize::try_from(map["n"].as_u64().unwrap()).unwrap();
            out.push_str(&map["x"].as_str().unwrap().repeat(n));
            out.into_bytes()
        }
        other => other.as_str().unwrap().as_bytes().to_vec(),
    }
}

/// A directory removed on drop, every folder under it opened first.
pub struct Scratch(pub PathBuf);

impl Scratch {
    pub fn new(tag: &str) -> Scratch {
        static SEQ: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let n = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let base = std::env::temp_dir().join(format!("bm-ohl-{tag}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir(&base).unwrap();
        let base = std::fs::canonicalize(&base).unwrap();
        for dir in base.ancestors().skip(1) {
            for name in [".git", "HEAD"] {
                assert!(
                    std::fs::symlink_metadata(dir.join(name)).is_err(),
                    "{} exists above the scratch directory",
                    dir.join(name).display()
                );
            }
        }
        Scratch(base)
    }
}

fn open_up(dir: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
    let Ok(list) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in list.flatten() {
        let path = entry.path();
        if std::fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir()) {
            open_up(&path);
        }
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Lays the case's tree under `root`.
pub fn write_tree(case: &Value, root: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let real = root.to_str().unwrap();
    if let Some(dirs) = case.get("d").and_then(Value::as_array) {
        for d in dirs {
            std::fs::create_dir_all(root.join(d.as_str().unwrap())).unwrap();
        }
    }
    for (rel, value) in case["f"].as_object().unwrap() {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let data = String::from_utf8(text(value))
            .unwrap()
            .replace("<root>", real);
        std::fs::write(&path, data).unwrap();
    }
    if let Some(modes) = case.get("m").and_then(Value::as_object) {
        let mut rels: Vec<&String> = modes.keys().collect();
        rels.sort_by_key(|r| std::cmp::Reverse(r.matches('/').count()));
        for rel in rels {
            let mode = u32::from_str_radix(modes[rel].as_str().unwrap(), 8).unwrap();
            std::fs::set_permissions(root.join(rel), std::fs::Permissions::from_mode(mode))
                .unwrap();
        }
    }
}

/// What a query is asked with, and what it changes.
pub struct State {
    pub env: Env,
    pub history: HistoryEnv,
    pub guards: Vec<Guard>,
}

impl State {
    pub fn new() -> State {
        State {
            env: Env::default(),
            history: Default::default(),
            guards: Vec::new(),
        }
    }

    fn set(&mut self, name: &str, value: Option<&str>) {
        let bytes = value.map(|v| v.as_bytes().to_vec());
        match name {
            "GIT_DIR" => self.env.git_dir = bytes,
            "GIT_WORK_TREE" => self.env.git_work_tree = bytes,
            "GIT_COMMON_DIR" => self.env.git_common_dir = bytes,
            "GIT_DISCOVERY_ACROSS_FILESYSTEM" => self.env.across_filesystem = bytes,
            "GIT_TEST_ASSUME_DIFFERENT_OWNER" => self.env.assume_different_owner = bytes,
            "GIT_CEILING_DIRECTORIES" => self.env.ceilings = bytes,
            other => {
                let at = HISTORY_ENV.iter().position(|n| *n == other).unwrap();
                self.history[at] = value.map(|v| v.chars().map(u32::from).collect());
            }
        }
    }
}

fn cut(found: Option<Cut>) -> Value {
    match found {
        None => Value::Null,
        Some(set) => json!(set
            .iter()
            .map(|c| c
                .iter()
                .map(|&u| char::from_u32(u).unwrap())
                .collect::<String>())
            .collect::<Vec<_>>()),
    }
}

/// The answer of one query over the tree at `root`.
pub fn answer(query: &str, root: &str, state: &mut State) -> Value {
    let (op, raw) = query.split_once(':').unwrap();
    let raw_args: Vec<&str> = raw.split('|').collect();
    let args: Vec<String> = raw_args.iter().map(|a| a.replace("<root>", root)).collect();
    let flags = args.get(1).map_or("", String::as_str);
    if op == "w" {
        std::fs::write(Path::new(root).join(raw_args[0]), &args[1]).unwrap();
        return Value::Null;
    }
    if op == "e" {
        state.set(&args[0], args.get(1).map(String::as_str));
        return Value::Null;
    }
    let ctx = Ctx {
        fs: &StdFs,
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
        env: state
            .env
            .git_common_dir
            .as_deref()
            .filter(|v| !v.is_empty()),
        git: &git,
    };
    let euid = {
        use std::os::unix::fs::MetadataExt;
        std::fs::metadata(root).unwrap().uid()
    };
    let host = Host {
        euid,
        owners: &StdFs,
    };
    let env = &state.env;
    let path = args[0].as_bytes();
    let walk = flags.contains('W');
    match op {
        "K" => match located_key(&rule, env, &RefStd, path) {
            None => Value::Null,
            Some((root_of, head)) => {
                let r: String = root_of
                    .iter()
                    .map(|&u| char::from_u32(u).unwrap())
                    .collect();
                let h: String = head.iter().map(|&u| char::from_u32(u).unwrap()).collect();
                json!([spelled(r.as_bytes(), root), h])
            }
        },
        "F" => {
            shallow_file(&rule, &host, env, path, walk).map_or(Value::Null, |p| spelled(&p, root))
        }
        "C" => cut(cuts(&rule, &host, env, path, walk).expect("no ValueError")),
        "I" => cut(cuts_in(&rule, path).expect("no ValueError")),
        "G" => match history_guard(&rule, &host, env, &state.history, path) {
            None => Value::Null,
            Some(got) => {
                state.guards.push(got.clone());
                let eq = state.guards.iter().position(|g| *g == got).unwrap();
                json!({ "eq": eq })
            }
        },
        other => panic!("op {other}"),
    }
}
