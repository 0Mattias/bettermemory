//! The stores and calls of `store_event_reads.jsonl` and
//! `store_event_reads_naive.jsonl`: the header's DDL and each store's
//! statements replayed into an in-memory SQLite file, and each call
//! answered as the generator spells it (`json.dumps` of what was yielded,
//! the class of what stopped it).

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::event_reads::{
    capture_rows, events_of_kinds, events_since, iter_events, iter_events_backward,
    session_events_of_kinds, window_floor, Read,
};
use bettermemory_core::unicode;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Value};

fn untag(v: &Value) -> Sql {
    if v.is_null() {
        return Sql::Null;
    }
    let t = &v[1];
    match v[0].as_str().unwrap() {
        "i" => Sql::Integer(t.as_i64().unwrap()),
        "s" => Sql::Text(t.as_str().unwrap().to_owned()),
        _ => Sql::Blob(super::unhex(t.as_str().unwrap())),
    }
}

/// A store: the DDL, then the statements in order.
pub fn store(ddl: &Value, statements: &Value) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    for sql in ddl.as_array().unwrap() {
        conn.execute(sql.as_str().unwrap(), []).unwrap();
    }
    for st in statements.as_array().unwrap() {
        let params: Vec<Sql> = st[1].as_array().unwrap().iter().map(untag).collect();
        conn.execute(
            st[0].as_str().unwrap(),
            rusqlite::params_from_iter(params.iter()),
        )
        .unwrap();
    }
    conn
}

/// A text argument: a string, or `{"cps": [...]}`.
pub fn text(v: &Value) -> PyStr {
    match v {
        Value::String(s) => unicode::cps(s),
        _ => v["cps"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c.as_u64().unwrap() as u32)
            .collect(),
    }
}

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array().unwrap().iter().map(text).collect()
}

fn log_row(r: &LogRow) -> PyValue {
    let s = |t: &str| PyValue::Str(unicode::cps(t));
    PyValue::List(vec![
        PyValue::Int(r.seq.to_string()),
        s(&r.ts),
        r.session.as_deref().map_or(PyValue::None, s),
        s(&r.kind),
        s(&r.payload),
        s(&r.prev_mac),
        s(&r.mac),
    ])
}

fn answer<T>(read: Read<T>, item: impl Fn(&T) -> PyValue) -> Value {
    let list = PyValue::List(read.items.iter().map(item).collect());
    let out = unicode::to_string(&dumps(&list, DumpOptions::DEFAULT).unwrap()).unwrap();
    let mut v = json!({ "out": out });
    if let Some(e) = read.error {
        v["raise"] = json!(e.class());
    }
    v
}

/// The case's answer as the file spells it: `out` and `raise`, or
/// `floor` for the window floor.
pub fn run(conn: &Connection, case: &Value, offset: Option<i32>) -> Value {
    let args = &case["args"];
    let local = move |_: &DateTime| offset;
    let since =
        || DateTime::from_isoformat(&unicode::cps(args["since"].as_str().unwrap())).unwrap();
    let ev = |e: &PyValue| e.clone();
    match case["call"].as_str().unwrap() {
        "iter_events" => answer(iter_events(conn, args["since_seq"].as_i64().unwrap()), ev),
        "iter_events_backward" => answer(iter_events_backward(conn), ev),
        "capture_rows" => answer(
            capture_rows(
                conn,
                &texts(&args["kinds"]),
                args["since_seq"].as_i64().unwrap(),
            ),
            log_row,
        ),
        "window_floor" => {
            let floor = window_floor(
                conn,
                args["prefix"].as_str().unwrap(),
                args["head_seq"].as_i64(),
            )
            .unwrap();
            json!({ "floor": floor })
        }
        "events_since" => answer(events_since(conn, &since(), &local), ev),
        "events_of_kinds" => {
            let naming = match &args["naming"] {
                Value::Null => None,
                n => {
                    let mut all = texts(&n["head"]);
                    for i in 0..n["count"].as_u64().unwrap() {
                        all.push(unicode::cps(&format!("n{i}")));
                    }
                    Some(all)
                }
            };
            answer(
                events_of_kinds(
                    conn,
                    &texts(&args["kinds"]),
                    &since(),
                    naming.as_deref(),
                    &local,
                ),
                ev,
            )
        }
        "session_events_of_kinds" => answer(
            session_events_of_kinds(conn, &text(&args["session"]), &texts(&args["kinds"])),
            ev,
        ),
        other => panic!("no call {other}"),
    }
}

/// What the file holds for the case, in the shape [`run`] answers.
pub fn expected(case: &Value) -> Value {
    if case["call"] == "window_floor" {
        return json!({ "floor": case["floor"] });
    }
    let mut v = json!({ "out": case["out"] });
    if let Some(r) = case.get("raise") {
        v["raise"] = r.clone();
    }
    v
}

/// Runs every call of `name` and returns how many were checked and the
/// differences.
pub fn check_file(name: &str, offset: Option<i32>) -> (usize, usize, Vec<String>) {
    let lines = super::jsonl(name);
    let ddl = lines[0]["ddl"].clone();
    let mut stores = std::collections::HashMap::new();
    let (mut checked, mut failures) = (0, Vec::new());
    for (i, case) in lines.iter().enumerate().skip(1) {
        let name = case["store"].as_str().unwrap().to_owned();
        if case.get("statements").is_some() {
            stores.insert(name, store(&ddl, &case["statements"]));
            continue;
        }
        let got = run(&stores[&name], case, offset);
        let want = expected(case);
        if got != want {
            failures.push(format!("#{i} {case}\n  got {got}"));
        }
        checked += 1;
    }
    (lines.len() - 1 - stores.len(), checked, failures)
}
