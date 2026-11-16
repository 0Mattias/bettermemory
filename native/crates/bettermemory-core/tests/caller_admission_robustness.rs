//! Hostile project tables never panic or loop in `store::caller`: cycles
//! in ``merged_into`` and ``parent``, a missing row, values of every type
//! in every column, NULLs, chains of 100,000 projects, malformed and
//! large and deeply nested native rows, missing tables and columns, and resolutions that
//! do not parse. Each gives a typed outcome. The reference's answers for
//! the shapes it has one for are pinned in `caller_admission.jsonl`
//! (`golden_caller_admission`); here only termination, the outcome's type
//! and the answers that follow from the rule are asserted.

mod common;

use bettermemory_core::store::caller::{
    Admission, Membership, ModelError, Resolution, Surface, Val,
};
use bettermemory_core::unicode::cps;
use rusqlite::{params, Connection};

const SCHEMA: &str = "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, \
     merged_into TEXT, provisional TEXT, parent TEXT, tenant TEXT, \
     folder INTEGER NOT NULL DEFAULT 0, boundary INTEGER NOT NULL DEFAULT 0, minted TEXT NOT NULL);
     CREATE TABLE native_membership_rows (member_kind TEXT NOT NULL, key_text TEXT NOT NULL, \
     row_text TEXT NOT NULL, PRIMARY KEY (member_kind, key_text));";

fn db(sql: &str) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(sql).expect("schema");
    conn
}

fn put(conn: &Connection, id: &str, merged: Option<&str>, parent: Option<&str>) {
    conn.execute(
        "INSERT INTO projects(id, name, merged_into, parent, minted) VALUES (?, ?, ?, ?, 'm')",
        params![id, id, merged, parent],
    )
    .expect("row");
}

fn caller(project: &str, chain: &[&str]) -> Resolution {
    let chain: Vec<String> = chain.iter().map(|c| format!("\"{c}\"")).collect();
    Resolution::from_json(&format!(
        "{{\"outcome\":\"silent\",\"project\":\"{project}\",\"checkout\":null,\"rule\":\"r\",\
         \"chain\":[{}]}}",
        chain.join(",")
    ))
    .expect("a resolution")
}

fn every_answer(model: &Membership, keys: &[&str]) {
    let r = caller(keys[0], &keys[..1]);
    for surface in Surface::ALL {
        let a = Admission::new(surface, Some(&r), model);
        for k in keys {
            let _ = a.admits_project(Some(&cps(k)));
            let _ = a.admits_row(&[cps(&format!("projects:{k}"))], Some(&cps("global")));
        }
        let _ = a.admitted_projects();
        let _ = a.admits_global();
    }
}

#[test]
fn cycles_terminate() {
    let conn = db(SCHEMA);
    put(&conn, "A", Some("B"), Some("C"));
    put(&conn, "B", Some("A"), None);
    put(&conn, "C", None, Some("D"));
    put(&conn, "D", None, Some("C"));
    put(&conn, "E", Some("E"), Some("E"));
    put(&conn, "F", Some("missing"), None);
    let model = Membership::read(&conn);
    assert!(model.error().is_none());
    // A cycle of merges stops where it would revisit a project.
    assert!(model
        .canonical(&Val::text("A"))
        .expect("read")
        .is_text(&cps("A")));
    assert!(model
        .canonical(&Val::text("E"))
        .expect("read")
        .is_text(&cps("E")));
    assert!(model
        .canonical(&Val::text("F"))
        .expect("read")
        .is_text(&cps("missing")));
    assert_eq!(
        model.chain_of(&Val::text("C")).expect("read"),
        vec![cps("C"), cps("D")]
    );
    every_answer(&model, &["A", "B", "C", "D", "E", "F", "missing", "global"]);
}

#[test]
fn chains_of_a_hundred_thousand() {
    const N: usize = 100_000;
    let conn = db(SCHEMA);
    conn.execute_batch("BEGIN").expect("begin");
    for i in 0..N {
        let merged = (i > 0 && i < N / 2).then(|| format!("M{}", i - 1));
        let parent = (i > N / 2).then(|| format!("M{}", i - 1));
        put(
            &conn,
            &format!("M{i}"),
            merged.as_deref(),
            parent.as_deref(),
        );
    }
    // Close both chains into cycles.
    conn.execute(
        "UPDATE projects SET merged_into = ?, parent = ? WHERE id = 'M0'",
        params![format!("M{}", N / 2 - 1), format!("M{}", N - 1)],
    )
    .expect("cycle");
    conn.execute_batch("COMMIT").expect("commit");
    let started = std::time::Instant::now();
    let model = Membership::read(&conn);
    let top = Val::text(&format!("M{}", N - 1));
    let chain = model.chain_of(&top).expect("read");
    assert!(chain.len() <= N);
    let r = caller(&format!("M{}", N - 1), &[]);
    let a = Admission::new(Surface::SearchUnscoped, Some(&r), &model);
    let _ = a.admits_project(Some(&cps(&format!("M{}", N / 2 - 1))));
    let _ = a.admits_row(&[cps("projects:M7")], Some(&cps("global")));
    assert!(
        started.elapsed() < std::time::Duration::from_secs(60),
        "took {:?}",
        started.elapsed()
    );
}

