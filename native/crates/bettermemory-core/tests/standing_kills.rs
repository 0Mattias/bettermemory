//! Cases a plausible wrong `standing` or `records::summaries` fails: a
//! bool taken as a number, an overflow bound off by one, -0.0 or nan
//! misread, the checks of `answer_of` in another order, an empty client
//! taken as declared, the ambient test folded to lower case, the windows
//! and counts of the line builders, and the empty scope filter.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::identity::Actor;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::records::summaries::{summaries, MemoryHit};
use bettermemory_core::records::{Confidence, Memory};
use bettermemory_core::standing::*;
use bettermemory_core::{json, unicode};

fn cps(s: &str) -> Vec<u32> {
    unicode::cps(s)
}

fn int(text: &str) -> PyValue {
    PyValue::Int(text.to_owned())
}

/// 2**1024 - 2**970 and its neighbour below, by `int` arithmetic in
/// decimal text (computed once here from the double's halfway point).
fn bound() -> (String, String) {
    // f64::MAX + half an ulp: (2**53 - 1/2) * 2**971 = (2**54 - 1) * 2**970.
    let mut digits = vec![1u32];
    let mul = |d: &mut Vec<u32>, m: u32| {
        let mut carry = 0u64;
        for x in d.iter_mut() {
            let v = u64::from(*x) * u64::from(m) + carry;
            *x = (v % 10) as u32;
            carry = v / 10;
        }
        while carry > 0 {
            d.push((carry % 10) as u32);
            carry /= 10;
        }
    };
    for _ in 0..970 {
        mul(&mut digits, 2);
    }
    // times (2**54 - 1): times 2**54 minus itself.
    let base = digits.clone();
    for _ in 0..54 {
        mul(&mut digits, 2);
    }
    let mut borrow = 0i64;
    for (i, x) in digits.iter_mut().enumerate() {
        let sub = i64::from(base.get(i).copied().unwrap_or(0)) + borrow;
        let mut v = i64::from(*x) - sub;
        borrow = 0;
        if v < 0 {
            v += 10;
            borrow = 1;
        }
        *x = v as u32;
    }
    while digits.last() == Some(&0) {
        digits.pop();
    }
    let text: String = digits
        .iter()
        .rev()
        .map(|d| char::from(b'0' + *d as u8))
        .collect();
    let mut below = digits.clone();
    let mut i = 0;
    while below[i] == 0 {
        below[i] = 9;
        i += 1;
    }
    below[i] -= 1;
    let below: String = below
        .iter()
        .rev()
        .map(|d| char::from(b'0' + *d as u8))
        .collect();
    (text, below)
}

#[test]
fn a_bool_is_not_a_number() {
    let e = check_probability(&PyValue::Bool(true), "standing").unwrap_err();
    assert_eq!(e.class, PyError::ValueError);
    assert_eq!(e.text, "standing must be a number from 0 to 1, got bool");
}

#[test]
fn the_overflow_bound_rounds_half_to_even() {
    let (at, below) = bound();
    let e = check_probability(&int(&at), "standing").unwrap_err();
    assert_eq!(e.class, PyError::OverflowError);
    assert_eq!(e.text, "int too large to convert to float");
    let e = check_probability(&int(&below), "standing").unwrap_err();
    assert_eq!(e.class, PyError::ValueError);
    assert_eq!(e.text, format!("standing must be from 0 to 1, got {below}"));
    let e = check_probability(&int(&format!("-{at}")), "p").unwrap_err();
    assert_eq!(e.class, PyError::OverflowError);
}

#[test]
fn negative_zero_passes_and_nan_fails() {
    let z = check_probability(&PyValue::Float(-0.0), "standing").unwrap();
    assert_eq!(z.to_bits(), (-0.0f64).to_bits());
    let e = check_probability(&PyValue::Float(f64::NAN), "standing").unwrap_err();
    assert_eq!(e.text, "standing must be from 0 to 1, got nan");
    let e = check_probability(&PyValue::Float(1.0000000000000002), "standing").unwrap_err();
    assert_eq!(
        e.text,
        "standing must be from 0 to 1, got 1.0000000000000002"
    );
    assert_eq!(check_probability(&int("1"), "standing").unwrap(), 1.0);
}

