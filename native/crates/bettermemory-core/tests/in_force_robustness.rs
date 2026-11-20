//! Hostile shapes for `in_force`: a supersession chain tens of thousands
//! long, a fan-in of as many superseders, a cycle through every record,
//! long ids and stamps. No panic, and time growing linearly: the fastest
//! of three runs at 2n against the fastest at n. Every case runs under a
//! deadline, so a broken cycle check fails instead of hanging.

#[path = "in_force_world/mod.rs"]
mod world;

use std::time::{Duration, Instant};

use world::{ids, row, run, run_within, s, Case};

const S: &str = "supersedes";
const E: &str = "extends";

fn name(i: usize) -> String {
    format!("r{i:08}")
}

/// `r0 <- r1 <- ... <- r(n-1)`, each superseding the one before; closed
/// into a cycle when `cycle`.
fn chain(n: usize, cycle: bool) -> Case {
    let mut links: Vec<_> = (0..n - 1)
        .map(|i| (name(i), vec![row(S, &name(i + 1), &format!("{i:08}"))]))
        .collect();
    if cycle {
        links.push((name(n - 1), vec![row(S, &name(0), "0")]));
    }
    Case {
        links,
        admit: None,
        held: Vec::new(),
        ids: vec![name(0)],
    }
}

fn fan(n: usize) -> Case {
    let rows = (1..=n)
        .map(|i| {
            row(
                if i % 3 == 0 { E } else { S },
                &name(i),
                &format!("{:08}", i % 97),
            )
        })
        .collect();
    Case {
        links: vec![(name(0), rows)],
        admit: None,
        held: (0..n).step_by(2).map(name).collect(),
        ids: vec![name(0)],
    }
}

/// `run` failing when it has not answered within `limit`.
fn within(case: Case, limit: Duration) -> Vec<bettermemory_core::in_force::InForce> {
    run_within(&case, false, limit).0
}

fn fastest(case: &Case) -> Duration {
    (0..3)
        .map(|_| {
            let t = Instant::now();
            let out = run(case, false).0;
            assert_eq!(out.len(), case.ids.len());
            t.elapsed()
        })
        .min()
        .expect("three runs")
}

fn linear(build: impl Fn(usize) -> Case, n: usize) {
    let small = fastest(&build(n));
    let large = fastest(&build(2 * n));
    assert!(
        large <= small * 5 + Duration::from_millis(50),
        "time at 2n {large:?} against {small:?} at n"
    );
}

#[test]
fn a_long_chain_is_followed_to_its_end_in_linear_time() {
    let out = run(&chain(20_000, false), false).0;
    assert_eq!(out[0].id, name(19_999));
    assert_eq!(out[0].replaces, Some(name(0)));
    linear(|n| chain(n, false), 10_000);
}

#[test]
fn a_cycle_through_every_record_ends_and_stays_linear() {
    let out = run(&chain(20_000, true), false).0;
    assert_eq!(out[0].id, name(19_999));
    linear(|n| chain(n, true), 10_000);
}

#[test]
fn a_wide_fan_in_picks_the_newest_free_head_in_linear_time() {
    let out = run(&fan(30_000), false).0;
    assert_eq!(out[0].id, name(29_875));
    assert!(!out[0].held);
    linear(fan, 20_000);
}

#[test]
fn long_ids_and_stamps_and_many_asked() {
    let big = "x".repeat(1 << 20);
    let case = Case {
        links: vec![(
            s("a"),
            vec![row(S, &big, &big), row(S, "b", &"9".repeat(1 << 16))],
        )],
        admit: Some(ids(&["a", "b"])),
        held: vec![big.clone()],
        ids: (0..50_000)
            .map(|i| name(i % 1000))
            .chain([s("a")])
            .collect(),
    };
    let out = run(&case, false).0;
    assert_eq!(out.len(), 1001);
    let a = out.iter().find(|f| f.asked == "a").expect("a");
    assert_eq!(a.id, "b");
}

#[test]
fn short_cycles_end_before_a_deadline() {
    for n in 2..=5 {
        let mut case = chain(n, true);
        case.ids = (0..n).map(name).collect();
        let out = within(case, Duration::from_secs(30));
        assert_eq!(out.len(), n, "cycle of {n}");
    }
    for n in 1..=5 {
        let mut case = chain(n + 1, true);
        case.links.pop();
        case.links
            .push((name(n), vec![row(S, &name(1), &format!("{n:08}"))]));
        case.ids = (0..=n).map(name).collect();
        let out = within(case, Duration::from_secs(30));
        assert_eq!(out.len(), n + 1, "lead-in to a cycle of {n}");
    }
}

#[test]
fn every_id_of_a_long_chain_asked_grows_no_worse_than_quadratic() {
    let every = |n: usize| {
        let mut case = chain(n, false);
        case.ids = (0..n).map(name).collect();
        case
    };
    let out = within(every(1_000), Duration::from_secs(120));
    assert_eq!(out.len(), 1_000);
    assert!(out.iter().all(|f| f.id == name(999)));
    let small = fastest(&every(250));
    let large = fastest(&every(500));
    assert!(
        large <= small * 10 + Duration::from_millis(200),
        "time at 2n {large:?} against {small:?} at n"
    );
}
