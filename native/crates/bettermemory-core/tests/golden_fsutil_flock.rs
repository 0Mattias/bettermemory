//! Every case of `fsutil_flock.jsonl` (not gated) and
//! `fsutil_flock_python.jsonl` (gated on the Python release) against
//! `fsutil`: `flock_windows_by` over a scripted `msvcrt`, clock and pause,
//! calls in turn sharing one warned flag, and `lock_path` for names and
//! suffixes.

mod common;

use std::cell::RefCell;
use std::path::Path;
use std::sync::atomic::AtomicBool;

use bettermemory_core::fsutil::{flock_windows_by, lock_path, Msvcrt};
use common::parity::Ledger;
use serde_json::{json, Value};

struct Scripted<'a> {
    call: &'a Value,
    events: &'a RefCell<Vec<String>>,
    attempts: i64,
    reads: u32,
}

impl Msvcrt for Scripted<'_> {
    type Fd = ();

    fn open(&mut self, _lock_path: &Path) -> Option<()> {
        self.events.borrow_mut().push("open".into());
        self.call["open"].as_bool().unwrap().then_some(())
    }

    fn lock(&mut self, _fd: &()) -> bool {
        self.events.borrow_mut().push("lock 1".into());
        let n = self.attempts;
        self.attempts += 1;
        let lf = self.call["lf"].as_i64().unwrap();
        !(lf < 0 || n < lf)
    }

    fn unlock(&mut self, _fd: &()) -> bool {
        self.events.borrow_mut().push("unlock 1".into());
        self.call["unlock"].as_bool().unwrap()
    }

    fn close(&mut self, _fd: ()) -> bool {
        self.events.borrow_mut().push("close".into());
        self.call["close"].as_bool().unwrap()
    }

    fn monotonic(&mut self) -> f64 {
        self.events.borrow_mut().push("mono".into());
        let start = self.call["mono"][0].as_f64().unwrap();
        let step = self.call["mono"][1].as_f64().unwrap();
        let value = start + f64::from(self.reads) * step;
        self.reads += 1;
        value
    }

    fn sleep(&mut self, seconds: f64) {
        self.events.borrow_mut().push(format!(
            "sleep {}",
            bettermemory_core::float_repr::repr(seconds)
        ));
    }
}

fn check_windows(r: &Value) {
    let warned = AtomicBool::new(false);
    let mut outs = Vec::new();
    for call in r["calls"].as_array().unwrap() {
        let events = RefCell::new(Vec::new());
        let mut m = Scripted {
            call,
            events: &events,
            attempts: 0,
            reads: 0,
        };
        let mut warnings = Vec::new();
        let present = call["m"].as_bool().unwrap();
        let result = {
            let mut warn = |w: bettermemory_core::fsutil::FlockWarning| warnings.push(w.message());
            let got = flock_windows_by(
                Path::new("x.lock"),
                present.then_some(&mut m),
                call["t"].as_f64().unwrap(),
                &warned,
                &mut warn,
            );
            match got {
                Ok(hold) => {
                    let locked = hold.locked();
                    events.borrow_mut().push("yield".into());
                    drop(hold);
                    if locked {
                        json!("held")
                    } else {
                        json!("unlocked")
                    }
                }
                Err(e) => json!(["TimeoutError", e.0]),
            }
        };
        let events = events.into_inner();
        outs.push(json!({"ev": events, "warn": warnings, "r": result}));
    }
    assert_eq!(Value::from(outs), r["o"]);
}

fn check_path(r: &Value) {
    let path = r["path"].as_str().unwrap();
    let sfx = r["sfx"].as_str().unwrap();
    let out = match lock_path(Path::new(path), sfx, cfg!(windows)) {
        Ok(p) => json!(p.to_string_lossy().replace('\\', "/")),
        Err(e) => json!(["ValueError", e.0]),
    };
    assert_eq!(out, r["o"]);
}

fn run(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, r), || match r["k"].as_str() {
            Some("w") => check_windows(r),
            Some("p") => check_path(r),
            other => panic!("unknown row kind {other:?}"),
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn fsutil_flock_every_case() {
    run("fsutil_flock_every_case", "fsutil_flock.jsonl");
}

#[test]
fn fsutil_flock_python_every_case() {
    run(
        "fsutil_flock_python_every_case",
        "fsutil_flock_python.jsonl",
    );
}
