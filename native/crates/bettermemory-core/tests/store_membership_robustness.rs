//! Hostile input to the membership tables' store methods: a `column_in`
//! of a hundred thousand values (time grows within a small multiple of
//! the input), a `_json` column nested deep, integer boundaries in an id
//! counter and a bound value, an enormous prefix, values of every type in
//! a read, and native rows that do not parse. Each call answers or returns
//! a typed error; none panics, in debug or release. POSIX only, as the
//! store fixture is.

#![cfg(unix)]

mod common;
mod timing;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::create;
use bettermemory_core::store::membership as m;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::{WriteError, WriteNotPorted};
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

#[test]
fn a_huge_column_in_reads_in_time_linear_in_its_size() {
    let (_d, store) = fresh("memb-robust-huge");
    let conn = store.connection();
    for i in 0..2000 {
        conn.execute(
            "INSERT INTO project_remotes(project, remote_key) VALUES ('P', ?)",
            [format!("r{i:06}")],
        )
        .expect("insert");
    }
    let values =
        |n: usize| -> Vec<PyValue> { (0..n).map(|i| s(&format!("r{:06}", i % 2000))).collect() };
    let read = |v: &[PyValue]| {
        let v = v.to_vec();
        timing::once(|| {
            m::membership_rows(
                conn,
                "project_remotes",
                None,
                Some((&cps("remote_key"), &v)),
                &[],
            )
        })
    };
    // Duplicates collapse: the read is linear in the distinct values.
    let (small, large) = (values(1000), values(10_000));
    timing::grows_within("column_in", 30, &mut || read(&small), &mut || read(&large));
    let distinct: Vec<PyValue> = (0..100_000).map(|i| s(&format!("x{i}"))).collect();
    let _ = m::membership_rows(
        conn,
        "project_remotes",
        None,
        Some((&cps("remote_key"), &distinct[..2000])),
        &[cps("-project")],
    );
}

#[test]
fn deep_json_and_odd_values_are_typed_errors_or_answers() {
    let (_d, mut store) = fresh("memb-robust-deep");
    let mut p = Counting::at(STAMP, 0);
    let mut deep = PyValue::List(vec![]);
    for _ in 0..5000 {
        deep = PyValue::List(vec![deep]);
    }
    let row = vec![
        (s("id"), s("M001")),
        (s("facts_json"), deep),
        (s("predecessor"), PyValue::None),
        (s("minted"), s("t")),
    ];
    let _ = store.put_membership(&mut p, &cps("machine"), &row, None);
    let _ = m::membership_rows(store.connection(), "machines", None, None, &[]);
    let odd = [
        PyValue::int(i128::MAX),
        PyValue::Float(f64::NAN),
        PyValue::Float(f64::INFINITY),
        PyValue::Tuple(vec![PyValue::List(vec![])]),
        PyValue::Dict(vec![]),
        PyValue::Bool(true),
        PyValue::Str(vec![0xD800]),
    ];
    for v in &odd {
        let w = vec![(cps("id"), v.clone())];
        let _ = m::membership_rows(store.connection(), "machines", Some(&w), None, &[]);
        let _ = m::membership_rows(
            store.connection(),
            "machines",
            None,
            Some((&cps("id"), std::slice::from_ref(v))),
            &[],
        );
        let r = vec![(s("project"), v.clone()), (s("remote_key"), s("k"))];
        let _ = store.put_membership(&mut p, &cps("project_remote"), &r, None);
        let _ = store.delete_membership(&mut p, &cps("project_remote"), &r, None);
    }
    let e = m::membership_rows(
        store.connection(),
        "machines",
        Some(&[(cps("id"), PyValue::int(i128::MAX))]),
        None,
        &[],
    )
    .expect_err("overflow");
    assert!(matches!(
        e,
        WriteError::Refused {
            class: "OverflowError",
            ..
        }
    ));
}

#[test]
fn counters_at_integer_bounds_and_huge_prefixes() {
    let (_d, store) = fresh("memb-robust-bounds");
    let conn = store.connection();
    conn.execute(
        "INSERT INTO machines(id, facts_json, predecessor, minted) VALUES ('M99999999999999999999999', '{}', NULL, 't')",
        [],
    )
    .expect("insert");
    assert_eq!(
        m::next_membership_id(conn, "machines", "M").expect("id"),
        format!("M{}", i128::from(i64::MAX) + 1)
    );
    // SQLite refuses a LIKE pattern past 50,000 bytes, as under the
    // reference (sqlite3.OperationalError).
    let prefix = "P".repeat(100_000);
    assert!(matches!(
        m::next_membership_id(conn, "machines", &prefix),
        Err(WriteError::Sqlite(_))
    ));
    let prefix = "P".repeat(10_000);
    assert_eq!(
        m::next_membership_id(conn, "machines", &prefix).expect("id"),
        format!("{prefix}001")
    );
    conn.execute(
        "INSERT INTO project_witnesses(project, commit_id, arrival, source, fingerprint) VALUES ('P', 'c', 9223372036854775807, 's', NULL)",
        [],
    )
    .expect("insert");
    assert_eq!(
        m::next_membership_number(conn, "project_witnesses", &cps("arrival")).expect("n"),
        i128::from(i64::MAX) + 1
    );
    for text in [
        "",
        "[",
        "{\"project\":1}",
        "null",
        "[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[",
    ] {
        conn.execute(
            "INSERT INTO native_membership_rows VALUES ('project_witness', ?, ?)",
            [format!("k{}", text.len()), text.to_owned()],
        )
        .expect("native");
    }
    let rows = m::membership_rows(conn, "project_witnesses", None, None, &[]).expect("rows");
    assert_eq!(rows.len(), 1);
    let _ = m::wrote_record(conn, &"\"".repeat(10_000));
}

#[test]
fn a_native_counter_at_i128_max_is_refused_not_wrapped() {
    let (_d, store) = fresh("memb-robust-i128");
    let conn = store.connection();
    conn.execute(
        "INSERT INTO native_membership_rows VALUES ('project_witness', '[\"P\",\"c\"]', ?)",
        [format!(
            "{{\"project\":\"P\",\"commit_id\":\"c\",\"arrival\":{},\"source\":\"s\",\"fingerprint\":null}}",
            i128::MAX
        )],
    )
    .expect("native");
    assert!(matches!(
        m::next_membership_number(conn, "project_witnesses", &cps("arrival")),
        Err(WriteError::NotPorted(WriteNotPorted::Value(_)))
    ));
}

#[test]
fn distinct_column_in_values_are_deduplicated_in_linear_time() {
    let (_d, store) = fresh("memb-robust-distinct");
    let conn = store.connection();
    let read = |n: usize| {
        let mut values: Vec<PyValue> = (0..n).map(|i| PyValue::str(&format!("x{i}"))).collect();
        values.extend((0..n).map(|i| PyValue::int(i128::try_from(i).unwrap_or(0))));
        values.extend((0..n).map(|i| PyValue::str(&format!("x{i}"))));
        let start = std::time::Instant::now();
        let rows = m::membership_rows(
            conn,
            "project_remotes",
            None,
            Some((&cps("remote_key"), &values)),
            &[],
        )
        .expect("rows");
        assert!(rows.is_empty());
        start.elapsed()
    };
    let small = read(4_000);
    let large = read(40_000);
    assert!(
        large < small * 30 + std::time::Duration::from_secs(2),
        "{small:?} then {large:?}"
    );
}
