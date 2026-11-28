//! `origin_reach_walk_posix.jsonl`: `commits_since_anchor` asking the
//! host's git in repositories made here by the steps each row records (a
//! linear history, a moved head, a `.gitmodules` written, a merge, a
//! rebased anchor, a shallow cut), the root and head read from the
//! repository's files, the files beside the history stamped, and faults
//! scripted on the walk where git cannot be made to fail (a timeout, an
//! exit 128, a file created and removed in the root, the index's times
//! set). The clock reads a call's fifth field, 0 where it has none, through
//! `WalkReads` and `RepositoryReads`. Each call's answer, the git processes
//! it started, the five tallies, the walks given up on and both memos'
//! lengths, the memos and tallies running on across the scenarios.
#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{self, Inputs, Sources};
use bettermemory_core::gitconfig::stamped::StampedReads;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::{fsencode, NoRecords};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env as HeadEnv, Host};
use bettermemory_core::gitsystem::ask::Env;
use bettermemory_core::origin_dates::OriginGit;
use bettermemory_core::origin_git::{Context, Counts, OriginRunner, RepositoryFiles};
use bettermemory_core::origin_gitfiles::attrs::IndexMemo;
use bettermemory_core::origin_gitfiles::root::{ConfigMemo, NotADirectorySeen};
use bettermemory_core::origin_history::{Unwalked, Walked};
use bettermemory_core::origin_reach::{commits_since_anchor, RepositoryReads, WalkMemo, WalkReads};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use common::origin_history_located::Scratch;
use common::origin_reach_walk::{given_up, walk_answer};
use common::parity::Ledger;
use serde_json::{json, Value};
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;
use std::process::Command;
use std::rc::Rc;
use std::sync::atomic::AtomicI64;
use std::time::{Duration, SystemTime};

const FILE: &str = "origin_reach_walk_posix.jsonl";

/// `gen_origin_reach._AGED`: 2001-02-03T04:05:06Z.
fn aged() -> SystemTime {
    SystemTime::UNIX_EPOCH + Duration::from_secs(981_173_106)
}

/// `os.utime(path, ns=(_AGED, _AGED))`, a folder's or a file's.
fn age(path: &std::path::Path) {
    let times = std::fs::FileTimes::new()
        .set_accessed(aged())
        .set_modified(aged());
    std::fs::File::open(path).unwrap().set_times(times).unwrap();
}

/// `gen_origin_gitfiles_attrs.RELEASE`, which the generator pins.
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn os(value: &[u32]) -> OsString {
    OsString::from_vec(fsencode(value).unwrap())
}

/// The host's git, with the row's fault scripted on the walk and every
/// process counted.
struct LocalGit {
    fault: Rc<RefCell<Option<String>>>,
    runs: Rc<Cell<usize>>,
}

impl GitProcess for LocalGit {
    fn run(
        &mut self,
        argv: &[PyStr],
        cwd: Option<&[u32]>,
        env: &Env,
        stdin: Option<&[u8]>,
        _timeout: f64,
    ) -> Spawned {
        assert!(stdin.is_none());
        self.runs.set(self.runs.get() + 1);
        if let Some(kind) = self.fault.borrow().as_deref() {
            if argv.contains(&cps("log")) {
                let names = argv.contains(&cps("--name-only"));
                let at = PathBuf::from(os(cwd.unwrap()));
                if kind == "root-touched" {
                    std::fs::write(at.join(".touched"), b"").unwrap();
                    std::fs::remove_file(at.join(".touched")).unwrap();
                }
                if kind == "index-touched" {
                    age(&at.join(".git/index"));
                }
                if kind == "log-timeout" || (kind == "names-timeout" && names) {
                    return Spawned::TimedOut;
                }
                if kind == "log-fails" || (kind == "names-fails" && names) {
                    return Spawned::Exited {
                        returncode: 128,
                        stdout: vec![],
                        stderr: b"fatal: scripted\n".to_vec(),
                    };
                }
            }
        }
        let done = Command::new(os(&argv[0]))
            .args(argv[1..].iter().map(|arg| os(arg)))
            .current_dir(os(cwd.unwrap()))
            .env_clear()
            .envs(env.iter().map(|(k, v)| (os(k), os(v))))
            .output();
        match done {
            Ok(done) => Spawned::Exited {
                returncode: i64::from(done.status.code().unwrap_or(-1)),
                stdout: done.stdout,
                stderr: done.stderr,
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Spawned::NotFound(cps(&e.to_string()))
            }
            Err(e) => Spawned::OsError(cps(&e.to_string())),
        }
    }
}

