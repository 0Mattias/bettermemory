//! `scope_match` on hostile input: bodies of a million code points with
//! ten thousand occurrences of a needle, a thousand declared roots, a
//! thousand scopes, needles past the engine's arena, positions at the
//! integer bounds. No panic in debug or release, and time within the
//! reference's growth family (`tests/timing`):
//!
//! - `_find_root_occurrence`: linear in the body here (one
//!   Knuth-Morris-Pratt walk); the reference's `str.find` from each failed
//!   index plus one is linear per call too, so ten times the body is held
//!   to `TENFOLD`;
//! - `_declared_root_spans`: one search from each match's start plus one,
//!   so a needle that overlaps itself costs its length at every match:
//!   linear in the body for a fixed needle (held to `TENFOLD`), and
//!   quadratic where the needle grows with the body (`a` * n/2 in `a` *
//!   n), the reference's family, held to a hundred times `TENFOLD`'s
//!   margin over the square;
//!   a needle whose leading run `_sre` leaves uncased (`/`, digits) is
//!   searched for that run as a literal prefix, so its time does not grow
//!   with the run: `/` * 500 + `x` in `/` * 200,000 is held to three times
//!   `/` * 50 + `x`, as the reference's prefix scan holds it;
//! - `_drop_ancestor_roots`: every root against every other, quadratic in
//!   the roots, as the reference.

#![cfg(unix)]

mod timing;

use bettermemory_core::pyre::regex::RegexError;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::scope_match::{
    boundary_after, declared_root_covers, declared_root_spans, drop_ancestor_roots,
    find_root_occurrence, home_alias, roots_of, trim_evidence, ScopeError, EVIDENCE_PADDING,
};

fn cps(t: &str) -> PyStr {
    t.chars().map(u32::from).collect()
}

fn never(_: &[u32]) -> Result<bool, ScopeError> {
    Ok(false)
}

fn always(_: &[u32]) -> Result<bool, ScopeError> {
    Ok(true)
}

const HOME: &str = "/hd/u";

/// A body of about `n` code points holding `hits` citations of `/w/r0`,
/// half of them re-cased, between runs of prose.
fn cited(n: usize, hits: usize) -> PyStr {
    let gap = (n / hits.max(1)).saturating_sub(12).max(1);
    let mut body = Vec::with_capacity(n + 16);
    for i in 0..hits {
        body.extend(std::iter::repeat_n(u32::from(b'x'), gap));
        body.extend(cps(if i % 2 == 0 { " /w/r0/f " } else { " /W/R0/f " }));
    }
    body.resize(n.max(body.len()), u32::from(b' '));
    body
}

fn declared(k: usize) -> (Vec<PyStr>, Vec<(PyStr, PyStr)>) {
    let scopes: Vec<PyStr> = (0..k).map(|i| cps(&format!("projects:p{i}"))).collect();
    let roots = (0..k)
        .map(|i| (scopes[i].clone(), cps(&format!("/w/r{i}"))))
        .collect();
    (scopes, roots)
}

#[test]
fn a_million_code_points_ten_thousand_hits_a_thousand_roots() {
    // Debug runs the same shape at a tenth of the roots.
    let k = if cfg!(debug_assertions) { 100 } else { 1000 };
    let body = cited(1_000_000, 10_000);
    let (scopes, roots) = declared(k);
    let home = cps(HOME);
    let mut probe = always;
    let spans = declared_root_spans(&body, &scopes, &roots, Some(&home), &mut probe).unwrap();
    // `/w/r0` (and `/w/r0` inside no other root) at every hit; `/w/r1`
    // and the rest nowhere.
    assert_eq!(spans.len(), 10_000);
    let mut probe = never;
    let spans = declared_root_spans(&body, &scopes, &roots, Some(&home), &mut probe).unwrap();
    assert_eq!(spans.len(), 5_000);
    // The occurrence past five thousand guard-rejected hits (`/w/r` then
    // `0`), and the cover.
    let needle = cps("/w/r");
    let mut tail = body.clone();
    tail.extend(cps(" /w/r"));
    assert_eq!(
        find_root_occurrence(&tail, &needle),
        Some((tail.len() - 4, tail.len()))
    );
    let declared_roots: Vec<PyStr> = roots.iter().map(|(_, r)| r.clone()).collect();
    let mut probe = always;
    assert!(declared_root_covers(
        &cps("/w/r1/x"),
        0,
        &cps("/w/r"),
        &declared_roots,
        false,
        Some(&home),
        &mut probe
    )
    .unwrap());
    // The evidence of a span the size of the body.
    let trimmed = trim_evidence(&body, 0, body.len(), EVIDENCE_PADDING);
    assert!(trimmed.len() < body.len());
}

#[test]
fn a_thousand_scopes_and_a_chain_of_ancestors() {
    let home = cps(HOME);
    let by: Vec<(PyStr, Vec<(PyStr, u64)>)> = (0..1000)
        .map(|i| {
            let root = cps(&"/a".repeat(i % 50 + 1));
            (
                cps(&format!("projects:s{i}")),
                vec![(cps(&format!("/w/{i}")), u64::MAX), (root, u64::MAX)],
            )
        })
        .collect();
    let mut probe = never;
    let got = roots_of(&by, Some(&home), &mut probe).unwrap();
    // The first counted wins each tie at u64::MAX; none is another's
    // ancestor.
    assert_eq!(got.len(), 1000);
    let chain: Vec<(PyStr, PyStr)> = (0..1000)
        .map(|i| (cps(&format!("s{i}")), cps(&"/a".repeat(i + 1))))
        .collect();
    let kept = drop_ancestor_roots(chain).unwrap();
    assert_eq!(kept.len(), 1);
}

