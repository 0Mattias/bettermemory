//! Every scenario of `origin_gitfiles_walk_posix.jsonl` (class i, POSIX
//! only), replayed on a scratch tree through `StdFs`: `capture_files`
//! (as the effective user and as another), `walk_files_signature`, the
//! key a search records and `files_held` over it, and `located_head`. The
//! git on PATH is release 2.54, as the generator patches it. A run as
//! root skips the comparison and records it as a gap of class os.

#![cfg(unix)]

mod common;

use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitconfig::legacy::Stamp;
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host};
use bettermemory_core::githead::signature::{First, Signature};
use bettermemory_core::origin_gitfiles::attrs::{
    attribute_files_signature, gitattributes_signature, IndexGitmodules, IndexMemo,
};
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::root::ConfigMemo;
use bettermemory_core::origin_gitfiles::walk::{
    capture_files, files_held, located_head, walk_files_signature, Keyed, Machine, WalkFiles,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "origin_gitfiles_walk_posix.jsonl";
const PAUSE: Duration = Duration::from_millis(50);
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

struct Scratch(PathBuf);

impl Scratch {
    fn new(n: usize) -> Scratch {
        let dir = std::env::temp_dir().join(format!("ogf-walk-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).expect("a new scratch directory");
        Scratch(std::fs::canonicalize(&dir).expect("a resolved scratch directory"))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn unruns(runs: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for run in runs.as_array().unwrap() {
        match run {
            Value::String(hex) => out.extend(common::unhex(hex)),
            Value::Array(pair) => out.extend(std::iter::repeat_n(
                pair[0].as_u64().unwrap() as u8,
                pair[1].as_u64().unwrap() as usize,
            )),
            other => panic!("no run {other}"),
        }
    }
    out
}

#[derive(PartialEq)]
enum Sig {
    Capture(Box<Signature>),
    Walk(Box<Option<WalkFiles>>),
}

struct Run<'a> {
    root: &'a Path,
    real: String,
    euid: u32,
    home: Vec<u8>,
    config: ConfigMemo,
    index: IndexMemo,
    sigs: Vec<Sig>,
    stamps: Vec<Stamp>,
    keyed: Option<(Vec<u8>, Vec<u8>, Keyed)>,
}

fn s<T>(stamp: &Option<T>) -> Value {
    stamp.as_ref().map_or(Value::Null, |_| json!("s"))
}

fn stamp_fields(stamps: &mut Vec<Stamp>, stamp: Option<Stamp>) -> Value {
    let Some(stamp) = stamp else {
        return Value::Null;
    };
    let first = stamps
        .iter()
        .position(|s| *s == stamp)
        .unwrap_or(stamps.len());
    stamps.push(stamp);
    let kind = stamp[4] >> 12;
    if kind == 0o10 {
        json!({"eq": first, "s": stamp[2], "t": kind})
    } else {
        json!({"eq": first, "t": kind})
    }
}

fn digest(fields: &Value) -> String {
    common::hex(&Sha256::digest(serde_json::to_vec(fields).unwrap()))
}

impl Run<'_> {
    fn text(&self, path: &[u8]) -> String {
        String::from_utf8(path.to_vec())
            .expect("a UTF-8 path")
            .replace(&self.real, "<root>")
    }

    fn path(&self, rel: &Value) -> Vec<u8> {
        self.root
            .join(rel.as_str().unwrap())
            .to_str()
            .unwrap()
            .as_bytes()
            .to_vec()
    }

    fn first(&self, sig: &Sig) -> i64 {
        self.sigs
            .iter()
            .position(|s| s == sig)
            .map_or(-1, |i| i as i64)
    }

    fn change(&self, kind: &str, step: &[Value]) {
        std::thread::sleep(PAUSE);
        let at = self.root.join(step[1].as_str().unwrap());
        match kind {
            "dir" => std::fs::create_dir_all(at).unwrap(),
            "write" => {
                std::fs::write(at, step[2].as_str().unwrap().replace("<root>", &self.real)).unwrap()
            }
            "bytes" => std::fs::write(at, unruns(&step[2])).unwrap(),
            "rm" => std::fs::remove_file(at).unwrap(),
            other => panic!("no change {other}"),
        }
    }

    fn step(&mut self, step: &[Value]) -> Value {
        let kind = step[0].as_str().expect("a step");
        if matches!(kind, "dir" | "write" | "bytes" | "rm") {
            self.change(kind, step);
            return Value::Null;
        }
        let real = self.real.clone();
        let ctx = Ctx {
            fs: &StdFs,
            flavour: Flavour::Posix,
            cwd: real.as_bytes(),
        };
        let rule = Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &GIT,
        };
        let env = Env::default();
        let other = kind == "capture" && step[2].as_bool().unwrap();
        let host = Host {
            euid: if other { self.euid + 1 } else { self.euid },
            owners: &StdFs,
        };
        let home = self.home.clone();
        let machine = Machine {
            env: &env,
            index_file_set: false,
            xdg_config_home: None,
            home: Some(&home),
        };
        match kind {
            "held" => {
                let (cwd, root, keyed) = self.keyed.as_ref().expect("a key");
                let held = files_held(
                    &mut self.config,
                    &mut self.index,
                    &rule,
                    &host,
                    &machine,
                    cwd,
                    root,
                    keyed,
                )
                .expect("ported");
                json!(held)
            }
            "capture" => {
                let (sig, found) =
                    capture_files(&rule, &host, &env, &self.path(&step[1])).expect("no raise");
                let first = match &sig.first {
                    First::Outside => Value::Null,
                    First::GitDir(dir) => json!(self.text(dir)),
                    First::Undetermined(_) => json!("undetermined"),
                };
                let repo = found.map_or(Value::Null, |f| {
                    json!([
                        self.text(&f.gd.gitdir),
                        self.text(&f.gd.commondir),
                        self.text(&f.worktree_root)
                    ])
                });
                let stamps = &mut self.stamps;
                let config = stamp_fields(stamps, sig.config);
                let head_file = stamp_fields(stamps, sig.head_file);
                let refs = sig.refs.as_ref().map(|refs| {
                    refs.iter()
                        .map(|r| stamp_fields(stamps, *r))
                        .collect::<Vec<_>>()
                });
                let packed = stamp_fields(stamps, sig.packed);
                let field_digest = digest(&json!([
                    first,
                    sig.head.as_deref().map(common::hex),
                    config,
                    head_file,
                    refs,
                    packed,
                    sig.git_dir.as_deref().map(|v| self.text(v)),
                    sig.git_work_tree.as_deref().map(|v| self.text(v)),
                    sig.ceilings.as_deref().map(|v| self.text(v)),
                ]));
                let sig = Sig::Capture(Box::new(sig));
                let equal = self.first(&sig);
                self.sigs.push(sig);
                json!([first, equal, repo, field_digest])
            }
            "walk" => {
                let mut held = step[2].as_bool().unwrap().then(Held::new);
                let at = self.path(&step[1]);
                let walk = walk_files_signature(
                    &mut self.config,
                    &mut self.index,
                    &rule,
                    &host,
                    &env,
                    false,
                    &at,
                    held.as_mut(),
                )
                .expect("ported");
                let (spelled, fields) = walk.as_ref().map_or((Value::Null, Value::Null), |w| {
                    let slot = match &w.index {
                        None => Value::Null,
                        Some(IndexGitmodules::Unmerged) => json!("unmerged"),
                        Some(IndexGitmodules::Entry(mode, oid)) => json!([mode, oid]),
                    };
                    let spelled = json!([
                        s(&w.config),
                        s(&w.worktree_config),
                        s(&w.gitmodules),
                        slot,
                        s(&w.shallow)
                    ]);
                    let stamps = &mut self.stamps;
                    let fields = json!([
                        stamp_fields(stamps, w.config),
                        stamp_fields(stamps, w.worktree_config),
                        stamp_fields(stamps, w.gitmodules),
                        slot,
                        stamp_fields(stamps, w.shallow),
                    ]);
                    (spelled, fields)
                });
                let keys = held.map_or(Value::Null, |h| {
                    let mut keys: Vec<String> = h.keys().map(|k| self.text(k)).collect();
                    keys.sort();
                    json!(keys)
                });
                let sig = Sig::Walk(Box::new(walk));
                let equal = self.first(&sig);
                self.sigs.push(sig);
                json!([spelled, equal, keys, digest(&fields)])
            }
            "key" => {
                let cwd = self.path(&step[1]);
                let root = self.path(&step[2]);
                let specs: Vec<Vec<u8>> = step[3]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().as_bytes().to_vec())
                    .collect();
                let mut held = Held::new();
                let repository = attribute_files_signature(
                    &mut self.config,
                    &rule,
                    &host,
                    &env,
                    None,
                    Some(&home),
                    &cwd,
                    &root,
                    Some(&mut held),
                )
                .expect("ported");
                let refs: Vec<&[u8]> = specs.iter().map(Vec::as_slice).collect();
                let chain =
                    gitattributes_signature(&ctx, &root, &refs, Some(&mut held)).expect("ported");
                let walk = walk_files_signature(
                    &mut self.config,
                    &mut self.index,
                    &rule,
                    &host,
                    &env,
                    false,
                    &root,
                    Some(&mut held),
                )
                .expect("ported");
                let answer = json!([repository.is_none(), chain.is_none(), walk.is_none()]);
                let keyed = Keyed {
                    repository,
                    chains: vec![(specs, chain)],
                    walk_files: walk,
                    directories: held,
                };
                self.keyed = Some((cwd, root, keyed));
                answer
            }
            "head" => json!(located_head(&rule, &env, &self.path(&step[1]))),
            other => panic!("no step {other}"),
        }
    }
}

#[test]
fn origin_gitfiles_walk_posix_every_case() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    let ledger = Ledger::new("golden_origin_gitfiles_walk_posix", FILE);
    let base = Scratch::new(usize::MAX);
    let as_root = std::fs::metadata(&base.0).unwrap().uid() == 0;
    let euid = std::fs::metadata(&base.0).unwrap().uid();
    let mut failures = Vec::new();
    let mut count = 0u64;
    for (i, row) in lines[1..].iter().enumerate() {
        let row = row.as_array().expect("a row");
        assert_eq!(row[0], "scenario");
        count += 1;
        if as_root {
            continue;
        }
        let scratch = Scratch::new(i);
        let mut run = Run {
            root: &scratch.0,
            real: scratch.0.to_str().unwrap().to_owned(),
            euid,
            home: base.0.join("h").to_str().unwrap().as_bytes().to_vec(),
            config: ConfigMemo::new(),
            index: IndexMemo::new(),
            sigs: Vec::new(),
            stamps: Vec::new(),
            keyed: None,
        };
        let got: Vec<Value> = row[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| run.step(s.as_array().unwrap()))
            .collect();
        let got = json!(got);
        if got != row[2] {
            failures.push(format!("#{i}: native {got} reference {}", row[2]));
        }
    }
    assert_eq!(header["scenarios"].as_u64(), Some(count));
    assert!(count > 0, "no case");
    ledger.settle(if as_root { 0 } else { count as usize }, &failures);
    if as_root {
        common::parity::record(
            "golden_origin_gitfiles_walk_posix",
            FILE,
            "os",
            0,
            &[format!("{count} scenarios")],
            &[
                "a run as root passes every owner's check; the scenarios are not compared"
                    .to_owned(),
            ],
        );
    }
}
