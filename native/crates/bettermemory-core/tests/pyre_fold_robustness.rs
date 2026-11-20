//! `pyre::fold` over hostile input: values that are no code point (past
//! U+10FFFF up to `u32::MAX`), the surrogates, the edges of every table,
//! every code point through every function under every flag set, and long
//! texts. No panic in a debug or a release build; time linear in the text
//! (ten times the text costs under thirty times the time); one literal over
//! every code point in a bounded time; and the three fast paths of the
//! lower case cheaper than a search of its table. The tables read first
//! from many threads at once are `pyre_fold_first_read`, a binary of its
//! own.
//!
//! Nothing here nests or backtracks: one call reads one code point and at
//! most two tables.

use std::time::{Duration, Instant};

use bettermemory_core::pyre::fold::{
    extra_cases, sre_iscased, sre_lower, sre_upper, Lit, Unreproduced, IGNORECASE, LOCALE, UNICODE,
};
use bettermemory_core::unicode;

const N: u32 = 0x110000;

/// The literal `c` under IGNORECASE on a `str` pattern.
fn fold(c: u32) -> Lit {
    Lit::new(c, IGNORECASE | UNICODE).expect("the Unicode fold")
}

/// Every function of the unit on one value; a value that is no code point
/// must come back unchanged, uncased and in no class.
fn through_everything(c: u32) {
    let (lower, upper, cased) = (sre_lower(c), sre_upper(c), sre_iscased(c));
    assert_eq!(cased, lower != c || upper != c, "{c:#x}");
    let extra = extra_cases(c);
    for flags in [UNICODE, IGNORECASE | UNICODE] {
        let ignorecase = flags & IGNORECASE != 0;
        let lit = Lit::new(c, flags).expect("the Unicode fold");
        assert!(lit.matches(c), "{c:#x} matches itself");
        match lit {
            Lit::Exact(p) => {
                assert!(p == c && !(ignorecase && cased), "{c:#x}");
                assert!(!lit.matches(c ^ 1), "{c:#x}");
            }
            Lit::Fold { lower: l, extra: e } => {
                assert!(ignorecase && cased && l == lower, "{c:#x}");
                assert_eq!(e, extra_cases(l), "{c:#x}");
                assert!(lit.matches(l) && e.iter().all(|&x| lit.matches(x)));
            }
        }
    }
    assert_eq!(Lit::new(c, IGNORECASE), Err(Unreproduced::AsciiFold));
    assert_eq!(Lit::new(c, IGNORECASE | LOCALE), Err(Unreproduced::Locale));
    if c >= N {
        assert_eq!((lower, upper, cased), (c, c, false), "{c:#x}");
        assert!(extra.is_empty(), "{c:#x}");
        assert!(
            !unicode::is_word(c) && !unicode::is_re_digit(c) && !unicode::is_space(c),
            "{c:#x} is in no class"
        );
        assert_eq!(fold(c), Lit::Exact(c));
    } else {
        // A code point's cases are code points, in its own plane.
        assert!(lower < N && upper < N, "{c:#x}");
        assert_eq!(lower > 0xFFFF, c > 0xFFFF, "{c:#x}");
        assert_eq!(upper > 0xFFFF, c > 0xFFFF, "{c:#x}");
        assert_eq!(sre_lower(lower), lower, "{c:#x}: lowering twice");
        assert!(extra.iter().all(|&x| x < 0x10000), "{c:#x}");
    }
}

#[test]
fn integer_boundaries_and_surrogates() {
    let mut values = vec![
        0,
        1,
        0x7F,
        0x80,
        0xFF,
        0x100,
        0xD7FF,
        0xD800,
        0xDBFF,
        0xDC00,
        0xDFFF,
        0xE000,
        0xFFFF,
        0x10000,
        0x1E943,
        0x1E944,
        0x10FFFF,
        N,
        N + 1,
        0x7FFF_FFFF,
        0x8000_0000,
        u32::MAX - 1,
        u32::MAX,
    ];
    // Around every power of two, and a cased code point with any high bit
    // set (a table read by a truncated index would answer for these).
    for bit in 0..32 {
        let p = 1u32 << bit;
        values.extend([p.wrapping_sub(2), p.wrapping_sub(1), p, p + 1]);
        for cased in [0x41, 0x6B, 0xDF, 0x130, 0x212A, 0x10400] {
            values.push(cased | p);
        }
    }
    for c in values {
        through_everything(c);
    }
    // A surrogate is uncased and has no class, as in a Python str.
    for c in 0xD800..0xE000 {
        assert_eq!((sre_lower(c), sre_upper(c), sre_iscased(c)), (c, c, false));
        assert_eq!(fold(c), Lit::Exact(c));
    }
}

