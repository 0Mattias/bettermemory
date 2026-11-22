//! Cases a plausible wrong port of `event_hook` fails: the primary code
//! masked (not compared whole), LOCKED counted with BUSY, only
//! `OperationalError` counted, the earlier deadline kept (`min`, not the
//! newer one), the busy timeout lowered only above the budget and given
//! back, both values restored after an error, the store lent to the
//! body, and a failing `set_busy_timeout` answered as the reference does.

mod event_hook_tree;

use bettermemory_core::event_hook::{
    is_busy, not_done, primary_code, HookFailure, HookScope, SQLITE_BUSY, SQLITE_LOCKED,
};
use event_hook_tree::{Fake, TErr};
use serde_json::json;
use std::cell::{Cell, RefCell};
use std::rc::Rc;

#[test]
fn the_code_is_masked_to_its_primary() {
    assert_eq!(primary_code(517), SQLITE_BUSY);
    assert_eq!(primary_code(262), SQLITE_LOCKED);
    assert_eq!(primary_code(-1), 255);
    assert_eq!(primary_code(-251), SQLITE_BUSY);
    assert!(is_busy("OperationalError", Some(517)));
    assert!(is_busy("OperationalError", Some(6)));
    assert!(!is_busy("OperationalError", Some(8)));
    assert!(!is_busy("OperationalError", None));
    assert!(!is_busy("IntegrityError", Some(5)));
    assert!(!is_busy("DatabaseError", Some(5)));
}

#[test]
fn unknown_events_show_nothing() {
    assert_eq!(not_done("prompt"), "the recall was not shown");
    assert_eq!(not_done("Prompt"), "nothing was shown");
    assert_eq!(not_done(""), "nothing was shown");
}

#[test]
fn the_earlier_deadline_wins_and_comes_back() {
    let tree = json!([
        "b",
        "stop",
        100.0,
        false,
        [["b", "prompt", 50.0, false, []]]
    ]);
    let trace = event_hook_tree::run_row(None, &tree);
    assert_eq!(trace[1], json!(["in", "58.0", false]));
    assert_eq!(trace[2], json!(["out", "113.0", false]));
    let tree = json!([
        "b",
        "stop",
        100.0,
        false,
        [["b", "prompt", 200.0, false, []]]
    ]);
    let trace = event_hook_tree::run_row(None, &tree);
    assert_eq!(trace[1], json!(["in", "113.0", false]));
}

#[test]
fn the_timeout_is_lowered_only_above_the_budget() {
    let lowered = event_hook_tree::run_row(Some(8.5), &json!(["b", "prompt", 0.0, true, []]));
    assert_eq!(lowered[0], json!(["set", "8.0"]));
    assert_eq!(lowered[2], json!(["set", "8.5"]));
    let equal = event_hook_tree::run_row(Some(8.0), &json!(["b", "prompt", 0.0, true, []]));
    assert_eq!(equal.len(), 4);
}

#[test]
fn an_error_gives_both_values_back() {
    let mut scope = HookScope::new();
    let mut clock = || 1.0;
    let r: Result<(), _> = scope.suppressed(|scope| {
        scope.within_hook_budget("stop", None, &mut clock, |s, _| {
            assert_eq!(s.deadline(), Some(14.0));
            Err(TErr::Sql {
                class: "OperationalError".into(),
                code: Some(5),
            })
        })
    });
    assert!(matches!(r, Err(HookFailure::StoreBusy { .. })));
    assert_eq!(scope, HookScope::new());
}

#[test]
fn a_nan_deadline_held_stays() {
    // Python's min(held, at) keeps held unless at < held: a NaN held stays.
    let mut scope = HookScope::new();
    let mut nan = || f64::NAN;
    let mut one = || 1.0;
    let _ = scope.within_hook_budget::<(), TErr>("prompt", None, &mut nan, |s, _| {
        s.within_hook_budget::<(), TErr>("prompt", None, &mut one, |s, _| {
            assert!(s.deadline().unwrap().is_nan());
            Ok(())
        })
        .map_err(|_| unreachable!())
    });
}

#[test]
fn the_body_gets_the_store_at_the_budget() {
    let mut fake = Fake {
        timeout: Rc::new(RefCell::new(30.0)),
        trace: Rc::new(RefCell::new(Vec::new())),
        fail: None,
        calls: Rc::new(Cell::new(0)),
    };
    let seen = fake.timeout.clone();
    let mut scope = HookScope::new();
    let mut clock = || 1.0;
    let got = scope.within_hook_budget::<_, TErr>("prompt", Some(&mut fake), &mut clock, |_, s| {
        Ok(s.expect("the store is lent to the body").busy_timeout())
    });
    assert_eq!(got.unwrap(), Some(8.0));
    assert_eq!(*seen.borrow(), 30.0);
}

#[test]
fn a_failing_restore_replaces_the_outcome_and_keeps_the_budget() {
    let mut fake = Fake {
        timeout: Rc::new(RefCell::new(30.0)),
        trace: Rc::new(RefCell::new(Vec::new())),
        fail: Some(1),
        calls: Rc::new(Cell::new(0)),
    };
    let seen = fake.timeout.clone();
    let mut scope = HookScope::new();
    let mut clock = || 1.0;
    let got =
        scope.within_hook_budget::<(), TErr>("prompt", Some(&mut fake), &mut clock, |_, _| {
            Err(TErr::Sql {
                class: "OperationalError".into(),
                code: Some(5),
            })
        });
    assert!(
        matches!(got, Err(HookFailure::Raised(TErr::Sql { ref class, .. })) if class == "ProgrammingError")
    );
    assert_eq!(*seen.borrow(), 8.0);
    assert_eq!(scope, HookScope::new());
}

#[test]
fn a_failing_lowering_leaves_the_deadline_set() {
    let mut fake = Fake {
        timeout: Rc::new(RefCell::new(30.0)),
        trace: Rc::new(RefCell::new(Vec::new())),
        fail: Some(0),
        calls: Rc::new(Cell::new(0)),
    };
    let mut scope = HookScope::new();
    let mut clock = || 1.0;
    let got =
        scope.within_hook_budget::<(), TErr>("prompt", Some(&mut fake), &mut clock, |_, _| {
            unreachable!("the body does not run")
        });
    assert!(matches!(got, Err(HookFailure::Raised(_))));
    assert_eq!(scope.deadline(), Some(9.0));
}
