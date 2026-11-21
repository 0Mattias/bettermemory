//! Every case of `location_answer.jsonl` (not gated) against
//! `location_answer`: the history, the re-mint and `view_state` scripted
//! as the generator scripts them.

mod common;
#[allow(dead_code)]
mod witness;

use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap};
use std::rc::Rc;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bettermemory_core::capture::{Capture, State};
use bettermemory_core::location_answer as la;
use bettermemory_core::location_git::Reach;
use bettermemory_core::location_reads::{Board, Clock};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use bettermemory_core::witness_questions::AskedView;
use common::parity::Ledger;
use serde_json::Value;
use witness::{checkout, cps, dumps, load, opt_text, text, view};

type Calls = Rc<RefCell<Vec<Value>>>;

struct Scripted {
    s: Value,
    calls: Calls,
}

fn s(t: &[u32]) -> String {
    unicode::to_string(t).unwrap()
}

fn strs(v: &Value) -> Vec<PyStr> {
    v.as_array().map_or(Vec::new(), |a| {
        a.iter().map(|x| cps(x.as_str().unwrap())).collect()
    })
}

impl Scripted {
    fn log(&self, name: &str, arg: Value) {
        self.calls
            .borrow_mut()
            .push(Value::Array(vec![Value::String(name.into()), arg]));
    }
    fn list(items: &[PyStr]) -> Value {
        Value::Array(items.iter().map(|i| text(i)).collect())
    }
}

impl la::AnswerHistory for Scripted {
    fn holds_objects(&mut self, ids: &[PyStr]) -> BTreeSet<PyStr> {
        self.log("holds_objects", Self::list(ids));
        let have: BTreeSet<PyStr> = strs(&self.s["objects"]).into_iter().collect();
        ids.iter().filter(|i| have.contains(*i)).cloned().collect()
    }
    fn fingerprint(&mut self, commit: &[u32]) -> Option<PyStr> {
        self.log("fingerprint", text(commit));
        self.s["commits"][s(commit)][0].as_str().map(cps)
    }
    fn parents(&mut self, commit: &[u32]) -> Vec<PyStr> {
        self.log("parents", text(commit));
        strs(&self.s["commits"][s(commit)][1])
    }
    fn reachable_among(&mut self, commits: &[PyStr]) -> Reach {
        self.log("reachable_among", Self::list(commits));
        let reach = &self.s["reach"];
        let mut relations: Vec<(String, Vec<String>)> =
            reach[0].as_object().map_or(Vec::new(), |m| {
                m.iter()
                    .map(|(k, v)| {
                        let mut d: Vec<String> = v
                            .as_array()
                            .unwrap()
                            .iter()
                            .map(|x| x.as_str().unwrap().to_owned())
                            .collect();
                        d.sort();
                        (k.clone(), d)
                    })
                    .collect()
            });
        relations.sort();
        Reach {
            relations,
            bounded: reach[1].as_bool().unwrap_or(false),
            handled: 0,
        }
    }
    fn blobs(&mut self, commit: &[u32]) -> Vec<PyStr> {
        self.log("blobs", text(commit));
        strs(&self.s["blobs"][s(commit)])
    }
    fn independent(&mut self, of: &[PyStr]) -> Option<Vec<String>> {
        self.log("independent", Self::list(of));
        self.s["independent"]
            .as_array()
            .map(|a| a.iter().map(|x| x.as_str().unwrap().to_owned()).collect())
    }
}

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn capture(c: &Value) -> Capture {
    let state = match c["state"].as_str().unwrap_or("repo") {
        "repo" => State::Repo,
        "outside" => State::Outside,
        _ => State::Indeterminate,
    };
    let mut cap = witness::capture(c);
    cap.state = state;
    cap.common_dir = opt(&c["common_dir"]);
    cap.head = opt(&c["head"]);
    cap.ref_tips = strs(&c["ref_tips"]);
    cap.evicted = c["evicted"].as_bool().unwrap_or(false);
    cap
}

fn reader(vs: &Value) -> la::ViewRead<String> {
    let table: HashMap<String, String> = vs
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
        .collect();
    Arc::new(move |path: &[u32], args: &AskedView| {
        let what = table.get(&s(path)).map_or("echo", String::as_str);
        if what == "slow" {
            std::thread::sleep(Duration::from_millis(1500));
            return Ok(cps("live"));
        }
        if let Some(class) = what.strip_prefix("raise:") {
            return Err(class.to_owned());
        }
        if what != "echo" {
            return Ok(cps(what));
        }
        Ok(cps(&witness::asked_text(&[(path.to_vec(), args.clone())])))
    })
}

