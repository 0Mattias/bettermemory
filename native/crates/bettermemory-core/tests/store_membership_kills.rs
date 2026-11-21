//! Cases a plausible wrong port of the membership tables' store methods
//! fails: `committed_seq` read off the last row instead of the first, a
//! batched read left unsorted, a native twin listed beside its legacy row
//! or its delete written before the put, `column_in`'s duplicates bound
//! twice, an id counter that truncates past three digits or counts a
//! prefix by bytes, a control kind check that lets a mutation kind
//! through, `wrote_record` matching an id that only begins the one asked
//! for, and a surrogate row written instead of refused. POSIX only, as
//! the store fixture is.

#![cfg(unix)]

mod common;

use std::path::Path;

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
use common::written;

const STAMP: &str = "2001-02-03T06:00:00.000001Z";

fn fresh(name: &str) -> (common::stores::Scratch, WritableStore) {
    let dir = scratch(name);
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let store = open(&c);
    (dir, store)
}

fn open(c: &Path) -> WritableStore {
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open")
}

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn remote(project: &str, key: &str) -> Vec<(PyValue, PyValue)> {
    vec![(s("project"), s(project)), (s("remote_key"), s(key))]
}

fn cps(t: &str) -> PyStr {
    unicode::cps(t)
}

fn kinds(store: &WritableStore) -> Vec<String> {
    let conn = store.connection();
    let mut st = conn
        .prepare("SELECT kind FROM log ORDER BY seq")
        .expect("log");
    let kinds = st.query_map([], |r| r.get(0)).expect("rows");
    kinds.collect::<rusqlite::Result<_>>().expect("kinds")
}

#[test]
fn committed_seq_is_the_row_before_the_first_written() {
    let (_d, mut store) = fresh("memb-kill-seq");
    let mut p = Counting::at(STAMP, 0);
    let (seqs, _) = store
        .batch(&mut p, None, |tx| {
            let before = tx.committed_seq();
            let first = tx.put_membership(&cps("project_remote"), &remote("P1", "a"), None)?;
            tx.put_membership(&cps("project_remote"), &remote("P1", "b"), None)?;
            let after = tx.committed_seq();
            assert!(tx.in_batch());
            Ok::<_, WriteError>((before, first.seq, after))
        })
        .expect("batch");
    assert_eq!(seqs.0, None);
    assert_eq!(seqs.2, Some(seqs.1 - 1));
    assert_eq!(store.committed_seq(), None);
    assert!(!store.in_batch());
}

#[test]
fn a_read_past_one_batch_is_sorted_as_a_whole() {
    let (_d, store) = fresh("memb-kill-batch");
    let conn = store.connection();
    for i in 0..700 {
        conn.execute(
            "INSERT INTO project_remotes(project, remote_key) VALUES (?, ?)",
            [format!("P{}", i % 3), format!("r{i:04}")],
        )
        .expect("insert");
    }
    let values: Vec<PyValue> = (0..700).rev().map(|i| s(&format!("r{i:04}"))).collect();
    let rows = m::membership_rows(
        conn,
        "project_remotes",
        None,
        Some((&cps("remote_key"), &values)),
        &[cps("-remote_key")],
    )
    .expect("rows");
    assert_eq!(rows.len(), 700);
    let keys: Vec<String> = rows
        .iter()
        .map(|r| match r.get("remote_key") {
            Some(PyValue::Str(k)) => unicode::to_string(k).expect("text"),
            other => panic!("{other:?}"),
        })
        .collect();
    let mut sorted = keys.clone();
    sorted.sort_by(|a, b| b.cmp(a));
    assert_eq!(keys, sorted);
    // Duplicates are bound once: one batch, no row twice.
    let same = vec![s("r0001"); 1200];
    let rows = m::membership_rows(
        conn,
        "project_remotes",
        None,
        Some((&cps("remote_key"), &same)),
        &[],
    )
    .expect("rows");
    assert_eq!(rows.len(), 1);
}

