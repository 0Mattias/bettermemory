//! What the tests of `gitobjects::files` share with
//! `tests/contract/gen_gitobjects_files.py`: a case's tree built in memory
//! ([`MemFs`]) or on disk, the generator's `os.stat` claims as a wrapping
//! file system ([`Claims`]), and a case's questions asked of the port and
//! answered in the fixture's spelling.

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::BTreeMap;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Refused, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitobjects::files::{
    ObjectReader, Stop, RECALL_ON_DATA_ACCESS, SF_DATALESS,
};
use serde_json::{json, Value};

/// The generator's `_spelled`: hex text, or pieces of hex and runs.
pub fn spelled(v: &Value) -> Vec<u8> {
    match v {
        Value::String(s) => super::unhex(s),
        Value::Array(pieces) => {
            let mut out = Vec::new();
            for piece in pieces {
                match piece {
                    Value::String(s) => out.extend(super::unhex(s)),
                    Value::Array(run) => {
                        let byte = super::unhex(run[0].as_str().unwrap())[0];
                        let n = run[1].as_u64().unwrap() as usize;
                        out.extend(std::iter::repeat_n(byte, n));
                    }
                    other => panic!("a piece {other}"),
                }
            }
            out
        }
        other => panic!("bytes spelled {other}"),
    }
}

/// Every `@` in `data` replaced by `root`.
pub fn at_root(data: &[u8], root: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for &b in data {
        if b == b'@' {
            out.extend_from_slice(root);
        } else {
            out.push(b);
        }
    }
    out
}

/// `root/rel`, `/` between components.
pub fn abs(root: &[u8], rel: &str) -> Vec<u8> {
    let mut out = root.to_vec();
    out.push(b'/');
    out.extend_from_slice(rel.as_bytes());
    out
}

/// A file system whose `stat` claims more of some paths, as the
/// generator's patched `os.stat`: `f` the dataless flag, `a` the recall
/// attribute, `o` every other bit of both, `r` a regular file's kind.
pub struct Claims<F: Fs> {
    pub inner: RefCell<F>,
    pub claims: BTreeMap<Vec<u8>, char>,
}

impl<F: Fs> Claims<F> {
    pub fn new(inner: F) -> Claims<F> {
        Claims {
            inner: RefCell::new(inner),
            claims: BTreeMap::new(),
        }
    }
}

impl<F: Fs> Fs for Claims<F> {
    type File = F::File;
    fn open(&self, path: &[u8]) -> Result<F::File, PyErr> {
        self.inner.borrow().open(path)
    }
    fn fstat(&self, file: &mut F::File) -> Result<Stat, PyErr> {
        self.inner.borrow().fstat(file)
    }
    fn read(&self, file: &mut F::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.borrow().read(file, max)
    }
    fn read_once(&self, file: &mut F::File, max: usize) -> Result<Vec<u8>, OsError> {
        self.inner.borrow().read_once(file, max)
    }
    fn seek(&self, file: &mut F::File, offset: u64) -> Result<(), Refused> {
        self.inner.borrow().seek(file, offset)
    }
    fn close(&self, file: F::File) -> Result<(), OsError> {
        self.inner.borrow().close(file)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.borrow().access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<F::File, OsError> {
        self.inner.borrow().open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.borrow().lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.borrow().readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.inner.borrow().readlink_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        let mut st = self.inner.borrow().stat(path)?;
        match self.claims.get(path) {
            Some('f') => st.flags |= SF_DATALESS,
            Some('a') => st.file_attributes |= RECALL_ON_DATA_ACCESS,
            Some('o') => {
                st.flags |= !SF_DATALESS;
                st.file_attributes |= !RECALL_ON_DATA_ACCESS;
            }
            Some('r') => st.kind = Kind::Regular,
            _ => {}
        }
        Ok(st)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.borrow().lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.borrow().listdir(path)
    }
}

/// Where a case's tree is built.
pub trait Tree: Fs + Sized {
    fn write(this: &Claims<Self>, path: &[u8], data: &[u8]);
}

impl Tree for MemFs {
    fn write(this: &Claims<MemFs>, path: &[u8], data: &[u8]) {
        this.inner
            .borrow_mut()
            .insert(path, Node::File(data.to_vec()));
    }
}

impl Tree for bettermemory_core::fsdata::StdFs {
    fn write(_this: &Claims<Self>, path: &[u8], data: &[u8]) {
        let path = path_of(path);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, data).unwrap();
    }
}

#[cfg(unix)]
pub fn path_of(path: &[u8]) -> std::path::PathBuf {
    use std::os::unix::ffi::OsStrExt;
    std::ffi::OsStr::from_bytes(path).into()
}

#[cfg(not(unix))]
pub fn path_of(path: &[u8]) -> std::path::PathBuf {
    String::from_utf8(path.to_vec()).unwrap().into()
}

/// The case's tree in memory, at `root`.
pub fn mem_tree(case: &Value, root: &[u8]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(root, Node::Dir);
    let mut modes = Vec::new();
    for entry in case["t"].as_array().unwrap() {
        let kind = entry[0].as_str().unwrap();
        let path = abs(root, entry[1].as_str().unwrap());
        match kind {
            "d" => fs.insert(&path, Node::Dir),
            "f" => fs.insert(&path, Node::File(at_root(&spelled(&entry[2]), root))),
            "l" => fs.insert(
                &path,
                Node::Link(at_root(entry[2].as_str().unwrap().as_bytes(), root)),
            ),
            "p" => fs.insert(&path, Node::Fifo),
            "m" => modes.push((path, entry[2].as_u64().unwrap() as u32)),
            other => panic!("a tree entry {other}"),
        }
    }
    for (path, mode) in modes {
        fs.set_mode(&path, mode);
    }
    fs
}