#[test]
fn every_code_point_and_the_whole_integer_range() {
    for c in 0..N + 0x100 {
        through_everything(c);
    }
    // The rest of the u32 range by a stride (about a million values).
    let mut c = N;
    while c < u32::MAX - 4099 {
        through_everything(c);
        c += 4099;
    }
}

/// A text of `n` code points that visits every kind: ASCII, cased and
/// uncased letters of the BMP, a surrogate, cased and uncased code points
/// past the BMP, and a value that is no code point.
fn hostile_text(n: usize) -> Vec<u32> {
    const PIECE: [u32; 16] = [
        0x6B, 0x4B, 0x212A, 0x20, 0x130, 0x131, 0x3A3, 0x3C2, 0xD800, 0x4E00, 0x10400, 0x10428,
        0x1F600, 0x1E943, 0x10FFFF, 0x110000,
    ];
    (0..n).map(|i| PIECE[i % PIECE.len()]).collect()
}

/// How many characters of `text` each of a few literals matches.
fn scan(text: &[u32]) -> [usize; 5] {
    let lits = [
        fold(0x6B),
        fold(0x69),
        fold(0x3C3),
        fold(0x10400),
        fold(0x20),
    ];
    lits.map(|lit| text.iter().filter(|&&c| lit.matches(c)).count())
}

#[test]
fn a_long_text_is_scanned_in_linear_time() {
    let small = hostile_text(200_000);
    let big = hostile_text(2_000_000);
    // 16 code points a piece: `k` matches 3 of them, `i` 2, sigma 2,
    // U+10400 2, the space 1.
    assert_eq!(scan(&small), [37_500, 25_000, 25_000, 25_000, 12_500]);
    assert_eq!(scan(&big), [375_000, 250_000, 250_000, 250_000, 125_000]);
    // The fastest run of each size over up to five rounds: time stolen by
    // other work only ever adds.
    let mut best: Option<(Duration, Duration)> = None;
    for _ in 0..5 {
        let t = Instant::now();
        std::hint::black_box(scan(std::hint::black_box(&small)));
        let s = t.elapsed();
        let t = Instant::now();
        std::hint::black_box(scan(std::hint::black_box(&big)));
        let b = t.elapsed();
        let (s, b) = best.map_or((s, b), |(bs, bb)| (bs.min(s), bb.min(b)));
        best = Some((s, b));
        if b <= s * 30 + Duration::from_millis(50) {
            return;
        }
    }
    panic!("scan: fastest {best:?}");
}

#[test]
fn one_literal_over_every_code_point_in_bounded_time() {
    // `re.compile('k', re.I).findall` over the string of every code point
    // takes the reference 4 to 6 ms (the best of 20 runs) on the machine
    // the fixtures were made on, and a release build about as long; a
    // release build is held to 50 ms, ten times that. A debug build is 30
    // to 100 times slower than a release one and is held to 2 s. The
    // fastest of up to ten rounds counts, the first under the bound ends
    // the test: time stolen by other work only ever adds.
    let bound = Duration::from_millis(if cfg!(debug_assertions) { 2000 } else { 50 });
    let lit = fold(0x6B);
    let mut best = Duration::MAX;
    for _ in 0..10 {
        let t = Instant::now();
        let found = (0..N).filter(|&c| lit.matches(c)).count();
        best = best.min(t.elapsed());
        assert_eq!(std::hint::black_box(found), 3);
        if best < bound {
            return;
        }
    }
    panic!("fastest {best:?}, over {bound:?}");
}

