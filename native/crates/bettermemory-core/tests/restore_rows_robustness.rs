//! Hostile and racing inputs to `store::restore_rows`: two settles at
//! once append one row and fill the record once; a record of 400
//! restores at the 1 MiB cap is read in time linear in its entries; a
//! record or a payload nested 100,000 deep raises RecursionError without
//! a panic, a value nested 9,000 deep is compared without one, one 400
//! deep is written and said, and two 600 deep make a record past the cap
//! that later reads decline; seqs at and past SQLite's INTEGER bounds; a
//! record at 0644 rewritten owner-only. After every path the connection
//! is out of any transaction.

#![cfg(unix)]

mod common;
mod restore_rows;
mod timing;

use std::thread;

use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::restore_rows::{
    append_for, append_missing, settle, RestoreError, Warning, RECORD_CAP_BYTES,
};
use bettermemory_core::{json, unicode};
use common::opened;
use restore_rows::{
    clock, entry, mac1, open_case, put_record, record_entries, record_of, rows_after,
};
use serde_json::{json, Value};

fn autocommit(opened: &restore_rows::Opened) {
    assert!(
        opened.store.connection().is_autocommit(),
        "the connection is left in a transaction"
    );
}

#[test]
fn two_settles_at_once_append_one_row_and_fill_the_record_once() {
    for round in 0..5 {
        let mut opened = open_case("race", &[], &Value::Null, true);
        // Opened before the record is there: the writing open refuses a
        // pending one.
        let mut other =
            open_for_writing(&opened.file, &opened.keys, &opened::engine_provider()).expect("open");
        let mac = mac1(&opened);
        put_record(&opened.record, &record_of(&[entry(&mac, json!({}), &[])]));
        let (a, b) = thread::scope(|s| {
            let a = s.spawn(|| {
                let mut log = Vec::new();
                settle(&mut opened.store, &mut clock(), &mut log).map(|()| log)
            });
            let b = s.spawn(|| {
                let mut log = Vec::new();
                settle(&mut other, &mut clock(), &mut log).map(|()| log)
            });
            (a.join().expect("a"), b.join().expect("b"))
        });
        let (a, b) = (a.expect("settle a"), b.expect("settle b"));
        assert_eq!(
            rows_after(&opened.file, 1).len(),
            1,
            "round {round}: {a:?} {b:?}"
        );
        let entries = record_entries(&opened.record);
        assert_eq!(entries[0]["row"]["seq"], 2, "round {round}");
        assert!(a
            .iter()
            .chain(&b)
            .all(|w| matches!(w, Warning::Named { seq: 2, .. })));
        assert!(other.connection().is_autocommit());
        autocommit(&opened);
        other.close().expect("close");
    }
}

/// A record of `n` restores whose rows are withheld for good (no row
/// decided it), each padded to `size` bytes.
fn kept_record(mac: &str, n: usize, pad: usize) -> Value {
    let entries: Vec<Value> = (0..n)
        .map(|i| {
            entry(
                mac,
                json!({
                    "restored_at": format!("2002-01-01T00:00:{:02}.{i:06}Z", i % 60),
                    "row_withheld": {"seq": null, "kind": null, "mac": null, "because": "elsewhere"},
                    "pad": "x".repeat(pad),
                }),
                &[],
            )
        })
        .collect();
    record_of(&entries)
}

#[test]
fn a_record_of_400_restores_at_the_cap_reads_in_linear_time() {
    let mut small = open_case("cap-small", &[], &Value::Null, true);
    let mut large = open_case("cap-large", &[], &Value::Null, true);
    let mac = mac1(&small);
    let pad = 2_150;
    let large_record = kept_record(&mac, 400, pad);
    let size = large_record["text"].as_str().expect("text").len() as u64;
    assert!(
        size <= RECORD_CAP_BYTES && size > RECORD_CAP_BYTES * 9 / 10,
        "{size}"
    );
    put_record(&small.record, &kept_record(&mac, 40, pad));
    put_record(&large.record, &large_record);
    let run = |o: &mut restore_rows::Opened| {
        timing::once(|| {
            let mut log = Vec::new();
            settle(&mut o.store, &mut clock(), &mut log).expect("settle");
            assert!(log.is_empty());
        })
    };
    timing::grows_within(
        "settle: 40 and 400 kept restores",
        timing::TENFOLD,
        &mut || run(&mut small),
        &mut || run(&mut large),
    );
    assert!(rows_after(&large.file, 1).is_empty());
    autocommit(&large);
}

fn nested(depth: usize) -> String {
    format!("{}{}", "[".repeat(depth), "]".repeat(depth))
}

