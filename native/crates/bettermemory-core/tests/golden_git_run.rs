//! Every case of `git_run.jsonl` against `git_run`: each call over a
//! scripted process, environment, clock and `os.path.isdir`, and each
//! line `log_nonzero_exit` logs.

mod common;

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::atomic::{AtomicI64, Ordering};

use bettermemory_core::float_repr;
use bettermemory_core::git_calls::{RunRaised, Tally};
use bettermemory_core::git_read_budget::{ReadBudget, Scope};
use bettermemory_core::git_run::{self as gr, Call, GitProcess, Level, Out, Spawned};
use bettermemory_core::gitsystem::ask::Env;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use serde_json::{json, Value};

fn s(v: &Value) -> PyStr {
    cps(v.as_str().unwrap())
}

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn sv(t: &[u32]) -> Value {
    json!(to_string(t).unwrap())
}

fn strs(v: &Value) -> Vec<PyStr> {
    v.as_array().unwrap().iter().map(s).collect()
}

fn pairs(v: &Value) -> Env {
    v.as_array()
        .unwrap()
        .iter()
        .map(|p| (s(&p[0]), s(&p[1])))
        .collect()
}

fn num(v: &Value) -> f64 {
    v.as_str().unwrap().parse().unwrap()
}

struct Scripted {
    sp: Value,
    seen: Option<Value>,
}

impl GitProcess for Scripted {
    fn run(
        &mut self,
        argv: &[PyStr],
        cwd: Option<&[u32]>,
        env: &Env,
        stdin: Option<&[u8]>,
        timeout: f64,
    ) -> Spawned {
        self.seen = Some(json!([
            argv.iter().map(|a| sv(a)).collect::<Vec<_>>(),
            cwd.map(sv),
            stdin.map(common::hex),
            float_repr::repr(timeout),
            env.iter()
                .map(|(k, v)| json!([sv(k), sv(v)]))
                .collect::<Vec<_>>(),
        ]));
        let sp = &self.sp;
        match sp[0].as_str().unwrap() {
            "ok" => Spawned::Exited {
                returncode: sp[1].as_i64().unwrap(),
                stdout: common::unhex(sp[2].as_str().unwrap()),
                stderr: common::unhex(sp[3].as_str().unwrap()),
            },
            "nf" => Spawned::NotFound(cps(&format!("[Errno 2] {}", sp[1].as_str().unwrap()))),
            "to" => Spawned::TimedOut,
            "os" => Spawned::OsError(cps(&format!("[Errno 13] {}", sp[1].as_str().unwrap()))),
            "v" => Spawned::Other,
            other => panic!("spawn {other}"),
        }
    }
}

fn out(o: &Out) -> Value {
    match o {
        Out::Text(t) => sv(t),
        Out::Bytes(b) => json!({ "hex": common::hex(b) }),
    }
}

fn level(l: Level) -> &'static str {
    match l {
        Level::Debug => "DEBUG",
        Level::Warning => "WARNING",
    }
}

