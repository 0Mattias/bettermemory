//! Hostile input for `history`'s third unit: values nested as deep as
//! `json.loads` reads, wide records, long bodies. No panic, and time
//! that grows with the input as the reference's does.

mod timing;

use bettermemory_core::history_rows as hr;
use bettermemory_core::json;
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn nested(depth: usize, inner: &str) -> PyValue {
    let text = format!(
        r#"{{"a":{}{}{}}}"#,
        "[".repeat(depth),
        inner,
        "]".repeat(depth)
    );
    json::loads(&cps(&text)).expect("json")
}

#[test]
fn deep_values_compare_without_overflow() {
    // The reference compares 9,900 levels without RecursionError.
    let depth = json::MAX_DEPTH - 2;
    let (a, b) = (nested(depth, ""), nested(depth, "1"));
    assert_eq!(hr::changed(&a, &b), vec![cps("a")]);
    assert!(hr::changed(&a, &a.clone()).is_empty());
    let deep = format!(r#"{{"status":{}{}}}"#, "[".repeat(depth), "]".repeat(depth));
    assert_eq!(
        hr::event_detail(&json::loads(&cps(&deep)).unwrap()).len(),
        1
    );
}

fn wide(n: usize, last: u32) -> PyValue {
    let mut items: Vec<(PyValue, PyValue)> = (0..n)
        .map(|i| {
            (
                PyValue::str(&format!("k{i}")),
                PyValue::Dict(vec![(
                    PyValue::str(&format!("x{i}")),
                    PyValue::int(i as i128),
                )]),
            )
        })
        .collect();
    items.push((PyValue::str("z"), PyValue::int(i128::from(last))));
    PyValue::Dict(items)
}

#[test]
fn wide_records_compare_in_linear_time() {
    let (a, b) = (wide(20_000, 1), wide(20_000, 2));
    assert_eq!(hr::changed(&a, &b), vec![cps("z")]);
    let (sa, sb) = (wide(4_000, 1), wide(4_000, 2));
    let (la, lb) = (wide(40_000, 1), wide(40_000, 2));
    timing::grows_within(
        "changed over wide records",
        30,
        &mut || timing::once(|| hr::changed(&sa, &sb)),
        &mut || timing::once(|| hr::changed(&la, &lb)),
    );
}

#[test]
fn large_integers_and_long_bodies() {
    let big = "9".repeat(4_300);
    let a = json::loads(&cps(&format!(r#"{{"a":{big},"b":1e308}}"#))).unwrap();
    let b = json::loads(&cps(&format!(r#"{{"a":{big}.0,"b":1e308}}"#))).unwrap();
    assert_eq!(hr::changed(&a, &b), vec![cps("a")]);
    let body = "line\n".repeat(5_000);
    let p = json::loads(&cps(&format!(
        r#"{{"memory":{{"body":"{}"}}}}"#,
        body.replace('\n', "\\n")
    )))
    .unwrap();
    let row = LogRow {
        seq: i64::MAX,
        ts: String::new(),
        session: None,
        kind: "memory_put".into(),
        payload: String::new(),
        prev_mac: String::new(),
        mac: String::new(),
    };
    let mut walk = hr::RecordWalk::default();
    let r = hr::record_row(&mut walk, &row, &p, None, None, None);
    assert_eq!(r.action, "created");
    assert_eq!(walk.previous_seq, Some(i64::MAX));
    assert_eq!(
        r.to_dict(true).get("seq"),
        Some(&PyValue::int(i128::from(i64::MAX)))
    );
}