fn board() -> Board<String, PyStr, String> {
    let start = Instant::now();
    let clock: Clock = Arc::new(move || start.elapsed().as_secs_f64());
    Board::new(clock)
}

fn error_name(e: la::AnswerError<String>) -> String {
    match e {
        la::AnswerError::NotMapping => "AttributeError".into(),
        la::AnswerError::Py(e) => e.name().into(),
        la::AnswerError::View(c) => c,
    }
}

fn check(board: &Board<String, PyStr, String>, row: &Value) {
    let o = &row["o"];
    match row["f"].as_str().unwrap() {
        "answer" => {
            let calls: Calls = Rc::default();
            let script = row["h"].clone();
            let c2 = Rc::clone(&calls);
            let mut factory = move |common: &[u32]| {
                c2.borrow_mut()
                    .push(Value::Array(vec!["history".into(), text(common)]));
                Scripted {
                    s: script.clone(),
                    calls: Rc::clone(&c2),
                }
            };
            let w = row["w"].clone();
            let c3 = Rc::clone(&calls);
            let mut remint = move |g: &[u32], policy: bool, bare: bool, r: Option<_>| {
                let mut call = vec![Value::String("remint".into())];
                call.extend(
                    witness::call(g, policy, bare, r)
                        .as_array()
                        .unwrap()
                        .clone(),
                );
                c3.borrow_mut().push(Value::Array(call));
                (
                    w[0].as_str().map(str::to_owned),
                    w[1].as_str().unwrap().to_owned(),
                )
            };
            let got = la::answer(
                &load(&row["q"]),
                &load(&row["a"]),
                &capture(&row["cap"]),
                &mut factory,
                &mut remint,
                board,
                &reader(&row["vs"]),
            );
            match got {
                Ok(v) => assert_eq!(Value::String(dumps(&v)), o["r"]),
                Err(e) => assert_eq!(Value::String(error_name(e)), o["raise"]),
            }
            assert_eq!(Value::Array(calls.borrow().clone()), row["calls"]);
        }
        "liveness" => {
            let got = la::path_liveness(
                board,
                &reader(&row["vs"]),
                &checkout(&row["k"]),
                &view(row["v"].as_str().unwrap()),
            );
            match got {
                Ok(st) => assert_eq!(text(&st), o["r"]),
                Err(c) => assert_eq!(Value::String(c), o["raise"]),
            }
        }
        "many" => {
            let pairs: Vec<_> = row["pairs"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| (checkout(&p[0]), view(p[1].as_str().unwrap())))
                .collect();
            let got = la::path_liveness_many(board, &reader(&row["vs"]), &pairs);
            assert_eq!(Value::Array(got.iter().map(opt_text).collect()), *o);
        }
        "exchange" => {
            let ex = la::Exchange {
                question: load(&row["q"]),
            };
            match ex.to_reply() {
                Ok(v) => assert_eq!(Value::String(dumps(&v)), o["r"]),
                Err(e) => assert_eq!(o["raise"], e.name()),
            }
        }
        "history_of" => {
            let got = la::history_of(&capture(&row["cap"]), &mut |c: &[u32]| c.to_vec());
            assert_eq!(opt_text(&got), *o);
        }
        other => panic!("unknown row kind {other}"),
    }
}

#[test]
fn location_answer_every_case() {
    let file = "location_answer.jsonl";
    let rows = common::jsonl(file);
    let board = board();
    let mut ledger = Ledger::new("golden_location_answer", file);
    for (n, row) in rows.iter().enumerate() {
        ledger.case(format!("row {n}: {row}"), || {
            let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| check(&board, row)));
            if let Err(e) = got {
                eprintln!("row {n} differs: {row}");
                std::panic::resume_unwind(e);
            }
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn exchange_defaults_to_an_empty_question() {
    let reply = la::Exchange::default().to_reply().unwrap();
    assert_eq!(dumps(&reply), "{}");
    assert_eq!(la::PROBE_BUDGET, 0.5);
}
