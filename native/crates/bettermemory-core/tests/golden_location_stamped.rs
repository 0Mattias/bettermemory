//! Every case of `location_stamped.jsonl`, POSIX only: sequences of
//! `machine_config_stamps`, `machine_config_reads`,
//! `repository_config_stamps`, a capture and `config_unchanged`, with
//! changes between them, against `gitconfig::stamped` and one
//! [`StampedReads`] per case.
//!
//! In memory the tree is a model rebuilt into a `MemFs` after each
//! change, behind [`Moved`], a test-local wrapper that delegates every
//! method of `Fs` and only adds to what `stat` answers the time and inode
//! a change gives a file (a `MemFs` answers 0 and an inode taken from the
//! path, so a write of the same size or a replace would not show). On
//! disk the changes are made as the generator makes them.

#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult, StdFs,
};
use bettermemory_core::gitconfig::legacy::{Files, Stamp, UNSTATED};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{Env, Inputs, Sources};
use bettermemory_core::gitconfig::spellings::rewrites;
use bettermemory_core::gitconfig::stamped::{
    config_unchanged, files_of, machine_config_reads, machine_config_stamps,
    repository_config_stamps, stamped_read, Key, StampedReads,
};
use bettermemory_core::gitconfig::tree::Branch;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::githead;
use common::gitconfig_file::content;
use common::gitconfig_tree::spelled;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};

const FILE: &str = "location_stamped.jsonl";
const MEM_ROOT: &str = "/<scratch>";
/// The generator's `CHANGED_AT`.
const CHANGED_AT: i128 = 1_000_000_000;

fn cases() -> Vec<Value> {
    let lines = jsonl(FILE);
    let cases = lines[1..].to_vec();
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    cases
}

fn list(case: &Value, key: &str) -> Vec<String> {
    case.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|v| v.as_str().unwrap().to_owned()).collect())
        .unwrap_or_default()
}

/// A case's tree and its changes.
trait World {
    type F: Fs;
    fn fs(&self) -> &Self::F;
    fn refs(&self) -> &dyn githead::RefFs;
    fn root(&self) -> String;
    fn change(&mut self, kind: &str, rel: &str, data: Option<Vec<u8>>, n: i128);
}

#[derive(Clone)]
enum Entry {
    File(Vec<u8>),
    Dir,
    Fifo,
}

/// `MemFs` with the time and inode changes give: delegates every method
/// of `Fs` (and, once `lstat_result` and `readlink_result` are on the
/// trait, must delegate those too).
struct Moved {
    inner: MemFs,
    mtime: HashMap<Vec<u8>, i128>,
    ctime: HashMap<Vec<u8>, i128>,
    ino: HashMap<Vec<u8>, u64>,
}

