//! Hostile input for `location_resolve`: many re-mints, many file errors,
//! long paths; no panic, and time that grows with the input as the
//! reference's does.

mod common;
mod location_resolve_support;
mod timing;

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use bettermemory_core::location_resolve::caller;
use location_resolve_support::*;
use serde_json::{json, Value};

fn repo() -> Value {
    json!({"state": "repo", "kind": "primary", "git_dir": "/w/.git", "common_dir": "/w/.git", "head": "h1"})
}

fn run(case: &Value) -> usize {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let process = new_process();
    let mut cs = caps(case, &log);
    let req = request(case);
    let got = with_hands(case, &log, &mut cs, &req, |h| {
        caller(&process, &req, None, h)
    })
    .unwrap();
    let n = log.borrow().len();
    n + got.resolution.lines.len()
}

#[test]
fn many_remints_each_logged_and_one_forget() {
    let writes: Vec<Value> = (0..5000).map(|i| json!(format!("t{i}"))).collect();
    let case = json!({"cwd": "/w", "caps": [repo()], "writes": writes});
    // capture, model, record, earlier, resolve, 5000 remints and wrotes, forget.
    assert_eq!(run(&case), 5 + 10_000 + 1);
}

#[test]
fn a_long_cwd_and_odd_code_points() {
    let cwd: String = "/\u{0}\u{7f}\u{85}\u{feff}a".repeat(20_000);
    let case = json!({"cwd": cwd, "caps": [{"state": "outside", "detail": "\u{0}"}], "session": "\u{10ffff}"});
    assert_eq!(run(&case), 5);
}

#[test]
fn file_errors_grow_linearly() {
    let case = |n: usize| -> Value {
        let mut cap = repo();
        cap["file_errors"] = json!((0..n).map(|i| format!("e{i}\n")).collect::<Vec<_>>());
        json!({"cwd": "/w", "caps": [cap]})
    };
    let (small, large) = (case(500), case(5000));
    assert_eq!(run(&small), 5 + 500);
    let time = |c: &Value| -> Duration {
        timing::once(|| {
            std::hint::black_box(run(c));
        })
    };
    timing::grows_within("caller file errors", 30, &mut || time(&small), &mut || {
        time(&large)
    });
}
