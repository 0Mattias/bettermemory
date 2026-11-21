//! A scripted git and scripted leaves for `origin_dates`, in the spelling
//! of `origin_dates_reads.jsonl`.

use std::cell::RefCell;
use std::collections::{BTreeSet, VecDeque};
use std::rc::Rc;

use bettermemory_core::githead::GitDir;
use bettermemory_core::origin_dates::{DateFiles, GitResult, OriginGit};
use bettermemory_core::pyvalue::PyStr;
use serde_json::{json, Value};

/// A fixture text: a string, or a list of code points.
pub fn text(v: &Value) -> PyStr {
    match v {
        Value::String(t) => t.chars().map(u32::from).collect(),
        other => super::cps_of(other),
    }
}

/// Text as the fixture spells it.
pub fn spell(t: &[u32]) -> Value {
    if t.iter().any(|c| (0xD800..=0xDFFF).contains(c)) {
        return json!(t);
    }
    json!(t
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect::<String>())
}

pub fn cut(v: &Value) -> BTreeSet<PyStr> {
    v.as_array().unwrap().iter().map(text).collect()
}

/// git's answers, the leaves, and what was asked, shared by the handles
/// that answer git and the leaves, so the calls keep one order.
#[derive(Clone)]
pub struct Script(pub Rc<RefCell<State>>);

pub struct State {
    pub answers: VecDeque<Value>,
    pub leaves: Value,
    pub counts: (i64, i64),
    pub calls: Vec<Value>,
    pub failed: i64,
}

impl Script {
    pub fn new(row: &Value) -> Script {
        Script(Rc::new(RefCell::new(State {
            answers: row["g"].as_array().unwrap().iter().cloned().collect(),
            leaves: row["l"].clone(),
            counts: (0, 0),
            calls: Vec::new(),
            failed: 0,
        })))
    }
}

impl State {
    /// The next answer: `Err(())` where it is a timeout or a missing binary.
    fn next(&mut self) -> Result<Value, ()> {
        let a = self.answers.pop_front().expect("an answer left");
        match a.as_str() {
            Some("T") => {
                self.counts.0 += 1;
                Err(())
            }
            Some("M") => {
                self.counts.1 += 1;
                Err(())
            }
            _ => Ok(a),
        }
    }

    fn gd(&mut self, which: &str) -> Option<GitDir> {
        self.calls.push(json!(which));
        (self.leaves.get("gd").and_then(Value::as_str) == Some(which)).then(|| GitDir {
            gitdir: b"G".to_vec(),
            commondir: b"G".to_vec(),
        })
    }

    fn flag(&self, key: &str) -> bool {
        self.leaves
            .get(key)
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }
}

impl OriginGit for State {
    fn git(
        &mut self,
        _cwd: &[u8],
        args: &[PyStr],
        timeout: f64,
        verbatim: bool,
        empty_ok: bool,
    ) -> Option<PyStr> {
        let spelled: Vec<Value> = args.iter().map(|a| spell(a)).collect();
        self.calls
            .push(json!(["g", spelled, timeout, verbatim, empty_ok]));
        match self.next() {
            Ok(Value::Null) | Err(()) => None,
            Ok(v) => Some(text(&v)),
        }
    }

    fn git_result(&mut self, _cwd: &[u8], args: &[PyStr], timeout: f64) -> Option<GitResult> {
        let spelled: Vec<Value> = args.iter().map(|a| spell(a)).collect();
        self.calls
            .push(json!(["R", spelled, timeout, false, false]));
        match self.next() {
            Ok(Value::Array(a)) => Some(GitResult {
                returncode: a[0].as_i64().unwrap(),
                stdout: text(&a[1]),
            }),
            _ => None,
        }
    }

    fn counts(&self) -> (i64, i64) {
        self.counts
    }

    fn note_failed(&mut self) {
        self.failed += 1;
    }
}

impl DateFiles for State {
    fn gitdir_at(&mut self, _cwd: &[u8]) -> Option<GitDir> {
        self.gd("at")
    }
    fn locate(&mut self, _cwd: &[u8]) -> Option<GitDir> {
        self.gd("loc")
    }
    fn head_unborn(&mut self, _gd: &GitDir) -> Option<bool> {
        self.calls.push(json!("unborn"));
        self.leaves.get("unborn").and_then(Value::as_bool)
    }
    fn packed_names(&mut self, _gd: &GitDir, name: &[u32]) -> bool {
        self.calls.push(json!(["packed", spell(name)]));
        self.flag("packed")
    }
    fn discovery_refusal(&mut self, _cwd: &[u8]) -> bool {
        self.calls.push(json!("refusal"));
        self.flag("refusal")
    }
    fn entry_above(&mut self, _cwd: &[u8]) -> bool {
        self.calls.push(json!("above"));
        self.flag("above")
    }
}

impl OriginGit for Script {
    fn git(
        &mut self,
        cwd: &[u8],
        args: &[PyStr],
        timeout: f64,
        verbatim: bool,
        empty_ok: bool,
    ) -> Option<PyStr> {
        self.0
            .borrow_mut()
            .git(cwd, args, timeout, verbatim, empty_ok)
    }
    fn git_result(&mut self, cwd: &[u8], args: &[PyStr], timeout: f64) -> Option<GitResult> {
        self.0.borrow_mut().git_result(cwd, args, timeout)
    }
    fn counts(&self) -> (i64, i64) {
        self.0.borrow().counts
    }
    fn note_failed(&mut self) {
        self.0.borrow_mut().failed += 1;
    }
}

impl DateFiles for Script {
    fn gitdir_at(&mut self, cwd: &[u8]) -> Option<GitDir> {
        self.0.borrow_mut().gitdir_at(cwd)
    }
    fn locate(&mut self, cwd: &[u8]) -> Option<GitDir> {
        self.0.borrow_mut().locate(cwd)
    }
    fn head_unborn(&mut self, gd: &GitDir) -> Option<bool> {
        self.0.borrow_mut().head_unborn(gd)
    }
    fn packed_names(&mut self, gd: &GitDir, name: &[u32]) -> bool {
        self.0.borrow_mut().packed_names(gd, name)
    }
    fn discovery_refusal(&mut self, cwd: &[u8]) -> bool {
        self.0.borrow_mut().discovery_refusal(cwd)
    }
    fn entry_above(&mut self, cwd: &[u8]) -> bool {
        self.0.borrow_mut().entry_above(cwd)
    }
}
