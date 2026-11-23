//! The real Python helpers over scripted Git replies, including both counts
//! of could-not-run, and pathlib's versioned directory-read boundary.

mod common;

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::{Flavour, OsError, PyErr};
use bettermemory_core::git_calls::RunRaised;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitfile_rule::discovery::Reading;
use bettermemory_core::gitfile_rule::gitdir::Failure;
use bettermemory_core::gitfile_rule::trust::Refusal;
use bettermemory_core::gitsystem::ask::Env;
use bettermemory_core::origin_dates::OriginGit;
use bettermemory_core::origin_git::{
    Completed, Context, Counts, Error, Files, Level, Logger, OriginRunner, Output,
};
use bettermemory_core::origin_gitfiles::root::NotADirectorySeen;
use bettermemory_core::origin_history::{AS_STORED_CONFIG, AS_STORED_ENV};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::atomic::AtomicI64;

#[derive(Default)]
struct Leaves {
    refuse: bool,
    directory_error: Option<OsError>,
}

impl Files for Leaves {
    fn reading(&mut self, _: &[u8]) -> Result<Option<Reading>, Error> {
        Ok(self.refuse.then(|| Reading {
            why: Some(Refusal {
                why: b"separate checkout".to_vec(),
                format_unknown: false,
            }),
            ..Reading::default()
        }))
    }
    fn stat_dir(&self, _: &[u8]) -> Result<bool, Failure> {
        self.directory_error
            .map_or(Ok(true), |error| Err(Failure::from(error)))
    }
    fn refused_root(&mut self, _: &[u8]) -> Result<(Option<Vec<u8>>, bool), Error> {
        panic!("unexpected refused root")
    }
    fn agrees(&mut self, _: &[u8], _: &[u8]) -> Result<bool, Error> {
        panic!("unexpected comparison")
    }
    fn resolve(&mut self, _: &[u8]) -> Result<Vec<u8>, Unresolved> {
        panic!("unexpected path resolution")
    }
}

struct Process {
    script: VecDeque<Value>,
    calls: Vec<Value>,
    input: Option<Vec<u8>>,
    cwd: PyStr,
}
impl GitProcess for Process {
    fn run(
        &mut self,
        argv: &[PyStr],
        cwd: Option<&[u32]>,
        env: &Env,
        stdin: Option<&[u8]>,
        timeout: f64,
    ) -> Spawned {
        assert_eq!(cwd, Some(self.cwd.as_slice()));
        assert_eq!(timeout, 1.0);
        assert_eq!(stdin, self.input.as_deref());
        assert!(!env.iter().any(|(k, _)| k == &cps("GIT_DIR")));
        for (key, value) in AS_STORED_ENV {
            assert!(env.contains(&(cps(key), cps(value))));
        }
        let prefix: Vec<_> = ["git"]
            .into_iter()
            .chain(bettermemory_core::gitsystem::ask::PINS)
            .chain(AS_STORED_CONFIG)
            .map(cps)
            .collect();
        let rest = argv
            .strip_prefix(prefix.as_slice())
            .expect("pins and as-stored config");
        self.calls.push(json!(rest
            .iter()
            .map(|s| to_string(s).unwrap())
            .collect::<Vec<_>>()));
        let reply = self.script.pop_front().expect("a Python subprocess");
        match reply.as_str() {
            Some("timeout") => Spawned::TimedOut,
            Some("missing") => Spawned::NotFound(cps("no executable")),
            Some("os") => Spawned::OsError(cps("not available")),
            Some("other") => Spawned::Other,
            Some(kind) => panic!("unexpected process {kind}"),
            None => Spawned::Exited {
                returncode: reply[0].as_i64().unwrap(),
                stdout: reply[1].as_str().unwrap().as_bytes().to_vec(),
                stderr: reply[2].as_str().unwrap().as_bytes().to_vec(),
            },
        }
    }
}
fn completed(done: Option<Completed>) -> Value {
    match done {
        None => Value::Null,
        Some(r) => {
            let args: Vec<_> = r.args.iter().map(|s| to_string(s).unwrap()).collect();
            let extra = r
                .args
                .iter()
                .position(|s| s == &cps("core.useReplaceRefs=false"))
                .map_or(0, |i| i + AS_STORED_CONFIG.len() - 1);
            json!([
                &args[extra..],
                r.returncode,
                to_string(&r.stdout),
                to_string(&r.stderr)
            ])
        }
    }
}
fn optional(value: Option<PyStr>) -> Value {
    json!(value.map(|s| to_string(&s).unwrap()))
}
fn script(case: &Value) {
    let scope = Scope::default();
    let global = AtomicI64::new(0);
    let env = vec![(cps("GIT_DIR"), cps("/external"))];
    let mut seen = NotADirectorySeen::new();
    let mut counts = Counts::default();
    let mut files = Leaves {
        refuse: case["script"] == json!(["refusal"]),
        directory_error: None,
    };
    let mode = case["mode"].as_str().unwrap();
    let stdin = matches!(mode, "git" | "result").then_some(b"data\0".as_slice());
    let mut process = Process {
        script: case["script"].as_array().unwrap().clone().into(),
        calls: vec![],
        input: stdin.map(<[u8]>::to_vec),
        cwd: cps("sample"),
    };
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
    let args = ["-c", "fixture=true", "status"].map(cps);
    let cwd = b"sample";
    let opt = &case["options"];
    let outcome = match mode {
        "result" => runner.result(cwd, &args, 1., stdin, false).map(completed),
        "identity" => runner.identity_result(cwd, &args).map(completed),
        "git" => runner
            .output(
                cwd,
                &args,
                Output {
                    stdin,
                    empty_ok: opt["empty_ok"] == true,
                    verbatim: opt["verbatim"] == true,
                    paths: opt["paths"] == true,
                    ..Output::default()
                },
            )
            .map(optional),
        "branch" => runner.branch(cwd).map(optional),
        "remote" => runner
            .remote_url_and_alternates(cwd)
            .map(|(url, others, unknown)| {
                json!([
                    optional(url),
                    others
                        .iter()
                        .map(|s| to_string(s).unwrap())
                        .collect::<Vec<_>>(),
                    unknown
                ])
            }),
        _ => panic!("unknown mode"),
    };
    let expected = match outcome {
        Ok(out) => out,
        Err(Error::Run(RunRaised::Other)) => json!({"raised":"ValueError"}),
        Err(error) => panic!("{error:?}"),
    };
    assert_eq!(expected, case["result"], "{case}");
    let (timed, missing) = runner.counts();
    assert_eq!(
        json!([
            runner.unanswered_git_calls(),
            runner.failed_git_calls(),
            timed,
            missing,
            runner.unanswered()
        ]),
        case["counts"],
        "{case}"
    );
    let logged: Vec<_> = runner
        .lines
        .iter()
        .map(|(logger, level, line)| {
            let logger = match logger {
                Logger::Git => "bettermemory._git",
                Logger::Origin => "bettermemory.origin",
            };
            let level = match level {
                Level::Debug => "DEBUG",
                Level::Warning => "WARNING",
                Level::Info => "INFO",
            };
            json!([logger, level, to_string(line).unwrap()])
        })
        .collect();
    assert_eq!(json!(logged), case["logs"], "{case}");
    assert_eq!(json!(process.calls), case["calls"], "{case}");
}

