//! Every case of `git_read_budget.jsonl` against `git_read_budget` and
//! `git_calls`: the generator's programs run by the same interpreter over
//! a scripted clock, and the text helpers.

mod common;

use std::cell::Cell;
use std::rc::Rc;
use std::sync::atomic::AtomicI64;

use bettermemory_core::float_repr;
use bettermemory_core::git_calls::{self as gc, RunRaised, Spawn, Tally};
use bettermemory_core::git_read_budget::{self as grb, Progresses, Raise, Scope, Store, WaitLock};
use bettermemory_core::origin_history::HistoryUnread;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use serde_json::{json, Value};

#[derive(Debug)]
enum Err {
    Deadline,
    Value,
    Assertion,
}

impl Err {
    fn name(&self) -> &'static str {
        match self {
            Err::Deadline => "ReadDeadline",
            Err::Value => "ValueError",
            Err::Assertion => "AssertionError",
        }
    }
}

enum Got {
    Ok,
    Unread(HistoryUnread),
}

impl From<HistoryUnread> for Got {
    fn from(u: HistoryUnread) -> Got {
        Got::Unread(u)
    }
}

impl Got {
    fn spelled(&self) -> Value {
        match self {
            Got::Ok => json!("ok"),
            Got::Unread(u) => json!(u.value()),
        }
    }
}

fn f(x: f64) -> Value {
    json!(float_repr::repr(x))
}

fn num(v: &Value) -> f64 {
    v.as_str()
        .expect("a float's repr")
        .parse()
        .expect("a float")
}

fn text(v: &Value) -> Option<Vec<u32>> {
    v.as_str().map(cps)
}

fn spell(t: &Option<Vec<u32>>) -> Value {
    match t {
        None => Value::Null,
        Some(t) => json!(to_string(t).unwrap()),
    }
}

fn root(r: &Option<grb::Root>) -> Value {
    match r {
        None => Value::Null,
        Some((t, h)) => json!([to_string(t).unwrap(), h]),
    }
}

fn root_of(v: &Value) -> Option<grb::Root> {
    v.as_array()
        .map(|a| (cps(a[0].as_str().unwrap()), a[1].as_bool().unwrap()))
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().unwrap() != 0.0,
        Value::String(s) => !s.is_empty(),
        _ => false,
    }
}

fn store(v: &Value) -> Store<String> {
    match v.as_str() {
        None => Store::Absent,
        Some("u") | Some("str") => Store::Refused,
        Some(label) => Store::Keyed(label.to_owned()),
    }
}

struct Ctx {
    out: Vec<Value>,
    unanswered: AtomicI64,
    tally: Tally,
}

struct Lock<'a>(&'a mut Vec<Value>);

impl WaitLock for Lock<'_> {
    fn acquire(&mut self, timeout: Option<f64>) -> bool {
        self.0.push(timeout.map_or(json!("-"), f));
        true
    }
    fn release(&mut self) {
        self.0.push(json!("rel"));
    }
}

type Clock<'a> = &'a dyn Fn() -> f64;

fn body(
    c: &mut Ctx,
    p: &Progresses<String>,
    s: &mut Scope,
    clock: Clock,
    ops: &Value,
) -> Result<(), Err> {
    for op in ops.as_array().unwrap() {
        one(c, p, s, clock, op)?;
    }
    Ok(())
}

fn raise_of(e: Err) -> Raise<Err> {
    match e {
        Err::Deadline => Raise::Deadline(grb::ReadDeadline),
        other => Raise::Other(other),
    }
}