fn run(r: &Value, environ: &Env) {
    let args = strs(&r["a"]);
    let cwd = opt(&r["cwd"]);
    let gd = opt(&r["gd"]);
    let stdin = r["in"].as_str().map(common::unhex);
    let extra = pairs(&r["env"]);
    let unset_owned: Vec<String> = r["unset"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    let unset: Vec<&str> = unset_owned.iter().map(String::as_str).collect();
    let call = Call {
        args: &args,
        cwd: cwd.as_deref(),
        git_dir: gd.as_deref(),
        stdin: stdin.as_deref(),
        timeout: num(&r["t"]),
        text: r["text"].as_bool().unwrap(),
        env: &extra,
        unset: &unset,
    };
    let mut scope = Scope::default();
    if !r["dl"].is_null() {
        scope.active = Some(Rc::new(RefCell::new(ReadBudget {
            deadline: Some(num(&r["dl"])),
            ..ReadBudget::default()
        })));
    }
    let clock: Vec<f64> = r["clock"].as_array().unwrap().iter().map(num).collect();
    let n = Cell::new(0usize);
    let tick = || {
        let v = clock[n.get()];
        n.set(n.get() + 1);
        v
    };
    let unanswered = AtomicI64::new(0);
    let mut tally = Tally::default();
    let mut process = Scripted {
        sp: r["sp"].clone(),
        seen: None,
    };
    let asked = RefCell::new(Vec::new());
    let isdir_answer = r["isdir"].as_bool().unwrap();
    let (got, lines) = gr::run_git(
        &scope,
        &tick,
        &unanswered,
        &mut tally,
        &mut process,
        environ,
        &call,
        || {
            asked.borrow_mut().push(sv(cwd.as_deref().unwrap()));
            isdir_answer
        },
    );
    let ans = match got {
        Ok(None) => json!("None"),
        Ok(Some(c)) => json!([
            c.args.iter().map(|a| sv(a)).collect::<Vec<_>>(),
            c.returncode,
            out(&c.stdout),
            out(&c.stderr)
        ]),
        Err(RunRaised::ReadDeadline) => json!("ReadDeadline"),
        Err(RunRaised::Other) => json!("ValueError"),
        Err(RunRaised::AssertionError) => json!("AssertionError"),
    };
    assert_eq!(ans, r["ans"], "ans {r}");
    assert_eq!(process.seen.unwrap_or(Value::Null), r["spawn"], "spawn {r}");
    let moved = json!([
        unanswered.load(Ordering::SeqCst),
        tally.failed,
        tally.timed_out,
        tally.missing
    ]);
    assert_eq!(moved, r["moved"], "moved {r}");
    assert_eq!(Value::Array(asked.into_inner()), r["asked"], "asked {r}");
    let logged: Vec<Value> = lines
        .iter()
        .map(|(l, m)| json!([level(*l), sv(m)]))
        .collect();
    assert_eq!(Value::Array(logged), r["log"], "log {r}");
    assert_eq!(n.get() as u64, r["n"].as_u64().unwrap(), "clock reads {r}");
}

fn nonzero(r: &Value) {
    let args = strs(&r["a"]);
    let stderr = match r["eb"].as_str() {
        Some(h) => Out::Bytes(common::unhex(h)),
        None => Out::Text(s(&r["e"])),
    };
    let where_ = opt(&r["w"]);
    let (l, m) = gr::log_nonzero_exit(&args, r["rc"].as_i64().unwrap(), &stderr, where_.as_deref());
    assert_eq!(json!([level(l), sv(&m)]), r["o"], "{r}");
}

fn points(v: &Value) -> PyStr {
    v.as_array()
        .unwrap()
        .iter()
        .map(|c| u32::try_from(c.as_u64().unwrap()).unwrap())
        .collect()
}

/// A `log_nonzero_exit` row whose texts are code points: a lone
/// surrogate is kept in the line, as the reference keeps it.
fn nonzero_points(r: &Value) {
    let args: Vec<PyStr> = r["a"].as_array().unwrap().iter().map(points).collect();
    let stderr = match r["eb"].as_str() {
        Some(h) => Out::Bytes(common::unhex(h)),
        None => Out::Text(points(&r["e"])),
    };
    let where_ = (!r["w"].is_null()).then(|| points(&r["w"]));
    let (l, m) = gr::log_nonzero_exit(&args, r["rc"].as_i64().unwrap(), &stderr, where_.as_deref());
    assert_eq!(json!(level(l)), r["o"][0], "{r}");
    assert_eq!(m, points(&r["o"][1]), "{r}");
}

#[test]
fn every_case() {
    let file = "git_run.jsonl";
    let lines = common::jsonl(file);
    let header = &lines[0];
    let environ = pairs(&header["environ"]);
    let rows = &lines[1..];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new("golden_git_run", file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(i, || match r["k"].as_str().unwrap() {
            "run" => run(r, &environ),
            "nz" => nonzero(r),
            "nzc" => nonzero_points(r),
            other => panic!("kind {other}"),
        });
    }
    ledger.finish(rows.len());
}