#[test]
fn needles_past_the_engine_s_arena_decline() {
    let home = cps(HOME);
    let fits = cps(&format!("/{}", "a".repeat(3000)));
    let past = cps(&format!("/{}", "a".repeat(5000)));
    let (scopes, _) = declared(1);
    let mut probe = never;
    let mut body = fits.clone();
    body.push(u32::from(b' '));
    let got = declared_root_spans(
        &body,
        &scopes,
        &[(scopes[0].clone(), fits.clone())],
        Some(&home),
        &mut probe,
    );
    assert_eq!(got.unwrap(), vec![(0, fits.len())]);
    let got = declared_root_spans(
        &body,
        &scopes,
        &[(scopes[0].clone(), past)],
        Some(&home),
        &mut probe,
    );
    // The reference builds it; the engine refuses an arena that large.
    assert!(matches!(got, Err(ScopeError::Regex(RegexError::Lower(_)))));
}

#[test]
fn positions_at_the_integer_bounds() {
    let home = cps(HOME);
    let body = cps("/p/c/x");
    assert!(boundary_after(&body, usize::MAX));
    assert_eq!(trim_evidence(&body, usize::MAX, usize::MAX, 0), cps("..."));
    assert_eq!(trim_evidence(&body, 0, usize::MAX, 0), cps("/p/c/x"));
    let mut probe = always;
    assert!(declared_root_covers(
        &body,
        usize::MAX,
        &cps("/p"),
        &[cps("/p/c")],
        false,
        Some(&home),
        &mut probe
    )
    .is_ok_and(|c| !c));
    let mut probe = always;
    assert_eq!(
        home_alias(&cps(""), Some(&cps("")), &mut probe).unwrap(),
        None
    );
}

#[test]
fn the_occurrence_search_grows_linearly() {
    // Every hit fails its guard until the last: `str.find` from each.
    let make = |n: usize| {
        let mut body: PyStr = std::iter::repeat_n(cps("/a/foox"), n / 7)
            .flatten()
            .collect();
        body.extend(cps(" /a/foo"));
        body
    };
    let (small, large) = (make(100_000), make(1_000_000));
    let needle = cps("/a/foo");
    timing::grows_within(
        "find_root_occurrence",
        timing::TENFOLD,
        &mut || timing::once(|| find_root_occurrence(&small, &needle)),
        &mut || timing::once(|| find_root_occurrence(&large, &needle)),
    );
}

#[test]
fn the_spans_grow_linearly_for_a_fixed_needle() {
    let (small, large) = (cited(20_000, 200), cited(200_000, 2_000));
    let (scopes, roots) = declared(10);
    let home = cps(HOME);
    let run = |body: &PyStr| {
        let mut probe = always;
        declared_root_spans(body, &scopes, &roots, Some(&home), &mut probe).map(|s| s.len())
    };
    timing::grows_within(
        "declared_root_spans",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

#[test]
fn the_spans_of_an_uncased_prefix_do_not_grow_with_it() {
    // Every start of the body begins the prefix; none is followed by the
    // last code point.
    let home = cps(HOME);
    let run = |body: &PyStr, needle: &PyStr| {
        let scope = cps("projects:a");
        let mut probe = never;
        declared_root_spans(
            body,
            std::slice::from_ref(&scope),
            &[(scope.clone(), needle.clone())],
            Some(&home),
            &mut probe,
        )
        .map(|s| s.len())
    };
    for (fill, last) in [("/", "x"), ("1", "2")] {
        let body = cps(&fill.repeat(200_000));
        let short = cps(&format!("{}{last}", fill.repeat(50)));
        let long = cps(&format!("{}{last}", fill.repeat(500)));
        assert_eq!(run(&body, &long).unwrap(), 0);
        timing::grows_within(
            &format!("declared_root_spans, {fill} * 500 + {last} against {fill} * 50 + {last}"),
            3,
            &mut || timing::once(|| run(&body, &short)),
            &mut || timing::once(|| run(&body, &long)),
        );
    }
}

#[test]
fn the_spans_of_a_self_overlapping_needle_are_the_reference_s_square() {
    // `a` * n/2 in `a` * n: n/2 matches, each n/2 long.
    let make = |n: usize| (vec![u32::from(b'a'); n], cps(&"a".repeat(n / 2)));
    let (small, large) = (make(300), make(3000));
    let home = cps(HOME);
    let run = |(body, needle): &(PyStr, PyStr)| {
        let scope = cps("projects:a");
        let mut probe = never;
        declared_root_spans(
            body,
            std::slice::from_ref(&scope),
            &[(scope.clone(), needle.clone())],
            Some(&home),
            &mut probe,
        )
        .map(|s| s.len())
    };
    assert_eq!(run(&small).unwrap(), 151);
    timing::grows_within(
        "declared_root_spans, self-overlapping",
        timing::TENFOLD * 100,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

#[test]
fn the_ancestor_drop_is_the_reference_s_square() {
    let make = |n: usize| -> Vec<(PyStr, PyStr)> {
        (0..n)
            .map(|i| (cps(&format!("s{i}")), cps(&format!("/w/{i}/x"))))
            .collect()
    };
    let (small, large) = (make(100), make(1000));
    timing::grows_within(
        "drop_ancestor_roots",
        timing::TENFOLD * 100,
        &mut || timing::once(|| drop_ancestor_roots(small.clone())),
        &mut || timing::once(|| drop_ancestor_roots(large.clone())),
    );
}
