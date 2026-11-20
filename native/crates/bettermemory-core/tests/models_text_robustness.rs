//! Hostile input for `models_text`: very long texts, texts of nothing but
//! dots, dashes or whitespace, the `i64` extremes. No case panics, and the
//! summary scan, the slug and the snippets grow linearly (the fastest of
//! three runs at 2n against n).

use std::sync::Mutex;
use std::time::{Duration, Instant};

use bettermemory_core::models_text::{
    first_summary_line, looks_truncated, make_slug, snippet_for, snippet_window, truncate_at_word,
    word_ending_at,
};
use bettermemory_core::unicode::cps;

/// One timing at a time: the tests of this file run on parallel threads.
static TIMING: Mutex<()> = Mutex::new(());

/// The fastest of three runs at n and at 2n, taken alternately so both
/// see the same load.
fn linear(make: &dyn Fn(usize) -> Vec<u32>, run: &dyn Fn(&[u32])) {
    let _held = TIMING.lock().unwrap_or_else(|e| e.into_inner());
    let n = 1_000_000;
    let a = make(n);
    let b = make(2 * n);
    let time = |t: &[u32]| {
        let start = Instant::now();
        run(t);
        start.elapsed()
    };
    let (mut ta, mut tb) = (Duration::MAX, Duration::MAX);
    for _ in 0..5 {
        ta = ta.min(time(&a));
        tb = tb.min(time(&b));
    }
    assert!(
        tb <= ta * 4 + Duration::from_millis(200),
        "2n took {tb:?} against {ta:?} at n"
    );
}

#[test]
fn the_summary_scan_is_linear_over_abbreviations() {
    // Every `.` is a boundary candidate followed by a space, and every run
    // before it is an abbreviation, so the scan walks the whole text.
    linear(&|n| cps(&"e.g. ".repeat(n / 5)), &|t| {
        first_summary_line(t, 80);
    });
}

#[test]
fn the_summary_scan_is_linear_over_dot_runs() {
    // `_word_ending_at` walks back through dots: a run of dots ending in
    // ". " is read back once per boundary, but only the one boundary.
    linear(&|n| cps(&format!("{} x", ".".repeat(n))), &|t| {
        first_summary_line(t, 80);
    });
}

#[test]
fn the_slug_and_snippets_are_linear() {
    linear(&|n| cps(&"ab-".repeat(n / 3)), &|t| {
        make_slug(t, i64::MAX, i64::MAX);
    });
    linear(&|n| cps(&" ".repeat(n)), &|t| {
        snippet_for(t, 200);
        snippet_window(t, 5, 200);
        looks_truncated(t);
    });
}

#[test]
fn extremes_do_not_panic() {
    let t = cps(&"word ".repeat(1000));
    for m in [i64::MIN, i64::MIN + 1, -41, -1, 0, 1, 39, 40, i64::MAX] {
        first_summary_line(&t, m);
        snippet_for(&t, m);
        truncate_at_word(&t, m);
        make_slug(&t, m, m);
        for s in [i64::MIN, -1, 0, 1, 5000, i64::MAX] {
            snippet_window(&t, s, m);
        }
    }
    for e in [i64::MIN, -1, 0, 5000, 5001, i64::MAX] {
        word_ending_at(&t, e);
    }
    let surrogates: Vec<u32> = (0xD800..0xE000).collect();
    make_slug(&surrogates, 6, 60);
    first_summary_line(&surrogates, 80);
    assert_eq!(
        make_slug(&cps(&"2024-01-02".repeat(1000)), 6, 60),
        cps("2024-01-022024-01-022024-01")
    );
}
