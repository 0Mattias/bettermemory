//! Hostile input for `credentials::gate`: values far past the ceiling (the
//! strips and the suffix still read the whole text), a 200,000-digit run
//! behind a placeholder word (the shape the reference's lookbehind exists
//! for), long separator runs, nested call parentheses, line feeds, the
//! integer edges (empty text, one code point, a lone surrogate). Nothing
//! panics, the answers are the reference's, and the time at four times the
//! length is held under ten times the time at the length (`linear`).
//! Code units past U+10FFFF, which no `str` holds, carry no class: they are
//! read as a character that is neither space, digit, letter nor word.

use std::time::{Duration, Instant};

use bettermemory_core::credentials::gate::{
    datetime_shape, is_placeholder, looks_like_secret, number_suffix,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn all(text: &[u32]) -> (bool, bool, bool, Option<(usize, usize)>) {
    (
        is_placeholder(text),
        datetime_shape(text),
        looks_like_secret(text),
        number_suffix(text),
    )
}

/// `reps` calls of the gate on `text`.
fn run(text: &[u32], reps: u32) -> Duration {
    let started = Instant::now();
    for _ in 0..reps {
        std::hint::black_box(all(std::hint::black_box(text)));
    }
    started.elapsed()
}

/// Time at `4n` against time at `n`. The repetition count is doubled until
/// the input of length `n` takes 20 ms, so the timer's grain and a single
/// stall decide nothing; each length is then the fastest of nine runs
/// taken in turn, so that a busy moment falls on both. Linear growth
/// quadruples the time, the square of the length would multiply it by 16,
/// and the bound of 10 leaves room for a loaded machine. A measurement
/// over the bound is taken again, up to five times in all: a load spike
/// passes on a later try, while quadratic growth fails every try.
fn linear(name: &str, make: impl Fn(usize) -> Vec<u32>, n: usize) {
    let mut last = (Duration::ZERO, Duration::ZERO);
    for _ in 0..5 {
        last = measure(&make, n);
        if last.1 < last.0 * 10 {
            return;
        }
    }
    panic!("{name}: {:?} then {:?}", last.0, last.1);
}

/// The fastest of nine runs at `n` and at `4n`.
fn measure(make: &impl Fn(usize) -> Vec<u32>, n: usize) -> (Duration, Duration) {
    let (small_text, large_text) = (make(n), make(4 * n));
    let mut reps = 1;
    while run(&small_text, reps) < Duration::from_millis(20) && reps < 1 << 20 {
        reps *= 2;
    }
    let (mut small, mut large) = (Duration::MAX, Duration::MAX);
    for _ in 0..9 {
        small = small.min(run(&small_text, reps));
        large = large.min(run(&large_text, reps));
    }
    (small, large)
}

#[test]
fn a_long_digit_run_is_read_in_linear_time() {
    let make = |n: usize| {
        let mut t = cps("changeme");
        t.extend(cps(&"7".repeat(n)));
        t
    };
    linear("digits", make, 50_000);
    let t = make(200_000);
    assert_eq!(all(&t), (true, false, false, Some((8, 200_008))));
}

#[test]
fn long_separator_and_word_runs() {
    linear("separators", |n| cps(&"-_".repeat(n)), 50_000);
    linear("words", |n| cps(&"dummy_".repeat(n)), 5_000);
    linear("dots", |n| cps(&format!("A{}", ".".repeat(n))), 50_000);
    linear(
        "calls",
        |n| cps(&format!("f{}x{}", "(".repeat(n), ")".repeat(n))),
        100_000,
    );
    assert_eq!(all(&cps(&"-_".repeat(1000))), (true, false, false, None));
    assert!(is_placeholder(&cps(&"dummy_".repeat(1000))));
}

#[test]
fn the_edges() {
    assert_eq!(all(&[]), (true, false, false, None));
    assert_eq!(all(&[0x30]), (true, false, false, Some((0, 1))));
    assert_eq!(all(&[0x0A]), (false, false, false, None));
    assert_eq!(all(&[0xD800]), (false, false, false, None));
    let mut t = cps("Abcdefgh1234");
    t.push(0xDFFF);
    assert!(looks_like_secret(&t));
    assert!(!looks_like_secret(&cps(&" ".repeat(100_000))));
    assert!(!looks_like_secret(&cps(&"\"'".repeat(100_000))));
    for unit in [0x11_0000, u32::MAX] {
        let mut t = cps("Abcdefgh1234");
        t.push(unit);
        assert!(looks_like_secret(&t));
        assert_eq!(all(&[unit]), (false, false, false, None));
        let mut t = cps("changeme");
        t.push(unit);
        assert_eq!(all(&t), (false, false, false, None));
    }
}
