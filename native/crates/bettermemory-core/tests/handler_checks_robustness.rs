//! Hostile input to the write-payload checks: very large bodies, scope
//! lists and surrogate runs, integer bounds; no panic, and time linear in
//! the input.

mod timing;

use bettermemory_core::handler_checks::{
    utf8_len, validate_content_floor, validate_content_size, validate_scope_count,
    validate_write_payload, CheckError, WriteArgs,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn payload(content: &[u32], scopes: &[PyStr], max: i64) -> Result<(), CheckError> {
    validate_write_payload(&WriteArgs {
        content,
        scopes,
        confidence: &cps("high"),
        source: &cps("inferred"),
        allowed_scopes: scopes,
        category: &cps("fact"),
        max_content_bytes: max,
        min_content_tokens: 0,
        max_scopes_per_write: i64::MAX,
        capture: false,
    })
    .map(|_| ())
}

#[test]
fn integer_bounds_do_not_overflow() {
    assert!(validate_scope_count(usize::MAX, i64::MAX).is_err());
    assert!(validate_scope_count(usize::MAX, i64::MIN).is_ok());
    assert!(validate_content_size(&cps("abc"), i64::MAX, &cps("c")).is_ok());
    assert!(validate_content_size(&cps("abc"), i64::MIN, &cps("c")).is_ok());
    assert!(validate_content_floor(&cps("a b"), i64::MAX).is_err());
    assert!(validate_content_floor(&cps(""), i64::MIN).is_ok());
}

#[test]
fn a_long_surrogate_run_is_one_range() {
    let text: Vec<u32> = std::iter::once(0x61)
        .chain(std::iter::repeat_n(0xDC00, 1 << 20))
        .collect();
    let e = utf8_len(&text).unwrap_err();
    assert!(matches!(e, CheckError::Encode(_)));
    let m: String = e
        .message()
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect();
    assert!(m.contains("position 1-1048576:"), "{m}");
}

fn big(n: usize) -> Vec<u32> {
    (0..n)
        .map(|i| {
            if i % 7 == 0 {
                0x3000
            } else {
                0x61 + (i % 26) as u32
            }
        })
        .collect()
}

#[test]
fn a_large_payload_is_checked_in_linear_time() {
    let run = |n: usize| {
        let content = big(n);
        let scopes: Vec<PyStr> = (0..n / 100).map(|i| cps(&format!("s{i}"))).collect();
        let _ = payload(&content, &scopes[..scopes.len().min(50)], 1);
        let _ = validate_content_floor(&content, i64::MAX);
    };
    timing::grows_within(
        "handler_checks payload",
        timing::TENFOLD,
        &mut || timing::once(|| run(20_000)),
        &mut || timing::once(|| run(200_000)),
    );
}

#[test]
fn many_scopes_against_many_allowed_are_linear() {
    let run = |n: usize| {
        let scopes: Vec<PyStr> = (0..n).map(|i| cps(&format!("s{i}"))).collect();
        assert!(payload(&cps("x"), &scopes, 0).is_ok());
    };
    timing::grows_within(
        "handler_checks allowed scopes",
        timing::TENFOLD,
        &mut || timing::once(|| run(5_000)),
        &mut || timing::once(|| run(50_000)),
    );
}
