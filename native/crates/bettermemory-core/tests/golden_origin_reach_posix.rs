//! `origin_reach_posix.jsonl`: the root and head reads, `commit_reachable`
//! and the reachable walk asking the host's git in repositories made here
//! by the steps each row records (linear, a merge, a branch that is no
//! ancestor, a shallow file cutting a merge's side, a shallow clone, a
//! linked worktree, `core.worktree` set, a bare repository, no commit, a
//! machine config git's parser refuses, reads from a subdirectory).
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
use bettermemory_core::origin_gitfiles::root::{ConfigMemo, NotADirectorySeen};
use bettermemory_core::origin_reach::{self, RepositoryReads};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::origin_history_located::Scratch;
use common::parity::Ledger;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::process::Command;
use std::sync::atomic::AtomicI64;

/// `gen_origin_gitfiles_attrs.RELEASE`, which the generator pins.
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn os(value: &[u32]) -> OsString {
    OsString::from_vec(fsencode(value).unwrap())
}

struct LocalGit;
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
    root: std::path::PathBuf,
    env: Env,
    tick: u32,
    tags: BTreeMap<String, String>,
}

impl World {
    fn git(&self, at: &str, args: &[&str]) -> String {
        let stamp = format!("2001-02-03T04:05:{:02}Z", self.tick);
        let root = self.root.to_str().unwrap();
        let url = format!("file://{root}/repo");
        let done = Command::new("git")
            .current_dir(self.root.join(at))
            .env_clear()
            .envs(self.env.iter().map(|(k, v)| (os(k), os(v))))
            .env("GIT_AUTHOR_DATE", &stamp)
            .env("GIT_COMMITTER_DATE", &stamp)
            .args(["-c", "core.hooksPath=/dev/null"])
            .args(
                args.iter()
                    .map(|a| a.replace("<root>", root).replace("<repo-url>", &url)),
            )
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
                std::fs::write(
                    self.root.join(s(1)).join(".git/shallow"),
                    format!("{sha}\n"),
                )
                .unwrap();
            }
            "dir" => std::fs::create_dir(self.root.join(s(1))).unwrap(),
            "write" => std::fs::write(self.root.join(s(1)), s(2)).unwrap(),
            "rm" => std::fs::remove_file(self.root.join(s(1))).unwrap(),
            other => panic!("unknown step {other}"),
        }
        self.tick += 1;
    }

    fn sha(&self, name: &str) -> PyStr {
        cps(if name == "unknown" {
            "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        } else {
            &self.tags[name]
        })
    }
}

