//! Compare three identity probes against Python on real local Git trees.
#![cfg(unix)]

mod common;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::{fsencode, NoRecords};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::Env as HeadEnv;
use bettermemory_core::gitsystem::ask::Env;
use bettermemory_core::origin_dates::OriginGit;
use bettermemory_core::origin_git::{Context, Counts, OriginRunner, RepositoryFiles};
use bettermemory_core::origin_gitfiles::root::{ConfigMemo, NotADirectorySeen};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::{parity::Ledger, stores::Scratch};
use serde_json::{json, Value};
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::symlink;
use std::path::Path;
use std::process::Command;
use std::sync::atomic::AtomicI64;

fn os(value: &[u32]) -> OsString {
    OsString::from_vec(fsencode(value).unwrap())
}

struct LocalGit {
    errors: Vec<Vec<u8>>,
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
        let mut command = Command::new(os(&argv[0]));
        command
            .args(argv[1..].iter().map(|arg| os(arg)))
            .current_dir(os(cwd.unwrap()))
            .env_clear()
            .envs(env.iter().map(|(k, v)| (os(k), os(v))));
        match command.output() {
            Ok(done) => {
                if argv.last() == Some(&cps("--show-toplevel")) {
                    self.errors.push(done.stderr.clone());
                }
                Spawned::Exited {
                    returncode: i64::from(done.status.code().unwrap()),
                    stdout: done.stdout,
                    stderr: done.stderr,
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Spawned::NotFound(cps(&e.to_string()))
            }
            Err(e) => Spawned::OsError(cps(&e.to_string())),
        }
    }
}

fn observe(root: &Path, named: &Value, env: &Env, git: &GitFacts) -> Value {
    let cwd = root.join(named[1].as_str().unwrap());
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: root.as_os_str().as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &StdFs,
        records: &NoRecords,
        env: None,
        git,
    };
    let mut memo = ConfigMemo::new();
    let head_env = HeadEnv::default();
    let mut files = RepositoryFiles {
        rule: &rule,
        config_memo: &mut memo,
        head_env: &head_env,
        darwin: cfg!(target_os = "macos"),
    };
    let mut process = LocalGit { errors: Vec::new() };
    let mut seen = NotADirectorySeen::new();
    let mut counts = Counts::default();
    let scope = Scope::default();
    let global = AtomicI64::new(0);
    let mut runner = OriginRunner::new(
        Context {
            flavour: Flavour::Posix,
            scope: &scope,
            clock: &|| 0.,
            env,
            global_unanswered: &global,
            first_not_a_directory: &mut seen,
        },
        &mut process,
        &mut files,
        &mut counts,
    );
    let cwd = cwd.as_os_str().as_bytes();
    let (top, unknown, reason) = runner.probe_worktree_root(cwd).unwrap();
    let branch = runner.branch(cwd).unwrap();
    let (url, others, remote_unknown) = runner.remote_url_and_alternates(cwd).unwrap();
    let totals = json!([
        runner.unanswered_git_calls(),
        runner.failed_git_calls(),
        runner.counts().0,
        runner.counts().1,
        runner.unanswered()
    ]);
    let reason = reason.as_ref().map(|reason| {
        let error = String::from_utf8_lossy(&process.errors[0]);
        assert_eq!(
            to_string(reason).unwrap(),
            error.trim().lines().next().unwrap()
        );
        "<first stderr line>"
    });
    let text = |s: &PyStr| {
        to_string(s)
            .unwrap()
            .replace(root.to_str().unwrap(), "<root>")
    };
    json!([
        [
            [top.as_ref().map(text), unknown, reason],
            branch.as_ref().map(text),
            [
                url.as_ref().map(text),
                others.iter().map(text).collect::<Vec<_>>(),
                remote_unknown
            ]
        ],
        totals
    ])
}

#[test]
fn normal_repositories() {
    const FILE: &str = "origin_git_posix.jsonl";
    let dir = std::env::temp_dir().join(format!(
        "bettermemory-origin-git-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir(&dir).unwrap();
    let tmp = Scratch(dir);
    let root = tmp.0.canonicalize().unwrap();
    let repo = root.join("repo");
    for dir in ["home", "empty", "repo", "refused"] {
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
        ("GIT_AUTHOR_DATE", "2001-02-03T04:05:06Z"),
        ("GIT_COMMITTER_DATE", "2001-02-03T04:05:06Z"),
    ]
    .map(|(k, v)| (cps(k), cps(v)))
    .to_vec();
    let git = |args: &[&str]| {
        let done = Command::new("git")
            .current_dir(&repo)
            .env_clear()
            .envs(env.iter().map(|(k, v)| (os(k), os(v))))
            .args(args)
            .output()
            .unwrap();
        assert!(
            done.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&done.stderr)
        );
        done.stdout
    };
    let version = String::from_utf8(git(&["--version"])).unwrap();
    let numbers: Vec<_> = version
        .split_whitespace()
        .nth(2)
        .unwrap()
        .split('.')
        .take(2)
        .map(|n| n.parse().unwrap())
        .collect();
    let facts = GitFacts {
        release: Some((numbers[0], numbers[1])),
        reads_compat: Some((numbers[0], numbers[1]) >= (2, 45)),
    };
    let rows = common::jsonl(FILE);
    let mut ledger = Ledger::new("normal_repositories", FILE);
    for row in &rows[1..] {
        let name = row[0].as_str().unwrap();
        let mut current = env.clone();
        match name {
            "outside" | "missing" => {}
            "unborn" => {
                git(&["init", "-q", "-b", "trunk"]);
            }
            "alternate" => {
                git(&["remote", "add", "backup", "https://example.invalid/first"]);
                git(&[
                    "config",
                    "--add",
                    "remote.backup.url",
                    "https://example.invalid/second ",
                ]);
            }
            "rewrite" => {
                git(&["remote", "add", "origin", "alias:repo"]);
                git(&["config", "url.https://example.invalid/.insteadOf", "alias:"]);
                git(&[
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "root",
                ]);
                std::fs::create_dir(repo.join("child")).unwrap();
            }
            "linked" => {
                git(&[
                    "worktree",
                    "add",
                    "-qb",
                    "linked",
                    root.join("linked").to_str().unwrap(),
                ]);
            }
            "detached" => {
                git(&["checkout", "--detach", "-q"]);
            }
            "refused-entry" => {
                symlink(repo.join(".git"), root.join("refused/.git")).unwrap();
            }
            "ownership" => {
                current.push((cps("GIT_TEST_ASSUME_DIFFERENT_OWNER"), cps("1")));
            }
            "file" => std::fs::write(root.join("regular"), []).unwrap(),
            _ => panic!("unknown fixture {name}"),
        }
        ledger.case(name, || {
            assert_eq!(
                observe(&root, row, &current, &facts),
                json!([row[3], row[4]])
            )
        });
    }
    assert_eq!(
        rows[0]["scenarios"].as_u64().unwrap(),
        (rows.len() - 1) as u64
    );
    ledger.finish(rows.len() - 1);
}
