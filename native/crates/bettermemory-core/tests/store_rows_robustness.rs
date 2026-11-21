//! Hostile input for `store::rows`: large and pathological sorts, integer
//! and float boundaries, payloads nested far past the recursion budget
//! and very large ones. No panic, and the sort grows as n log n.

mod timing;

use bettermemory_core::records::rows::Row;
use bettermemory_core::records::RecordError;
use bettermemory_core::store::rows::{event_from_columns, sort_rows, Fail};
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;

fn rows(n: usize, value: impl Fn(usize) -> SqlValue) -> Vec<Row> {
    (0..n)
        .map(|i| Row {
            columns: vec![
                ("a".into(), value(i)),
                ("n".into(), SqlValue::Integer(i as i64)),
            ],
        })
        .collect()
}

fn is_sorted_stably(rows: &[Row], descending: bool) -> bool {
    rows.windows(2).all(|w| {
        let (SqlValue::Integer(a), SqlValue::Integer(b)) =
            (w[0].get("a").unwrap(), w[1].get("a").unwrap())
        else {
            return true;
        };
        let (SqlValue::Integer(i), SqlValue::Integer(j)) =
            (w[0].get("n").unwrap(), w[1].get("n").unwrap())
        else {
            panic!()
        };
        if a == b {
            i < j
        } else {
            (a < b) != descending
        }
    })
}

#[test]
fn large_sorts_are_stable_both_ways() {
    for n in [63, 64, 65, 127, 128, 1000, 5003] {
        for by in ["a", "-a"] {
            let mut r = rows(n, |i| SqlValue::Integer(((i * 7919) % 13) as i64));
            sort_rows(&mut r, &[by]).unwrap();
            assert_eq!(r.len(), n);
            assert!(is_sorted_stably(&r, by == "-a"), "{n} {by}");
        }
    }
}

#[test]
fn boundaries_do_not_panic() {
    let values = [
        SqlValue::Integer(i64::MIN),
        SqlValue::Integer(i64::MAX),
        SqlValue::Real(f64::INFINITY),
        SqlValue::Real(f64::NEG_INFINITY),
        SqlValue::Real(f64::NAN),
        SqlValue::Real(-0.0),
        SqlValue::Real(2f64.powi(63)),
        SqlValue::Real(-(2f64.powi(63))),
        SqlValue::Null,
    ];
    for n in [9, 100] {
        let mut r = rows(n, |i| values[i % values.len()].clone());
        sort_rows(&mut r, &["a", "-a"]).unwrap();
        assert_eq!(r.len(), n);
    }
    let mut mixed = rows(500, |i| {
        if i == 400 {
            SqlValue::Text("x".into())
        } else {
            SqlValue::Integer(i as i64)
        }
    });
    assert!(matches!(
        sort_rows(&mut mixed, &["a"]),
        Err(Fail::Caught("TypeError", _))
    ));
    assert_eq!(mixed.len(), 500);
}

#[test]
fn sort_grows_as_n_log_n() {
    let run = |n: usize| {
        let mut r = rows(n, |i| {
            SqlValue::Integer(((i * 2_654_435_761) % 1_000_003) as i64)
        });
        sort_rows(&mut r, &["-a"]).unwrap();
        r.len()
    };
    timing::grows_within(
        "sort: 2,000 and 20,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| run(2_000)),
        &mut || timing::once(|| run(20_000)),
    );
}

/// Many ties and many runs: each run is merged in place, never cut off
/// with a copy of the whole tail.
#[test]
fn sort_with_ties_grows_as_n_log_n() {
    let run = |n: usize| {
        let mut r = rows(n, |i| SqlValue::Integer(((i * 7919) % 1000) as i64));
        sort_rows(&mut r, &["-a", "n"]).unwrap();
        r.len()
    };
    timing::grows_within(
        "sort with ties: 20,000 and 200,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| run(20_000)),
        &mut || timing::once(|| run(200_000)),
    );
}

#[test]
fn deep_and_large_payloads() {
    let kind = unicode::cps("recall");
    let deep = format!("{}{}", "[".repeat(1_000_000), "]".repeat(1_000_000));
    assert_eq!(
        event_from_columns(&[], None, &kind, &SqlValue::Text(deep)),
        Err(RecordError::Raised("RecursionError"))
    );
    let unclosed = "{\"a\":".repeat(200_000);
    assert_eq!(
        event_from_columns(&[], None, &kind, &SqlValue::Text(unclosed)),
        Err(RecordError::Raised("RecursionError"))
    );
    let wide = format!("{{\"s\": \"{}\"}}", "x".repeat(4_000_000));
    assert!(matches!(
        event_from_columns(&[], None, &kind, &SqlValue::Text(wide)),
        Ok(Some(_))
    ));
    let blob = vec![0xffu8; 1_000_000];
    assert_eq!(
        event_from_columns(&[], None, &kind, &SqlValue::Blob(blob)),
        Ok(None)
    );
}