struct World {
    root: PathBuf,
    env: Env,
    tick: u32,
    tags: BTreeMap<String, String>,
}

impl World {
    fn git(&self, at: &str, args: &[&str]) -> String {
        let stamp = format!("2001-02-03T04:05:{:02}Z", self.tick);
        let done = Command::new("git")
            .current_dir(self.root.join(at))
            .env_clear()
            .envs(self.env.iter().map(|(k, v)| (os(k), os(v))))
            .env("GIT_AUTHOR_DATE", &stamp)
            .env("GIT_COMMITTER_DATE", &stamp)
            .args(["-c", "core.hooksPath=/dev/null"])
            .args(args)
            .output()
            .unwrap();
        assert!(
            done.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&done.stderr)
        );
        String::from_utf8(done.stdout).unwrap()
    }

    fn step(&mut self, step: &[Value]) {
        let s = |i: usize| step[i].as_str().unwrap();
        match s(0) {
            "git" => {
                let args: Vec<&str> = step[2..].iter().map(|a| a.as_str().unwrap()).collect();
                self.git(s(1), &args);
            }
            "commit" => {
                for (name, text) in step[3].as_object().unwrap() {
                    let path = self.root.join(s(1)).join(name);
                    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                    std::fs::write(path, text.as_str().unwrap()).unwrap();
                }
                self.git(s(1), &["add", "-A"]);
                self.tick += 1;
                self.git(s(1), &["commit", "-qm", s(2)]);
                self.git(s(1), &["tag", s(2)]);
            }
            "shallow" => {
                let sha = &self.tags[s(2)];
                let path = self.root.join(s(1)).join(".git/shallow");
                std::fs::write(path, format!("{sha}\n")).unwrap();
            }
            "write" => std::fs::write(self.root.join(s(1)), s(2)).unwrap(),
            "rm" => std::fs::remove_file(self.root.join(s(1))).unwrap(),
            "age" => age(&self.root.join(s(1))),
            other => panic!("unknown step {other}"),
        }
        self.tick += 1;
    }
}

