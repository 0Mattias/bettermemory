//! A scripted git, files, leaves and shallow hold for
//! `origin_dates::memo`, in the spelling of `origin_dates_memo.jsonl`.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::githead::GitDir;
use bettermemory_core::origin_dates::memo::{
    commit_author_timestamps, MemoLeaves, Place, Reader, ShallowHold, Timestamps,
};
use bettermemory_core::origin_dates::{DateFiles, GitResult, OriginGit};
use bettermemory_core::origin_history::{Cut, Key, TimestampsMemo, ValueError};
use bettermemory_core::pyvalue::PyStr;
use serde_json::{json, Value};

use super::origin_dates::{spell, text};

/// One step's script.
pub struct State {
    pub answers: VecDeque<Value>,
    pub leaves: Value,
    pub cuts: VecDeque<Value>,
    pub held: VecDeque<Value>,
    pub counts: (i64, i64),
    pub calls: Vec<Value>,
    pub failed: i64,
    pub timed: i64,
    pub raised: bool,
}

fn expand(v: &Value) -> Value {
    match v.get("rep") {
        Some(rep) => json!(rep
            .as_str()
            .unwrap()
            .repeat(v["n"].as_u64().unwrap() as usize)),
        None => v.clone(),
    }
}

fn place(p: Place<'_>) -> Value {
    match p {
        Place::Cwd(b) => json!(String::from_utf8_lossy(b)),
        Place::Root(t) => spell(t),
    }
}

fn key_of(v: &Value) -> Option<Key> {
    v.as_array().map(|a| (text(&a[0]), text(&a[1])))
}

/// A step's handle: every trait answers from one shared state.
#[derive(Clone)]
pub struct Step(pub Rc<RefCell<State>>);

impl Step {
    pub fn new(step: &Value) -> Step {
        let l = step["l"].clone();
        let list = |k: &str| -> VecDeque<Value> {
            l.get(k)
                .and_then(Value::as_array)
                .map(|a| a.iter().cloned().collect())
                .unwrap_or_default()
        };
        Step(Rc::new(RefCell::new(State {
            answers: step["g"].as_array().unwrap().iter().map(expand).collect(),
            cuts: list("cuts"),
            held: list("held"),
            leaves: l,
            counts: (0, 0),
            calls: Vec::new(),
            failed: 0,
            timed: 0,
            raised: false,
        })))
    }

    fn live(&self) -> bool {
        !self.0.borrow().raised
    }

    /// Whether git answers its calls: after the deadline it answers none,
    /// unless the step's `loose` leaf asks for a runner that breaks the
    /// `OriginGit::deadline` contract and keeps answering.
    fn answers(&self) -> bool {
        self.live() || self.leaf("loose").as_bool().unwrap_or(false)
    }

    fn call(&self, v: Value) {
        self.0.borrow_mut().calls.push(v);
    }

    fn leaf(&self, k: &str) -> Value {
        self.0
            .borrow()
            .leaves
            .get(k)
            .cloned()
            .unwrap_or(Value::Null)
    }

    /// The next git answer: `Err(())` for a timeout, a missing binary or
    /// the deadline.
    fn next(&self) -> Result<Value, ()> {
        let mut s = self.0.borrow_mut();
        let a = s.answers.pop_front().expect("an answer left");
        match a.as_str() {
            Some("T") => {
                s.counts.0 += 1;
                Err(())
            }
            Some("M") => {
                s.counts.1 += 1;
                Err(())
            }
            Some("D") => {
                s.raised = true;
                Err(())
            }
            _ => Ok(a),
        }
    }

    fn gd(&self, which: &str) -> Option<GitDir> {
        if !self.live() {
            return None;
        }
        self.call(json!(which));
        (self.leaf("gd").as_str() == Some(which)).then(|| GitDir {
            gitdir: b"G".to_vec(),
            commondir: b"G".to_vec(),
        })
    }

    fn flag(&self, name: Value, k: &str) -> bool {
        if !self.live() {
            return false;
        }
        self.call(name);
        self.leaf(k).as_bool().unwrap_or(false)
    }
}

impl OriginGit for Step {
    fn git(
        &mut self,
        _: &[u8],
        args: &[PyStr],
        timeout: f64,
        verbatim: bool,
        empty_ok: bool,
    ) -> Option<PyStr> {
        if !self.answers() {
            return None;
        }
        let spelled: Vec<Value> = args.iter().map(|a| spell(a)).collect();
        self.call(json!(["g", spelled, timeout, verbatim, empty_ok]));
        match self.next() {
            Ok(Value::Null) | Err(()) => None,
            Ok(v) => Some(text(&v)),
        }
    }
    fn git_result(&mut self, _: &[u8], args: &[PyStr], timeout: f64) -> Option<GitResult> {
        if !self.answers() {
            return None;
        }
        let spelled: Vec<Value> = args.iter().map(|a| spell(a)).collect();
        self.call(json!(["R", spelled, timeout, false, false]));
        match self.next() {
            Ok(Value::Array(a)) => Some(GitResult {
                returncode: a[0].as_i64().unwrap(),
                stdout: text(&a[1]),
            }),
            _ => None,
        }
    }
    fn counts(&self) -> (i64, i64) {
        self.0.borrow().counts
    }
    fn note_failed(&mut self) {
        if self.live() {
            self.0.borrow_mut().failed += 1;
        }
    }
    fn note_timed_out(&mut self) {
        if self.live() {
            self.0.borrow_mut().timed += 1;
        }
    }
    fn toplevel_and_head_from_files(&mut self, _: &[u8]) -> Option<Key> {
        self.call(json!("files"));
        key_of(&self.leaf("files"))
    }
    fn deadline(&self) -> bool {
        !self.live()
    }
}