#[test]
fn scripted_processes() {
    const FILE: &str = "origin_git.jsonl";
    let cases = common::jsonl(FILE);
    for case in &cases {
        script(case);
    }
    Ledger::new("scripted_processes", FILE).settle(cases.len(), &[]);
}

#[test]
fn directory_error_by_python() {
    const FILE: &str = "origin_git_python.jsonl";
    let cases = common::jsonl(FILE);
    let mut ledger = Ledger::new("directory_error_by_python", FILE);
    for case in &cases {
        let name = case[0].as_str().unwrap();
        let errno = [
            "ENOENT",
            "ENOTDIR",
            "ELOOP",
            "EBADF",
            "EACCES",
            "EPERM",
            "EIO",
            "ENAMETOOLONG",
        ]
        .into_iter()
        .find(|s| s == &name)
        .unwrap();
        let mut files = Leaves {
            directory_error: Some(OsError::new(PyErr::Os, errno)),
            ..Leaves::default()
        };
        let mut proc = Process {
            script: VecDeque::from([json!([0, "", ""])]),
            calls: vec![],
            input: None,
            cwd: cps("stat-check"),
        };
        let mut counts = Counts::default();
        let mut seen = NotADirectorySeen::new();
        let scope = Scope::default();
        let env = Env::new();
        let global = AtomicI64::new(0);
        let mut r = OriginRunner::new(
            Context {
                flavour: Flavour::Posix,
                scope: &scope,
                clock: &|| 0.,
                env: &env,
                global_unanswered: &global,
                first_not_a_directory: &mut seen,
            },
            &mut proc,
            &mut files,
            &mut counts,
        );
        let answer = r.probe_worktree_root(b"stat-check").unwrap();
        let counts = json!([
            r.unanswered_git_calls(),
            r.failed_git_calls(),
            r.counts().0,
            r.counts().1,
            r.unanswered()
        ]);
        let observed = json!([
            errno,
            [optional(answer.0), answer.1, optional(answer.2)],
            proc.calls.len(),
            counts
        ]);
        ledger.case(errno, || assert_eq!(&observed, case));
    }
    ledger.finish(cases.len());
}
