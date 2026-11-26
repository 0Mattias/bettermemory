//! `origin_reach.jsonl`: the root and head reads, `commit_reachable` and
//! the reachable walk over scripted git replies and scripted file reads:
//! the answers, every git call (its timeout and argv), every file read in
//! order, the five tallies and the log lines.

mod common;

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitfile_rule::discovery::Reading;
use bettermemory_core::gitfile_rule::gitdir::Failure;
use bettermemory_core::gitfile_rule::trust::Refusal;
use bettermemory_core::githead::discovery::Found;
use bettermemory_core::githead::GitDir;
use bettermemory_core::gitsystem::ask::{Env, PINS};
use bettermemory_core::origin_dates::OriginGit;
use bettermemory_core::origin_git::{self, Context, Counts, Files, Level, Logger, OriginRunner};
use bettermemory_core::origin_gitfiles::root::NotADirectorySeen;
use bettermemory_core::origin_history::{Cut, ReachableWalk, AS_STORED_CONFIG, DEVNULL};
use bettermemory_core::origin_reach::{self, Error, Reads};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;
use std::sync::atomic::AtomicI64;

type Log = Rc<RefCell<Vec<Value>>>;

fn text(s: &[u32]) -> String {
    to_string(s).unwrap()
}
fn bytes(s: &[u8]) -> String {
    String::from_utf8(s.to_vec()).unwrap()
}

/// The runner's file reads: the refusal and `toplevel_agrees`.
struct Leaves {
    log: Log,
    refuse: bool,
    agrees: bool,
}
impl Files for Leaves {
    fn reading(&mut self, _: &[u8]) -> Result<Option<Reading>, origin_git::Error> {
        Ok(self.refuse.then(|| Reading {
            why: Some(Refusal {
                why: b"separate checkout".to_vec(),
                format_unknown: false,
            }),
            ..Reading::default()
        }))
    }
    fn stat_dir(&self, _: &[u8]) -> Result<bool, Failure> {
        Ok(true)
    }
    fn refused_root(&mut self, _: &[u8]) -> Result<(Option<Vec<u8>>, bool), origin_git::Error> {
        panic!("unexpected refused root")
    }
    fn agrees(&mut self, cwd: &[u8], top: &[u8]) -> Result<bool, origin_git::Error> {
        self.log
            .borrow_mut()
            .push(json!(["agrees", bytes(cwd), bytes(top)]));
        Ok(self.agrees)
    }
    fn resolve(&mut self, _: &[u8]) -> Result<Vec<u8>, Unresolved> {
        panic!("unexpected runner resolution")
    }
}

/// The scripted in-process reads, as the generator patches them.
struct Scripted {
    log: Log,
    leaves: Value,
}
impl Scripted {
    fn note(&self, name: &str, arg: &[u8]) {
        self.log.borrow_mut().push(json!([name, bytes(arg)]));
    }
}
impl Reads for Scripted {
    fn flavour(&self) -> Flavour {
        Flavour::Posix
    }
    fn find_gitdir(&mut self, cwd: &[u8]) -> Option<Found> {
        self.note("find_gitdir", cwd);
        (self.leaves["gitdir"] == true).then(|| Found {
            gd: GitDir {
                gitdir: b"gd".to_vec(),
                commondir: b"cd".to_vec(),
            },
            worktree_root: b"wt".to_vec(),
        })
    }
    fn head_sha(&mut self, gd: &GitDir) -> Option<String> {
        self.note("head_sha", &gd.gitdir);
        self.leaves["head"].as_str().map(str::to_owned)
    }
    fn config_moves_worktree(&mut self, gd: &GitDir) -> Result<bool, Error> {
        self.note("moves", &gd.gitdir);
        Ok(self.leaves["moves"] == true)
    }
    fn machine_config_dies(&mut self, git_dir: &[u8]) -> Result<bool, Error> {
        self.note("dies", git_dir);
        Ok(self.leaves["dies"] == true)
    }
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error> {
        self.note("spelled", root);
        match self.leaves["spelled"].as_str() {
            Some("value") => Err(Error::Files(Failure::Value)),
            Some(s) => Ok(Some(s.as_bytes().to_vec())),
            None => Ok(None),
        }
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        self.note("resolve", path);
        match self.leaves["resolve"].as_str() {
            Some("os") => Err(Unresolved::Raised(PyErr::Os)),
            Some("value") => Err(Unresolved::Raised(PyErr::Value)),
            _ => Ok([path, b"~r"].concat()),
        }
    }
    fn cuts(&mut self, toplevel: &[u8]) -> Result<Option<Cut>, Error> {
        self.note("cuts", toplevel);
        Ok(self.leaves["cuts"]
            .as_array()
            .map(|shas| shas.iter().map(|s| cps(s.as_str().unwrap())).collect()))
    }
}