impl DateFiles for Step {
    fn gitdir_at(&mut self, _: &[u8]) -> Option<GitDir> {
        self.gd("at")
    }
    fn locate(&mut self, _: &[u8]) -> Option<GitDir> {
        self.gd("loc")
    }
    fn head_unborn(&mut self, _: &GitDir) -> Option<bool> {
        if !self.live() {
            return None;
        }
        self.call(json!("unborn"));
        self.leaf("unborn").as_bool()
    }
    fn packed_names(&mut self, _: &GitDir, name: &[u32]) -> bool {
        self.flag(json!(["packed", spell(name)]), "packed")
    }
    fn discovery_refusal(&mut self, _: &[u8]) -> bool {
        self.flag(json!("refusal"), "refusal")
    }
    fn entry_above(&mut self, _: &[u8]) -> bool {
        self.flag(json!("above"), "above")
    }
}

impl MemoLeaves<Value> for Step {
    fn located_key(&mut self, _: &[u8]) -> Option<Key> {
        self.call(json!("lk"));
        key_of(&self.leaf("lk"))
    }
    fn history_guard(&mut self, root: &[u32]) -> Option<Value> {
        self.call(json!(["guard", spell(root)]));
        Some(self.leaf("guard")).filter(|g| !g.is_null())
    }
    fn cuts(&mut self, start: Place<'_>, walk: bool) -> Result<Option<Cut>, ValueError> {
        self.call(json!(["cuts", place(start), walk]));
        let a = self.0.borrow_mut().cuts.pop_front().expect("a cut left");
        match a {
            Value::Null => Ok(None),
            Value::String(_) => Err(ValueError),
            v => Ok(Some(v.as_array().unwrap().iter().map(text).collect())),
        }
    }
}

impl ShallowHold for Step {
    type Held = Value;
    fn hold(&mut self, start: Place<'_>, walk: bool) -> Option<Value> {
        self.call(json!(["hold", place(start), walk]));
        Some(self.leaf("hold")).filter(|h| !h.is_null())
    }
    fn shallow_held(
        &mut self,
        start: Place<'_>,
        cut: &Cut,
        held: Option<&Value>,
        walk: bool,
    ) -> bool {
        let cut: Vec<Value> = cut.iter().map(|c| spell(c)).collect();
        let held = held.cloned().unwrap_or(Value::Null);
        self.call(json!(["held", place(start), cut, held, walk]));
        let a = self.0.borrow_mut().held.pop_front();
        a.expect("a hold answer left").as_bool().unwrap()
    }
}

pub fn dates(d: &[DateTime]) -> Value {
    json!(d.iter().map(DateTime::isoformat).collect::<Vec<_>>())
}

/// The answer as the fixture spells it.
pub fn answer(r: &Result<Timestamps, ValueError>) -> Value {
    match r {
        Err(ValueError) => json!("ValueError"),
        Ok(Timestamps::Absent) => Value::Null,
        Ok(Timestamps::Dates(d)) => dates(d),
        Ok(Timestamps::Unread(w)) => json!(w.value()),
    }
}

/// The memo as the fixture spells it.
pub fn memo_of(memo: &TimestampsMemo<Value>) -> Value {
    let opt = |g: &Option<Value>| g.clone().unwrap_or(Value::Null);
    let e: Vec<Value> = memo
        .entries()
        .iter()
        .map(|((r, h), e)| {
            let cut: Vec<Value> = e.cut.iter().map(|c| spell(c)).collect();
            json!([spell(r), spell(h), dates(&e.dates), opt(&e.guard), cut])
        })
        .collect();
    let f: Vec<Value> = memo
        .failures()
        .iter()
        .map(|((r, h), f)| json!([spell(r), spell(h), f.at, opt(&f.guard), f.why.value()]))
        .collect();
    json!({"e": e, "f": f})
}

/// Runs one step over `memo`: the answer and the step's state.
pub fn run_step(memo: &mut TimestampsMemo<Value>, step: &Value) -> (Value, Step) {
    let s = Step::new(step);
    let (mut git, mut files, mut leaves, mut hold) = (s.clone(), s.clone(), s.clone(), s.clone());
    let now = step["now"].as_f64().unwrap();
    let clock = move || now;
    let cwd: Option<Vec<u8>> = step["cwd"].as_str().map(|c| c.as_bytes().to_vec());
    let located = step.get("located").and_then(key_of);
    let mut reader = Reader {
        git: &mut git,
        files: &mut files,
        leaves: &mut leaves,
        hold: &mut hold,
        memo,
        clock: &clock,
    };
    let r = commit_author_timestamps(&mut Scope::default(), &mut reader, cwd.as_deref(), located);
    (answer(&r), s)
}