#[test]
fn a_native_twin_replaces_its_legacy_row_and_is_cleared_after_a_put() {
    let (_d, mut store) = fresh("memb-kill-native");
    let mut p = Counting::at(STAMP, 0);
    store
        .put_membership(&mut p, &cps("project_remote"), &remote("P1", "a"), None)
        .expect("put");
    store
        .connection()
        .execute(
            "INSERT INTO native_membership_rows VALUES ('project_remote', '[\"P1\",\"a\"]', \
             '{\"project\":\"P1\",\"remote_key\":\"a\"}')",
            [],
        )
        .expect("native row");
    let rows =
        m::membership_rows(store.connection(), "project_remotes", None, None, &[]).expect("rows");
    assert_eq!(rows.len(), 1);
    store
        .put_membership(&mut p, &cps("project_remote"), &remote("P1", "a"), None)
        .expect("put again");
    let k = kinds(&store);
    assert_eq!(
        &k[k.len() - 2..],
        ["project_remote_put", "native_member_delete"]
    );
}

#[test]
fn ids_count_past_three_digits_and_prefixes_by_characters() {
    let (_d, mut store) = fresh("memb-kill-ids");
    let mut p = Counting::at(STAMP, 0);
    let machine = |id: &str| {
        vec![
            (s("id"), s(id)),
            (s("facts_json"), PyValue::Dict(vec![])),
            (s("predecessor"), PyValue::None),
            (s("minted"), s("t")),
        ]
    };
    store
        .put_membership(&mut p, &cps("machine"), &machine("M999"), None)
        .expect("put");
    assert_eq!(
        m::next_membership_id(store.connection(), "machines", "M").expect("id"),
        "M1000"
    );
    store
        .put_membership(&mut p, &cps("machine"), &machine("\u{e9}7"), None)
        .expect("put");
    assert_eq!(
        m::next_membership_id(store.connection(), "machines", "\u{e9}").expect("id"),
        "\u{e9}008"
    );
}

#[test]
fn a_control_row_kind_is_one_of_design_8_10() {
    let (_d, mut store) = fresh("memb-kill-control");
    let mut p = Counting::at(STAMP, 0);
    let e = store
        .record_membership(&mut p, &cps("project_remote_put"), &[], None)
        .expect_err("a mutation kind");
    assert!(matches!(
        e,
        WriteError::Refused {
            class: "ValueError",
            ..
        }
    ));
    store
        .record_membership(&mut p, &cps("store_forked"), &[], None)
        .expect("a control kind");
}

#[test]
fn wrote_record_matches_the_whole_id() {
    let (_d, mut store) = fresh("memb-kill-wrote");
    let mut p = Counting::at(STAMP, 0);
    let id = written::ulid(10, "01J");
    store
        .mutate(
            "memory_put",
            &written::memory_payload(&id, "a body", &["global"], &[]),
            None,
            &mut p,
        )
        .expect("put");
    assert!(m::wrote_record(store.connection(), &id).expect("read"));
    assert!(!m::wrote_record(store.connection(), &id[..id.len() - 1]).expect("read"));
}

#[test]
fn a_surrogate_row_is_refused_with_nothing_written() {
    let (_d, mut store) = fresh("memb-kill-surrogate");
    let mut p = Counting::at(STAMP, 0);
    let before = kinds(&store).len();
    let row = vec![
        (s("project"), s("P1")),
        (s("remote_key"), PyValue::Str(vec![0x72, 0xDC80])),
    ];
    let e = store
        .put_membership(&mut p, &cps("project_remote"), &row, None)
        .expect_err("refused");
    assert!(matches!(
        e,
        WriteError::NotPorted(WriteNotPorted::NativeMember)
    ));
    assert_eq!(kinds(&store).len(), before);
}

fn witness_native(conn: &rusqlite::Connection, commit: &str, arrival: &str) {
    conn.execute(
        "INSERT INTO native_membership_rows VALUES ('project_witness', ?, ?)",
        [
            format!("[\"P\",\"{commit}\"]"),
            format!(
                "{{\"project\":\"P\",\"commit_id\":\"{commit}\",\"arrival\":{arrival},\"source\":\"s\",\"fingerprint\":null}}"
            ),
        ],
    )
    .expect("native");
}

