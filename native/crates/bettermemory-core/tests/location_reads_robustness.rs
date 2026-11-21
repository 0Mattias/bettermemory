//! Hostile batches for `location_reads`: many jobs on one path, many
//! paths, budgets at the bounds (zero, negative, NaN, huge), and the cost
//! of a batch growing linearly with its jobs.

mod common;
mod timing;

use std::time::{Duration, Instant};

use bettermemory_core::location_reads::{bounded_read, bounded_reads, Job, Read};
use common::location_reads::{clock, Script, B};

fn healthy(script: &Script, n: usize, paths: usize) -> Vec<Job<String, String, String>> {
    (0..n)
        .map(|i| {
            let p = format!("/p{}", i % paths);
            let w = format!("w{i}");
            let read = script.make(&p, &w, &format!("v:{i}"));
            (p, w, read)
        })
        .collect()
}

#[test]
fn a_batch_of_healthy_reads_grows_linearly() {
    let run = |n: usize| {
        let script = Script::default();
        let jobs = healthy(&script, n, n);
        let board = B::new(clock());
        timing::wall(|| bounded_reads(&board, jobs, 30.0, false).unwrap())
    };
    timing::grows_within(
        "location_reads: 1,000 and 10,000 healthy jobs",
        timing::TENFOLD,
        &mut || run(1_000),
        &mut || run(10_000),
    );
}

#[test]
fn many_jobs_on_one_path_answer_in_order() {
    let script = Script::default();
    let board = B::new(clock());
    let out = bounded_reads(&board, healthy(&script, 20_000, 1), 30.0, false).unwrap();
    assert_eq!(out.len(), 20_000);
    assert_eq!(out[19_999], Read::Value("19999".into()));
}

#[test]
fn budgets_at_the_bounds_answer_without_panic() {
    for budget in [0.0, -1.0, f64::NAN, f64::NEG_INFINITY, 1e-300] {
        let script = Script::default();
        let board = B::new(clock());
        let t = Instant::now();
        let r = bounded_read(
            &board,
            "/a",
            "x".into(),
            script.make("/a", "x", "h:k"),
            budget,
        );
        assert_eq!(r, Ok(Read::Unanswered), "{budget}");
        let out = bounded_reads(&board, healthy(&script, 50, 5), budget, false).unwrap();
        assert_eq!(out.len(), 50);
        assert!(t.elapsed() < Duration::from_secs(10), "{budget}");
        script.release_all();
    }
    // A huge budget waits as long as the read takes.
    for budget in [1e300, f64::INFINITY] {
        let script = Script::default();
        let board = B::new(clock());
        let out = bounded_reads(&board, healthy(&script, 10, 3), budget, true).unwrap();
        assert_eq!(out[9], Read::Value("9".into()));
    }
}
