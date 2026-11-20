//! Hostile input for `durability.py`'s unit 1: very long texts, unpaired
//! quotes, positions at the integer bound. Nothing panics and time grows
//! linearly (the fastest of three runs at 2n against n).

use std::time::{Duration, Instant};

use bettermemory_core::durability as d;
use bettermemory_core::unicode::cps;

fn fastest(f: &dyn Fn()) -> Duration {
    (0..3)
        .map(|_| {
            let t = Instant::now();
            f();
            t.elapsed()
        })
        .min()
        .unwrap()
}

fn linear(make: &dyn Fn(usize) -> Vec<u32>, run: &dyn Fn(&[u32])) {
    let n = 200_000;
    let (a, b) = (make(n), make(2 * n));
    let t1 = fastest(&|| run(&a));
    let t2 = fastest(&|| run(&b));
    assert!(
        t2 <= t1 * 4 + Duration::from_millis(20),
        "not linear: {t1:?} at n, {t2:?} at 2n"
    );
}

#[test]
fn quoted_spans_stay_linear_on_unpaired_quotes() {
    linear(&|n| cps(&"\u{201c}a\"".repeat(n)), &|t| {
        d::quoted_spans(t);
    });
    linear(
        &|n| {
            let mut v = cps("\"");
            v.extend(cps(&"a".repeat(n)));
            v
        },
        &|t| {
            assert!(d::quoted_spans(t).is_empty());
        },
    );
    linear(&|n| cps(&"\"ab\"\n".repeat(n)), &|t| {
        assert_eq!(d::quoted_spans(t).len(), t.len() / 5);
    });
}

#[test]
fn sentence_and_snippet_stay_linear() {
    linear(&|n| cps(&"9.9".repeat(n)), &|t| {
        d::sentence_around(t, t.len() / 2);
    });
    linear(&|n| cps(&" \u{3000}".repeat(n)), &|t| {
        d::snippet_around(t, 0, t.len());
        d::at_sentence_start(t, t.len());
    });
    linear(
        &|n| {
            let mut v = cps("Today ");
            v.extend(cps(&"Ab".repeat(n)));
            v
        },
        &|t| {
            d::is_titlecase_name(t, 0, 5);
            d::is_titlecase_name(t, 0, t.len());
        },
    );
}

#[test]
fn integer_bounds_do_not_panic() {
    let t = cps("Today. It \"ships\" now");
    let m = usize::MAX;
    assert_eq!(d::at_sentence_start(&t, m), None);
    assert_eq!(d::is_titlecase_name(&t, m, m), None);
    assert_eq!(d::is_titlecase_name(&t, 0, m), Some(true));
    assert_eq!(d::sentence_around(&t, m), cps(" It \"ships\" now"));
    assert_eq!(d::snippet_around(&t, m, m), cps("..."));
    assert_eq!(d::snippet_around(&t, 0, m), cps("Today. It \"ships\" now"));
    assert!(d::in_quoted_span(&[(0, m)], m, m));
    assert!(d::quoted_spans(&[]).is_empty());
    let lone = [0xD800, 0x2E, 0xDFFF];
    assert_eq!(d::sentence_around(&lone, 1), vec![0xD800]);
    assert_eq!(d::canonical_marker(&lone), lone.to_vec());
}

/// `unicode::is_space` answers ASCII without its table; the answer is
/// still `str.isspace`'s for every code point below 0x100.
#[test]
fn is_space_ascii_path_matches_isspace() {
    let python = [9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160];
    for c in 0..0x100u32 {
        let expected = python.contains(&c);
        assert_eq!(bettermemory_core::unicode::is_space(c), expected, "{c:#x}");
    }
}
