//! Hostile input for `chain_segments` and the history-replaced leaves:
//! twenty thousand rekey rows in time linear in the rows, a rekey payload
//! nested 100,000 deep (RecursionError, as the reference's `json.loads`
//! raises, and no overflow of the stack), seqs at the ends of i64, and an
//! entry nested deep and wide.

mod timing;

use bettermemory_core::log::replaced::{
    history_replaced_kind, history_replaced_row, replaced_payload,
};
use bettermemory_core::log::segments::{chain_segments, chain_trust_with};
use bettermemory_core::log::{self, Key, KeyRing, LogRow, VerifyError, GENESIS_MAC, REKEY};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::unicode;

fn row(seq: i64, kind: &str, payload: String, key: &Key) -> LogRow {
    let ts = "2026-01-02T03:04:05.000Z";
    let mac = log::compute_mac(key.bytes(), seq, ts, None, kind, &payload, GENESIS_MAC);
    LogRow {
        seq,
        ts: ts.to_owned(),
        session: None,
        kind: kind.to_owned(),
        payload,
        prev_mac: GENESIS_MAC.to_owned(),
        mac,
    }
}

/// A first row, then `n` rekey rows each naming a fingerprint of its own
/// that no key holds.
fn rekeys(n: usize, key: &Key) -> Vec<LogRow> {
    let mut rows = vec![row(1, "store_created", "{}".to_owned(), key)];
    for i in 0..n {
        let name = format!("{i:064x}");
        let payload = format!("{{\"new_fingerprint\":\"{name}\",\"old_fingerprint\":\"{name}\"}}");
        rows.push(row(i as i64 + 2, REKEY, payload, key));
    }
    rows
}

fn ring(key: &Key) -> KeyRing {
    KeyRing {
        current: Some(key.clone()),
        ..KeyRing::default()
    }
}

#[test]
fn twenty_thousand_rekey_rows_grow_linearly() {
    let key = Key::new(vec![7; 32]);
    let r = ring(&key);
    let meta = unicode::cps(&log::fingerprint(key.bytes()));
    let small = rekeys(2_000, &key);
    let large = rekeys(20_000, &key);
    let got = chain_segments(large.as_slice(), &r, &meta, Some(&key)).unwrap();
    assert_eq!(got.len(), 20_001);
    assert!(got[1..].iter().all(|s| s.foreign && !s.held()));
    timing::grows_within(
        "chain_segments",
        timing::TENFOLD,
        &mut || timing::once(|| chain_segments(small.as_slice(), &r, &meta, Some(&key))),
        &mut || timing::once(|| chain_segments(large.as_slice(), &r, &meta, Some(&key))),
    );
}

#[test]
fn a_rekey_payload_nested_deep_is_a_recursion_error() {
    let key = Key::new(vec![7; 32]);
    let deep = format!(
        "{{\"old_fingerprint\": {}1{}}}",
        "[".repeat(100_000),
        "]".repeat(100_000)
    );
    let rows = vec![
        row(1, "store_created", "{}".to_owned(), &key),
        row(2, REKEY, deep, &key),
    ];
    let got = chain_trust_with(rows.as_slice(), &ring(&key), &[], Some(&key));
    assert!(matches!(got, Err(VerifyError::Py(PyError::RecursionError))));
}

#[test]
fn seqs_at_the_ends_of_i64() {
    let key = Key::new(vec![9; 32]);
    let fp = log::fingerprint(key.bytes());
    let rekey = format!("{{\"new_fingerprint\":\"{fp}\",\"old_fingerprint\":\"{fp}\"}}");
    let rows = vec![
        row(1, "store_created", "{}".to_owned(), &key),
        row(i64::MAX, REKEY, rekey, &key),
    ];
    let mut r = ring(&key);
    r.origin_file =
        Some(format!("{{\"seq\": {}, \"mac\": \"{}\"}}", i64::MAX, rows[1].mac).into_bytes());
    let t = chain_trust_with(rows.as_slice(), &r, &unicode::cps(&fp), Some(&key)).unwrap();
    assert_eq!(t.origin.status, "ok");
    assert_eq!(t.segments.len(), 2);
    assert_eq!(t.segments[1].first_seq, i64::MAX);
    assert!(t.segments.iter().all(|s| s.held() && !s.foreign));
}

fn nested(depth: usize, width: usize) -> PyValue {
    let mut value = PyValue::List((0..width).map(|i| PyValue::int(i as i128)).collect());
    for _ in 0..depth {
        value = PyValue::List(vec![value, PyValue::None]);
    }
    value
}

#[test]
fn an_entry_nested_deep_and_wide() {
    let deep = nested(200_000, 10_000);
    let entry = PyValue::Dict(vec![
        (PyValue::str("kind"), deep.clone()),
        (PyValue::str("accepted_history"), deep.clone()),
        (
            PyValue::str("row"),
            PyValue::Dict(vec![
                (PyValue::str("seq"), deep.clone()),
                (PyValue::str("mac"), deep.clone()),
            ]),
        ),
        (PyValue::str("seq"), PyValue::Int("9".repeat(5_000))),
        (PyValue::str("wide"), deep),
    ]);
    assert_eq!(history_replaced_kind(&entry), "store_restored");
    let row = history_replaced_row(&entry).expect("the entry's seq");
    assert_eq!(row.seq, PyValue::Int("9".repeat(5_000)));
    let payload = replaced_payload(&entry, "store_restored", "9.0.0");
    let PyValue::Dict(items) = &payload else {
        panic!("a dict")
    };
    assert_eq!(items.len(), 5);
    assert_eq!(items[0].1, PyValue::str("store_restored"));
}
