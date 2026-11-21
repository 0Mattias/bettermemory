//! Cases a plausible wrong `git_run` fails.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::atomic::{AtomicI64, Ordering};

use bettermemory_core::git_calls::{RunRaised, Tally};
use bettermemory_core::git_read_budget::{ReadBudget, Scope};
use bettermemory_core::git_run::{self as gr, Call, GitProcess, Level, Out, Spawned};
use bettermemory_core::gitsystem::ask::Env;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

struct One {
    answer: Spawned,
    env: Env,
    timeout: f64,
    calls: usize,
}

impl GitProcess for One {
    fn run(
        &mut self,
        _argv: &[PyStr],
        _cwd: Option<&[u32]>,
        env: &Env,
        _stdin: Option<&[u8]>,
        timeout: f64,
    ) -> Spawned {
        self.env = env.clone();
        self.timeout = timeout;
        self.calls += 1;
        self.answer.clone()
    }
}

fn one(answer: Spawned) -> One {
    One {
        answer,
        env: Vec::new(),
        timeout: f64::NAN,
        calls: 0,
    }
}

struct Ran {
    got: Result<Option<gr::Completed>, RunRaised>,
    lines: Vec<(Level, String)>,
    tally: Tally,
    unanswered: i64,
    asked: usize,
}

fn go(p: &mut One, call: &Call<'_>, scope: &Scope, clock: f64, isdir: bool) -> Ran {
    let unanswered = AtomicI64::new(0);
    let mut tally = Tally::default();
    let asked = Cell::new(0);
    let (got, lines) = gr::run_git(
        scope,
        &|| clock,
        &unanswered,
        &mut tally,
        p,
        &vec![(cps("GIT_DIR"), cps("/x")), (cps("A"), cps("1"))],
        call,
        || {
            asked.set(asked.get() + 1);
            isdir
        },
    );
    Ran {
        got,
        lines: lines
            .into_iter()
            .map(|(l, m)| (l, m.iter().map(|&c| char::from_u32(c).unwrap()).collect()))
            .collect(),
        tally,
        unanswered: unanswered.load(Ordering::SeqCst),
        asked: asked.get(),
    }
}

fn args() -> Vec<PyStr> {
    vec![cps("log")]
}

#[test]
fn not_found_in_a_folder_that_exists_blames_the_binary() {
    let a = args();
    let c = Call {
        cwd: Some(&cps("/r")),
        ..Call::new(&a)
    };
    let r = go(
        &mut one(Spawned::NotFound(cps("e"))),
        &c,
        &Scope::default(),
        0.0,
        true,
    );
    assert_eq!(
        r.lines,
        vec![(Level::Warning, "git binary not found on PATH: e".to_owned())]
    );
    assert_eq!(r.asked, 1);
    let r = go(
        &mut one(Spawned::NotFound(cps("e"))),
        &c,
        &Scope::default(),
        0.0,
        false,
    );
    assert_eq!(
        r.lines[0].1,
        "git cannot run in /r: the directory does not exist"
    );
    assert_eq!(r.tally.missing, 1);
}

#[test]
fn whole_timeouts_keep_their_point_zero() {
    let a = args();
    let c = Call {
        timeout: 5.0,
        ..Call::new(&a)
    };
    let r = go(
        &mut one(Spawned::TimedOut),
        &c,
        &Scope::default(),
        0.0,
        true,
    );
    assert_eq!(r.lines[0].1, "git log timed out after 5.0s in None");
    assert_eq!(r.asked, 0);
}

#[test]
fn the_logged_timeout_is_the_allowed_one() {
    let a = args();
    let c = Call {
        cwd: Some(&cps("/r")),
        ..Call::new(&a)
    };
    let scope = Scope {
        active: Some(Rc::new(RefCell::new(ReadBudget {
            deadline: Some(10.0),
            ..ReadBudget::default()
        }))),
        ..Scope::default()
    };
    let mut p = one(Spawned::TimedOut);
    let r = go(&mut p, &c, &scope, 9.75, true);
    assert_eq!(p.timeout, 0.25);
    assert_eq!(r.lines[0].1, "git log timed out after 0.25s in /r");
}