#[test]
fn a_record_nested_100000_deep_raises_without_a_panic() {
    let record = json!({"text": format!("{{\"restores\": {}}}", nested(100_000))});
    let mut opened = open_case("nested-record", &[], &record, true);
    let mut log = Vec::new();
    let got = append_missing(&mut opened.store, &mut clock(), None, &mut log);
    assert!(
        matches!(got, Err(RestoreError::Py(PyError::RecursionError))),
        "{got:?}"
    );
    settle(&mut opened.store, &mut clock(), &mut log).expect("settle logs it");
    match log.as_slice() {
        [Warning::NotAppended(cause)] => assert_eq!(cause.class, "RecursionError"),
        other => panic!("{other:?}"),
    }
    autocommit(&opened);
}

#[test]
fn a_payload_nested_100000_deep_raises_without_a_panic() {
    let rows = [
        json!({"seq": 2, "ts": restore_rows::LATE, "kind": "store_restored",
                       "payload": nested(100_000), "mac": "m2"}),
    ];
    let mut opened = open_case("nested-payload", &rows, &Value::Null, true);
    let mac = mac1(&opened);
    put_record(&opened.record, &record_of(&[entry(&mac, json!({}), &[])]));
    let mut log = Vec::new();
    let got = append_missing(&mut opened.store, &mut clock(), None, &mut log);
    assert!(
        matches!(got, Err(RestoreError::Py(PyError::RecursionError))),
        "{got:?}"
    );
    assert!(rows_after(&opened.file, 2).is_empty());
    autocommit(&opened);
}

/// `entries` as a record's text, each `"@DEEP<n>@"` in them spelled as
/// lists `depth` deep with `n` at the bottom (`n` 0: none). Built as
/// text: serde_json stops at 128 levels; the crate's JSON does not.
fn spelled_deep(v: &Value, depth: usize) -> String {
    let mut text = v.to_string();
    for n in 0..3 {
        let bottom = if n == 0 { String::new() } else { n.to_string() };
        let deep = format!("{}{bottom}{}", "[".repeat(depth), "]".repeat(depth));
        text = text.replace(&format!("\"@DEEP{n}@\""), &deep);
    }
    text
}

#[test]
fn a_value_nested_9000_deep_is_compared_without_a_panic() {
    // Two entries 9,000 lists deep that differ only at the bottom, both
    // kept by a withholding that stands, and the restore's own equal to
    // the second: found pending by a comparison that walks the whole
    // depth, kept, nothing written (a record rewritten with `indent=2`
    // at this depth is past the cap, as the reference's is).
    let mut opened = open_case("deep-compare", &[], &Value::Null, true);
    let mac = mac1(&opened);
    let kept = json!({"seq": null, "because": "elsewhere"});
    let first = entry(
        &mac,
        json!({"restored_at": "@DEEP0@", "row_withheld": kept}),
        &[],
    );
    let second = entry(
        &mac,
        json!({"restored_at": "@DEEP1@", "row_withheld": kept}),
        &[],
    );
    let record = json!({"restores": [first, second.clone()]});
    put_record(
        &opened.record,
        &json!({ "text": spelled_deep(&record, 9_000) }),
    );
    let before = std::fs::read(&opened.record).expect("record");
    let own = json::loads(&unicode::cps(&spelled_deep(&second, 9_000))).expect("JSON");
    let mut log = Vec::new();
    let row = append_for(&mut opened.store, &mut clock(), &own, &mut log).expect("append_for");
    assert!(row.is_none() && log.is_empty());
    assert!(rows_after(&opened.file, 1).is_empty());
    assert_eq!(std::fs::read(&opened.record).expect("record"), before);
    autocommit(&opened);
}

#[test]
fn a_value_nested_400_deep_is_written_and_said() {
    // Two values 400 deep come to about 640 KB written with `indent=2`,
    // under the cap (600 deep: the test after this one).
    let mut opened = open_case("deep-value", &[], &Value::Null, true);
    let mac = mac1(&opened);
    let withheld = entry(&mac, json!({"restored_at": "@DEEP0@"}), &["head_after"]);
    let appended = entry(
        &mac,
        json!({"restored_at": "@DEEP1@", "bundle": "/b/2"}),
        &[],
    );
    let record = json!({"restores": [withheld, appended.clone()]});
    put_record(
        &opened.record,
        &json!({ "text": spelled_deep(&record, 400) }),
    );
    let own = json::loads(&unicode::cps(&spelled_deep(&appended, 400))).expect("JSON");
    let mut log = Vec::new();
    let row = append_for(&mut opened.store, &mut clock(), &own, &mut log).expect("append_for");
    assert_eq!(row.map(|r| r.seq), Some(2));
    assert!(
        matches!(log.as_slice(), [Warning::Withheld { .. }]),
        "a withheld warning"
    );
    let doc = json::loads_bytes(&std::fs::read(&opened.record).expect("record")).expect("JSON");
    let Some(PyValue::List(entries)) = doc.get("restores") else {
        panic!("no restores");
    };
    let field = |e: &PyValue, a: &str, b: &str| e.get(a).and_then(|v| v.get(b)).cloned();
    assert_eq!(
        field(&entries[0], "row_withheld", "because"),
        Some(PyValue::str("elsewhere"))
    );
    assert_eq!(field(&entries[1], "row", "seq"), Some(PyValue::int(2)));
    autocommit(&opened);
}

