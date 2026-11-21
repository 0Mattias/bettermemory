//! Cases a plausible wrong `_check_body_candidates` fails, and a large
//! input. Each names the wrong implementation it kills.

use std::collections::HashSet;

use bettermemory_core::path_drift_steps::{check_body_candidates, Buckets};

fn run(
    c: &[(&str, bool, bool)],
    verified: &[&str],
    absent: &[&str],
    existing: &[&str],
) -> (Buckets, Vec<String>) {
    let c: Vec<(String, bool, bool)> = c.iter().map(|&(p, d, s)| (p.into(), d, s)).collect();
    let set = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<HashSet<_>>();
    let (v, x, e) = (set(verified), set(absent), set(existing));
    let log = std::cell::RefCell::new(Vec::new());
    let mut b = Buckets::default();
    check_body_candidates(
        &c,
        &v,
        &x,
        &mut b,
        &mut |p| {
            log.borrow_mut().push(format!("e {p}"));
            e.contains(p)
        },
        &mut |p| {
            log.borrow_mut().push(format!("rt {p}"));
            p.starts_with("/api")
        },
        &mut |p| {
            log.borrow_mut().push(format!("k {p}"));
            p.to_owned()
        },
    );
    (b, log.into_inner())
}

#[test]
fn the_prefix_is_keyed_before_it_is_checked() {
    // Kills `exists(prefix)` called before `keys(prefix)`.
    let (_, log) = run(&[("/a b", false, true)], &[], &[], &[]);
    assert_eq!(log, ["e /a b", "k /a b", "k /a", "e /a"]);
}

#[test]
fn the_prefix_splits_on_a_space_only() {
    // Kills a split on any whitespace.
    let (b, _) = run(&[("/a\tb c", false, true)], &[], &[], &["/a\tb"]);
    assert_eq!(b.checked, ["/a\tb"]);
}

#[test]
fn an_attested_candidate_skips_every_drop() {
    // Kills attestation read after the drops.
    let (b, _) = run(&[("/api/x", true, false)], &["/api/x"], &[], &[]);
    assert_eq!(
        (b.missing, b.claim_anchored),
        (vec!["/api/x".to_string()], vec!["/api/x".to_string()])
    );
    let (b, _) = run(&[("/api/x", false, false)], &[], &["/api/x"], &[]);
    assert_eq!(b.expected_absent, ["/api/x"]);
}

#[test]
fn the_route_check_comes_last() {
    // Kills the route arm placed before the spaced and ambiguous arms.
    let (b, log) = run(&[("/api/x Y", true, false)], &[], &[], &[]);
    assert!(b.dropped_as_route.is_empty());
    assert!(!log.iter().any(|l| l.starts_with("rt")));
}

#[test]
fn routes_are_appended_unguarded() {
    // Kills a `not in dropped_as_route` guard.
    let (b, _) = run(
        &[("/api/x", false, false), ("/api/x", false, false)],
        &[],
        &[],
        &[],
    );
    assert_eq!(b.dropped_as_route, ["/api/x", "/api/x"]);
}

#[test]
fn an_existing_verified_path_is_verified_not_missing() {
    let (b, _) = run(&[("/v", false, false)], &["/v"], &["/v"], &["/v"]);
    assert_eq!(
        (b.verified.len(), b.missing.len(), b.expected_absent.len()),
        (1, 0, 0)
    );
}

#[test]
fn many_candidates() {
    let paths: Vec<String> = (0..100_000).map(|i| format!("/p/{} Q", i % 5000)).collect();
    let c: Vec<(&str, bool, bool)> = paths.iter().map(|p| (p.as_str(), false, true)).collect();
    let (b, _) = run(&c, &[], &[], &["/p/1"]);
    assert_eq!(b.checked, ["/p/1"]);
}
