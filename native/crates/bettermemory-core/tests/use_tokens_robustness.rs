//! Hostile input to `use_tokens`: long logs, many pending tokens, long
//! id lists, deeply nested session values, huge timestamps and the float
//! extremes. No panic, and linear growth.

mod common;
mod timing;

use std::time::Duration;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::session::SessionState;
use bettermemory_core::use_tokens::{
    advance_turn, already_recorded_pending_ids, attach_use_tokens, event_ts_epoch, Row,
    UseTokenError,
};
use common::session::{cps, Fx};
use common::use_tokens::Rec;

fn fx(now: f64) -> Fx {
    Fx {
        now,
        n: 0,
        reads: 0,
        draws: 0,
    }
}

fn use_row(ts: &str, session: PyValue, ids: Vec<PyValue>) -> Row {
    vec![
        (PyValue::str("kind"), PyValue::str("use")),
        (PyValue::str("ts"), PyValue::str(ts)),
        (PyValue::str("session"), session),
        (PyValue::str("ids"), PyValue::List(ids)),
    ]
}

fn ids(n: usize) -> Vec<PyStr> {
    (0..n).map(|i| cps(&format!("m{i}"))).collect()
}

/// `n` pending tokens and a log of `n` rows each naming one of them.
fn scan(n: usize) -> Duration {
    let mut f = fx(1000.0);
    let st = SessionState::new(&mut f);
    let all = ids(n);
    st.lock().issue_use_tokens(&all, &mut f);
    let mut rec = Rec::new("S");
    rec.rows = all
        .iter()
        .map(|m| {
            use_row(
                "1970-01-01T00:16:41Z",
                PyValue::str("S"),
                vec![PyValue::Str(m.clone())],
            )
        })
        .collect();
    timing::once(|| {
        let got = already_recorded_pending_ids(&st, &mut rec, None, None).unwrap();
        assert_eq!(got.len(), n);
    })
}

#[test]
fn a_long_log_over_many_tokens_grows_linearly() {
    timing::grows_within("scan", timing::TENFOLD, &mut || scan(2_000), &mut || {
        scan(20_000)
    });
}

/// One row naming `n` ids, half of them pending, with extra pending ids.
fn long_ids(n: usize) -> Duration {
    let mut f = fx(1000.0);
    let st = SessionState::new(&mut f);
    let all = ids(n);
    st.lock().issue_use_tokens(&all[..n / 2], &mut f);
    let extra: Vec<(PyStr, f64)> = all[n / 2..].iter().map(|m| (m.clone(), 999.0)).collect();
    let mut rec = Rec::new("S");
    let list = all.iter().map(|m| PyValue::Str(m.clone())).collect();
    rec.rows = vec![use_row("1970-01-01T00:16:41Z", PyValue::str("S"), list)];
    timing::once(|| {
        let got = already_recorded_pending_ids(&st, &mut rec, None, Some(&extra)).unwrap();
        assert_eq!(got.len(), n);
    })
}

#[test]
fn a_long_id_list_with_extra_pending_grows_linearly() {
    timing::grows_within("ids", timing::TENFOLD, &mut || long_ids(2_000), &mut || {
        long_ids(20_000)
    });
}

#[test]
fn a_deeply_nested_session_value_does_not_overflow() {
    let mut f = fx(1000.0);
    let st = SessionState::new(&mut f);
    st.lock().issue_use_tokens(&[cps("a")], &mut f);
    let mut v = PyValue::List(vec![]);
    for _ in 0..200_000 {
        v = PyValue::Tuple(vec![PyValue::str("x"), v]);
    }
    let mut rec = Rec::new("S");
    rec.rows = vec![use_row("1970-01-01T00:16:41Z", v, vec![])];
    assert_eq!(
        already_recorded_pending_ids(&st, &mut rec, None, None),
        Err(UseTokenError::Unhashable("list"))
    );
    let mut v = PyValue::str("S");
    for _ in 0..200_000 {
        v = PyValue::Tuple(vec![v]);
    }
    rec.rows = vec![use_row("1970-01-01T00:16:41Z", v, vec![PyValue::str("a")])];
    assert!(already_recorded_pending_ids(&st, &mut rec, None, None)
        .unwrap()
        .is_empty());
}

#[test]
fn huge_and_odd_timestamps_answer_none_or_a_number() {
    let long = "9".repeat(1 << 20);
    for raw in [
        long.as_str(),
        "\u{0}",
        "Z",
        "ZZZZ",
        "9999-12-31T23:59:59.999999-23:59",
        "0001-01-01T00:00:00+23:59",
    ] {
        let _ = event_ts_epoch(Some(&PyValue::str(raw)));
    }
    assert_eq!(event_ts_epoch(Some(&PyValue::str(&long))), None);
}

#[test]
fn the_float_extremes_raise_or_answer() {
    for (issue, now) in [
        (f64::NEG_INFINITY, f64::INFINITY),
        (f64::NAN, f64::NAN),
        (-1e308, 1e308),
        (f64::MIN_POSITIVE, 1e300),
        (0.0, f64::MAX),
    ] {
        let mut f = fx(issue);
        let st = SessionState::new(&mut f);
        st.lock().issue_use_tokens(&ids(3), &mut f);
        st.lock().turn_counter = i64::MAX - 2;
        f.now = now;
        let mut rec = Rec::new("S");
        let _ = advance_turn(&st, &mut rec, None, &mut f);
        let _ = advance_turn(&st, &mut rec, Some(&Default::default()), &mut f);
    }
}

#[test]
fn many_hits_get_tokens() {
    let mut f = fx(1000.0);
    let st = SessionState::new(&mut f);
    let mut hits: Vec<Row> = (0..50_000)
        .map(|i| vec![(PyValue::str("id"), PyValue::str(&format!("m{}", i % 1000)))])
        .collect();
    attach_use_tokens(&mut hits, &st, &mut f).unwrap();
    assert_eq!(st.lock().pending_use_tokens.len(), 1000);
    assert!(hits.iter().all(|h| h.len() == 2));
}