fn observe(world: &World, row: &Value) -> Value {
    let root = world.root.to_str().unwrap();
    let text = |s: &PyStr| to_string(s).unwrap().replace(root, "<root>");
    let at = world.root.join(row[2].as_str().unwrap());
    let at = at.as_os_str().as_bytes();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &StdFs,
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: std::fs::metadata(&world.root).unwrap().uid(),
        owners: &StdFs,
    };
    let machine = Machine {
        flavour: Flavour::Posix,
        home: Some(world.root.join("home").as_os_str().as_bytes().to_vec()),
        profile: None,
        user_home: &|_| None,
    };
    let inputs = Inputs {
        machine: &machine,
        env: remotes::Env {
            git_config_nosystem: Some(b"1".to_vec()),
            git_config_global: Some(format!("{root}/machine").into_bytes()),
            ..Default::default()
        },
        sources: Sources::default(),
        git: GIT,
        cwd: root.as_bytes().into(),
        asked_branch: None,
    };
    let head_env = HeadEnv::default();
    let (mut runner_memo, mut reads_memo) = (ConfigMemo::new(), ConfigMemo::new());
    let mut stamped = StampedReads::new();
    let mut clock = || 0.;
    let darwin = cfg!(target_os = "macos");
    let mut files = RepositoryFiles {
        rule: &rule,
        config_memo: &mut runner_memo,
        head_env: &head_env,
        darwin,
    };
    let mut reads = RepositoryReads {
        rule: &rule,
        host: &host,
        env: &head_env,
        config_memo: &mut reads_memo,
        machine_reads: &mut stamped,
        inputs: &inputs,
        clock: &mut clock,
        darwin,
    };
    let mut process = LocalGit;
    let mut seen = NotADirectorySeen::new();
    let mut counts = Counts::default();
    let (scope, global) = (Scope::default(), AtomicI64::new(0));
    let mut runner = OriginRunner::new(
        Context {
            flavour: Flavour::Posix,
            scope: &scope,
            clock: &|| 0.,
            env: &world.env,
            global_unanswered: &global,
            first_not_a_directory: &mut seen,
        },
        &mut process,
        &mut files,
        &mut counts,
    );
    let r = &mut runner;
    let top = origin_reach::repo_toplevel(r, &mut reads, Some(at)).unwrap();
    let pair = origin_reach::repo_toplevel_and_head(r, &mut reads, Some(at)).unwrap();
    let from_files = origin_reach::toplevel_and_head_from_files(&mut reads, Some(at)).unwrap();
    let head = origin_reach::head_sha(r, Some(at)).unwrap();
    let mut reached = Vec::new();
    for anchor in row[3].as_array().unwrap() {
        let sha = world.sha(anchor.as_str().unwrap());
        let answer = origin_reach::commit_reachable(r, &mut reads, Some(at), &sha).unwrap();
        reached.push(json!([anchor, answer]));
    }
    let mut walks = Vec::new();
    for walk in row[4].as_array().unwrap() {
        let (anchor, end) = (walk[0].as_str().unwrap(), walk[1].as_str().unwrap());
        let names = walk[2] == true;
        let (anchor_sha, end_sha) = (world.sha(anchor), world.sha(end));
        let found =
            origin_reach::walk_reachable(r, &mut reads, at, &anchor_sha, &end_sha, names).unwrap();
        let found = found.map(|w| {
            let touched: serde_json::Map<String, Value> = w
                .touched
                .iter()
                .map(|(p, s)| (text(p), json!(s.iter().map(&text).collect::<Vec<_>>())))
                .collect();
            json!([
                w.commits.iter().map(&text).collect::<Vec<_>>(),
                touched,
                w.listed
            ])
        });
        walks.push(json!([anchor, end, names, found]));
    }
    let both = |p: Option<(PyStr, PyStr)>| json!(p.map(|(a, b)| [text(&a), text(&b)]));
    let answers = json!({
        "top": top.as_ref().map(&text),
        "pair": both(pair),
        "files": both(from_files),
        "head": head.as_ref().map(&text),
        "reachable": reached,
        "walks": walks,
    });
    let totals = json!([
        runner.unanswered_git_calls(),
        runner.failed_git_calls(),
        runner.counts().0,
        runner.counts().1,
        runner.unanswered()
    ]);
    json!([answers, totals])
}

#[test]
fn local_repositories() {
    const FILE: &str = "origin_reach_posix.jsonl";
    let rows = common::jsonl(FILE);
    let scratch = Scratch::new("origin-reach");
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
        env,
        tick: 0,
        tags: BTreeMap::new(),
    };
    let mut ledger = Ledger::new("local_repositories", FILE);
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
        if root.join("moved").is_dir() {
            let sha = world.git("moved", &["rev-parse", "w1"]).trim().to_owned();
            world.tags.insert("w1".to_owned(), sha);
        }
        let label = row[0].as_str().unwrap();
        ledger.case(label, || {
            assert_eq!(observe(&world, row), json!([row[5], row[6]]), "{label}")
        });
    }
    assert_eq!(json!(world.tags), rows[0]["tags"]);
    assert_eq!(
        rows[0]["scenarios"].as_u64().unwrap() as usize,
        rows.len() - 1
    );
    ledger.finish(rows.len() - 1);
    assert!(!Path::new(&root).join("repo/.git/shallow").exists());
}
