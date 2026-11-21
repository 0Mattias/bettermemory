//! Cases a plausible wrong `git_read_budget` or `git_calls` fails.

use std::cell::Cell;
use std::sync::atomic::AtomicI64;

use bettermemory_core::git_calls::{self as gc, RunRaised, Spawn, Tally};
use bettermemory_core::git_read_budget::{self as grb, Progresses, ReadBudget, Scope, Store};
use bettermemory_core::unicode::cps;

fn clock(values: &[f64]) -> impl Fn() -> f64 + '_ {
    let n = Cell::new(0);
    move || {
        let v = values[n.get()];
        n.set(n.get() + 1);
        v
    }
}

#[test]
fn remaining_exactly_zero_raises() {
    let mut b = ReadBudget {
        deadline: Some(3.0),
        ..ReadBudget::default()
    };
    assert!(b.allowance(&clock(&[3.0]), 1.0, &cps("/r"), true).is_err());
    // The root is recorded before the raise, first_home is not.
    assert_eq!(b.last_root, Some((cps("/r"), true)));
    assert_eq!(b.first_home, None);
    assert_eq!(b.deferred, Some((cps("/r"), false)));
}

#[test]
fn limited_only_when_strictly_less() {
    let mut b = ReadBudget::default();
    assert_eq!(
        b.allowance(&clock(&[0.0]), 5.0, &cps("/r"), false),
        Ok((5.0, false))
    );
    let mut b = ReadBudget::default();
    assert_eq!(
        b.allowance(&clock(&[0.0]), 6.0, &cps("/r"), false),
        Ok((5.0, true))
    );
}

#[test]
fn nan_request_is_given_back() {
    let mut b = ReadBudget::default();
    let (given, limited) = b
        .allowance(&clock(&[0.0]), f64::NAN, &cps("/r"), true)
        .unwrap();
    assert!(given.is_nan() && !limited);
}

#[test]
fn deferred_once_and_flags_the_first_home() {
    let mut b = ReadBudget {
        first_home: Some(cps("/a")),
        last_root: Some((cps("/a"), true)),
        ..ReadBudget::default()
    };
    b.exhausted();
    assert_eq!(b.deferred, Some((cps("/a"), true)));
    b.last_root = Some((cps("/b"), true));
    b.exhausted();
    assert_eq!(b.deferred, Some((cps("/a"), true)));
}

#[test]
fn a_scope_inside_another_reuses_and_does_not_finish() {
    let p = Progresses::<u8>::default();
    let mut s = Scope::default();
    grb::call_scope(&mut s, &p, &Store::Keyed(1), "n", |s, outer| {
        outer.borrow_mut().deferred = Some((cps("/x"), false));
        grb::call_scope(s, &p, &Store::Keyed(2), "m", |_, inner| {
            assert!(std::rc::Rc::ptr_eq(outer, inner));
        });
    });
    assert_eq!(p.peek(&Store::Keyed(2), "m"), None);
    assert_eq!(
        p.peek(&Store::Keyed(1), "n"),
        Some(Some((cps("/x"), false)))
    );
    assert!(s.owner.is_none());
}

#[test]
fn reading_restores_root_and_active() {
    let mut s = Scope::default();
    grb::reading(&mut s, &cps("/a"), true, |s, _| {
        grb::reading(s, &cps("/b"), false, |s, _| {
            assert_eq!(s.root, Some((cps("/b"), false)));
        });
        assert_eq!(s.root, Some((cps("/a"), true)));
        assert!(s
            .active
            .as_ref()
            .is_some_and(|b| b.borrow().deadline.is_none()));
    });
    assert!(s.active.is_none() && s.root.is_none());
}

#[test]
fn allowance_spells_none_for_no_place() {
    let mut s = Scope::default();
    grb::reading(&mut s, &cps("/r"), true, |s, b| {
        s.root = None;
        grb::allowance(s, &clock(&[0.0]), 1.0, None, None).unwrap();
        assert_eq!(b.borrow().last_root, Some((cps("None"), true)));
    });
}