#[test]
fn building_a_literal_for_every_code_point_is_linear() {
    // `Lit::new` reads both case tables; code points past the BMP go by
    // binary search. All the code points cost under 48 times (three times
    // the ratio of the counts) the time of their first sixteenth, plus
    // 50 ms, each the fastest of up to five rounds.
    let build = |upto: u32| -> usize {
        (0..upto)
            .filter(|&c| matches!(fold(c), Lit::Fold { .. }))
            .count()
    };
    assert_eq!(build(N), 2927);
    let mut best: Option<(Duration, Duration)> = None;
    for _ in 0..5 {
        let t = Instant::now();
        std::hint::black_box(build(N / 16));
        let s = t.elapsed();
        let t = Instant::now();
        std::hint::black_box(build(N));
        let b = t.elapsed();
        let (s, b) = best.map_or((s, b), |(bs, bb)| (bs.min(s), bb.min(b)));
        best = Some((s, b));
        if b <= s * 48 + Duration::from_millis(50) {
            return;
        }
    }
    panic!("build: fastest {best:?}");
}

/// The time `sre_lower` takes over `text`.
fn lowering(text: &[u32]) -> Duration {
    let t = Instant::now();
    let sum: u64 = text
        .iter()
        .map(|&c| u64::from(sre_lower(std::hint::black_box(c))))
        .sum();
    std::hint::black_box(sum);
    t.elapsed()
}

#[test]
fn the_fast_paths_skip_the_table_search() {
    // `sre_lower` answers three kinds of code point without searching the
    // lower-case table: ASCII (A to Z by arithmetic), a code point of the
    // BMP whose dense flags say it has no lower case, and one past the
    // table's last key (U+1E921). Each is timed against code points of its
    // range that are searched; without its fast path it is searched too
    // and costs about as much.
    //
    // Measured on the machine the fixtures were made on, as a share of the
    // searched time: a debug build 0.02 to 0.05, where a search costs 0.4
    // to 1.5 us; a release build 0.14 for A to Z, 0.2 to 0.46 for the other
    // two, and without the fast path 0.8 to 1.15, 0.48 and 0.54 (a search
    // costs 6 to 10 ns there, a lookup of the flags 2). So a debug build, which
    // the parity job runs, holds each to a quarter; a release build holds
    // each to three quarters, which removing the ASCII path breaks and the
    // other two do not. Every text is 100,000 code points; each time is the
    // fastest of 3 to 10 rounds in a debug build, of 20 to 50 in a release
    // one, whose rounds take milliseconds and whose cores differ in speed.
    let ((num, den), (least, most)) = if cfg!(debug_assertions) {
        ((1, 4), (3, 10))
    } else {
        ((3, 4), (20, 50))
    };
    let cycle = |set: Vec<u32>| -> Vec<u32> {
        assert!(!set.is_empty());
        (0..100_000).map(|i| set[i % set.len()]).collect()
    };
    let has_lower = |c: &u32| sre_lower(*c) != *c;
    // A to Z, and Latin-1 and Cyrillic capitals, each a short cycle.
    let capitals = cycle((0x41..=0x5A).collect());
    let capitals_searched = cycle(
        (0xC0..=0xDE)
            .filter(|&c| c != 0xD7)
            .chain(0x410..=0x42F)
            .collect(),
    );
    let bmp_none = cycle(
        (0x80..0x10000)
            .filter(|c| !has_lower(c) && !(0xD800..0xE000).contains(c))
            .collect(),
    );
    let bmp_searched = cycle((0x80..0x10000).filter(has_lower).collect());
    let past_last = cycle((0x1E922..N).collect());
    let past_searched = cycle((0x10000..N).filter(has_lower).collect());
    let texts = [
        &capitals,
        &capitals_searched,
        &bmp_none,
        &bmp_searched,
        &past_last,
        &past_searched,
    ];
    // (fast text, searched text, the fast path)
    let pairs = [
        (0, 1, "A to Z by arithmetic"),
        (2, 3, "the BMP's flags"),
        (4, 5, "past the last key"),
    ];
    let held = |best: &[Duration], &(fast, searched, _): &(usize, usize, &str)| {
        best[fast] * den < best[searched] * num
    };
    let mut best = [Duration::MAX; 6];
    for round in 1..=most {
        for (b, text) in best.iter_mut().zip(texts) {
            *b = (*b).min(lowering(text));
        }
        if round >= least && pairs.iter().all(|pair| held(&best, pair)) {
            return;
        }
    }
    let failed: Vec<String> = pairs
        .iter()
        .filter(|pair| !held(&best, pair))
        .map(|&(fast, searched, what)| format!("{what}: {:?} of {:?}", best[fast], best[searched]))
        .collect();
    panic!("over {num}/{den}: {}", failed.join("; "));
}
