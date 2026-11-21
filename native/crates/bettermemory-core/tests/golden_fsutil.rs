//! Every case of `fsutil.jsonl` (not gated) and `fsutil_python.jsonl`
//! (gated on the Python release) against `fsutil`: `unlink_retrying` over
//! a scripted remover on either platform, and `bounded_stream_read` over
//! an in-memory stream and a pipe-like stream that gives a few bytes per
//! read.

mod common;

use std::io::Read;

use bettermemory_core::fsutil::{bounded_stream_read, unlink_retrying_by, Remover};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

struct Scripted {
    fails: Vec<Option<String>>,
    tries: usize,
    sleeps: Vec<String>,
}

impl Remover for Scripted {
    type Error = (String, usize);

    fn unlink(&mut self) -> Result<(), (String, usize)> {
        let n = self.tries;
        self.tries += 1;
        match self.fails.get(n) {
            Some(Some(class)) => Err((class.clone(), n)),
            _ => Ok(()),
        }
    }

    fn permission(&self, error: &(String, usize)) -> bool {
        error.0 == "PermissionError"
    }

    fn sleep(&mut self, seconds: f64) {
        self.sleeps
            .push(bettermemory_core::float_repr::repr(seconds));
    }
}

fn check_unlink(r: &Value) {
    let fails: Vec<Option<String>> = r["f"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().map(String::from))
        .collect();
    let mut remover = Scripted {
        fails,
        tries: 0,
        sleeps: Vec::new(),
    };
    let answers: Option<Vec<bool>> = r["s"]
        .as_array()
        .map(|a| a.iter().map(|v| v.as_bool().unwrap()).collect());
    let mut asked = 0usize;
    let mut ask = || {
        let a = answers.as_ref().unwrap()[asked];
        asked += 1;
        a
    };
    let still: Option<&mut dyn FnMut() -> bool> = if answers.is_some() {
        Some(&mut ask)
    } else {
        None
    };
    let got = unlink_retrying_by(&mut remover, r["win"].as_bool().unwrap(), still);
    let result = match got {
        Ok(()) => json!("ok"),
        Err((class, n)) => json!([class, n]),
    };
    let out = json!({
        "sl": remover.sleeps,
        "asked": asked,
        "tries": remover.tries,
        "r": result,
    });
    assert_eq!(out, r["o"]);
}

/// A pipe as a reader sees it: at most `step` bytes per read, with an
/// interruption before each.
struct Trickle {
    data: Vec<u8>,
    at: usize,
    step: usize,
    interrupt: bool,
}

impl Read for Trickle {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        self.interrupt = !self.interrupt;
        if self.interrupt {
            return Err(std::io::ErrorKind::Interrupted.into());
        }
        let n = buf.len().min(self.step).min(self.data.len() - self.at);
        buf[..n].copy_from_slice(&self.data[self.at..self.at + n]);
        self.at += n;
        Ok(n)
    }
}

fn data(n: usize) -> Vec<u8> {
    (0..n).map(|i| ((7 * i + 3) % 256) as u8).collect()
}

fn check_stream(r: &Value) {
    let n = r["n"].as_u64().unwrap() as usize;
    let max = r["max"].as_i64().unwrap();
    let got = if r["src"] == "bytesio" {
        bounded_stream_read(&mut std::io::Cursor::new(data(n)), max)
    } else {
        let mut t = Trickle {
            data: data(n),
            at: 0,
            step: 4093,
            interrupt: false,
        };
        bounded_stream_read(&mut t, max)
    };
    let out = match got {
        Ok(raw) => {
            json!({"len": raw.len(), "sha": common::hex(&Sha256::digest(&raw))})
        }
        Err(e) => json!([e.class(), e.message().unwrap()]),
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
            Some("u") => check_unlink(r),
            Some("b") => check_stream(r),
            other => panic!("unknown row kind {other:?}"),
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn fsutil_every_case() {
    run("fsutil_every_case", "fsutil.jsonl");
}

#[test]
fn fsutil_python_every_case() {
    run("fsutil_python_every_case", "fsutil_python.jsonl");
}