struct Process {
    script: VecDeque<Value>,
    calls: Vec<Value>,
    cwd: PyStr,
}
impl GitProcess for Process {
    fn run(
        &mut self,
        argv: &[PyStr],
        cwd: Option<&[u32]>,
        _env: &Env,
        stdin: Option<&[u8]>,
        timeout: f64,
    ) -> Spawned {
        assert_eq!(cwd, Some(self.cwd.as_slice()));
        assert!(stdin.is_none());
        let prefix: Vec<_> = ["git"]
            .into_iter()
            .chain(PINS)
            .chain(AS_STORED_CONFIG)
            .map(cps)
            .collect();
        let rest = argv
            .strip_prefix(prefix.as_slice())
            .expect("pins and config");
        let null = format!("-O{DEVNULL}");
        let rest: Vec<_> = rest
            .iter()
            .map(|a| match text(a) {
                a if a == null => "-O<devnull>".to_owned(),
                a => a,
            })
            .collect();
        self.calls.push(json!([timeout, rest]));
        let reply = self.script.pop_front().expect("a scripted git");
        match reply.as_str() {
            Some("timeout") => Spawned::TimedOut,
            Some("missing") => Spawned::NotFound(cps("no executable")),
            Some(kind) => panic!("unexpected reply {kind}"),
            None => Spawned::Exited {
                returncode: reply[0].as_i64().unwrap(),
                stdout: match reply[1].as_str() {
                    Some(s) => s.as_bytes().to_vec(),
                    None => hex(reply[1]["hex"].as_str().unwrap()),
                },
                stderr: reply[2].as_str().unwrap().as_bytes().to_vec(),
            },
        }
    }
}

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn walk_json(walk: &ReachableWalk) -> Value {
    let touched: serde_json::Map<String, Value> = walk
        .touched
        .iter()
        .map(|(p, shas)| {
            (
                text(p),
                json!(shas.iter().map(|s| text(s)).collect::<Vec<_>>()),
            )
        })
        .collect();
    json!({
        "anchor": text(&walk.anchor),
        "head": text(&walk.head),
        "commits": walk.commits.iter().map(|s| text(s)).collect::<Vec<_>>(),
        "touched": touched,
        "listed": walk.listed,
        "timed_out": walk.timed_out,
    })
}

fn pair(found: Option<(PyStr, PyStr)>) -> Value {
    json!(found.map(|(root, head)| [text(&root), text(&head)]))
}

fn run_case(case: &Value) {
    let log: Log = Rc::default();
    let leaves = &case["leaves"];
    let mut files = Leaves {
        log: log.clone(),
        refuse: leaves["refusal"] == true,
        agrees: leaves["agrees"] != false,
    };
    let mut reads = Scripted {
        log: log.clone(),
        leaves: leaves.clone(),
    };
    let cwd_text = case["cwd"].as_str().map(str::to_owned);
    let mut process = Process {
        script: case["script"].as_array().unwrap().clone().into(),
        calls: vec![],
        cwd: cps(cwd_text.as_deref().unwrap_or("")),
    };
    let (scope, global, env) = (Scope::default(), AtomicI64::new(0), Env::new());
    let mut seen = NotADirectorySeen::new();
    let mut counts = Counts::default();
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
    let cwd = cwd_text.as_deref().map(str::as_bytes);
    let r = &mut runner;
    let answer = match case["fn"].as_str().unwrap() {
        "repo_toplevel" => {
            origin_reach::repo_toplevel(r, &mut reads, cwd).map(|t| json!(t.map(|t| text(&t))))
        }
        "repo_toplevel_and_head" => {
            origin_reach::repo_toplevel_and_head(r, &mut reads, cwd).map(pair)
        }
        "toplevel_and_head_from_files" => {
            origin_reach::toplevel_and_head_from_files(&mut reads, cwd).map(pair)
        }
        "head_sha" => origin_reach::head_sha(r, cwd).map(|t| json!(t.map(|t| text(&t)))),
        "commit_reachable" => {
            let sha = cps(case["sha"].as_str().unwrap());
            origin_reach::commit_reachable(r, &mut reads, cwd, &sha).map(|b| json!(b))
        }
        "walk" => {
            let anchor = cps(case["anchor"].as_str().unwrap());
            let head = cps(case["head"].as_str().unwrap());
            let names = case["names"] == true;
            origin_reach::walk_reachable(r, &mut reads, cwd.unwrap(), &anchor, &head, names)
                .map(|w| w.as_ref().map_or(Value::Null, walk_json))
        }
        other => panic!("unknown function {other}"),
    };
    let answer = match answer {
        Ok(value) => value,
        Err(Error::Files(Failure::Value)) => json!({"raised": "ValueError"}),
        Err(error) => panic!("{error:?}"),
    };
    assert_eq!(answer, case["result"], "{case}");
    let (timed, missing) = runner.counts();
    let totals = json!([
        runner.unanswered_git_calls(),
        runner.failed_git_calls(),
        timed,
        missing,
        runner.unanswered()
    ]);
    assert_eq!(totals, case["counts"], "{case}");
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
            json!([logger, level, text(line)])
        })
        .collect();
    assert_eq!(json!(logged), case["logs"], "{case}");
    drop(runner);
    assert_eq!(json!(process.calls), case["calls"], "{case}");
    assert!(process.script.is_empty(), "{case}");
    assert_eq!(json!(*log.borrow()), case["reads"], "{case}");
}

#[test]
fn scripted_reads() {
    const FILE: &str = "origin_reach.jsonl";
    let cases = common::jsonl(FILE);
    let mut ledger = Ledger::new("scripted_reads", FILE);
    for (i, case) in cases.iter().enumerate() {
        ledger.case(format!("{i} {}", case["fn"]), || run_case(case));
    }
    ledger.finish(cases.len());
}
