//! Hostile input for `git_read_budget` and `git_calls`: float extremes,
//! deep nesting, long argument lists and texts; no panic, and linear time.

mod timing;

use bettermemory_core::git_calls as gc;
use bettermemory_core::git_read_budget::{self as grb, Progresses, ReadBudget, Scope, Store};
use bettermemory_core::unicode::cps;

#[test]
fn float_extremes_never_panic() {
    let xs = [
        0.0,
        -0.0,
        f64::MIN_POSITIVE,
        f64::MAX,
        f64::MIN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NAN,
    ];
    for &now in &xs {
        for &req in &xs {
            for &dl in &xs {
                let mut b = ReadBudget {
                    deadline: Some(dl),
                    ..ReadBudget::default()
                };
                let _ = b.allowance(&|| now, req, &cps("/r"), true);
            }
        }
    }
}

fn nest(s: &mut Scope, p: &Progresses<u32>, depth: u32) -> u32 {
    if depth == 0 {
        return 0;
    }
    grb::reading(s, &cps("/r"), depth.is_multiple_of(2), |s, _| {
        grb::call_scope(s, p, &Store::Keyed(depth), "n", |s, _| {
            1 + nest(s, p, depth - 1)
        })
    })
}

#[test]
fn deep_nesting_unwinds_every_level() {
    // Python stops at its recursion limit (1,000 frames); two thousand
    // levels run on a thread with room for them.
    std::thread::Builder::new()
        .stack_size(256 << 20)
        .spawn(|| {
            let p = Progresses::<u32>::default();
            let mut s = Scope::default();
            assert_eq!(nest(&mut s, &p, 2000), 2000);
            assert!(s.owner.is_none() && s.active.is_none() && s.root.is_none());
        })
        .unwrap()
        .join()
        .unwrap();
}

#[test]
fn for_resume_and_subcommand_grow_linearly() {
    let rows =
        |n: usize| -> Vec<Vec<u32>> { (0..n).map(|i| cps(&format!("/r{}", i % 7))).collect() };
    let small = rows(20_000);
    let large = rows(200_000);
    let p = Progresses::<u8>::default();
    let run = |rows: &Vec<Vec<u32>>| {
        let mut s = Scope::default();
        grb::call_scope(&mut s, &p, &Store::Absent, "", |s, b| {
            b.borrow_mut().resume = Some((cps("/r3"), true));
            grb::for_resume(s, rows, |r| Some(r.clone())).len()
        })
    };
    timing::grows_within(
        "for_resume",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
    let args = |n: usize| -> Vec<String> {
        let mut a: Vec<String> = (0..n)
            .flat_map(|_| ["-c".to_owned(), "k=v".to_owned()])
            .collect();
        a.push("log".into());
        a
    };
    let (a, b) = (args(50_000), args(500_000));
    timing::grows_within(
        "subcommand",
        timing::TENFOLD,
        &mut || timing::once(|| gc::subcommand(&a).len()),
        &mut || timing::once(|| gc::subcommand(&b).len()),
    );
    let long = "x\u{2028}".repeat(100_000) + &" ".repeat(100_000);
    let line = gc::nonzero_exit_line(&["log"], i64::MIN, "w", &long);
    assert_eq!(line, format!("git log exited {} in w: x", i64::MIN));
    let huge = cps(&("a".repeat(1_000_000) + "\r\n"));
    assert_eq!(gc::printed_path(&huge, true).len(), 1_000_000);
}