#[test]
fn for_resume_after_rotates_past_the_last() {
    let mut s = Scope::default();
    let p = Progresses::<u8>::default();
    grb::call_scope(&mut s, &p, &Store::Absent, "", |s, b| {
        b.borrow_mut().resume = Some((cps("a"), true));
        let rows = vec![cps("a"), cps("b"), cps("a"), cps("c")];
        let got = grb::for_resume(s, &rows, |r| Some(r.clone()));
        assert_eq!(got, vec![cps("c"), cps("a"), cps("b"), cps("a")]);
        b.borrow_mut().resume = Some((cps("a"), false));
        let got = grb::for_resume(s, &rows, |r| Some(r.clone()));
        assert_eq!(got, rows);
    });
}

#[test]
fn timeout_under_a_spent_shared_limit_counts_three_and_raises() {
    let mut s = Scope::default();
    let u = AtomicI64::new(0);
    let mut t = Tally::default();
    grb::reading(&mut s, &cps("/r"), true, |s, _| {
        let c = clock(&[4.5, 9.5]);
        let got = gc::run_git(
            s,
            &c,
            &u,
            &mut t,
            9.0,
            None,
            None,
            || true,
            |_| Spawn::TimedOut,
        );
        assert_eq!(got, Err(RunRaised::ReadDeadline));
    });
    assert_eq!(
        (gc::unanswered_calls(&u), t.failed, t.timed_out, t.missing),
        (1, 1, 1, 0)
    );
}

#[test]
fn os_error_counts_missing_only_without_a_folder() {
    let s = Scope::default();
    let u = AtomicI64::new(0);
    let mut t = Tally::default();
    let c = clock(&[]);
    gc::run_git(
        &s,
        &c,
        &u,
        &mut t,
        1.0,
        None,
        None,
        || false,
        |_| Spawn::OsError,
    )
    .unwrap();
    assert_eq!(t.missing, 0);
    gc::run_git(
        &s,
        &c,
        &u,
        &mut t,
        1.0,
        Some(&cps("/c")),
        None,
        || false,
        |_| Spawn::OsError,
    )
    .unwrap();
    assert_eq!((t.missing, t.failed, gc::unanswered_calls(&u)), (1, 2, 2));
}

#[test]
fn printed_path_keeps_a_posix_cr() {
    assert_eq!(
        gc::printed_path(&cps("a\r\n"), false),
        cps("a\r").as_slice()
    );
    assert_eq!(gc::printed_path(&cps("a\r\n"), true), cps("a").as_slice());
    assert_eq!(gc::printed_path(&cps("a\r"), true), cps("a\r").as_slice());
    assert_eq!(gc::printed_path(&cps("a\n\n"), true), cps("a\n").as_slice());
}

#[test]
fn subcommand_keeps_a_lone_trailing_c() {
    assert_eq!(gc::subcommand(&["-c", "a=b", "-c"]), "-c");
    assert_eq!(gc::subcommand(&["-c"]), "-c");
    assert_eq!(gc::subcommand::<&str>(&[]), "");
    assert_eq!(gc::subcommand(&["-c", "a"]), "");
}

#[test]
fn wait_for_without_time_never_acquires() {
    struct L(u32);
    impl grb::WaitLock for L {
        fn acquire(&mut self, _: Option<f64>) -> bool {
            self.0 += 1;
            true
        }
        fn release(&mut self) {}
    }
    let mut s = Scope::default();
    let p = Progresses::<u8>::default();
    grb::call_scope(&mut s, &p, &Store::Absent, "", |s, b| {
        b.borrow_mut().deadline = Some(2.0);
        let mut l = L(0);
        assert!(!grb::wait_for(s, &clock(&[2.0]), &mut l, |g| g));
        assert_eq!(l.0, 0);
    });
}
