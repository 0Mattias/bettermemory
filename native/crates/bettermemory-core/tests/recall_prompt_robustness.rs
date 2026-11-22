//! Hostile input for `recall_prompt`: a long log window, events nested
//! deep, a megabyte block, many chosen and shown pointers, clocks at the
//! ends of the calendar and segment numbers past every machine integer.
//! The steps hand the log on without reading it, so its size costs a
//! copy; the delivery's drop is a set difference, timed for growth.

mod common;
mod timing;

use bettermemory_core::pyvalue::PyValue;
use common::recall_prompt::run;
use serde_json::{json, Value};

fn nested(depth: usize) -> Value {
    let mut v = json!("x");
    for _ in 0..depth {
        v = json!([v]);
    }
    v
}

#[test]
fn a_long_window_is_handed_on() {
    let recent: Vec<Value> = (0..100_000)
        .map(|i| json!({"kind": "show", "id": i}))
        .collect();
    let (a, _) = run("win", &json!({"recent": recent}));
    let PyValue::List(items) = &a else { panic!() };
    assert_eq!(items[1], PyValue::int(100_000));
}

#[test]
fn deep_events_do_not_overflow_the_stack() {
    // serde_json refuses nesting past 128 on reading; the step only
    // passes the events on, so a deep one read in is enough.
    let recent = vec![json!({"kind": "x", "v": nested(100)})];
    let (_, log) = run("probe", &json!({"recent": recent, "rep": {}}));
    assert_eq!(log.len(), 2);
}

#[test]
fn clocks_at_the_ends_of_the_calendar() {
    for now in [
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59.999999+00:00",
    ] {
        run("win", &json!({"now": now}));
        run("settle", &json!({"now": now}));
        run("probe", &json!({"now": now, "rep": {}}));
    }
}

#[test]
fn segment_numbers_past_machine_integers() {
    for n in [
        json!(i64::MAX),
        json!(u64::MAX),
        json!(-9_223_372_036_854_775_808_i64),
        json!(1.5e300),
    ] {
        let (a, _) = run("win", &json!({"seg": {"n": n}}));
        assert!(matches!(a, PyValue::List(_)));
    }
    for seg in [i64::MAX, i64::MIN] {
        run("hold", &json!({"seg": seg}));
        run("deliv", &json!({"seg": seg}));
    }
}

#[test]
fn a_megabyte_block() {
    let block = "\u{1F600}".repeat(1 << 20);
    let (a, _) = run("deliv", &json!({"block": block}));
    let PyValue::Dict(pairs) = &a else { panic!() };
    assert!(pairs.contains(&(PyValue::str("injected_chars"), PyValue::int(1 << 20))));
}

fn many(n: usize) -> Value {
    let ids: Vec<String> = (0..n).map(|i| format!("m{i:07}")).collect();
    json!({"chosen": ids, "shown": ids[..n / 2], "rc3": ids, "cand": []})
}

#[test]
fn the_drop_grows_linearly() {
    let small = many(2_000);
    let large = many(20_000);
    timing::grows_within(
        "record_delivery drop",
        timing::TENFOLD,
        &mut || timing::once(|| run("deliv", &small)),
        &mut || timing::once(|| run("deliv", &large)),
    );
}
