//! Hostile input to the native membership writes, `membership_log` and
//! `reissue_membership_store_id`: rows with a surrogate in every column of
//! every table, a `_json` value nested deep with a surrogate at its
//! bottom (RecursionError past the stated budget), a hundred thousand membership rows read through the log (time
//! grows within a small multiple of the rows read), a checkout a megabyte
//! long, a hundred thousand kinds, `since_seq` at the ends of 64 bits, and
//! a reissue with texts a megabyte long. Each call answers or returns a
//! typed error; none panics, in debug or release. POSIX only, as the
//! store fixture is.

#![cfg(unix)]

mod common;
mod timing;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::create;
use bettermemory_core::store::membership as m;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::rows::SPECS;
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode;
use common::created::{fixed, fixture};
use common::opened;
use common::rows::Counting;
use common::stores::scratch;

const STAMP: &str = "2001-02-03T06:00:00.000001Z";

fn fresh(name: &str) -> (common::stores::Scratch, WritableStore) {
    let dir = scratch(name);
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open");
    (dir, store)
}

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn cps(t: &str) -> PyStr {
    unicode::cps(t)
}

fn surrogate(i: u32) -> PyValue {
    PyValue::Str(vec![0x61, 0xD800 + i, 0xDC00 + i])
}

#[test]
fn a_surrogate_in_every_column_of_every_table_answers_or_refuses() {
    let (_d, mut store) = fresh("memb-native-robust-every");
    let mut p = Counting::at(STAMP, 0);
    for spec in &SPECS {
        let row: Vec<(PyValue, PyValue)> = spec
            .columns
            .iter()
            .zip(0..)
            .map(|(c, i)| {
                let v = if c.ends_with("_json") {
                    PyValue::List(vec![surrogate(i)])
                } else {
                    surrogate(i)
                };
                (s(c), v)
            })
            .collect();
        let _ = store.put_membership(&mut p, &cps(spec.kind), &row, None);
        let _ = store.delete_membership(&mut p, &cps(spec.kind), &row, None);
        // The key alone holding none: a legacy row beside the native one.
        let mut keyed = row.clone();
        for (c, v) in &mut keyed {
            if spec.key.iter().any(|k| PyValue::str(k) == *c) {
                *v = s("k");
            }
        }
        let _ = store.put_membership(&mut p, &cps(spec.kind), &keyed, None);
        let _ = m::membership_rows(store.connection(), spec.table, None, None, &[]);
    }
    let rows = m::membership_log(store.connection(), None, 0, None).expect("log");
    assert!(rows.iter().any(|r| r.kind == "native_member_put"));
}

#[test]
fn a_deep_json_value_with_a_surrogate_at_its_bottom_is_recursion_error_past_the_budget() {
    let (_d, mut store) = fresh("memb-native-robust-deep");
    let mut p = Counting::at(STAMP, 0);
    // The reference (CPython 3.13.13, called from a module's top level)
    // writes the row 10 deep and raises RecursionError in
    // `has_surrogates` 900 and 5,000 deep, and in `_membership_values`'
    // JSON encoder 100,000 deep; nothing is written past the budget.
    for (depth, answers) in [(10, true), (900, false), (5000, false), (100_000, false)] {
        let mut deep = PyValue::List(vec![surrogate(0)]);
        for _ in 0..depth {
            deep = PyValue::List(vec![deep]);
        }
        let row = vec![
            (s("id"), s(&format!("M{depth}"))),
            (s("facts_json"), deep),
            (s("predecessor"), PyValue::None),
            (s("minted"), s("t")),
        ];
        let before = log_len(&store);
        match store.put_membership(&mut p, &cps("machine"), &row, None) {
            Ok((written, _)) => {
                assert!(answers, "{depth}: {written:?}");
                assert_eq!(written.kind, "native_member_put");
            }
            Err(e) => {
                assert!(!answers, "{depth}: {e:?}");
                assert!(
                    matches!(
                        e,
                        WriteError::Refused {
                            class: "RecursionError",
                            ..
                        }
                    ),
                    "{depth}: {e:?}"
                );
                assert_eq!(log_len(&store), before, "{depth}");
            }
        }
    }
    let rows = m::membership_rows(store.connection(), "machines", None, None, &[]).expect("rows");
    assert_eq!(rows.len(), 1);
}

