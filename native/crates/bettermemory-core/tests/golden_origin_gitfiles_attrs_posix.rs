//! Every scenario of `origin_gitfiles_attrs_posix.jsonl` (class i, POSIX
//! only), replayed on a scratch tree through `StdFs`:
//! `attribute_files_signature`, `gitattributes_signature`,
//! `gitdir_at_root`, `index_gitmodules` (with a file system that appends
//! a byte to the index after each prefix read, as the generator patches
//! `_read_prefix`) and the index memo's keys. The git on PATH is release
//! 2.54, as the generator patches it. A run as root, which reads mode-000
//! entries, skips the comparison and records it as a gap of class os.

#![cfg(unix)]

mod common;

use std::io::Write;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

use bettermemory_core::fsdata::{
    Access, Fs, Kind, OsError, PyErr, Refused, Stat, StatResult, StdFile, StdFs,
};
use bettermemory_core::gitconfig::legacy::Stamp;
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host};
use bettermemory_core::origin_gitfiles::attrs::{
    attribute_files_signature, gitattributes_signature, gitdir_at_root, index_gitmodules,
    AttributeFiles, IndexFailure, IndexGitmodules, IndexMemo,
};
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::root::ConfigMemo;
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "origin_gitfiles_attrs_posix.jsonl";
const PAUSE: Duration = Duration::from_millis(50);
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

struct Scratch(PathBuf);