fn one(
    c: &mut Ctx,
    p: &Progresses<String>,
    s: &mut Scope,
    clock: Clock,
    op: &Value,
) -> Result<(), Err> {
    let kind = op[0].as_str().unwrap();
    let current = |s: &Scope| s.active.clone().or_else(|| s.owner.clone());
    match kind {
        "scope" => grb::call_scope(s, p, &store(&op[1]), op[2].as_str().unwrap(), |s, _| {
            body(c, p, s, clock, &op[3])
        }),
        "call" | "acall" => grb::call(s, p, &store(&op[1]), "m.f", |s| {
            body(c, p, s, clock, &op[2])
        }),
        "health" => {
            let none = op[1].is_null();
            grb::health_call(s, p, none, |s| body(c, p, s, clock, &op[2]))
        }
        "read" => grb::reading(
            s,
            &cps(op[1].as_str().unwrap()),
            op[2].as_bool().unwrap(),
            |s, _| body(c, p, s, clock, &op[3]),
        ),
        "unread" | "hit" => {
            let first = op[1]
                .as_array()
                .map(|a| (cps(a[0].as_str().unwrap()), truthy(&a[1])));
            if kind == "unread" {
                let got: Got = grb::unread(s, first, |s| {
                    body(c, p, s, clock, &op[2]).map_err(raise_of)?;
                    Ok(Got::Ok)
                })?;
                c.out.push(json!(["unread", got.spelled()]));
            } else {
                let (flag, got): (bool, Got) = grb::hit(s, first, |s| {
                    body(c, p, s, clock, &op[2]).map_err(raise_of)?;
                    Ok((false, Got::Ok))
                })?;
                c.out.push(json!(["hit", [flag, got.spelled()]]));
            }
            Ok(())
        }
        "allow" => {
            let (cwd, gd) = (text(&op[2]), text(&op[3]));
            let (given, limited) =
                grb::allowance(s, clock, num(&op[1]), cwd.as_deref(), gd.as_deref())
                    .map_err(|_| Err::Deadline)?;
            c.out.push(json!(["allow", f(given), limited]));
            Ok(())
        }
        "expired" => {
            c.out.push(json!(["expired", grb::expired(s, clock)]));
            Ok(())
        }
        "exh" => Err(match grb::exhausted(s) {
            grb::Exhausted::ReadDeadline => Err::Deadline,
            grb::Exhausted::AssertionError => Err::Assertion,
        }),
        "wait" => {
            let mut asked = Vec::new();
            let scope = s.clone();
            let (got, r) = grb::wait_for(&scope, clock, &mut Lock(&mut asked), |got| {
                (got, body(c, p, s, clock, &op[1]))
            });
            r?;
            c.out.push(json!(["wait", got, asked]));
            Ok(())
        }
        "raise" => Err(if op[1] == "D" {
            Err::Deadline
        } else {
            Err::Value
        }),
        "resume" => {
            let rows: Vec<Option<Vec<u32>>> = op[1].as_array().unwrap().iter().map(text).collect();
            let got = grb::for_resume(s, &rows, Clone::clone);
            c.out
                .push(json!(["resume", got.iter().map(spell).collect::<Vec<_>>()]));
            Ok(())
        }
        "set" => {
            let b = current(s).ok_or(Err::Assertion)?;
            let mut b = b.borrow_mut();
            let v = &op[2];
            match op[1].as_str().unwrap() {
                "deadline" => b.deadline = if v.is_null() { None } else { Some(num(v)) },
                "first_home" => b.first_home = text(v),
                "last_root" => b.last_root = root_of(v),
                "deferred" => b.deferred = root_of(v),
                "resume" => b.resume = root_of(v),
                other => panic!("{other}"),
            }
            Ok(())
        }
        "ballow" => {
            let b = current(s).ok_or(Err::Assertion)?;
            let (given, limited) = b
                .borrow_mut()
                .allowance(
                    clock,
                    num(&op[1]),
                    &cps(op[2].as_str().unwrap()),
                    op[3].as_bool().unwrap(),
                )
                .map_err(|_| Err::Deadline)?;
            c.out.push(json!(["ballow", f(given), limited]));
            Ok(())
        }
        "bexh" => {
            current(s).ok_or(Err::Assertion)?.borrow_mut().exhausted();
            Err(Err::Deadline)
        }
        "bfinish" => {
            current(s).ok_or(Err::Assertion)?.borrow().finish();
            Ok(())
        }
        "state" => {
            let v = match current(s) {
                None => Value::Null,
                Some(b) => {
                    let b = b.borrow();
                    let same = match (&s.active, &s.owner) {
                        (Some(a), Some(o)) => Rc::ptr_eq(a, o),
                        (None, None) => true,
                        _ => false,
                    };
                    json!({
                        "deadline": b.deadline.map_or(Value::Null, f),
                        "first_home": spell(&b.first_home),
                        "last_root": root(&b.last_root),
                        "deferred": root(&b.deferred),
                        "resume": root(&b.resume),
                        "progress": b.progress.is_some(),
                        "root": root(&s.root),
                        "same": same,
                        "owner": s.owner.is_some(),
                        "active": s.active.is_some(),
                    })
                }
            };
            c.out.push(json!(["state", v]));
            Ok(())
        }
        "progress" => {
            let v = match p.peek(&store(&op[1]), op[2].as_str().unwrap()) {
                None => json!("absent"),
                Some(r) => root(&r),
            };
            c.out.push(json!(["progress", v]));
            Ok(())
        }
        "git" => {
            let (cwd, gd) = (text(&op[2]), text(&op[3]));
            let spawn = match op[4].as_str().unwrap() {
                "ok" => Spawn::Ran,
                "nf" => Spawn::NotFound,
                "to" => Spawn::TimedOut,
                "os" => Spawn::OsError,
                _ => Spawn::Other,
            };
            let isdir = op[5].as_bool().unwrap();
            let before = (gc::unanswered_calls(&c.unanswered), c.tally);
            let asked = Cell::new(0);
            let mut given = Vec::new();
            let answer = gc::run_git(
                s,
                clock,
                &c.unanswered,
                &mut c.tally,
                num(&op[1]),
                cwd.as_deref(),
                gd.as_deref(),
                || {
                    asked.set(asked.get() + 1);
                    isdir
                },
                |t| {
                    given.push(f(t));
                    spawn
                },
            );
            let t = c.tally;
            let moved = json!([
                gc::unanswered_calls(&c.unanswered) - before.0,
                t.failed_calls() - before.1.failed_calls(),
                t.timed_out_calls() - before.1.timed_out_calls(),
                t.missing_calls() - before.1.missing_calls(),
            ]);
            let (spelled, err) = match answer {
                Ok(b) => (json!(b), None),
                Result::Err(RunRaised::ReadDeadline) => {
                    (json!("ReadDeadline"), Some(Err::Deadline))
                }
                Result::Err(RunRaised::AssertionError) => {
                    (json!("AssertionError"), Some(Err::Assertion))
                }
                Result::Err(RunRaised::Other) => (json!("ValueError"), Some(Err::Value)),
            };
            c.out
                .push(json!(["git", given, spelled, moved, asked.get()]));
            err.map_or(Ok(()), Result::Err)
        }
        other => panic!("op {other}"),
    }
}

