//! The scripted reads of `gen_location_reads`: `v:x` answers `x`, `e:x`
//! fails with `x`, `h:k` blocks until the event `k` is set and answers
//! `late:k`. Shared by the golden, kills and robustness tests.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use bettermemory_core::location_reads::{
    bounded_read, bounded_reads, Board, Clock, Job, Read, ReadFn,
};
use bettermemory_core::store::create::system_monotonic;
use serde_json::{json, Value};

pub type B = Board<String, String, String>;

pub fn clock() -> Clock {
    Arc::new(system_monotonic)
}

/// The events of a case and the reads started.
#[derive(Clone, Default)]
pub struct Script {
    set: Arc<(Mutex<HashSet<String>>, Condvar)>,
    pub ran: Arc<Mutex<Vec<String>>>,
    pub keys: Arc<Mutex<HashMap<String, (String, String)>>>,
}

impl Script {
    pub fn make(&self, path: &str, what: &str, spec: &str) -> ReadFn<String, String> {
        let (kind, arg) = spec.split_once(':').unwrap();
        let (kind, arg) = (kind.to_owned(), arg.to_owned());
        if kind == "h" {
            self.keys
                .lock()
                .unwrap()
                .insert(arg.clone(), (path.into(), what.into()));
        }
        let (set, ran, spec) = (
            Arc::clone(&self.set),
            Arc::clone(&self.ran),
            spec.to_owned(),
        );
        Box::new(move || {
            ran.lock().unwrap().push(spec);
            match kind.as_str() {
                "e" => Err(arg),
                "h" => {
                    let (m, c) = &*set;
                    let mut g = m.lock().unwrap();
                    while !g.contains(&arg) {
                        g = c.wait(g).unwrap();
                    }
                    Ok(format!("late:{arg}"))
                }
                _ => Ok(arg),
            }
        })
    }

    pub fn release(&self, k: &str) {
        let (m, c) = &*self.set;
        m.lock().unwrap().insert(k.into());
        c.notify_all();
    }

    pub fn release_all(&self) {
        let keys: Vec<String> = self.keys.lock().unwrap().keys().cloned().collect();
        for k in keys {
            self.release(&k);
        }
    }

    /// Waits until each released read of `ks` is off the board.
    pub fn settle(&self, board: &B, ks: &[String]) {
        let end = Instant::now() + Duration::from_secs(10);
        for k in ks {
            let (p, w) = self.keys.lock().unwrap()[k].clone();
            while board.in_flight(&p, &w) {
                assert!(Instant::now() < end, "a released read did not land");
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    }
}

pub fn answer(r: &Read<String>) -> Value {
    match r {
        Read::Value(v) => json!(["v", v]),
        Read::Unanswered => json!(["u"]),
        Read::NotRead => json!(["n"]),
    }
}

fn s(v: &Value) -> String {
    v.as_str().unwrap().to_owned()
}

/// One call of a case: its answer and the reads it started.
pub fn call(board: &B, script: &Script, call: &Value) -> Value {
    script.ran.lock().unwrap().clear();
    let b = call["b"].as_f64().unwrap();
    let r = if let Some(job) = call.get("one") {
        let j = job.as_array().unwrap();
        let read = script.make(&s(&j[0]), &s(&j[1]), &s(&j[2]));
        match bounded_read(board, &s(&j[0]), s(&j[1]), read, b) {
            Ok(r) => answer(&r),
            Err(e) => json!(["e", e]),
        }
    } else {
        let jobs: Vec<Job<String, String, String>> = call["many"]
            .as_array()
            .unwrap()
            .iter()
            .map(|j| {
                let read = script.make(&s(&j[0]), &s(&j[1]), &s(&j[2]));
                (s(&j[0]), s(&j[1]), read)
            })
            .collect();
        match bounded_reads(board, jobs, b, call["raises"].as_bool().unwrap()) {
            Ok(rs) => Value::Array(rs.iter().map(answer).collect()),
            Err(e) => json!(["e", e]),
        }
    };
    let mut ran = script.ran.lock().unwrap().clone();
    ran.sort();
    let rel: Vec<String> = call["rel"].as_array().unwrap().iter().map(s).collect();
    for k in &rel {
        script.release(k);
    }
    script.settle(board, &rel);
    json!({"r": r, "ran": ran})
}

/// Every call of a case on a fresh board; every event set at the end.
pub fn run_case(case: &Value) -> Vec<Value> {
    let board = B::with_limits(
        clock(),
        case["handoff"].as_f64().unwrap(),
        bettermemory_core::location_reads::READ_LANES,
        case["slots"].as_u64().unwrap() as usize,
    );
    let script = Script::default();
    let out = case["calls"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| call(&board, &script, c))
        .collect();
    script.release_all();
    out
}
