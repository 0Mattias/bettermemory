//! Hostile input for `standing` and `records::summaries`: ints of a
//! hundred thousand digits, nested values, a body of a million code
//! points, a hundred thousand ids, many scopes. No case panics, and the
//! texts and the scope filter grow linearly (`timing::TENFOLD` at ten
//! times the input).

mod timing;

use bettermemory_core::json;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::records::summaries::summaries;
use bettermemory_core::records::Memory;
use bettermemory_core::standing::*;
use bettermemory_core::unicode;

#[test]
fn huge_ints_and_nesting() {
    let big = format!("1{}", "0".repeat(100_000));
    let e = check_probability(&PyValue::Int(big.clone()), "s").unwrap_err();
    assert_eq!(e.class, PyError::OverflowError);
    let e = check_probability(&PyValue::Int(format!("-{big}")), "s").unwrap_err();
    assert_eq!(e.class, PyError::OverflowError);
    let mut v = PyValue::None;
    for _ in 0..100_000 {
        v = PyValue::List(vec![v]);
    }
    let e = check_probability(&v, "s").unwrap_err();
    assert_eq!(e.text, "s must be a number from 0 to 1, got list");
    for extreme in [i128::MIN, -1, i128::MAX] {
        let _ = backlog_line(extreme);
        let _ = batch_hint(extreme);
    }
}

#[test]
fn texts_grow_linearly() {
    let body = |n: usize| unicode::cps(&format!("  {}  ", "word ".repeat(n)));
    let (b1, b10) = (body(100_000), body(1_000_000));
    timing::grows_within(
        "record_text",
        timing::TENFOLD,
        &mut || timing::once(|| record_text(&b1).len()),
        &mut || timing::once(|| record_text(&b10).len()),
    );
    let ids =
        |n: usize| -> Vec<Vec<u32>> { (0..n).map(|i| unicode::cps(&i.to_string())).collect() };
    let (i1, i10) = (ids(10_000), ids(100_000));
    timing::grows_within(
        "held_lines",
        timing::TENFOLD,
        &mut || timing::once(|| held_lines(&i1).len() + held_line(&i1).len()),
        &mut || timing::once(|| held_lines(&i10).len() + held_line(&i10).len()),
    );
    let src = unicode::cps(&"q'\"\\".repeat(100_000));
    assert!(answer_of(&PyValue::Float(0.5), &src, None).is_err());
}

fn memory(scopes: usize) -> Memory {
    let sc: Vec<String> = (0..scopes).map(|i| format!("\"s{i}\"")).collect();
    let text = format!(
        "{{\"id\":\"01J00000000000000000000000\",\"created\":\"2026-01-01T00:00:00Z\",\
\"updated\":\"2026-01-01T00:00:00Z\",\"scopes\":[{}],\"confidence\":\"high\",\
\"source\":\"inferred\",\"body\":\"b\"}}",
        sc.join(",")
    );
    Memory::from_payload(&json::loads(&unicode::cps(&text)).unwrap()).unwrap()
}

#[test]
fn the_scope_filter_grows_linearly() {
    let ms = vec![memory(8); 40];
    let wanted =
        |n: usize| -> Vec<Vec<u32>> { (0..n).map(|i| unicode::cps(&format!("w{i}"))).collect() };
    let (w1, w10) = (wanted(2_000), wanted(20_000));
    timing::grows_within(
        "summaries",
        timing::TENFOLD,
        &mut || timing::once(|| summaries(&ms, Some(&w1)).len()),
        &mut || timing::once(|| summaries(&ms, Some(&w10)).len()),
    );
    assert!(summaries(&ms, Some(&w10)).is_empty());
}