#[test]
fn an_int_literal_error_shows_200_code_points_of_the_repr() {
    let (_d, store) = fresh("memb-kill-int-repr");
    let conn = store.connection();
    conn.execute(
        "INSERT INTO projects(id, name, minted) VALUES ('P001', ?, 't')",
        ["x".repeat(300)],
    )
    .expect("insert");
    let e = m::next_membership_number(conn, "projects", &cps("name")).expect_err("int");
    let WriteError::Refused { class, text } = e else {
        panic!("{e:?}")
    };
    assert_eq!(class, "ValueError");
    assert_eq!(
        text.as_deref(),
        Some(
            format!(
                "invalid literal for int() with base 10: '{}",
                "x".repeat(199)
            )
            .as_str()
        )
    );
}

#[test]
fn the_largest_native_value_counts_even_when_negative() {
    let (_d, store) = fresh("memb-kill-negative");
    let conn = store.connection();
    conn.execute(
        "INSERT INTO project_witnesses(project, commit_id, arrival, source, fingerprint) VALUES ('P', 'old', -10, 's', NULL)",
        [],
    )
    .expect("insert");
    witness_native(conn, "c", "-5");
    witness_native(conn, "d", "-7");
    // max(int(-10 or 0), max(-5, -7)) + 1
    assert_eq!(
        m::next_membership_number(conn, "project_witnesses", &cps("arrival")).expect("n"),
        -4
    );
}

#[test]
fn equality_follows_python_for_bools_zeros_and_repeated_keys() {
    let (_d, store) = fresh("memb-kill-eq");
    let conn = store.connection();
    conn.execute(
        "INSERT INTO project_witnesses(project, commit_id, arrival, source, fingerprint) VALUES ('P', 'one', 1, 's', NULL)",
        [],
    )
    .expect("insert");
    let read = |where_: &[(PyStr, PyValue)]| {
        m::membership_rows(conn, "project_witnesses", Some(where_), None, &[]).expect("rows")
    };
    // True is bound as 1.
    assert_eq!(read(&[(cps("arrival"), PyValue::Bool(true))]).len(), 1);
    // 0 == -0.0 against a native row.
    witness_native(conn, "zero", "0");
    assert_eq!(read(&[(cps("arrival"), PyValue::Float(-0.0))]).len(), 1);
    // A repeated key: Python's dict keeps the last value.
    conn.execute(
        "INSERT INTO native_membership_rows VALUES ('project_remote', '[\"P1\",\"b\"]', '{\"project\":\"P1\",\"remote_key\":\"a\",\"remote_key\":\"b\"}')",
        [],
    )
    .expect("native");
    let rows = m::membership_rows(
        conn,
        "project_remotes",
        Some(&[(cps("remote_key"), s("b"))]),
        None,
        &[],
    )
    .expect("rows");
    assert_eq!(rows.len(), 1);
}

#[test]
fn a_surrogate_in_where_skips_the_legacy_table() {
    let (_d, store) = fresh("memb-kill-surrogate-where");
    let conn = store.connection();
    conn.execute(
        "INSERT INTO project_remotes(project, remote_key) VALUES ('P1', '')",
        [],
    )
    .expect("insert");
    let rows = m::membership_rows(
        conn,
        "project_remotes",
        Some(&[(cps("remote_key"), PyValue::Str(vec![0xD800]))]),
        None,
        &[],
    )
    .expect("rows");
    assert!(rows.is_empty());
}

#[test]
fn a_tuple_holding_a_list_is_unhashable() {
    let (_d, store) = fresh("memb-kill-tuple");
    let values = [PyValue::Tuple(vec![PyValue::List(vec![])])];
    let e = m::membership_rows(
        store.connection(),
        "project_remotes",
        None,
        Some((&cps("remote_key"), &values)),
        &[],
    )
    .expect_err("unhashable");
    assert!(matches!(
        e,
        WriteError::Refused {
            class: "TypeError",
            ..
        }
    ));
}

#[test]
fn a_transaction_no_call_owns_gives_committed_seq_0() {
    let (_d, store) = fresh("memb-kill-residue");
    store.connection().execute_batch("BEGIN").expect("begin");
    assert_eq!(store.committed_seq(), Some(0));
    store.connection().execute_batch("ROLLBACK").expect("end");
    assert_eq!(store.committed_seq(), None);
}
