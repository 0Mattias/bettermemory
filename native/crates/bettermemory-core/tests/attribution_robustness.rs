//! Hostile input to `attribution` and `verify_status`: large texts, a
//! substring search built to be quadratic for a naive matcher, many
//! sentences and memories, the integer and calendar bounds. No case may
//! panic, in debug or release. Each time bound is three times the
//! reference's on the same input (measured on the machine the unit was
//! built on, CPython 3.13.13, in seconds), widened twentyfold for a debug
//! build.

use std::time::{Duration, Instant};

use bettermemory_core::attribution::{attribute_uses, candidate_sentences, normalize};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify_status::{
    compute_verification_status, micros_to_seconds, remote_verification_status, Status,
};

fn within<T>(reference_seconds: f64, f: impl FnOnce() -> T) -> T {
    let widen = if cfg!(debug_assertions) { 20.0 } else { 1.0 };
    let bound = Duration::from_secs_f64(3.0 * reference_seconds * widen);
    let started = Instant::now();
    let out = f();
    let took = started.elapsed();
    assert!(took <= bound, "took {took:?}, bound {bound:?}");
    out
}

fn one(body: &str, reply: &str) -> Vec<(PyStr, PyStr)> {
    let _ = reply;
    vec![(cps("m"), cps(body))]
}

#[test]
fn a_large_body_against_an_unrelated_reply() {
    let body = "alpha bravo charlie delta echo foxtrot ".repeat(200_000);
    let got = within(2.66, || {
        attribute_uses(&one(&body, ""), &cps("unrelated reply text here"))
    });
    assert!(got.is_empty());
}

#[test]
fn a_needle_built_to_make_a_naive_search_quadratic() {
    let body = format!("{}b  x y z w v", "a".repeat(400));
    let reply = "a".repeat(2_000_000);
    let got = within(0.24, || attribute_uses(&one(&body, ""), &cps(&reply)));
    assert!(got.is_empty());
}

#[test]
fn many_sentences() {
    let body: Vec<String> = (0..20_000)
        .map(|i| format!("zebra yak quokka wombat lemur gecko {i}"))
        .collect();
    let body = body.join(". ");
    let reply = "zebra yak ".repeat(1000);
    let got = within(3.6, || attribute_uses(&one(&body, ""), &cps(&reply)));
    assert!(got.is_empty());
}

#[test]
fn many_memories() {
    let mems: Vec<(PyStr, PyStr)> = (0..10_000)
        .map(|i| {
            (
                cps(&i.to_string()),
                cps("the auth middleware lives in src/auth/middleware.py today"),
            )
        })
        .collect();
    let got = within(1.16, || {
        attribute_uses(&mems, &cps(&"nothing here ".repeat(1000)))
    });
    assert!(got.is_empty());
}

#[test]
fn separators_only() {
    let body = format!("{}{}", "\n".repeat(1_000_000), ". ".repeat(500_000));
    let got = within(0.26, || attribute_uses(&one(&body, ""), &cps("x y")));
    assert!(got.is_empty());
    assert!(candidate_sentences(&[0xD800; 64]).is_empty());
    assert_eq!(
        normalize(&[0xDFFF, 0x20, 0xD800]),
        vec![0xDFFF, 0x20, 0xD800]
    );
}

#[test]
fn the_calendar_and_integer_bounds() {
    let first = DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(-86_399_999_999)).unwrap();
    let last = DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(86_399_999_999)).unwrap();
    for s in [i64::MIN, -1, 0, 1, i64::MAX] {
        for (a, b) in [(first, last), (last, first), (first, first)] {
            let got = compute_verification_status(Some(a), b, s).unwrap();
            assert!(got.stale_after_days >= 0);
            let _ = got.to_dict();
            let _ = remote_verification_status(a, s).to_dict();
        }
    }
    let got = compute_verification_status(Some(first), last, i64::MAX).unwrap();
    assert_eq!(got.status, Status::Fresh);
    let n = within(1.38, || {
        let mut n = 0;
        for _ in 0..100_000 {
            let lv = DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(0)).unwrap();
            let now = DateTime::new(9999, 12, 31, 0, 0, 0, 0, Some(0)).unwrap();
            n += compute_verification_status(Some(lv), now, 30)
                .unwrap()
                .age_days
                .unwrap();
        }
        n
    });
    assert_eq!(n, 100_000 * 3_652_058);
    for us in [i128::from(i64::MIN), -1, 1, i128::from(i64::MAX)] {
        assert!(micros_to_seconds(us).is_finite());
    }
}
