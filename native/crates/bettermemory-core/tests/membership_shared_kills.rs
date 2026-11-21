//! Cases a plausible wrong port of `membership_shared`'s rules fails: a
//! second's bound taken inclusive, a remainder taken as truncated where
//! the value is negative or past `i128`, the inode compared across
//! namespaces, `_in_ticks`' order of words, the carriers asked for
//! before the early returns, `_asked_where` reading `state` before
//! `kind`'s hash, a JSON RecursionError swallowed as a decode error.

mod common;
#[path = "common/membership_shared.rs"]
mod shared;

use bettermemory_core::capture::Stamp;
use bettermemory_core::membership_shared as ms;
use bettermemory_core::pyvalue::{PyError, PyValue};
use shared::*;

const S: i128 = 1_000_000_000;

fn st(name: &str, ino: &str, m: &str, c: &str, size: &str) -> Stamp {
    Stamp {
        name: cps(name),
        numbers: [ino.into(), m.into(), c.into(), size.into()],
    }
}

fn n(x: i128) -> String {
    x.to_string()
}

#[test]
fn a_second_apart_is_not_within_the_second() {
    let a = st("d", "1", &n(6 * S), &n(6 * S), "1");
    assert!(ms::same_file(
        &a,
        &st("d", "1", &n(7 * S - 1), &n(7 * S - 1), "1")
    ));
    assert!(!ms::same_file(&a, &st("d", "1", &n(7 * S), &n(7 * S), "1")));
    assert!(!ms::same_file(&a, &st("d", "1", &n(5 * S), &n(5 * S), "1")));
}

#[test]
fn coarse_needs_both_times_of_one_side() {
    let a = st("d", "1", &n(6 * S), &n(6 * S + 1), "1");
    let b = st("d", "1", &n(6 * S + 5), &n(6 * S + 5), "1");
    assert!(!ms::same_file(&a, &b));
}

#[test]
fn the_inode_is_left_out_but_name_and_size_are_not() {
    let a = st("d", "1", "5", "6", "1");
    assert!(ms::same_file(&a, &st("d", "99", "5", "6", "1")));
    assert!(!ms::same_file(&a, &st("e", "1", "5", "6", "1")));
    assert!(!ms::same_file(&a, &st("d", "1", "5", "6", "2")));
}

#[test]
fn negative_and_huge_remainders_are_floor_ones() {
    // -1 % 1e9 is 999999999 in Python: that side is not coarse, the
    // other is, and the two are a nanosecond apart.
    let a = st("d", "1", "-1", "-1", "0");
    assert!(ms::same_file(&a, &st("d", "1", "0", "0", "0")));
    assert!(!ms::same_file(&a, &st("d", "1", "-2", "-1", "0")));
    let big = format!("1{}", "0".repeat(40));
    let big_minus = format!("9{}1", "9".repeat(30));
    let x = st("d", "1", &big, &big, "0");
    assert!(ms::same_file(
        &x,
        &st("d", "1", &format!("{}999999999", &big[..32]), &big, "0")
    ));
    assert!(!ms::same_file(&x, &st("d", "1", &big_minus, &big, "0")));
    // Opposite signs past i128: never within a second.
    let neg = format!("-{big}");
    assert!(!ms::same_file(
        &st("d", "1", &neg, "0", "0"),
        &st("d", "1", &big, "0", "0")
    ));
    // i128::MIN minus i128::MAX overflows i128 and is far apart.
    let lo = i128::MIN.to_string();
    let hi = i128::MAX.to_string();
    assert!(!ms::same_file(
        &st("d", "1", &lo, "0", "0"),
        &st("d", "1", &hi, "0", "0")
    ));
}

#[test]
fn ticks_past_i128_read_off_the_digits() {
    let big = format!("1{}", "0".repeat(40));
    assert!(!ms::ticked(&[st("d", "1", &big, &big, "0")]));
    let tick = format!("{}100", &big[..big.len() - 3]);
    assert!(ms::ticked(&[st("d", "1", &tick, &big, "0")]));
    let off = format!("{}101", &big[..big.len() - 3]);
    assert!(!ms::ticked(&[st("d", "1", &off, &big, "0")]));
    assert!(ms::ticked(&[st("d", "1", &format!("-{tick}"), "0", "0")]));
    assert!(!ms::ticked(&[]));
}

#[test]
fn the_sightings_word_comes_before_the_records() {
    let fine = vec![st("d", "1", "5000000123", "6000000007", "1")];
    let k = make_checkout("m1", None, fine.clone(), Some(true), None);
    assert!(!ms::in_ticks_at(
        &k,
        &make_location(fine.clone(), Some(false))
    ));
    assert!(ms::in_ticks_at(&k, &make_location(fine.clone(), None)));
    let k = make_checkout("m1", None, fine.clone(), None, None);
    let tick = vec![st("d", "1", "5000000100", "6000000200", "1")];
    assert!(ms::in_ticks_at(&k, &make_location(tick, None)));
}

#[test]
fn no_carriers_are_read_before_the_early_returns() {
    let a = st("d", "1", "5000000123", "6000000007", "1");
    let token = cps("0123456789ABCDEFGHJKMNPQRS");
    let other = cps("ZZZZZZZZZZZZZZZZZZZZZZZZZZ");
    let k = make_checkout("m1", Some(token.clone()), vec![a.clone()], None, None);
    let mut called = false;
    let far = make_location(vec![st("e", "2", "1", "2", "3")], None);
    let r = ms::shared_remint(&k, &far, Some(&other), |_| {
        called = true;
        Vec::new()
    });
    assert_eq!(r, Ok(false));
    assert!(!called);
    let here = make_location(vec![a], None);
    let mut asked = Vec::new();
    let r = ms::shared_remint(&k, &here, Some(&other), |t| {
        asked = t.to_vec();
        Vec::new()
    });
    assert_eq!(r, Ok(false));
    assert_eq!(asked, vec![token, other]);
}

