//! Hostile input for `verify_candidates::validate`: long runs, nested
//! parentheses, texts built to make a backtracking matcher retry, code
//! point boundaries. Nothing panics, and each call stays linear: every
//! function on a million code points within one second in a release
//! build (ten in a debug one). The reference's
//! `_normalize_candidate` refuses these lengths before any work, and its
//! `_trim_candidate` called directly is quadratic in a run of `)` (a
//! `str.count` per character cut); the port counts once, so the bound
//! here is far under the reference's time on the same text.

use std::time::{Duration, Instant};

use bettermemory_core::verify_candidates::validate::{
    anchored_candidate, bounded_scan_text, command_shaped, escapes_root, fold_altsep,
    is_placeholder_path, is_single_segment_routelike, line_suffix, normalize_candidate,
    refused_spelling, trim_candidate,
};

const N: usize = 1_000_000;

fn rep(s: &str, n: usize) -> Vec<u32> {
    let piece: Vec<u32> = s.chars().map(u32::from).collect();
    let mut out = Vec::with_capacity(piece.len() * n);
    for _ in 0..n {
        out.extend_from_slice(&piece);
    }
    out
}

fn timed(label: &str, f: impl FnOnce()) {
    let start = Instant::now();
    f();
    let took = start.elapsed();
    // Every function of the unit runs on each text; a debug build under a
    // parallel test run is allowed ten times the release bound.
    let bound = Duration::from_secs(if cfg!(debug_assertions) { 10 } else { 1 });
    assert!(took < bound, "{label} took {took:?}");
}

fn every(text: &[u32]) {
    let _ = normalize_candidate(text);
    let _ = trim_candidate(text);
    let _ = command_shaped(text);
    let _ = anchored_candidate(text);
    let _ = line_suffix(text);
    let _ = is_placeholder_path(text);
    let _ = is_single_segment_routelike(text);
    let _ = refused_spelling(text);
    let _ = escapes_root(text);
    let _ = bounded_scan_text(text);
    let _ = fold_altsep(text, &[u32::from('\\')], Some(&[u32::from('/')]));
}

#[test]
fn long_runs_stay_linear() {
    for (label, text) in [
        ("closing parens", rep(")", N)),
        (
            "open then close",
            [rep("(", N / 2), rep(")", N / 2)].concat(),
        ),
        ("escaped spaces", rep("\\ ", N)),
        ("slashes", rep("/", N)),
        ("colons after a slash", [rep("/a", 1), rep(":", N)].concat()),
        (
            "digit runs",
            [rep("/a/b:", 1), rep("1", N), rep("-", 1)].concat(),
        ),
        ("separator then colon runs", rep("a/:1", N / 4)),
        ("climbing", rep("../", N)),
        ("spaced words", rep("/a b", N / 4)),
        ("whitespace only", rep(" ", N)),
        ("punctuation", rep(".,;:!?)>]}\"'", N / 12)),
        ("newlines", rep("\n", N)),
        ("dots", [rep("/etc/foo", 1), rep(".", N)].concat()),
    ] {
        timed(label, || every(&text));
    }
}

#[test]
fn trimming_a_paren_run_counts_once() {
    let text = rep(")", N);
    timed("trim", || assert!(trim_candidate(&text).is_empty()));
    let text = [rep("(", 3), rep(")", N)].concat();
    timed("trim balanced", || {
        assert_eq!(trim_candidate(&text), rep("((()))", 1));
    });
}

#[test]
fn folding_overlapping_patterns() {
    let text = rep("a", N + 1);
    timed("fold", || {
        let got = fold_altsep(&text, &[u32::from('b')], Some(&rep("aa", 1)));
        assert_eq!(got.len(), N / 2 + 1);
    });
}

#[test]
fn folding_a_long_needle_stays_linear() {
    let text = rep("a", N);
    let needle = [rep("a", N / 10), rep("b", 1)].concat();
    timed("fold long needle", || {
        let got = fold_altsep(&text, &[u32::from('/')], Some(&needle));
        assert_eq!(got, text);
    });
    let text = [rep("a", N), rep("b", 1)].concat();
    timed("fold long needle hit", || {
        let got = fold_altsep(&text, &[u32::from('/')], Some(&needle));
        assert_eq!(got, [rep("a", N - N / 10), rep("/", 1)].concat());
    });
}

#[test]
fn code_point_boundaries_never_panic() {
    for cp in [
        0u32, 0x7f, 0x80, 0xd7ff, 0xd800, 0xdbff, 0xdc00, 0xdfff, 0xe000, 0xffff, 0x10000, 0x10ffff,
    ] {
        for text in [
            vec![cp],
            vec![u32::from('/'), cp],
            vec![
                u32::from('/'),
                u32::from('a'),
                u32::from('/'),
                cp,
                u32::from(':'),
                u32::from('1'),
            ],
            vec![cp, u32::from(':'), u32::from('/'), u32::from('x')],
            vec![
                u32::from('/'),
                u32::from('a'),
                u32::from('/'),
                u32::from('b'),
                u32::from(' '),
                cp,
            ],
        ] {
            every(&text);
        }
    }
    every(&[]);
}

#[test]
fn lengths_at_the_caps() {
    for n in [511usize, 512, 513, 575, 576, 577, 32_767, 32_768, 32_769] {
        let text = [rep("/a/", 1), rep("b", n)].concat();
        every(&text);
        let text = [rep("/a/", 1), rep(".", n)].concat();
        every(&text);
    }
}
