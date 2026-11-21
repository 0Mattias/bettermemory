//! The worlds of `verify_anchoring_skip.jsonl` in memory, a file system
//! that rewrites a file just before its first open (a stamp that moves
//! during a read), and the native answers in the fixture's spelling.

use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, BTreeSet};

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{locate, Env};
use bettermemory_core::verify_anchoring::skip::{
    absent_by_sparsity, parts_below, skip_index, sparse_enabled, SkipMemo,
};
use serde_json::{json, Value};

use super::githead_discovery::{replaced, world, MEM_ROOT};

/// A [`MemFs`] whose files named in `moves` are rewritten just before
/// their first open once `armed` and once stamped: the reference rewrites
/// them in its `Path.read_bytes`, which only `_skip_index` calls, and
/// `_skip_index` stamps a file before it reads it, while discovery and
/// `_sparse_enabled` (`Path.read_text`) read without a stamp. So
/// discovery's own reads and the switch's read see the old bytes, in a
/// `K` query and in the `_skip_index` call inside an `A` query alike.
/// A rewritten file's stat answers a later modification and change time,
/// as the disk's does: [`MemFs`] stamps every file at time 0, so a
/// rewrite of the same size would otherwise leave its stamp unmoved.
pub struct Moving {
    pub inner: RefCell<MemFs>,
    pub moves: RefCell<BTreeMap<Vec<u8>, Vec<u8>>>,
    pub armed: Cell<bool>,
    primed: RefCell<BTreeSet<Vec<u8>>>,
    rewritten: RefCell<BTreeSet<Vec<u8>>>,
}

impl Moving {
    pub fn new(fs: MemFs) -> Moving {
        Moving {
            inner: RefCell::new(fs),
            moves: RefCell::new(BTreeMap::new()),
            armed: Cell::new(false),
            primed: RefCell::new(BTreeSet::new()),
            rewritten: RefCell::new(BTreeSet::new()),
        }
    }

    fn prime(&self, path: &[u8]) {
        if self.armed.get() && self.moves.borrow().contains_key(path) {
            self.primed.borrow_mut().insert(path.to_vec());
        }
    }

    fn later(&self, path: &[u8], got: Result<StatResult, OsError>) -> Result<StatResult, OsError> {
        got.map(|mut st| {
            if self.rewritten.borrow().contains(path) {
                st.mtime_ns += 1;
                st.ctime_ns = st.ctime_ns.map(|t| t + 1);
            }
            st
        })
    }

    fn arrive(&self, path: &[u8]) {
        if !self.armed.get() || !self.primed.borrow().contains(path) {
            return;
        }
        if let Some(data) = self.moves.borrow_mut().remove(path) {
            self.inner.borrow_mut().insert(path, Node::File(data));
            self.rewritten.borrow_mut().insert(path.to_vec());
        }
    }
}

impl Fs for Moving {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.arrive(path);
        self.inner.borrow().open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.arrive(path);
        self.inner.borrow().open_result(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.borrow().fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.borrow().read(file, max)
    }
    fn read_once(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, OsError> {
        self.inner.borrow().read_once(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.borrow().access(path, mode)
    }
    fn access_read(&self, path: &[u8]) -> bool {
        self.inner.borrow().access_read(path)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.arrive(path);
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
        self.prime(path);
        self.later(path, self.inner.borrow().stat(path))
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.prime(path);
        self.later(path, self.inner.borrow().lstat_result(path))
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.borrow().listdir(path)
    }
}

/// The world of a fixture line, its moves armed.
pub fn moving_world(case: &Value) -> Moving {
    let fs = Moving::new(world(case));
    if let Some(Value::Object(mv)) = case.get("mv") {
        for (rel, v) in mv {
            let data = super::unhex(v["hex"].as_str().unwrap());
            fs.moves
                .borrow_mut()
                .insert(format!("{MEM_ROOT}/{rel}").into_bytes(), data);
        }
    }
    fs
}

pub fn git() -> GitFacts {
    GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    }
}

fn spelled(path: &[u8]) -> Value {
    json!(String::from_utf8(replaced(path, MEM_ROOT.as_bytes(), b"<root>")).unwrap())
}

fn hexes(names: &[Vec<u8>]) -> Value {
    json!(names.iter().map(|n| super::hex(n)).collect::<Vec<_>>())
}

/// Each query's answer over one memo, `~` naming `home`.
pub fn answers(fs: &Moving, case: &Value) -> Vec<Value> {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: MEM_ROOT.as_bytes(),
    };
    let git = git();
    let hung = Hung::default();
    let rule = Rule {
        ctx: &ctx,
        answers: &hung,
        records: &NoRecords,
        env: None,
        git: &git,
    };
    let env = Env::default();
    let home = case["home"]
        .as_str()
        .map(|h| h.replace("<root>", MEM_ROOT).into_bytes());
    let expand = |p: &[u8]| -> Vec<u8> {
        match &home {
            Some(h) if p == b"~" || p.starts_with(b"~/") => {
                let mut out = h.clone();
                out.extend_from_slice(&p[1..]);
                out
            }
            _ => p.to_vec(),
        }
    };
    let mut memo = SkipMemo::new();
    let mut out = Vec::new();
    for q in case["q"].as_array().unwrap() {
        let p = q["p"].as_str().unwrap();
        let (op, raw) = p.split_once(':').unwrap();
        let args: Vec<Vec<u8>> = raw
            .split('|')
            .map(|a| a.replace("<root>", MEM_ROOT).into_bytes())
            .collect();
        let v = match op {
            "S" => match locate(&rule, &env, &args[0], true, false) {
                None => Value::Null,
                Some(found) => json!(sparse_enabled(&ctx, &found.gd)),
            },
            "B" => match parts_below(&ctx, &args[0], &args[1]).expect("POSIX answers") {
                None => Value::Null,
                Some(parts) => json!(parts
                    .iter()
                    .map(|p| String::from_utf8(p.clone()).unwrap())
                    .collect::<Vec<_>>()),
            },
            "K" => {
                let index = match locate(&rule, &env, &args[0], true, false) {
                    None => None,
                    Some(found) => {
                        fs.armed.set(true);
                        let got = skip_index(&rule, &found.gd, &mut memo).expect("POSIX answers");
                        fs.armed.set(false);
                        got
                    }
                };
                json!({
                    "i": index.map_or(Value::Null, |i| json!({"s": hexes(&i.skipped), "k": hexes(&i.kept)})),
                    "m": memo.keys().into_iter().map(spelled).collect::<Vec<_>>(),
                })
            }
            "A" => {
                fs.armed.set(true);
                let got = absent_by_sparsity(&rule, &env, &expand, &mut memo, &args[0])
                    .expect("POSIX answers");
                fs.armed.set(false);
                json!(got)
            }
            other => panic!("op {other}"),
        };
        out.push(json!({"p": p, "v": v}));
    }
    out
}