#[test]
fn values_of_every_type_in_every_column() {
    let conn = db(
        "CREATE TABLE projects (id, name, merged_into, provisional, parent, tenant, folder, \
         boundary, minted)",
    );
    let values: Vec<rusqlite::types::Value> = vec![
        rusqlite::types::Value::Null,
        rusqlite::types::Value::Integer(i64::MIN),
        rusqlite::types::Value::Integer(0),
        rusqlite::types::Value::Real(f64::INFINITY),
        rusqlite::types::Value::Real(-0.0),
        rusqlite::types::Value::Text("P1".into()),
        rusqlite::types::Value::Text(String::new()),
        rusqlite::types::Value::Blob(vec![0, b'\'', b'"', 0xff]),
    ];
    for (i, v) in values.iter().enumerate() {
        for (j, w) in values.iter().enumerate() {
            conn.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params![
                    if i == 5 {
                        format!("P{j}").into()
                    } else {
                        v.clone()
                    },
                    w,
                    v,
                    w,
                    v,
                    w,
                    v,
                    w,
                    v
                ],
            )
            .expect("row");
        }
    }
    let model = Membership::read(&conn);
    assert!(model.error().is_none(), "{:?}", model.error());
    every_answer(&model, &["P1", "P2", "0", "None", "inf", "", "global"]);
    // With a native row beside them, ids of different types cannot be
    // sorted (the reference's `_sort_rows`), and bytes cannot be keyed.
    conn.execute_batch(
        "CREATE TABLE native_membership_rows (member_kind, key_text, row_text);
         INSERT INTO native_membership_rows VALUES ('project', '[\"Q\"]', 'null');",
    )
    .expect("native");
    let model = Membership::read(&conn);
    assert_eq!(model.error(), Some(&ModelError::Raised("TypeError")));
    every_answer(&model, &["P1", "global"]);
}

#[test]
fn missing_tables_columns_and_rows() {
    let model = Membership::read(&db(""));
    assert_eq!(model.error(), Some(&ModelError::Raised("OperationalError")));
    every_answer(&model, &["P1", "global"]);
    let r = caller("P1", &["P1"]);
    let a = Admission::new(Surface::SearchUnscoped, Some(&r), &model);
    // No read for a record with no home, nor for `global`'s home.
    assert_eq!(a.admits_project(None), Ok(false));
    assert_eq!(a.admits_global(), Ok(true));
    let model = Membership::read(&db("CREATE TABLE projects (id, name)"));
    assert_eq!(model.error(), Some(&ModelError::Raised("OperationalError")));
    let conn = db(SCHEMA);
    put(&conn, "P1", None, Some("P404"));
    let model = Membership::read(&conn);
    assert_eq!(model.chain_of(&Val::text("P1")), Ok(vec![cps("P1")]));
}