#[test]
fn text_keeps_carriage_returns_and_bytes_stay_bytes() {
    let a = args();
    let ran = Spawned::Exited {
        returncode: 128,
        stdout: b"a\r\nb\r".to_vec(),
        stderr: b"\xff".to_vec(),
    };
    let r = go(
        &mut one(ran.clone()),
        &Call::new(&a),
        &Scope::default(),
        0.0,
        true,
    );
    let done = r.got.unwrap().unwrap();
    assert_eq!(done.returncode, 128);
    assert_eq!(done.stdout, Out::Text(cps("a\r\nb\r")));
    assert_eq!(done.stderr, Out::Text(vec![0xfffd]));
    assert_eq!((r.unanswered, r.tally), (0, Tally::default()));
    assert!(r.lines.is_empty());
    let c = Call {
        text: false,
        ..Call::new(&a)
    };
    let done = go(&mut one(ran), &c, &Scope::default(), 0.0, true)
        .got
        .unwrap()
        .unwrap();
    assert_eq!(done.stderr, Out::Bytes(b"\xff".to_vec()));
}

#[test]
fn extra_variables_cannot_loosen_the_scrub() {
    let a = args();
    let extra = vec![
        (cps("GIT_DIR"), cps("/y")),
        (cps("GIT_ALLOW_PROTOCOL"), cps("file")),
        (cps("B"), cps("2")),
    ];
    let c = Call {
        env: &extra,
        unset: &["A"],
        ..Call::new(&a)
    };
    let mut p = one(Spawned::TimedOut);
    go(&mut p, &c, &Scope::default(), 0.0, true);
    let names: Vec<(String, String)> = p
        .env
        .iter()
        .map(|(k, v)| {
            (
                String::from_utf8(k.iter().map(|&c| c as u8).collect()).unwrap(),
                String::from_utf8(v.iter().map(|&c| c as u8).collect()).unwrap(),
            )
        })
        .collect();
    assert_eq!(
        names,
        vec![
            ("GIT_ALLOW_PROTOCOL".to_owned(), String::new()),
            ("B".to_owned(), "2".to_owned()),
            ("GIT_OPTIONAL_LOCKS".to_owned(), "0".to_owned()),
            ("GIT_NO_LAZY_FETCH".to_owned(), "1".to_owned()),
        ]
    );
}

#[test]
fn another_exception_passes_uncounted_and_unlogged() {
    let a = args();
    let c = Call {
        cwd: Some(&cps("/gone")),
        ..Call::new(&a)
    };
    let r = go(&mut one(Spawned::Other), &c, &Scope::default(), 0.0, false);
    assert_eq!(r.got, Err(RunRaised::Other));
    assert!(r.lines.is_empty());
    assert_eq!((r.unanswered, r.tally, r.asked), (0, Tally::default(), 0));
}

#[test]
fn os_error_asks_once_and_logs_where() {
    let a = args();
    let c = Call {
        cwd: Some(&cps("/gone")),
        git_dir: Some(&cps("/g")),
        ..Call::new(&a)
    };
    let r = go(
        &mut one(Spawned::OsError(cps("p"))),
        &c,
        &Scope::default(),
        0.0,
        false,
    );
    assert_eq!(r.asked, 1);
    assert_eq!(r.lines[0].1, "git invocation failed in /gone: p");
    assert_eq!((r.unanswered, r.tally.failed, r.tally.missing), (1, 1, 1));
    let c = Call {
        git_dir: Some(&cps("/g")),
        ..Call::new(&a)
    };
    let r = go(
        &mut one(Spawned::OsError(cps("p"))),
        &c,
        &Scope::default(),
        0.0,
        false,
    );
    assert_eq!(r.asked, 0);
    assert_eq!(r.lines[0].1, "git invocation failed in /g: p");
    assert_eq!(r.tally.missing, 0);
}

#[test]
fn an_expired_shared_deadline_raises_without_a_line() {
    let a = args();
    let scope = Scope {
        active: Some(Rc::new(RefCell::new(ReadBudget {
            deadline: Some(10.0),
            ..ReadBudget::default()
        }))),
        ..Scope::default()
    };
    // The clock reads 9.75 for the allowance and again for `expired`.
    let mut p = one(Spawned::TimedOut);
    let r = go(&mut p, &Call::new(&a), &scope, 9.75, true);
    assert_eq!(r.got, Ok(None));
    let r = go(
        &mut one(Spawned::TimedOut),
        &Call::new(&a),
        &scope,
        10.0,
        true,
    );
    assert_eq!(r.got, Err(RunRaised::ReadDeadline));
    assert!(r.lines.is_empty());
}

#[test]
fn nonzero_exit_decodes_bytes_and_spells_none() {
    let (l, m) = gr::log_nonzero_exit(&args(), 1, &Out::Bytes(b"\xff\nx".to_vec()), None);
    assert_eq!(l, Level::Debug);
    assert_eq!(m, cps("git log exited 1 in None: \u{fffd}"));
    let (_, m) = gr::log_nonzero_exit(&[], 128, &Out::Text(Vec::new()), Some(&cps("/r")));
    assert_eq!(m, cps("git  exited 128 in /r: "));
}