fn log_len(store: &WritableStore) -> i64 {
    store
        .connection()
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count")
}

#[test]
fn a_hundred_thousand_rows_read_through_the_log_in_linear_time() {
    let (_d, store) = fresh("memb-native-robust-log");
    let conn = store.connection();
    conn.execute_batch("BEGIN").expect("begin");
    {
        let mut st = conn
            .prepare(
                "INSERT INTO log(ts, session, kind, payload, prev_mac, mac) \
                 VALUES ('t', NULL, ?, ?, 'p', 'm')",
            )
            .expect("prepare");
        for i in 0..100_000u32 {
            let kind = if i % 3 == 0 {
                "checkout_registered"
            } else {
                "machine_put"
            };
            st.execute([kind, &format!("{{\"checkout\":\"C{}\"}}", i % 10)])
                .expect("insert");
        }
    }
    conn.execute_batch("COMMIT").expect("commit");
    let last: i64 = conn
        .query_row("SELECT max(seq) FROM log", [], |r| r.get(0))
        .expect("last");
    let all = m::membership_log(conn, None, 0, None).expect("log");
    assert_eq!(all.len(), 100_000);
    assert!(all.windows(2).all(|w| w[0].seq < w[1].seq));
    let c3 = m::membership_log(conn, None, 0, Some(&s("C3"))).expect("log");
    assert_eq!(c3.len(), 10_000);
    let read = |since: i64| timing::wall(|| m::membership_log(conn, None, since, None));
    timing::grows_within(
        "membership_log",
        30,
        &mut || read(last - 10_000),
        &mut || read(0),
    );
    for since in [i64::MIN, -1, last, i64::MAX] {
        let rows = m::membership_log(conn, None, since, None).expect("edge");
        assert_eq!(rows.len(), if since < 0 { 100_000 } else { 0 });
    }
    // A hundred thousand kinds, most of them no membership kind.
    let kinds: Vec<PyValue> = (0..100_000)
        .map(|i| {
            if i == 99_999 {
                s("checkout_registered")
            } else {
                s(&format!("k{i}"))
            }
        })
        .collect();
    let rows = m::membership_log(conn, Some(&kinds), last - 30, None).expect("kinds");
    assert_eq!(rows.len(), 10);
}

#[test]
fn a_checkout_a_megabyte_long_is_found_by_sqlite() {
    let (_d, mut store) = fresh("memb-native-robust-checkout");
    let mut p = Counting::at(STAMP, 0);
    let long = "c".repeat(1 << 20);
    store
        .record_membership(
            &mut p,
            &cps("checkout_registered"),
            &[(s("checkout"), s(&long))],
            None,
        )
        .expect("record");
    let found = m::membership_log(store.connection(), None, 0, Some(&s(&long))).expect("log");
    assert_eq!(found.len(), 1);
    let other = format!("{long}d");
    let none = m::membership_log(store.connection(), None, 0, Some(&s(&other))).expect("log");
    assert!(none.is_empty());
    let mut unencodable = vec![0x63; 1 << 20];
    unencodable.push(0xDC80);
    let _ = m::membership_log(
        store.connection(),
        None,
        0,
        Some(&PyValue::Str(unencodable)),
    )
    .expect_err("a surrogate");
}

#[test]
fn a_reissue_with_long_texts_answers() {
    let (_d, mut store) = fresh("memb-native-robust-reissue");
    let mut p = Counting::at(STAMP, 0);
    let long = "r".repeat(1 << 20);
    let ((_, new), _) = store
        .reissue_membership_store_id(&mut p, &long, &long, Some(&long))
        .expect("reissue");
    let path = store.path().to_owned();
    assert_eq!(
        m::membership_store_id(store.connection(), &path).expect("id"),
        new
    );
    let rows =
        m::membership_log(store.connection(), Some(&[s("store_forked")]), 0, None).expect("log");
    assert_eq!(rows.len(), 1);
    assert!(rows[0].payload.len() > 2 << 20);
}
