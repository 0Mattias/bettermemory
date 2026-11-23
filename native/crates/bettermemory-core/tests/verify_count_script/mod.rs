//! The reads `verify_count.jsonl` scripts beyond `verify_weak_script`'s:
//! [`CountGit`] for its [`Script`], the walk of `verify_basis` as an
//! [`AnchorWalk`], and the whole-history dates, each logged as the
//! generator logs it (`tests/contract/gen_verify_count.py`).

#![allow(dead_code)]

use std::cell::Cell;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::origin_history::{HistoryUnread, ReachableWalk, Unwalked};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::count::CountGit;
use bettermemory_core::verify::weak::Place;
use bettermemory_core::verify_anchoring::attest::pure_text;
use bettermemory_core::verify_basis::AnchorWalk;
use serde_json::{json, Value};

use crate::verify_weak_script::{out, outs, set, text, texts, top, Script};

/// A walk as the generator spells one: `[anchor, commits, touched,
/// listed]`.
pub fn walk(w: &Value) -> Option<ReachableWalk> {
    if w.is_null() {
        return None;
    }
    Some(ReachableWalk {
        anchor: text(&w[0]),
        head: cps(&"f".repeat(40)),
        commits: texts(&w[1]),
        touched: w[2]
            .as_array()
            .expect("touched")
            .iter()
            .map(|p| (text(&p[0]), set(&p[1])))
            .collect(),
        listed: w[3].as_bool().expect("listed"),
        timed_out: false,
    })
}

/// The Python fixture returns a pathlib.Path from both scripted repository lookups.
fn repo_path(value: &Value) -> PyStr {
    pure_text(Flavour::Posix, &text(value)).expect("a POSIX path")
}

fn dates(v: &Value) -> Vec<DateTime> {
    v.as_array()
        .expect("dates")
        .iter()
        .map(|t| DateTime::from_isoformat(&text(t)).expect("a date"))
        .collect()
}

/// [`Script`] with the deadline a read met.
pub struct Counted {
    pub script: Script,
    pub deadline: Cell<bool>,
}

impl Counted {
    pub fn new(s: &Value) -> Counted {
        Counted {
            script: Script::new(s),
            deadline: Cell::new(false),
        }
    }

    pub fn log(&self, v: Value) {
        self.script.calls.borrow_mut().push(v);
    }

    /// `commit_author_timestamps(cwd)` as scripted.
    pub fn timestamps(&self, cwd: &[u32]) -> Option<Result<Vec<DateTime>, HistoryUnread>> {
        self.log(json!(["dates", out(cwd)]));
        let s = &self.script.s;
        if s.get("deadline").and_then(Value::as_bool).unwrap_or(false) {
            self.deadline.set(true);
            return None;
        }
        match s.get("dates") {
            None | Some(Value::Null) => None,
            Some(Value::String(w)) => Some(Err(HistoryUnread::from_value(w).expect("a why"))),
            Some(v) => Some(Ok(dates(v))),
        }
    }
}

macro_rules! delegate {
    ($($name:ident($($arg:ident: $t:ty),*) -> $r:ty;)*) => {$(
        fn $name(&self, $($arg: $t),*) -> $r {
            self.script.$name($($arg),*)
        }
    )*};
}

use bettermemory_core::claims::Claim;
use bettermemory_core::verify::weak::{DriftGit, LiteralFloor, PatchWindow, Spelling};

impl DriftGit for Counted {
    fn deadline(&self) -> bool {
        self.deadline.get()
            || self.script.s["deadline_at"]
                .as_u64()
                .is_some_and(|n| self.script.calls.borrow().len() >= n as usize)
    }

    delegate! {
        resolve_repo_pathspecs(at: Place, paths: &[PyStr]) -> Option<Vec<PyStr>>;
        commit_author_sha_pairs_touching_pathspecs(at: Place, p: &[PyStr]) -> Option<Vec<(DateTime, PyStr)>>;
        paths_in_history(at: Place, p: &[PyStr]) -> Option<bool>;
        too_large(at: Place, shas: &[PyStr], p: &[PyStr]) -> bool;
        commit_patch_stream(at: Place, shas: &[PyStr], p: &[PyStr]) -> Option<PyStr>;
        local_patch_window(at: Place, shas: &[PyStr], p: &[PyStr]) -> Option<PatchWindow>;
        literal_floor(at: Place, shas: &[PyStr], specs: &[PyStr], p: &[PyStr], claims: &[Claim], spell: &dyn Spelling) -> Option<LiteralFloor>;
        spelled(at: Place, specs: &[PyStr], anchor: Option<&[u32]>, since: Option<&DateTime>) -> Box<dyn Spelling>;
    }
}

impl CountGit for Counted {
    fn repo_toplevel(&self, cwd: &[u32]) -> Option<PyStr> {
        self.log(json!(["rt", out(cwd)]));
        self.script
            .s
            .get("rt")
            .filter(|v| !v.is_null())
            .map(repo_path)
    }
    fn commit_author_timestamps_touching_pathspecs(
        &self,
        at: Place,
        pathspecs: &[PyStr],
    ) -> Option<Vec<DateTime>> {
        self.log(json!(["ts", out(at.cwd), top(&at), outs(pathspecs)]));
        self.script.s.get("ts").filter(|v| !v.is_null()).map(dates)
    }
}

/// `repo_toplevel_and_head` and `commits_since_anchor` as scripted.
pub struct Walker<'a>(pub &'a Counted);

impl AnchorWalk for Walker<'_> {
    fn toplevel_and_head(&mut self, cwd: &[u32]) -> Option<(PyStr, PyStr)> {
        self.0.log(json!(["th", out(cwd)]));
        let th = self.0.script.s.get("th").filter(|v| !v.is_null())?;
        Some((repo_path(&th[0]), text(&th[1])))
    }
    fn commits_since_anchor(
        &mut self,
        cwd: &[u32],
        verified_head: &[u32],
        toplevel: &[u32],
        head: &[u32],
        unwalked: &mut Unwalked,
    ) -> Option<ReachableWalk> {
        self.0.log(json!([
            "walk",
            out(cwd),
            out(verified_head),
            out(toplevel),
            out(head)
        ]));
        let s = &self.0.script.s;
        let w = walk(s.get("walk").unwrap_or(&Value::Null));
        if w.is_none() {
            if let Some(why) = s.get("gu").and_then(Value::as_str) {
                unwalked.note(HistoryUnread::from_value(why).expect("a why"));
            }
        }
        w
    }
}