impl Fs for Moved {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max)
    }
    fn access_read(&self, path: &[u8]) -> bool {
        self.inner.access_read(path)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.inner.readlink_result(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        let mut st = self.inner.stat(path)?;
        if let Some(t) = self.mtime.get(path) {
            st.mtime_ns = *t;
        }
        if let Some(t) = self.ctime.get(path) {
            st.ctime_ns = st.ctime_ns.map(|_| *t);
        }
        if let (Some(bump), Some(ino)) = (self.ino.get(path), st.ino.as_mut()) {
            *ino = ino.wrapping_add(*bump);
        }
        Ok(st)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

struct Memory {
    model: BTreeMap<String, (Entry, u32)>,
    fs: Moved,
    refs: githead::MemFs,
}

impl Memory {
    fn new(case: &Value) -> Memory {
        let mut model = BTreeMap::new();
        let parents = |rel: &str, model: &mut BTreeMap<String, (Entry, u32)>| {
            let parts: Vec<&str> = rel.split('/').collect();
            for i in 1..parts.len() {
                model
                    .entry(parts[..i].join("/"))
                    .or_insert((Entry::Dir, 0o755));
            }
        };
        for rel in list(case, "d") {
            parents(&rel, &mut model);
            model.insert(rel, (Entry::Dir, 0o755));
        }
        if let Some(files) = case["f"].as_object() {
            for (rel, data) in files {
                parents(rel, &mut model);
                model.insert(rel.clone(), (Entry::File(content(data)), 0o644));
            }
        }
        for rel in list(case, "p") {
            parents(&rel, &mut model);
            model.insert(rel, (Entry::Fifo, 0o644));
        }
        if let Some(modes) = case.get("m").and_then(Value::as_object) {
            for (rel, mode) in modes {
                let mode = u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap();
                model.get_mut(rel).expect("a moded entry").1 = mode;
            }
        }
        let mut world = Memory {
            model,
            fs: Moved {
                inner: MemFs::new(Flavour::Posix),
                mtime: HashMap::new(),
                ctime: HashMap::new(),
                ino: HashMap::new(),
            },
            refs: githead::MemFs::new(githead::Flavour::Posix),
        };
        world.rebuild();
        world
    }

    fn rebuild(&mut self) {
        let mut fs = MemFs::new(Flavour::Posix);
        let mut refs = githead::MemFs::new(githead::Flavour::Posix);
        fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
        for (rel, (entry, mode)) in &self.model {
            let at = format!("{MEM_ROOT}/{rel}").into_bytes();
            let rat = format!("{}/{rel}", &MEM_ROOT[1..]).into_bytes();
            match entry {
                Entry::File(data) => {
                    fs.insert(&at, Node::File(data.clone()));
                    let node = if mode & 0o400 == 0 {
                        githead::fs::Node::Special
                    } else {
                        githead::fs::Node::File(data.clone())
                    };
                    refs.insert(&rat, node);
                }
                Entry::Dir => {
                    fs.insert(&at, Node::Dir);
                    refs.insert(&rat, githead::fs::Node::Dir);
                }
                Entry::Fifo => {
                    fs.insert(&at, Node::Fifo);
                    refs.insert(&rat, githead::fs::Node::Special);
                }
            }
        }
        for (rel, (_, mode)) in &self.model {
            fs.set_mode(format!("{MEM_ROOT}/{rel}").as_bytes(), *mode);
        }
        self.fs.inner = fs;
        self.refs = refs;
    }
}

impl World for Memory {
    type F = Moved;
    fn fs(&self) -> &Moved {
        &self.fs
    }
    fn refs(&self) -> &dyn githead::RefFs {
        &self.refs
    }
    fn root(&self) -> String {
        MEM_ROOT.to_owned()
    }
    fn change(&mut self, kind: &str, rel: &str, data: Option<Vec<u8>>, n: i128) {
        let at = format!("{MEM_ROOT}/{rel}").into_bytes();
        let time = (CHANGED_AT + n) * 1_000_000_000;
        let parent = rel.rsplit_once('/').map(|(p, _)| p.to_owned());
        // Every change moves the file's change time, as on disk (a mode
        // changed and back, a rename onto it, a time set).
        self.fs.ctime.insert(at.clone(), time);
        match kind {
            "write" => {
                self.model.get_mut(rel).unwrap().0 = Entry::File(data.unwrap());
                self.fs.mtime.insert(at, time);
            }
            "mtime" => {
                self.fs.mtime.insert(at, time);
            }
            "rename" => {
                self.model
                    .insert(rel.to_owned(), (Entry::File(data.unwrap()), 0o644));
                self.fs.mtime.insert(at.clone(), time);
                *self.fs.ino.entry(at).or_insert(0) += 1;
            }
            "mode" => {
                let mode = std::str::from_utf8(&data.unwrap_or(b"600".to_vec()))
                    .ok()
                    .and_then(|m| u32::from_str_radix(m, 8).ok())
                    .unwrap();
                self.model.get_mut(rel).unwrap().1 = mode;
            }
            "remove" => {
                self.model.remove(rel);
            }
            "create" => {
                let parts: Vec<&str> = rel.split('/').collect();
                for i in 1..parts.len() {
                    self.model
                        .entry(parts[..i].join("/"))
                        .or_insert((Entry::Dir, 0o755));
                }
                self.model
                    .insert(rel.to_owned(), (Entry::File(data.unwrap()), 0o644));
                self.fs.mtime.insert(at, time);
                if let Some(p) = parent {
                    let dir = format!("{MEM_ROOT}/{p}").into_bytes();
                    self.fs.mtime.insert(dir.clone(), time);
                    self.fs.ctime.insert(dir, time);
                }
            }
            "dir" => {
                self.model.insert(rel.to_owned(), (Entry::Dir, 0o755));
                *self.fs.ino.entry(at).or_insert(0) += 1;
            }
            "fifo" => {
                self.model.insert(rel.to_owned(), (Entry::Fifo, 0o644));
                *self.fs.ino.entry(at).or_insert(0) += 1;
            }
            other => panic!("no change {other}"),
        }
        self.rebuild();
    }
}

struct Disk {
    root: std::path::PathBuf,
}

fn mkfifo(path: &std::path::Path) {
    let made = std::process::Command::new("mkfifo")
        .arg(path)
        .status()
        .is_ok_and(|s| s.success());
    assert!(made, "mkfifo could not run");
}

/// Apply a change whose only mark may be the change time (a mode set and
/// set back leaves size, mode and mtime as they were), again until the
/// change time has moved from before it, as the generator does
/// (`_until_ctime_moves`). Linux stamps it from a coarse clock (a 4 or 10
/// ms tick), so a file created and changed within one tick would keep it;
/// APFS's nanosecond times never show that. No fixed sleep: the loop ends
/// once the clock has ticked, and fails loudly past five seconds.
fn until_ctime_moves(path: &std::path::Path, apply: impl Fn()) {
    use std::os::unix::fs::MetadataExt;
    let ctime = |p: &std::path::Path| {
        let m = std::fs::metadata(p).unwrap();
        (m.ctime(), m.ctime_nsec())
    };
    let before = ctime(path);
    let started = std::time::Instant::now();
    apply();
    while ctime(path) == before {
        assert!(
            started.elapsed() < std::time::Duration::from_secs(5),
            "the change time of {} did not move",
            path.display()
        );
        std::thread::sleep(std::time::Duration::from_millis(2));
        apply();
    }
}

fn set_time(path: &std::path::Path, n: i128) {
    let at = std::time::UNIX_EPOCH + std::time::Duration::from_secs((CHANGED_AT + n) as u64);
    let times = std::fs::FileTimes::new().set_accessed(at).set_modified(at);
    if path.is_dir() {
        std::fs::File::open(path).unwrap().set_times(times).unwrap();
        return;
    }
    // `os.utime` opens nothing; std sets the times through a handle, which
    // a file no one may write refuses. Its mode is opened up around the
    // write and put back: the change time moves either way, as it does
    // for `os.utime`, and the mode ends as it was.
    use std::os::unix::fs::PermissionsExt;
    let mode = std::fs::metadata(path).unwrap().permissions().mode() & 0o7777;
    let opened_up = mode & 0o200 == 0;
    if opened_up {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
    let file = std::fs::OpenOptions::new().write(true).open(path).unwrap();
    file.set_times(times).unwrap();
    drop(file);
    if opened_up {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
    }
}

impl Disk {
    fn new(case: &Value, root: std::path::PathBuf) -> Disk {
        use std::os::unix::fs::PermissionsExt;
        let mode = |p: &std::path::Path, m: u32| {
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap()
        };
        for rel in list(case, "d") {
            std::fs::create_dir_all(root.join(rel)).unwrap();
        }
        if let Some(files) = case["f"].as_object() {
            for (rel, data) in files {
                let path = root.join(rel);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(&path, content(data)).unwrap();
                mode(&path, 0o644);
            }
        }
        for rel in list(case, "p") {
            let path = root.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            mkfifo(&path);
            mode(&path, 0o644);
        }
        fn dirs(p: &std::path::Path, mode: &dyn Fn(&std::path::Path, u32)) {
            for entry in std::fs::read_dir(p).unwrap().flatten() {
                let path = entry.path();
                if entry.file_type().unwrap().is_dir() {
                    dirs(&path, mode);
                    mode(&path, 0o755);
                }
            }
        }
        dirs(&root, &mode);
        if let Some(modes) = case.get("m").and_then(Value::as_object) {
            for (rel, m) in modes {
                mode(
                    &root.join(rel),
                    u32::from_str_radix(m.as_str().unwrap(), 8).unwrap(),
                );
            }
        }
        Disk { root }
    }
}

impl World for Disk {
    type F = StdFs;
    fn fs(&self) -> &StdFs {
        &StdFs
    }
    fn refs(&self) -> &dyn githead::RefFs {
        &githead::StdFs
    }
    fn root(&self) -> String {
        self.root.to_str().unwrap().to_owned()
    }
    fn change(&mut self, kind: &str, rel: &str, data: Option<Vec<u8>>, n: i128) {
        use std::os::unix::fs::PermissionsExt;
        let path = self.root.join(rel);
        let mode = |p: &std::path::Path, m: u32| {
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap()
        };
        match kind {
            "write" => {
                std::fs::write(&path, data.unwrap()).unwrap();
                set_time(&path, n);
            }
            "mtime" => set_time(&path, n),
            "rename" => {
                let new = path.with_file_name(format!(
                    "{}.new",
                    path.file_name().unwrap().to_str().unwrap()
                ));
                std::fs::write(&new, data.unwrap()).unwrap();
                mode(&new, 0o644);
                set_time(&new, n);
                std::fs::rename(&new, &path).unwrap();
            }
            "mode" => {
                let m = std::str::from_utf8(&data.unwrap_or(b"600".to_vec()))
                    .ok()
                    .and_then(|m| u32::from_str_radix(m, 8).ok())
                    .unwrap();
                until_ctime_moves(&path, || mode(&path, m));
            }
            "remove" => std::fs::remove_file(&path).unwrap(),
            "create" => {
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(&path, data.unwrap()).unwrap();
                mode(&path, 0o644);
                set_time(&path, n);
                set_time(path.parent().unwrap(), n);
            }
            "dir" => {
                std::fs::remove_file(&path).unwrap();
                std::fs::create_dir(&path).unwrap();
                mode(&path, 0o755);
            }
            "fifo" => {
                std::fs::remove_file(&path).unwrap();
                mkfifo(&path);
                mode(&path, 0o644);
            }
            other => panic!("no change {other}"),
        }
    }
}

/// The generator's `_digest`: a list longer than `most` as its length
/// and the FNV-1a 64 hash of its compact JSON (ASCII throughout).
fn digest(value: Value, most: usize) -> Value {
    let Some(items) = value.as_array() else {
        return value;
    };
    if items.len() <= most {
        return value;
    }
    let text = serde_json::to_string(&value).unwrap();
    assert!(text.is_ascii());
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in text.bytes() {
        h = (h ^ u64::from(byte)).wrapping_mul(0x0100_0000_01b3);
    }
    json!({"n": items.len(), "h": format!("{h:016x}")})
}

fn stamp(s: Option<Stamp>) -> Value {
    match s {
        None => Value::Null,
        Some(s) if s == UNSTATED => json!("u"),
        Some(s) => json!([format!("{:o}", s[4]), s[2] as u64]),
    }
}

/// The native answer to each step.
fn answer<W: World>(world: &mut W, case: &Value) -> Value {
    let root = world.root();
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
    let files = |f: &Files| -> Value {
        json!(f
            .iter()
            .map(|(p, s)| json!([label(p), stamp(*s)]))
            .collect::<Vec<_>>())
    };
    let at = |v: &str| -> Vec<u8> {
        match v.strip_prefix("@/") {
            Some(rest) => format!("{root}/{rest}").into_bytes(),
            None => v.as_bytes().to_vec(),
        }
    };
    let k = &case["k"];
    let mut env_now = k["env"].as_object().cloned().unwrap_or_default();
    let common_dir = at(&format!("@/{}", k["cd"].as_str().unwrap()));
    let mut git_dir = at(&format!("@/{}", k["gd"].as_str().unwrap()));
    let mut cache = StampedReads::new();
    let mut out = Vec::new();
    let mut last_stamps = None;
    let mut last_repo: Option<Vec<Files>> = None;
    let mut seen: HashMap<Key, u64> = HashMap::new();
    let mut captured: Option<(Files, Vec<Vec<u8>>)> = None;
    let mut changes: i128 = 0;
    for step in case["s"].as_array().unwrap() {
        let s = step.as_array().unwrap();
        if s[0] == "gitdir" {
            git_dir = at(&format!("@/{}", s[1].as_str().unwrap()));
            out.push(json!({}));
            continue;
        }
        if s[0] == "env" {
            let name = s[1].as_str().unwrap().to_owned();
            match &s[2] {
                Value::Null => env_now.remove(&name),
                v => env_now.insert(name, v.clone()),
            };
            out.push(json!({}));
            continue;
        }
        let env_var = |name: &str| env_now.get(name).and_then(Value::as_str).map(&at);
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
                git_test_default_initial_branch_name: None,
            },
            sources: Sources {
                own: so["own"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|p| at(p.as_str().unwrap()))
                    .chain(
                        (0..so.get("many").and_then(Value::as_u64).unwrap_or(0))
                            .map(|i| at(&format!("@/e/{i}"))),
                    )
                    .collect(),
                system: so["system"].as_str().map(&at),
                prefix: None,
                hasconfig: Some(true),
                legacy: Some(true),
                resets: Some(true),
                version: Some((2, 54, 0)),
            },
            git: GitFacts {
                release: k["v"]
                    .as_array()
                    .map(|v| (v[0].as_u64().unwrap() as u32, v[1].as_u64().unwrap() as u32)),
                reads_compat: None,
            },
            cwd: root.as_bytes().to_vec(),
            asked_branch: Some(Branch::Elsewhere),
        };

        let mut got = json!({});
        match s[0].as_str().unwrap() {
            "change" => {
                let data = s.get(3).map(content);
                world.change(
                    s[1].as_str().unwrap(),
                    s[2].as_str().unwrap(),
                    data,
                    changes,
                );
                changes += 1;
                out.push(got);
                continue;
            }
            "stamps" => {
                let now = s[2].as_f64().unwrap();
                let found = machine_config_stamps(
                    &mut cache,
                    world.fs(),
                    &inputs,
                    &git_dir,
                    s[1].as_bool().unwrap(),
                    now,
                )
                .expect("ported");
                got["nm"] = json!(found.named.iter().map(|p| label(p)).collect::<Vec<_>>());
                got["fl"] = json!(found.files.iter().map(files).collect::<Vec<_>>());
                got["eq"] = match &last_stamps {
                    None => Value::Null,
                    Some(last) => json!(*last == found),
                };
                last_stamps = Some(found);
            }
            "reads" => {
                let now = s[1].as_f64().unwrap();
                let reads =
                    machine_config_reads(&mut cache, world.fs(), &inputs, &git_dir, &mut || now)
                        .expect("ported");
                got["fl"] = json!(reads
                    .iter()
                    .map(|r| files(
                        &r.files
                            .iter()
                            .cloned()
                            .zip(r.stamps.iter().copied())
                            .collect()
                    ))
                    .collect::<Vec<_>>());
            }
            "repo" => {
                let found = repository_config_stamps(
                    &mut cache,
                    world.fs(),
                    &inputs,
                    &common_dir,
                    &git_dir,
                )
                .expect("ported");
                got["fl"] = json!(found.iter().map(files).collect::<Vec<_>>());
                got["eq"] = match &last_repo {
                    None => Value::Null,
                    Some(last) => json!(*last == found),
                };
                last_repo = Some(found);
            }
            "capture" => {
                let found = rewrites(world.fs(), world.refs(), &common_dir, &git_dir, &inputs)
                    .expect("ported");
                got["fl"] = files(&found.files);
                got["nm"] = json!(found.named.iter().map(|p| label(p)).collect::<Vec<_>>());
                captured = Some((found.files, found.named));
            }
            "unchanged" => {
                let (f, named) = captured.as_ref().expect("a capture");
                got["u"] = json!(config_unchanged(world.fs(), &inputs, named, f).expect("ported"));
            }
            "read" => {
                let path = at(s[1].as_str().unwrap());
                let read =
                    stamped_read(&mut cache, world.fs(), &inputs, &path, &git_dir).expect("ported");
                got["fl"] = files(&files_of(&read));
            }
            "clear" => cache.clear(),
            other => panic!("no step {other}"),
        }
        let mut now_seen = HashMap::new();
        let kept: Vec<Value> = cache
            .kept()
            .into_iter()
            .map(|(key, seq)| {
                now_seen.insert(key.clone(), seq);
                json!([
                    label(&key.0),
                    label(&key.1),
                    key.2.as_deref().map(&label),
                    seen.get(key) == Some(&seq)
                ])
            })
            .collect();
        seen = now_seen;
        got["kp"] = json!(kept);
        got["fr"] = json!(cache
            .fresh()
            .into_iter()
            .map(|(key, t)| json!([label(&key.0), t]))
            .collect::<Vec<_>>());
        let mut digested = json!({});
        for (key, value) in got.as_object().unwrap() {
            digested[key] = digest(value.clone(), if key == "kp" { 1 } else { 8 });
        }
        out.push(digested);
    }
    json!(out)
}

#[test]
fn location_stamped_every_case_in_memory() {
    let cases = cases();
    let ledger = Ledger::new("golden_location_stamped", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let mut world = Memory::new(case);
        let got = answer(&mut world, case);
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
    let dir = std::env::temp_dir().join(format!("bm-locstamped-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
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
fn location_stamped_every_case_on_disk() {
    use std::os::unix::fs::PermissionsExt;
    let cases = cases();
    let ledger = Ledger::new("golden_location_stamped_stdfs", FILE);
    let base = Scratch(fresh_dir());
    refuse_root(&base.0);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        let mut world = Disk::new(case, dir);
        let got = answer(&mut world, case);
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}