/// The case's tree on disk, at `root` (a real path). POSIX alone.
#[cfg(unix)]
pub fn disk_tree(case: &Value, root: &[u8]) {
    use std::os::unix::fs::PermissionsExt;
    let mut modes = Vec::new();
    for entry in case["t"].as_array().unwrap() {
        let kind = entry[0].as_str().unwrap();
        let path = path_of(&abs(root, entry[1].as_str().unwrap()));
        if kind != "m" {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        }
        match kind {
            "d" => std::fs::create_dir_all(&path).unwrap(),
            "f" => std::fs::write(&path, at_root(&spelled(&entry[2]), root)).unwrap(),
            "l" => std::os::unix::fs::symlink(
                path_of(&at_root(entry[2].as_str().unwrap().as_bytes(), root)),
                &path,
            )
            .unwrap(),
            "p" => {
                let made = std::process::Command::new("mkfifo")
                    .arg(&path)
                    .status()
                    .unwrap();
                assert!(made.success());
            }
            "m" => modes.push((path, entry[2].as_u64().unwrap() as u32)),
            other => panic!("a tree entry {other}"),
        }
    }
    for (path, mode) in modes {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
    }
}

/// The modes the case set, put back so the tree can be removed.
#[cfg(unix)]
pub fn undo_modes(case: &Value, root: &[u8]) {
    use std::os::unix::fs::PermissionsExt;
    for entry in case["t"].as_array().unwrap() {
        if entry[0] == "m" {
            let path = path_of(&abs(root, entry[1].as_str().unwrap()));
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
    }
}

/// The case's stat claims.
pub fn claims_of(case: &Value, root: &[u8]) -> BTreeMap<Vec<u8>, char> {
    let mut out = BTreeMap::new();
    if let Some(st) = case.get("st") {
        for claim in st.as_array().unwrap() {
            let kind = claim[0].as_str().unwrap().chars().next().unwrap();
            out.insert(abs(root, claim[1].as_str().unwrap()), kind);
        }
    }
    out
}

/// The case's git facts.
pub fn facts_of(case: &Value) -> GitFacts {
    let release = match case.get("gv") {
        None => Some((2, 54)),
        Some(Value::Null) => None,
        Some(v) => Some((v[0].as_u64().unwrap() as u32, v[1].as_u64().unwrap() as u32)),
    };
    let reads_compat = match case.get("rc") {
        None => Some(true),
        Some(v) => v.as_bool(),
    };
    GitFacts {
        release,
        reads_compat,
    }
}

fn rel(root: &[u8], path: &[u8]) -> Value {
    let mut prefix = root.to_vec();
    prefix.push(b'/');
    let rest = path
        .strip_prefix(prefix.as_slice())
        .unwrap_or_else(|| panic!("a path outside the root: {path:?}"));
    json!(rest.iter().map(|&b| b as char).collect::<String>())
}

fn rels(root: &[u8], paths: &[Vec<u8>]) -> Value {
    Value::Array(paths.iter().map(|p| rel(root, p)).collect())
}

fn stopped(stop: Stop) -> Value {
    match stop {
        Stop::Raised(PyErr::Value) => json!({ "raise": "ValueError" }),
        other => panic!("an answer the fixture has no spelling for: {other:?}"),
    }
}

/// The case's questions asked of the port over `fs` (the tree built at
/// `root`): `(a, s)` in the fixture's spelling.
pub fn ask<F: Tree>(fs: &Claims<F>, root: &[u8], case: &Value) -> (Value, Value) {
    let git_dir = abs(root, case.get("gd").and_then(Value::as_str).unwrap_or("g"));
    let mut reader = ObjectReader::new(fs, Flavour::Posix, b"/", facts_of(case), &git_dir);
    let mut answers = Vec::new();
    for q in case["q"].as_array().unwrap() {
        let answer = match q.as_str() {
            Some("d") => reader
                .object_dirs()
                .map_or_else(stopped, |d| rels(root, &d)),
            Some("c") => reader.complete().map_or_else(stopped, |c| json!(c)),
            Some("e") => rels(root, &reader.evicted()),
            Some("r") => reader.rescan().map_or_else(stopped, |n| json!(n)),
            _ => {
                let path = abs(root, q[1].as_str().unwrap());
                F::write(fs, &path, &spelled(&q[2]));
                Value::Null
            }
        };
        answers.push(answer);
    }
    let packs: Vec<Vec<u8>> = reader.packs.iter().map(|p| p.pack.clone()).collect();
    let state = json!({
        "h": reader.hash_len,
        "l": rels(root, &reader.listed()),
        "p": rels(root, &packs),
        "u": reader.unreadable,
        "e": rels(root, &reader.evicted()),
    });
    (Value::Array(answers), state)
}

/// The case asked over its tree in memory at `/m`.
pub fn ask_in_memory(case: &Value) -> (Value, Value) {
    let root = b"/m".to_vec();
    let mut fs = Claims::new(mem_tree(case, &root));
    fs.claims = claims_of(case, &root);
    ask(&fs, &root, case)
}
