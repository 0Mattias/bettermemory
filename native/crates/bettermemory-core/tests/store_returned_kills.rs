//! Cases a plausible wrong `missing_links` or `returned_rows` fails: a
//! note compared by type rather than as Python compares it, a source id
//! compared after `str()`, the first lost target taken instead of the
//! least, a hash checked in the wrong order, the ids not taken once each,
//! a batch boundary dropped, any error read as a missing table.

use bettermemory_core::store::returned::{missing_links, returned_rows};
use rusqlite::types::Value as Sql;
use rusqlite::Connection;

fn store(memories: &[(Sql, &str)], links: &[(Sql, Sql, Sql, Sql)]) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(
        "CREATE TABLE memories (id, links_json);
         CREATE TABLE memory_links (source_id, type, target_id, note);
         CREATE TABLE returned (record_id, via, returned);",
    )
    .expect("tables");
    for (id, json) in memories {
        conn.execute(
            "INSERT INTO memories VALUES (?, ?)",
            rusqlite::params![id, json],
        )
        .expect("memory");
    }
    for (a, b, c, d) in links {
        conn.execute(
            "INSERT INTO memory_links VALUES (?, ?, ?, ?)",
            rusqlite::params![a, b, c, d],
        )
        .expect("link");
    }
    conn
}

fn t(s: &str) -> Sql {
    Sql::Text(s.to_owned())
}

fn sup_note(note: &str) -> String {
    format!(r#"[{{"type": "supersedes", "target_id": "a", "note": {note}}}]"#)
}

fn lost(note_json: &str, note: Sql) -> Vec<(String, String)> {
    let conn = store(
        &[(t("a"), "[]"), (t("b"), &sup_note(note_json))],
        &[(t("b"), t("supersedes"), t("a"), note)],
    );
    missing_links(&conn).expect("an answer")
}

#[test]
fn a_note_is_compared_as_python_compares_it() {
    assert!(lost("1", Sql::Real(1.0)).is_empty());
    assert!(lost("true", Sql::Integer(1)).is_empty());
    assert!(lost("false", Sql::Integer(0)).is_empty());
    assert!(lost("2.0", Sql::Integer(2)).is_empty());
    assert!(lost("null", Sql::Null).is_empty());
    assert_eq!(lost("\"1\"", Sql::Integer(1)).len(), 1);
    assert_eq!(lost("1.5", Sql::Integer(1)).len(), 1);
    assert_eq!(lost("true", Sql::Integer(2)).len(), 1);
    assert_eq!(lost("\"a\"", Sql::Blob(b"a".to_vec())).len(), 1);
    assert_eq!(lost("null", t("None")).len(), 1);
    // An integer past i64 against a real holding the same value.
    assert!(lost(
        "9223372036854775808",
        Sql::Real(9_223_372_036_854_775_808.0)
    )
    .is_empty());
}

#[test]
fn an_integer_source_id_is_never_the_index_text() {
    let conn = store(
        &[(t("7"), "[]"), (Sql::Integer(8), r#"[{"target_id": "7"}]"#)],
        &[(t("8"), t("None"), t("7"), Sql::Null)],
    );
    assert_eq!(
        missing_links(&conn).expect("an answer"),
        vec![("8".to_owned(), "7".to_owned())]
    );
}

#[test]
fn the_least_lost_target_is_named() {
    let conn = store(
        &[
            (t("a"), "[]"),
            (t("c"), "[]"),
            (t("d"), r#"[{"target_id": "c"}, {"target_id": "a"}]"#),
        ],
        &[],
    );
    assert_eq!(
        missing_links(&conn).expect("an answer"),
        vec![("d".to_owned(), "a".to_owned())]
    );
}

#[test]
fn hashing_follows_the_conditions_order() {
    // An inactive target: the unhashable note is never hashed.
    let conn = store(&[(t("b"), r#"[{"target_id": "z", "note": []}]"#)], &[]);
    assert!(missing_links(&conn).expect("an answer").is_empty());
    // A list target raises at the `in active` test, whatever else.
    let conn = store(&[(t("b"), r#"[{"target_id": []}]"#)], &[]);
    assert_eq!(
        missing_links(&conn).expect_err("raises").class(),
        "TypeError"
    );
    // An active target with an unhashable type raises.
    let conn = store(
        &[
            (t("a"), "[]"),
            (t("b"), r#"[{"target_id": "a", "type": {}}]"#),
        ],
        &[],
    );
    assert_eq!(
        missing_links(&conn).expect_err("raises").class(),
        "TypeError"
    );
}

#[test]
fn a_link_list_that_is_not_iterable_raises_but_bad_json_is_passed_over() {
    for json in ["null", "1", "1.5", "true", "false", "NaN"] {
        let conn = store(&[(t("b"), json)], &[]);
        assert_eq!(
            missing_links(&conn).expect_err(json).class(),
            "TypeError",
            "{json}"
        );
    }
    for json in ["[", "{}", "\"ab\"", ""] {
        let conn = store(&[(t("b"), json)], &[]);
        assert!(missing_links(&conn).expect(json).is_empty(), "{json}");
    }
}

fn marks(n: usize) -> Connection {
    let conn = store(&[], &[]);
    for i in 0..n {
        conn.execute(
            "INSERT INTO returned VALUES (?, ?, ?)",
            rusqlite::params![format!("id{i:05}"), "v", "r"],
        )
        .expect("mark");
    }
    conn
}

#[test]
fn ids_are_read_once_each_across_batches() {
    let conn = marks(1200);
    let mut ids: Vec<String> = (0..1200).rev().map(|i| format!("id{i:05}")).collect();
    ids.extend(ids.clone());
    let got = returned_rows(&conn, Some(&ids)).expect("rows");
    assert_eq!(got.len(), 1200);
    let mut seen: Vec<&str> = got.iter().map(|(id, _, _)| id.as_str()).collect();
    seen.sort_unstable();
    seen.dedup();
    assert_eq!(seen.len(), 1200);
    // The ids at a batch's edges.
    for edge in [499, 500, 999, 1000] {
        let one = vec![format!("id{edge:05}")];
        assert_eq!(returned_rows(&conn, Some(&one)).expect("rows").len(), 1);
    }
    assert!(returned_rows(&conn, Some(&[])).expect("rows").is_empty());
}

#[test]
fn only_a_missing_table_reads_as_no_marks() {
    let conn = Connection::open_in_memory().expect("memory");
    assert!(returned_rows(&conn, None).expect("none").is_empty());
    conn.execute_batch("CREATE TABLE returned (record_id, via)")
        .expect("table");
    assert!(returned_rows(&conn, None).is_err());
    assert!(returned_rows(&conn, Some(&["x".to_owned()])).is_err());
    // No ids asked: nothing is read, so nothing raises.
    assert!(returned_rows(&conn, Some(&[])).expect("none").is_empty());
}