#[test]
fn the_source_is_checked_before_the_probability() {
    let e = answer_of(&PyValue::Bool(true), &cps("Review"), None).unwrap_err();
    assert_eq!(e.text, "unknown standing source 'Review'");
    let e = answer_of(&PyValue::Bool(true), &cps("x'y"), None).unwrap_err();
    assert_eq!(e.text, "unknown standing source \"x'y\"");
    let ok = answer_of(&int("1"), &cps("asked"), None).unwrap();
    assert_eq!(ok.probability, PyValue::Float(1.0));
    assert_eq!(ok.answered_by, PyValue::Str(cps("none/none")));
}

#[test]
fn an_empty_client_is_none() {
    let a = Actor {
        client: Some(Vec::new()),
        model: Some(cps("m")),
        ..Actor::default()
    };
    assert_eq!(answered_by(Some(&a)), cps("none/m"));
}

#[test]
fn ambient_is_matched_exactly() {
    assert!(is_standing(Some(&cps("ambient")), None));
    assert!(!is_standing(Some(&cps("Ambient")), None));
    assert!(is_standing(None, Some(0.3)));
    assert!(!is_standing(None, Some(0.29999999999999993)));
    assert!(!is_standing(Some(&cps("fact")), Some(f64::NAN)));
}

#[test]
fn record_text_keeps_600_and_windows_601() {
    let six = "x".repeat(600);
    assert_eq!(record_text(&cps(&format!("  {six}\n"))), cps(&six));
    let more = record_text(&cps(&"x".repeat(601)));
    assert_ne!(more, cps(&"x".repeat(601)));
    assert!(more.len() <= 600);
}

#[test]
fn line_builders_count_and_cap() {
    assert_eq!(backlog_line(0), "");
    assert!(backlog_line(1).contains("1 memory has"));
    assert!(backlog_line(2).contains("shows 11 at a time"));
    let ids: Vec<Vec<u32>> = (0..12).map(|i| cps(&format!("id{i}"))).collect();
    let line = unicode::to_string(&held_line(&ids)).unwrap();
    assert!(line.ends_with("id10 (1 more)."), "{line}");
    assert!(!line.contains("id11"));
    let one = unicode::to_string(&held_line(&ids[..1])).unwrap();
    assert!(one.contains("1 standing memory is held"));
    assert_eq!(held_lines(&ids).len(), 13);
    assert!(held_lines(&[]).is_empty());
    assert!(batch_hint(1).contains("memory_admin(action=\"answer_standing\", standing={"));
    assert_eq!(batch_hint(0), "No memory here waits for a standing answer.");
}

#[test]
fn question_keys_in_order() {
    let q = question(&cps("I"));
    let PyValue::Dict(items) = &q else { panic!() };
    let keys: Vec<_> = items.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(
        keys,
        vec![
            PyValue::str("type"),
            PyValue::str("instructions"),
            PyValue::str("hint")
        ]
    );
}

fn memory(scopes: &[&str]) -> Memory {
    let sc: Vec<String> = scopes.iter().map(|s| format!("\"{s}\"")).collect();
    let text = format!(
        "{{\"id\":\"01J00000000000000000000000\",\"created\":\"2026-01-01T00:00:00Z\",\
\"updated\":\"2026-01-01T00:00:00Z\",\"scopes\":[{}],\"confidence\":\"high\",\
\"source\":\"inferred\",\"body\":\"B. c\"}}",
        sc.join(",")
    );
    Memory::from_payload(&json::loads(&cps(&text)).unwrap()).unwrap()
}

#[test]
fn an_empty_filter_keeps_all() {
    let ms = vec![memory(&["a"]), memory(&["b"])];
    assert_eq!(summaries(&ms, Some(&[])).len(), 2);
    assert_eq!(summaries(&ms, None).len(), 2);
    assert_eq!(summaries(&ms, Some(&[cps("b")])).len(), 1);
    assert_eq!(summaries(&ms, Some(&[cps("A")])).len(), 0);
    assert_eq!(summaries(&ms, None)[0].summary, cps("B"));
}

#[test]
fn hit_defaults() {
    let d = DateTime::from_isoformat(&cps("2026-01-01T00:00:00+00:00")).unwrap();
    let h = MemoryHit::new(cps("i"), vec![], Confidence::Low, cps("s"), 1.5, d, d);
    assert_eq!(h.relevance, cps("medium"));
    assert_eq!(h.query_unique, 0);
    assert!(h.category.is_none() && h.last_verified_at.is_none());
}