#[test]
fn hostile_native_rows() {
    let conn = db(SCHEMA);
    put(&conn, "P1", None, None);
    let deep = format!("{}{}", "[".repeat(200_000), "]".repeat(200_000));
    let big = format!(
        "{{\"id\":\"P2\",\"name\":\"{}\",\"merged_into\":null,\"provisional\":null,\
         \"parent\":null,\"tenant\":null,\"folder\":0,\"boundary\":0,\"minted\":\"m\"}}",
        "x".repeat(200_000)
    );
    for (key, text) in [
        ("[\"P1\"]", deep.as_str()),
        ("[\"P2\"]", big.as_str()),
        ("[\"P3\"]", "{\"id\": 1e400}"),
        ("[\"P4\"]", "\u{feff}{}"),
        ("[\"P5\"]", "{\"id\":\"P5\",\"id\":\"P5\"}"),
    ] {
        conn.execute(
            "INSERT INTO native_membership_rows VALUES ('project', ?, ?)",
            params![key, text],
        )
        .expect("native");
    }
    let model = Membership::read(&conn);
    assert!(model.error().is_none());
    // The malformed row keyed P1 still takes the legacy P1's place.
    let ids: Vec<_> = model
        .all_projects()
        .expect("read")
        .iter()
        .map(|p| p.id.clone())
        .collect();
    assert_eq!(ids, vec![cps("P2")]);
    every_answer(&model, &["P1", "P2", "global"]);
    // Bytes in every shape `json.detect_encoding` reads, short, odd,
    // truncated or not decoding: each row reads or is malformed, and the
    // store is read.
    let mut shapes: Vec<Vec<u8>> = Vec::new();
    for len in 0..9 {
        for fill in [0u8, 0xd8, 0xdc, 0xed, 0xfe, 0xff, b'{'] {
            shapes.push(vec![fill; len]);
            let mut mixed = vec![0xff, 0xfe, 0, 0, 0xed, 0xa0, 0x80, 0xd8];
            mixed.truncate(len);
            shapes.push(mixed);
        }
    }
    let conn = db(SCHEMA);
    put(&conn, "P1", None, None);
    for (i, bytes) in shapes.iter().enumerate() {
        conn.execute(
            "INSERT OR REPLACE INTO native_membership_rows VALUES ('project', ?, ?)",
            params![format!("[\"Q{i}\"]"), bytes],
        )
        .expect("native");
    }
    let model = Membership::read(&conn);
    assert!(model.error().is_none(), "{:?}", model.error());
    every_answer(&model, &["P1", "Q0", "global"]);
}

/// The read is linear in the rows: ten times the rows (legacy and
/// native, every native key replacing a legacy row) take at most thirty
/// times as long, plus 50 ms. Best of five runs at each size. Linear
/// growth is ten times and quadratic a hundred, so the bound holds on a
/// slow, noisy machine and still catches the quadratic read this replaced.
#[test]
fn reading_native_rows_is_linear() {
    fn store(n: usize) -> Connection {
        let conn = db(SCHEMA);
        conn.execute_batch("BEGIN").expect("begin");
        for i in 0..n {
            put(&conn, &format!("L{i:06}"), None, None);
            let row = format!(
                "{{\"id\":\"L{i:06}\",\"name\":\"n\",\"merged_into\":null,\"provisional\":null,\
                 \"parent\":null,\"tenant\":null,\"folder\":0,\"boundary\":0,\"minted\":\"m\"}}"
            );
            conn.execute(
                "INSERT INTO native_membership_rows VALUES ('project', ?, ?)",
                params![format!("[\"L{i:06}\"]"), row],
            )
            .expect("native");
        }
        conn.execute_batch("COMMIT").expect("commit");
        conn
    }
    let best = |conn: &Connection| {
        (0..5)
            .map(|_| {
                let started = std::time::Instant::now();
                let model = Membership::read(conn);
                assert!(model.error().is_none());
                started.elapsed()
            })
            .min()
            .expect("a run")
    };
    let (small, large) = (store(2_000), store(20_000));
    let (s, l) = (best(&small), best(&large));
    assert!(
        l < s * 30 + std::time::Duration::from_millis(50),
        "2,000 rows {s:?}, 20,000 rows {l:?}: more than linear"
    );
}

#[test]
fn resolutions_that_do_not_parse() {
    for text in [
        "",
        "[]",
        "{}",
        "{\"outcome\":1,\"project\":null,\"checkout\":null,\"rule\":\"r\"}",
        "{\"outcome\":\"silent\",\"project\":null,\"checkout\":null}",
        "{\"outcome\":\"silent\",\"project\":null,\"checkout\":null,\"rule\":\"r\",\"x\":1}",
        "{\"outcome\":\"silent\",\"project\":null,\"checkout\":null,\"rule\":\"r\",\"rows\":[\"1\"]}",
        "{\"outcome\":\"silent\",\"project\":null,\"checkout\":null,\"rule\":\"r\",\"witnesses\":[]}",
    ] {
        assert!(Resolution::from_json(text).is_err(), "{text}");
    }
    let ok = Resolution::from_json(
        "{\"outcome\":\"deferred\",\"project\":\"P1\",\"checkout\":null,\"rule\":\"r\"}",
    )
    .expect("parses");
    let model = Membership::from_projects(Vec::new());
    let a = Admission::new(Surface::SearchUnscoped, Some(&ok), &model);
    assert_eq!(a.nothing(), Some(cps("deferred")));
    assert_eq!(a.admitted_projects(), Ok(None));
}