impl Scratch {
    fn new(n: usize) -> Scratch {
        let dir = std::env::temp_dir().join(format!("ogf-attrs-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).expect("a new scratch directory");
        Scratch(std::fs::canonicalize(&dir).expect("a resolved scratch directory"))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::process::Command::new("chmod")
            .args(["-R", "u+rwx"])
            .arg(&self.0)
            .status();
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn running_as_root(base: &Path) -> bool {
    let probe = base.join("probe");
    std::fs::write(&probe, b"x").unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o000)).unwrap();
    let seen = std::fs::read(&probe).is_ok();
    std::fs::remove_file(&probe).unwrap();
    seen
}

/// `gen_origin_gitfiles._runs` read back.
fn unruns(runs: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for run in runs.as_array().unwrap() {
        match run {
            Value::String(hex) => out.extend(common::unhex(hex)),
            Value::Array(pair) => {
                let byte = pair[0].as_u64().unwrap() as u8;
                out.extend(std::iter::repeat_n(
                    byte,
                    pair[1].as_u64().unwrap() as usize,
                ));
            }
            other => panic!("no run {other}"),
        }
    }
    out
}

/// `gen_origin_gitfiles._entry(name, version=version, oid=oid)`.
fn entry(name: &[u8], version: u32, oid: &[u8]) -> Vec<u8> {
    let mut out = vec![0u8; 24];
    out.extend(0o100_644u32.to_be_bytes());
    out.extend([0u8; 12]);
    out.extend(oid);
    out.extend((name.len().min(0x0FFF) as u16).to_be_bytes());
    if version == 4 {
        out.push(0);
        out.extend(name);
        out.push(0);
        return out;
    }
    let pad = (out.len() + name.len() + 8) & !7;
    out.extend(name);
    out.resize(pad, 0);
    out
}

/// The generator's `_big_index(count, version)`.
fn big_index(count: usize, version: u32) -> Vec<u8> {
    let mut entries: Vec<Vec<u8>> = (0..count)
        .map(|i| entry(format!(".a{i:05}").as_bytes(), version, &[0x11; 20]))
        .collect();
    let oid: Vec<u8> = (0..20).collect();
    entries.push(entry(b".gitmodules", version, &oid));
    let mut out = b"DIRC".to_vec();
    out.extend(version.to_be_bytes());
    out.extend((entries.len() as u32).to_be_bytes());
    out.extend(entries.concat());
    out
}

/// `StdFs`, appending a byte to `index` after each `read`.
struct Appending {
    index: PathBuf,
}

impl Fs for Appending {
    type File = StdFile;
    fn open(&self, path: &[u8]) -> Result<StdFile, PyErr> {
        StdFs.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<StdFile, OsError> {
        StdFs.open_result(path)
    }
    fn fstat(&self, file: &mut StdFile) -> Result<Stat, PyErr> {
        StdFs.fstat(file)
    }
    fn fstat_full(&self, file: &mut StdFile) -> Option<Result<StatResult, OsError>> {
        StdFs.fstat_full(file)
    }
    fn read(&self, file: &mut StdFile, max: usize) -> Result<Vec<u8>, PyErr> {
        let data = StdFs.read(file, max);
        let mut fh = std::fs::OpenOptions::new()
            .append(true)
            .open(&self.index)
            .unwrap();
        fh.write_all(b"\0").unwrap();
        data
    }
    fn read_once(&self, file: &mut StdFile, max: usize) -> Result<Vec<u8>, OsError> {
        StdFs.read_once(file, max)
    }
    fn rewind(&self, file: &mut StdFile) -> Result<(), Refused> {
        StdFs.rewind(file)
    }
    fn close(&self, file: StdFile) -> Result<(), OsError> {
        StdFs.close(file)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        StdFs.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<StdFile, OsError> {
        StdFs.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        StdFs.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        StdFs.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        StdFs.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        StdFs.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        StdFs.listdir(path)
    }
}

#[derive(PartialEq)]
enum Sig {
    Attrs(Box<Option<AttributeFiles>>),
    Gattr(Option<Vec<Option<Stamp>>>),
}

struct Run<'a> {
    root: &'a Path,
    real: String,
    euid: u32,
    config: ConfigMemo,
    index: IndexMemo,
    sigs: Vec<Sig>,
}

fn spelled(s: &Option<Stamp>) -> Value {
    s.map_or(Value::Null, |_| json!("s"))
}

impl Run<'_> {
    fn text(&self, path: &[u8]) -> String {
        String::from_utf8(path.to_vec())
            .expect("a UTF-8 path")
            .replace(&self.real, "<root>")
    }

    fn held(&self, held: Option<Held>) -> Value {
        held.map_or(Value::Null, |h| {
            let mut keys: Vec<String> = h.keys().map(|k| self.text(k)).collect();
            keys.sort();
            json!(keys)
        })
    }

    fn env(&self, env: &Value) -> (Option<Vec<u8>>, Option<Vec<u8>>, Env) {
        let get = |name: &str| {
            env[name]
                .as_str()
                .map(|v| v.replace("<root>", &self.real).into_bytes())
        };
        let discovery = Env {
            git_dir: get("GIT_DIR"),
            ..Env::default()
        };
        (get("XDG_CONFIG_HOME"), get("HOME"), discovery)
    }

    fn first(&self, sig: &Sig) -> i64 {
        self.sigs
            .iter()
            .position(|s| s == sig)
            .map_or(-1, |i| i as i64)
    }

    fn step(&mut self, step: &[Value]) -> Value {
        let kind = step[0].as_str().expect("a step");
        let real = self.real.clone();
        let at = |i: usize| self.root.join(step[i].as_str().expect("a path"));
        if matches!(
            kind,
            "dir" | "write" | "bytes" | "bigindex" | "link" | "fifo" | "chmod" | "rm"
        ) {
            std::thread::sleep(PAUSE);
        }
        match kind {
            "dir" => std::fs::create_dir_all(at(1)).unwrap(),
            "write" => {
                std::fs::write(at(1), step[2].as_str().unwrap().replace("<root>", &real)).unwrap()
            }
            "bytes" => std::fs::write(at(1), unruns(&step[2])).unwrap(),
            "bigindex" => {
                let n = step[2].as_u64().unwrap() as usize;
                std::fs::write(at(1), big_index(n, step[3].as_u64().unwrap() as u32)).unwrap()
            }
            "link" => std::os::unix::fs::symlink(step[2].as_str().unwrap(), at(1)).unwrap(),
            "fifo" => {
                let status = std::process::Command::new("mkfifo")
                    .arg(at(1))
                    .status()
                    .expect("mkfifo");
                assert!(status.success());
            }
            "chmod" => {
                let mode = step[2].as_u64().unwrap() as u32;
                std::fs::set_permissions(at(1), std::fs::Permissions::from_mode(mode)).unwrap();
            }
            "rm" => std::fs::remove_file(at(1)).unwrap(),
            "imemo" => {
                return json!(self
                    .index
                    .keys()
                    .iter()
                    .map(|k| self.text(k))
                    .collect::<Vec<_>>())
            }
            _ => return self.query(kind, step),
        }
        Value::Null
    }

    fn query(&mut self, kind: &str, step: &[Value]) -> Value {
        let path = |i: usize| {
            self.root
                .join(step[i].as_str().unwrap())
                .to_str()
                .unwrap()
                .as_bytes()
                .to_vec()
        };
        let real = self.real.clone();
        let ctx = Ctx {
            fs: &StdFs,
            flavour: bettermemory_core::fsdata::Flavour::Posix,
            cwd: real.as_bytes(),
        };
        let rule = Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &GIT,
        };
        let host = Host {
            euid: self.euid,
            owners: &StdFs,
        };
        match kind {
            "attrs" => {
                let (xdg, home, env) = self.env(&step[3]);
                let mut held = step[4].as_bool().unwrap().then(Held::new);
                let sig = attribute_files_signature(
                    &mut self.config,
                    &rule,
                    &host,
                    &env,
                    xdg.as_deref(),
                    home.as_deref(),
                    &path(1),
                    &path(2),
                    held.as_mut(),
                )
                .expect("ported");
                let shown = sig.as_ref().map_or(Value::Null, |s| {
                    json!([
                        spelled(&s.config),
                        spelled(&s.worktree_config),
                        spelled(&s.info_attributes),
                        s.global_file.as_deref().map(|g| self.text(g)),
                        spelled(&s.global_attributes)
                    ])
                });
                self.answer(Sig::Attrs(Box::new(sig)), shown, held)
            }
            "gattr" => {
                let specs: Vec<Vec<u8>> = step[2]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|s| s.as_str().unwrap().as_bytes().to_vec())
                    .collect();
                let specs: Vec<&[u8]> = specs.iter().map(Vec::as_slice).collect();
                let mut held = step[3].as_bool().unwrap().then(Held::new);
                let sig =
                    gitattributes_signature(&ctx, &path(1), &specs, held.as_mut()).expect("ported");
                let shown = sig.as_ref().map_or(Value::Null, |s| {
                    json!(s.iter().map(spelled).collect::<Vec<_>>())
                });
                self.answer(Sig::Gattr(sig), shown, held)
            }
            "gar" => {
                let (_, _, env) = self.env(&step[2]);
                match gitdir_at_root(&rule, &host, &env, &path(1)) {
                    None => Value::Null,
                    Some(found) => {
                        json!([self.text(&found.gd.gitdir), self.text(&found.gd.commondir)])
                    }
                }
            }
            "index" => {
                let Some(found) = gitdir_at_root(&rule, &host, &Env::default(), &path(1)) else {
                    return json!("no repository");
                };
                let answer = if step[2].as_bool().unwrap() {
                    let fs = Appending {
                        index: PathBuf::from(String::from_utf8(found.gd.gitdir.clone()).unwrap())
                            .join("index"),
                    };
                    let ctx = Ctx {
                        fs: &fs,
                        flavour: ctx.flavour,
                        cwd: ctx.cwd,
                    };
                    index_gitmodules(&mut self.index, &mut self.config, &ctx, &found.gd, &GIT)
                } else {
                    index_gitmodules(&mut self.index, &mut self.config, &ctx, &found.gd, &GIT)
                };
                match answer {
                    Ok(None) => Value::Null,
                    Ok(Some(IndexGitmodules::Unmerged)) => json!("unmerged"),
                    Ok(Some(IndexGitmodules::Entry(mode, oid))) => json!([mode, oid]),
                    Err(IndexFailure::Unsettled) => json!("unsettled"),
                    Err(IndexFailure::Raised(class)) => json!(class.name()),
                    Err(IndexFailure::NotPorted) => panic!("not ported"),
                }
            }
            other => panic!("no step {other}"),
        }
    }

    fn answer(&mut self, sig: Sig, shown: Value, held: Option<Held>) -> Value {
        let first = self.first(&sig);
        self.sigs.push(sig);
        json!([shown, first, self.held(held)])
    }
}

#[test]
fn origin_gitfiles_attrs_posix_every_case() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    let ledger = Ledger::new("golden_origin_gitfiles_attrs_posix", FILE);
    let base = Scratch::new(usize::MAX);
    let as_root = running_as_root(&base.0);
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
            config: ConfigMemo::new(),
            index: IndexMemo::new(),
            sigs: Vec::new(),
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
            "golden_origin_gitfiles_attrs_posix",
            FILE,
            "os",
            0,
            &[format!("{count} scenarios")],
            &["a run as root reads mode-000 entries; the scenarios are not compared".to_owned()],
        );
    }
}