fn program(r: &Value) {
    let clock: Vec<f64> = r["clock"].as_array().unwrap().iter().map(num).collect();
    let n = Cell::new(0usize);
    let tick = || {
        let v = clock[n.get()];
        n.set(n.get() + 1);
        v
    };
    let mut c = Ctx {
        out: Vec::new(),
        unanswered: AtomicI64::new(0),
        tally: Tally::default(),
    };
    let p = Progresses::<String>::default();
    let mut s = Scope::default();
    if let Result::Err(e) = body(&mut c, &p, &mut s, &tick, &r["ops"]) {
        c.out.push(json!(["raise", e.name()]));
    }
    assert_eq!(Value::Array(c.out), r["out"], "{}", r["ops"]);
    assert_eq!(
        n.get() as u64,
        r["n"].as_u64().unwrap(),
        "clock reads {}",
        r["ops"]
    );
}

fn texts(r: &Value) {
    match r["k"].as_str().unwrap() {
        "notes" => {
            let mut t = Tally::default();
            for n in r["notes"].as_array().unwrap() {
                if n == "f" {
                    t.note_failed();
                } else {
                    t.note_timed_out();
                }
            }
            assert_eq!(json!([t.failed, t.timed_out]), r["o"]);
            assert_eq!(t.missing, 0);
        }
        "printed" => {
            let got = gc::printed_path(&cps(r["t"].as_str().unwrap()), r["win"].as_bool().unwrap())
                .to_vec();
            assert_eq!(to_string(&got).unwrap(), r["o"].as_str().unwrap(), "{r}");
        }
        "sub" => {
            let args: Vec<&str> = r["a"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| a.as_str().unwrap())
                .collect();
            assert_eq!(gc::subcommand(&args), r["o"].as_str().unwrap(), "{r}");
        }
        "nonzero" => {
            let args: Vec<&str> = r["a"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| a.as_str().unwrap())
                .collect();
            let err = match r.get("eb") {
                Some(h) => {
                    String::from_utf8_lossy(&common::unhex(h.as_str().unwrap())).into_owned()
                }
                None => r["e"].as_str().unwrap().to_owned(),
            };
            let got = gc::nonzero_exit_line(
                &args,
                r["rc"].as_i64().unwrap(),
                r["w"].as_str().unwrap(),
                &err,
            );
            assert_eq!(got, r["o"].as_str().unwrap(), "{r}");
        }
        other => panic!("kind {other}"),
    }
}

#[test]
fn every_case() {
    let file = "git_read_budget.jsonl";
    let lines = common::jsonl(file);
    let header = &lines[0];
    assert_eq!(header["read_seconds"], f(grb::READ_SECONDS));
    let rows = &lines[1..];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, rows.len());
    let mut ledger = Ledger::new("golden_git_read_budget", file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(i, || {
            if r["k"] == "prog" {
                program(r);
            } else {
                texts(r);
            }
        });
    }
    ledger.finish(rows.len());
}