#[test]
fn seqs_at_and_past_sqlites_integer_bounds() {
    for (seq, overflows) in [
        (json!(i64::MAX), false),
        (json!(i64::MIN), false),
        (json!(9_223_372_036_854_775_808u64), true),
    ] {
        let mut opened = open_case("bounds", &[], &Value::Null, true);
        let e = entry("x", json!({"head_after": {"seq": seq, "mac": "x"}}), &[]);
        put_record(&opened.record, &record_of(&[e]));
        let mut log = Vec::new();
        let got = settle(&mut opened.store, &mut clock(), &mut log);
        if overflows {
            assert!(
                matches!(got, Err(RestoreError::Py(PyError::OverflowError))),
                "{got:?}"
            );
            assert!(log.is_empty());
        } else {
            got.expect("settle");
            assert!(
                matches!(log.as_slice(), [Warning::Withheld { .. }]),
                "{log:?}"
            );
        }
        autocommit(&opened);
    }
}

#[test]
fn a_record_at_0644_is_owner_only_once_its_row_is_named() {
    // `_write_private` is `atomic_write_bytes(mode_before_rename=0o600)`:
    // on macOS and Linux the temporary file is made 0600 and renamed over
    // the record, so a record put at 0644 is 0600 once rewritten. (On
    // Windows the reference's `fchmod` is skipped and its `chmod` only
    // clears a read-only flag; this file does not run there.)
    use std::os::unix::fs::PermissionsExt;
    let mut opened = open_case("record-mode", &[], &Value::Null, true);
    let mac = mac1(&opened);
    put_record(&opened.record, &record_of(&[entry(&mac, json!({}), &[])]));
    std::fs::set_permissions(&opened.record, std::fs::Permissions::from_mode(0o644))
        .expect("chmod");
    let mut log = Vec::new();
    settle(&mut opened.store, &mut clock(), &mut log).expect("settle");
    assert!(
        matches!(log.as_slice(), [Warning::Named { seq: 2, .. }]),
        "{log:?}"
    );
    assert_eq!(record_entries(&opened.record)[0]["row"]["seq"], 2);
    let mode = std::fs::metadata(&opened.record)
        .expect("record")
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600, "{mode:o}");
    autocommit(&opened);
}

#[test]
fn values_600_deep_make_a_record_past_the_cap_that_later_reads_decline() {
    // As `a_value_nested_400_deep_is_written_and_said`, 600 deep: the
    // withheld entry's `row_withheld` is written with `indent=2` as
    // 1,457,513 bytes (the reference's size), past the cap, so the
    // second entry's row is appended (seq 2, returned) but `record_row`
    // reads no record and names it nowhere; every later read logs
    // ValueError. The reference answers each step alike.
    let mut opened = open_case("deep-600", &[], &Value::Null, true);
    let mac = mac1(&opened);
    let withheld = entry(&mac, json!({"restored_at": "@DEEP0@"}), &["head_after"]);
    let appended = entry(
        &mac,
        json!({"restored_at": "@DEEP1@", "bundle": "/b/2"}),
        &[],
    );
    let record = json!({"restores": [withheld, appended.clone()]});
    put_record(
        &opened.record,
        &json!({ "text": spelled_deep(&record, 600) }),
    );
    let own = json::loads(&unicode::cps(&spelled_deep(&appended, 600))).expect("JSON");
    let mut log = Vec::new();
    let row = append_for(&mut opened.store, &mut clock(), &own, &mut log).expect("append_for");
    assert_eq!(row.map(|r| r.seq), Some(2));
    assert!(
        matches!(log.as_slice(), [Warning::Withheld { .. }]),
        "{log:?}"
    );
    let size = std::fs::metadata(&opened.record).expect("record").len();
    assert_eq!(size, 1_457_513);
    assert!(size > RECORD_CAP_BYTES);
    let text = std::fs::read(&opened.record).expect("record");
    for later in 0..2 {
        let mut log = Vec::new();
        if later == 0 {
            settle(&mut opened.store, &mut clock(), &mut log).expect("settle");
        } else {
            let settled =
                append_missing(&mut opened.store, &mut clock(), None, &mut log).expect("append");
            assert!(settled.is_empty());
        }
        match log.as_slice() {
            [Warning::RecordUnread { cause, .. }] => assert_eq!(cause.class, "ValueError"),
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(rows_after(&opened.file, 1).len(), 1);
    assert_eq!(std::fs::read(&opened.record).expect("record"), text);
    autocommit(&opened);
}
