//! Hostile tables for `missing_links` and `returned_rows`: many records
//! and index rows (linear, as the reference's sets make it), link lists
//! nested deep, long and odd, integer bounds in notes. No panic in debug
//! or release.

mod timing;

use bettermemory_core::store::returned::{missing_links, returned_rows};
use rusqlite::Connection;

fn tables() -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(
        "CREATE TABLE memories (id, links_json);
         CREATE TABLE memory_links (source_id, type, target_id, note);
         CREATE TABLE returned (record_id, via, returned);",
    )
    .expect("tables");
    conn
}

/// `n` records, each superseding the one before, half of the links
/// indexed, and `n` marks.
fn chain(n: usize) -> Connection {
    let conn = tables();
    conn.execute_batch("BEGIN").expect("begin");
    for i in 0..n {
        let links = if i == 0 {
            "[]".to_owned()
        } else {
            format!(
                r#"[{{"type": "supersedes", "target_id": "r{:07}"}}]"#,
                i - 1
            )
        };
        conn.execute(
            "INSERT INTO memories VALUES (?, ?)",
            rusqlite::params![format!("r{i:07}"), links],
        )
        .expect("record");
        if i % 2 == 0 && i > 0 {
            conn.execute(
                "INSERT INTO memory_links VALUES (?, 'supersedes', ?, NULL)",
                rusqlite::params![format!("r{i:07}"), format!("r{:07}", i - 1)],
            )
            .expect("link");
        }
        conn.execute(
            "INSERT INTO returned VALUES (?, 'v', 'r')",
            rusqlite::params![format!("r{i:07}")],
        )
        .expect("mark");
    }
    conn.execute_batch("COMMIT").expect("commit");
    conn
}

#[test]
fn many_records_take_linear_time() {
    let small = chain(2_000);
    let large = chain(20_000);
    assert_eq!(missing_links(&small).expect("answer").len(), 1_000);
    assert_eq!(missing_links(&large).expect("answer").len(), 10_000);
    timing::grows_within(
        "missing_links: 2,000 and 20,000 records",
        timing::TENFOLD,
        &mut || timing::once(|| missing_links(&small).expect("answer")),
        &mut || timing::once(|| missing_links(&large).expect("answer")),
    );
    let ids = |n: usize| -> Vec<String> { (0..n).map(|i| format!("r{i:07}")).collect() };
    let (few, many) = (ids(2_000), ids(20_000));
    assert_eq!(
        returned_rows(&large, Some(&many)).expect("rows").len(),
        20_000
    );
    timing::grows_within(
        "returned_rows: 2,000 and 20,000 ids",
        timing::TENFOLD,
        &mut || timing::once(|| returned_rows(&small, Some(&few)).expect("rows")),
        &mut || timing::once(|| returned_rows(&large, Some(&many)).expect("rows")),
    );
}

#[test]
fn odd_link_lists_do_not_panic() {
    let conn = tables();
    let deep = format!("{}{}", "[".repeat(5_000), "]".repeat(5_000));
    let wide = format!(
        "[{}]",
        (0..50_000)
            .map(|i| format!(r#"{{"target_id": "x{i}", "note": {i}}}"#))
            .collect::<Vec<_>>()
            .join(",")
    );
    let cases = [
        deep.as_str(),
        wide.as_str(),
        r#"[{"target_id": "a", "note": 18446744073709551616}]"#,
        r#"[{"target_id": "a", "note": -9223372036854775809}]"#,
        r#"[{"target_id": "a", "note": 1e400}]"#,
        r#"[{"target_id": "a", "note": "\ud800"}]"#,
        "\"\u{10FFFF}\"",
    ];
    conn.execute("INSERT INTO memories VALUES ('a', '[]')", [])
        .expect("a");
    conn.execute(
        "INSERT INTO memory_links VALUES ('b', 'None', 'a', 9223372036854775807)",
        [],
    )
    .expect("link");
    for (i, json) in cases.iter().enumerate() {
        conn.execute(
            "INSERT INTO memories VALUES (?, ?)",
            rusqlite::params![format!("b{i}"), json],
        )
        .expect("record");
        // Raising or not, never a panic.
        let _ = missing_links(&conn);
        conn.execute("DELETE FROM memories WHERE id != 'a'", [])
            .expect("clear");
    }
}

#[test]
fn integer_bound_notes_compare_exactly() {
    let conn = tables();
    conn.execute("INSERT INTO memories VALUES ('a', '[]')", [])
        .expect("a");
    conn.execute(
        "INSERT INTO memory_links VALUES ('b', 'supersedes', 'a', 9223372036854775807)",
        [],
    )
    .expect("link");
    for (note_json, held) in [
        ("9223372036854775807", true),
        ("9223372036854775808", false),
        ("-9223372036854775808", false),
        ("9223372036854775807.0", false),
    ] {
        conn.execute("DELETE FROM memories WHERE id = 'b'", [])
            .expect("clear");
        conn.execute(
            "INSERT INTO memories VALUES ('b', ?)",
            [format!(
                r#"[{{"type": "supersedes", "target_id": "a", "note": {note_json}}}]"#
            )],
        )
        .expect("b");
        let lost = missing_links(&conn).expect("answer");
        assert_eq!(lost.is_empty(), held, "{note_json}");
    }
}