fn row(kind: PyValue, state: Option<&str>) -> PyValue {
    let mut items = vec![(PyValue::str("kind"), kind)];
    if let Some(s) = state {
        items.push((PyValue::str("state"), PyValue::str(s)));
    }
    PyValue::Dict(items)
}

#[test]
fn an_unhashable_kind_raises_before_state_is_read() {
    let r = ms::asked_where(&[row(PyValue::List(vec![]), None)]);
    assert_eq!(r, Err(ms::RowError::Py(PyError::TypeError)));
    let r = ms::asked_where(&[row(PyValue::str("first_sight"), None)]);
    assert_eq!(r, Ok(true));
    let r = ms::asked_where(&[row(PyValue::str("other"), None)]);
    assert_eq!(r, Err(ms::RowError::Key("state")));
}

#[test]
fn a_deep_payload_raises_and_a_malformed_one_is_passed_over() {
    let deep = log_row("checkout_registered", "[".repeat(200_000));
    assert_eq!(
        ms::joined_silently_by_token(&cps("k1"), &[deep]),
        Err(PyError::RecursionError)
    );
    let bad = log_row("checkout_registered", "{".into());
    let good = log_row(
        "checkout_registered",
        r#"{"checkout": "k1", "rule": "join:A+T"}"#.into(),
    );
    assert_eq!(
        ms::joined_silently_by_token(&cps("k1"), &[bad, good]),
        Ok(true)
    );
}

#[test]
fn the_last_registration_decides() {
    let t = log_row(
        "checkout_registered",
        r#"{"checkout": "k1", "rule": "join:T"}"#.into(),
    );
    let a = log_row(
        "checkout_registered",
        r#"{"checkout": "k1", "rule": "join:AT"}"#.into(),
    );
    assert_eq!(
        ms::joined_silently_by_token(&cps("k1"), &[t.clone(), a.clone()]),
        Ok(false)
    );
    assert_eq!(ms::joined_silently_by_token(&cps("k1"), &[a, t]), Ok(true));
}

fn answered(answer: PyValue) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("kind"), PyValue::str("other")),
        (PyValue::str("state"), PyValue::str("answered")),
        (PyValue::str("answer"), answer),
        (PyValue::str("answered_by"), PyValue::str("x")),
    ])
}

#[test]
fn an_int_past_4300_digits_raises_at_any_depth() {
    // str(10**4300) (4,301 digits) and str([10**5000]) raise ValueError
    // on 3.13.13; str(10**4299) (4,300 digits) does not.
    let at = PyValue::Int(format!("1{}", "0".repeat(4299)));
    let past = PyValue::Int(format!("1{}", "0".repeat(4300)));
    let far = PyValue::Int(format!("-1{}", "0".repeat(5000)));
    assert_eq!(ms::asked_where(&[answered(at.clone())]), Ok(false));
    assert!(ms::asked_where(&[answered(PyValue::List(vec![at]))]).is_ok());
    for v in [
        past.clone(),
        PyValue::List(vec![far.clone()]),
        PyValue::Tuple(vec![PyValue::str("a"), past.clone()]),
        PyValue::Dict(vec![(PyValue::str("a"), PyValue::List(vec![far.clone()]))]),
        PyValue::Dict(vec![(far, PyValue::None)]),
    ] {
        assert_eq!(
            ms::asked_where(&[answered(v)]),
            Err(ms::RowError::Py(PyError::ValueError))
        );
    }
}

#[test]
fn text_that_is_not_a_number_is_not_an_int() {
    // The reference's `_same_file` and `_ticked` answer False for a time
    // that is not an `int`; the same in debug and release.
    let good = st("d", "1", "1000000000", "1000000000", "1");
    for bad in ["//", "", "-", "1a", "01", "-0", "+1", " 1", "1.0"] {
        let x = st("d", "1", bad, "1000000000", "1");
        assert!(!ms::same_file(&x, &good), "{bad:?}");
        assert!(!ms::same_file(&good, &x), "{bad:?}");
        let y = st("d", "1", "1000000000", bad, "1");
        assert!(!ms::same_file(&y, &good), "{bad:?}");
        assert!(!ms::ticked(&[x, good.clone()]), "{bad:?}");
    }
    let wide = format!("1{}", "0".repeat(60));
    let a = st("d", "1", &format!("1//{}", "0".repeat(58)), &wide, "1");
    assert!(!ms::same_file(&a, &st("d", "1", &wide, &wide, "1")));
}

#[test]
fn read_as_now_needs_a_sighting_with_stamps() {
    // `_read_as_now` over an empty sighting: `here` is empty and falsy,
    // so the reference answers False whatever the carriers hold.
    let found = cps("0123456789ABCDEFGHJKMNPQRS");
    let a = st("d", "1", "5", "6", "1");
    let k = make_checkout("m1", None, vec![a.clone()], None, None);
    let other = make_checkout("m2", Some(found.clone()), vec![a.clone()], None, None);
    let empty = make_location(Vec::new(), None);
    assert!(!ms::read_as_now(
        &k,
        &empty,
        &found,
        std::slice::from_ref(&other)
    ));
    assert!(ms::read_as_now(
        &k,
        &make_location(vec![a], None),
        &found,
        &[other]
    ));
}
