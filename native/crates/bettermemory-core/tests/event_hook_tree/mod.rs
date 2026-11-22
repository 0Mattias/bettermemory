//! The run trees of `event_hook.jsonl` (`tests/contract/gen_event_hook.py`)
//! interpreted over the native `event_hook`: a fake store whose busy
//! timeout calls are traced, the clock one reading per block.

#![allow(dead_code)]

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use bettermemory_core::event_hook::{BusyTimeout, HookFailure, HookScope, Raised};
use bettermemory_core::float_repr;
use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq)]
pub enum TErr {
    Sql { class: String, code: Option<i64> },
    Busy { message: String, cause: String },
}

impl Raised for TErr {
    fn class_name(&self) -> &str {
        match self {
            TErr::Sql { class, .. } => class,
            TErr::Busy { .. } => "StoreBusy",
        }
    }
    fn sqlite_code(&self) -> Option<i64> {
        match self {
            TErr::Sql { code, .. } => *code,
            TErr::Busy { .. } => None,
        }
    }
}

#[derive(Clone)]
pub struct Fake {
    pub timeout: Rc<RefCell<f64>>,
    pub trace: Rc<RefCell<Vec<Value>>>,
    /// The `set_busy_timeout` call (from 0) that fails as a closed
    /// connection's does.
    pub fail: Option<usize>,
    pub calls: Rc<Cell<usize>>,
}

impl BusyTimeout<TErr> for Fake {
    fn busy_timeout(&self) -> Option<f64> {
        Some(*self.timeout.borrow())
    }
    fn set_busy_timeout(&mut self, seconds: f64) -> Result<(), TErr> {
        let call = self.calls.get();
        self.calls.set(call + 1);
        if Some(call) == self.fail {
            return Err(TErr::Sql {
                class: "ProgrammingError".into(),
                code: None,
            });
        }
        self.trace
            .borrow_mut()
            .push(json!(["set", float_repr::repr(seconds)]));
        *self.timeout.borrow_mut() = seconds;
        Ok(())
    }
}

fn state(tag: &str, scope: &HookScope) -> Value {
    json!([
        tag,
        scope.deadline().map(float_repr::repr),
        scope.is_suppressed()
    ])
}

fn lift(f: HookFailure<TErr>) -> TErr {
    match f {
        HookFailure::Raised(e) => e,
        HookFailure::StoreBusy { message, cause } => TErr::Busy {
            message,
            cause: cause.class_name().to_string(),
        },
    }
}

pub fn run(
    node: &Value,
    scope: &mut HookScope,
    store: &Option<Fake>,
    trace: &Rc<RefCell<Vec<Value>>>,
    reads: &mut usize,
) -> Result<(), TErr> {
    let n = node.as_array().unwrap();
    let tag = n[0].as_str().unwrap();
    if tag == "x" {
        return Err(TErr::Sql {
            class: n[1].as_str().unwrap().to_string(),
            code: n[2].as_i64(),
        });
    }
    if tag == "s" {
        let r = scope.suppressed(|scope| {
            trace.borrow_mut().push(state("in", scope));
            for child in n[1].as_array().unwrap() {
                run(child, scope, store, trace, reads)?;
            }
            Ok(())
        });
        trace.borrow_mut().push(state("out", scope));
        return r;
    }
    let event = n[1].as_str().unwrap();
    let now = float(&n[2]).unwrap();
    let mut given = if n[3].as_bool().unwrap() {
        store.clone()
    } else {
        None
    };
    let lends = given.is_some();
    let given_dyn: Option<&mut dyn BusyTimeout<TErr>> =
        given.as_mut().map(|s| s as &mut dyn BusyTimeout<TErr>);
    let mut clock_reads = 0usize;
    let mut clock = || {
        clock_reads += 1;
        now
    };
    let body = |scope: &mut HookScope, lent: Option<&mut dyn BusyTimeout<TErr>>| {
        assert_eq!(
            lent.is_some(),
            lends,
            "the store passed is lent to the body"
        );
        trace.borrow_mut().push(state("in", scope));
        for child in n[4].as_array().unwrap() {
            run(child, scope, store, trace, reads)?;
        }
        Ok(())
    };
    let r = if tag == "b" {
        scope.within_hook_budget(event, given_dyn, &mut clock, body)
    } else {
        scope.hook_body(event, given_dyn, &mut clock, body)
    };
    assert_eq!(clock_reads, 1, "one clock reading per block");
    *reads += 1;
    trace.borrow_mut().push(state("out", scope));
    r.map_err(lift)
}

/// A float as the generator spells it: a number, or the `repr` of one JSON
/// cannot hold (`"nan"`, `"inf"`, `"-inf"`).
pub fn float(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| v.as_str().map(|s| s.parse().unwrap()))
}

/// The trace of one `run` row, spelled as the generator spells it.
pub fn run_row(waited: Option<f64>, tree: &Value) -> Vec<Value> {
    run_row_failing(waited, None, tree)
}

/// `run_row` over a store whose `set_busy_timeout` call number `fail`
/// fails.
pub fn run_row_failing(waited: Option<f64>, fail: Option<usize>, tree: &Value) -> Vec<Value> {
    let trace = Rc::new(RefCell::new(Vec::new()));
    let store = waited.map(|w| Fake {
        timeout: Rc::new(RefCell::new(w)),
        trace: trace.clone(),
        fail,
        calls: Rc::new(Cell::new(0)),
    });
    let mut scope = HookScope::new();
    let mut reads = 0;
    let r = run(tree, &mut scope, &store, &trace, &mut reads);
    let last = match r {
        Ok(()) => json!(["ok"]),
        Err(TErr::Busy { message, cause }) => json!(["raised", "StoreBusy", message, cause]),
        Err(TErr::Sql { class, code }) => json!(["raised", class, code]),
    };
    trace.borrow_mut().push(last);
    trace.borrow_mut().push(state("end", &scope));
    let out = trace.borrow().clone();
    out
}