#[test]
fn walks_in_local_repositories() {
    let rows = common::jsonl(FILE);
    let scratch = Scratch::new("origin-reach-walk");
    let root = scratch.0.clone();
    for dir in ["home", "empty"] {
        std::fs::create_dir(root.join(dir)).unwrap();
    }
    let path = std::env::var("PATH").unwrap();
    let home = root.join("home");
    let env: Env = [
        ("PATH", path.as_str()),
        ("HOME", home.to_str().unwrap()),
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
        ("LC_ALL", "C"),
        ("GIT_AUTHOR_NAME", "Fixture"),
        ("GIT_AUTHOR_EMAIL", "fixture@example.invalid"),
        ("GIT_COMMITTER_NAME", "Fixture"),
        ("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
    ]
    .map(|(k, v)| (cps(k), cps(v)))
    .to_vec();
    let mut world = World {
        root: root.clone(),
        env: env.clone(),
        tick: 0,
        tags: BTreeMap::new(),
    };
    let top = root.to_str().unwrap();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: top.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &StdFs,
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: std::fs::metadata(&root).unwrap().uid(),
        owners: &StdFs,
    };
    let machine = Machine {
        flavour: Flavour::Posix,
        home: Some(home.as_os_str().as_bytes().to_vec()),
        profile: None,
        user_home: &|_| None,
    };
    let inputs = Inputs {
        machine: &machine,
        env: remotes::Env {
            git_config_nosystem: Some(b"1".to_vec()),
            git_config_global: Some(format!("{top}/machine").into_bytes()),
            ..Default::default()
        },
        sources: Sources::default(),
        git: GIT,
        cwd: top.as_bytes().into(),
        asked_branch: None,
    };
    let head_env = HeadEnv::default();
    let (mut runner_memo, mut reads_memo) = (ConfigMemo::new(), ConfigMemo::new());
    let mut index_memo = IndexMemo::new();
    let mut stamped = StampedReads::new();
    let now = Rc::new(Cell::new(0.0));
    let mut clock = {
        let now = now.clone();
        move || now.get()
    };
    let darwin = cfg!(target_os = "macos");
    let mut files = RepositoryFiles {
        rule: &rule,
        config_memo: &mut runner_memo,
        head_env: &head_env,
        darwin,
    };
    let mut repository = RepositoryReads {
        rule: &rule,
        host: &host,
        env: &head_env,
        config_memo: &mut reads_memo,
        machine_reads: &mut stamped,
        inputs: &inputs,
        clock: &mut clock,
        darwin,
    };
    let mut reads = WalkReads {
        reads: &mut repository,
        index_memo: &mut index_memo,
        index_file_set: false,
    };
    let (fault, runs) = (Rc::new(RefCell::new(None)), Rc::new(Cell::new(0)));
    let mut process = LocalGit {
        fault: fault.clone(),
        runs: runs.clone(),
    };
    let mut seen = NotADirectorySeen::new();
    let mut counts = Counts::default();
    let (scope, global) = (Scope::default(), AtomicI64::new(0));
    let mut runner = OriginRunner::new(
        Context {
            flavour: Flavour::Posix,
            scope: &scope,
            clock: &|| 0.,
            env: &env,
            global_unanswered: &global,
            first_not_a_directory: &mut seen,
        },
        &mut process,
        &mut files,
        &mut counts,
    );
    let memo = WalkMemo::new();
    let mut unwalked = Unwalked::new();
    let mut ledger = Ledger::new("walks_in_local_repositories", FILE);
    for row in &rows[1..] {
        for step in row[1].as_array().unwrap() {
            world.step(step.as_array().unwrap());
        }
        if root.join("repo").is_dir() {
            for tag in world.git("repo", &["tag"]).split_whitespace() {
                let sha = world.git("repo", &["rev-parse", tag]).trim().to_owned();
                world.tags.insert(tag.to_owned(), sha);
            }
        }
        let label = row[0].as_str().unwrap();
        let mut walked = Walked::new();
        let mut answers = Vec::new();
        for call in row[2].as_array().unwrap() {
            let at = root.join(call[0].as_str().unwrap());
            let tag = call[1].as_str().unwrap();
            let anchor = match tag {
                "unknown" => "e".repeat(40),
                tag => world.tags[tag].clone(),
            };
            *fault.borrow_mut() = call[3].as_str().map(str::to_owned);
            runs.set(0);
            now.set(call.get(4).and_then(Value::as_f64).unwrap_or(0.0));
            let passed = (call[2] == true).then_some(&mut walked);
            let walk = commits_since_anchor(
                &memo,
                &mut runner,
                &mut reads,
                &mut unwalked,
                Some(at.as_os_str().as_bytes()),
                &cps(&anchor),
                (None, None),
                passed,
            )
            .unwrap();
            let (timed, missing) = runner.counts();
            let totals = json!([
                runner.unanswered_git_calls(),
                runner.failed_git_calls(),
                timed,
                missing,
                runner.unanswered()
            ]);
            let mut answer = vec![walk_answer(walk.as_deref()), json!(runs.get()), totals];
            answer.extend(given_up(&unwalked).as_array().unwrap().iter().cloned());
            answer.push(json!(memo.memo_keys().len()));
            answer.push(json!(memo.timed_out_entries().len()));
            answers.push(json!(answer));
        }
        *fault.borrow_mut() = None;
        ledger.case(label, || assert_eq!(json!(answers), row[3], "{label}"));
    }
    assert_eq!(json!(world.tags), rows[0]["tags"]);
    let scenarios = rows[0]["scenarios"].as_u64().unwrap() as usize;
    assert_eq!(scenarios, rows.len() - 1);
    ledger.finish(scenarios);
}
