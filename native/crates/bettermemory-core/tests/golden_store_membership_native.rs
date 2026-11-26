//! The native membership writes, `membership_log` and
//! `reissue_membership_store_id` against `store_membership_native.jsonl`:
//! each case replayed from the store `store_create.json` pins, its steps
//! as `gen_store_membership._native_run` runs them (a batch's inner calls
//! inside one transaction), each answer compared as the reference records
//! it (`encoded_json` text, a text holding two backslashes in a row as its
//! UTF-8 hex, a log read as its sequence numbers and digest, a long table
//! as its count and digest), then the step's log rows (a payload spelled
//! the same way), clock reads, state digest and index, and the refold at
//! the end. POSIX only, as the fixture is;
//! `store_membership_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use std::io;
use std::path::Path;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::log::{Key, LogRow};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::membership as m;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::tx::Transaction;
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode;
use common::gitobjects::sha;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::rows::{refusal, Replay};
use common::stores::scratch;
use common::{jsonl, written};
use rusqlite::types::Value as Bound;
use serde_json::{json, Value};

/// The step's clocks, and the token a reissue draws.
struct Clocks {
    replay: Replay,
    tokens: Vec<String>,
}

impl Provider for Clocks {
    fn store_id(&mut self) -> io::Result<String> {
        self.replay.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.replay.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        if self.tokens.is_empty() {
            return Err(io::Error::other("an unscripted token"));
        }
        Ok(self.tokens.remove(0))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.replay.now()
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        self.replay.wall_now()
    }
    fn engine_version(&self) -> String {
        self.replay.engine_version()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.replay.ulid()
    }
}

/// An argument: `{"$json": text}` read as the reference's `json.loads`.
fn arg(v: &Value) -> PyValue {
    match v.get("$json").and_then(Value::as_str) {
        Some(text) => written::payload(text),
        None => written::payload(&serde_json::to_string(v).expect("json")),
    }
}

fn items(v: &Value) -> Vec<(PyValue, PyValue)> {
    match &arg(v) {
        PyValue::Dict(items) => items.clone(),
        other => panic!("not a mapping: {other:?}"),
    }
}

fn cps(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("text"))
}

/// `spelled`: a text holding two backslashes in a row as its UTF-8 hex.
fn spelled(text: String) -> Value {
    if text.contains("\\\\") {
        let hex: String = text.bytes().map(|b| format!("{b:02x}")).collect();
        json!({"hex": hex})
    } else {
        Value::String(text)
    }
}

fn encoded(value: &PyValue) -> String {
    let text = json::dumps(value, DumpOptions::SORTED_ASCII_COMPACT).expect("dumps");
    unicode::to_string(&text).expect("ascii")
}

/// What a call answered, as `native_answer` records it.
enum Answer {
    Value(PyValue),
    Log(Vec<LogRow>),
    Table(Vec<PyValue>),
}

fn answer(value: &Answer) -> Value {
    match value {
        Answer::Log(rows) => {
            let listed = PyValue::List(
                rows.iter()
                    .map(|r| {
                        PyValue::List(vec![
                            PyValue::int(i128::from(r.seq)),
                            PyValue::str(&r.ts),
                            r.session.as_deref().map_or(PyValue::None, PyValue::str),
                            PyValue::str(&r.kind),
                            PyValue::str(&r.payload),
                            PyValue::str(&r.prev_mac),
                            PyValue::str(&r.mac),
                        ])
                    })
                    .collect(),
            );
            json!({
                "seqs": rows.iter().map(|r| r.seq).collect::<Vec<_>>(),
                "sha256": sha(encoded(&listed).as_bytes()),
            })
        }
        Answer::Table(rows) if rows.len() > 8 => {
            let text = encoded(&PyValue::List(rows.clone()));
            json!({"count": rows.len(), "sha256": sha(text.as_bytes())})
        }
        Answer::Table(rows) => spelled(encoded(&PyValue::List(rows.clone()))),
        Answer::Value(PyValue::List(rows)) if rows.len() > 40 => {
            let text = encoded(&PyValue::List(rows.clone()));
            json!({"count": rows.len(), "sha256": sha(text.as_bytes())})
        }
        Answer::Value(v) => spelled(encoded(v)),
    }
}

fn logged(row: &LogRow) -> PyValue {
    PyValue::List(vec![
        PyValue::int(i128::from(row.seq)),
        PyValue::str(&row.kind),
    ])
}

enum Target<'a, 's> {
    Store(&'a mut WritableStore, &'a mut Clocks),
    Tx(&'a mut Transaction<'s>, &'a Path),
}

impl Target<'_, '_> {
    fn conn(&self) -> &rusqlite::Connection {
        match self {
            Target::Store(s, _) => s.connection(),
            Target::Tx(t, _) => t.connection(),
        }
    }
}

fn one(t: &mut Target<'_, '_>, call: &Value) -> Result<Answer, WriteError> {
    let session = call["session"].as_str();
    let what = call["call"].as_str().expect("call");
    Ok(Answer::Value(match what {
        "put" | "delete" | "record" => {
            let kind = cps(&call["kind"]);
            let given = items(match what {
                "put" => &call["row"],
                "delete" => &call["key"],
                _ => &call["payload"],
            });
            let row = match (t, what) {
                (Target::Store(s, p), "put") => s.put_membership(*p, &kind, &given, session)?.0,
                (Target::Store(s, p), "delete") => {
                    s.delete_membership(*p, &kind, &given, session)?.0
                }
                (Target::Store(s, p), _) => s.record_membership(*p, &kind, &given, session)?.0,
                (Target::Tx(x, _), "put") => x.put_membership(&kind, &given, session)?,
                (Target::Tx(x, _), "delete") => x.delete_membership(&kind, &given, session)?,
                (Target::Tx(x, _), _) => x.record_membership(&kind, &given, session)?,
            };
            logged(&row)
        }
        "rows" => {
            let where_: Option<Vec<(PyStr, PyValue)>> = call.get("where").map(|w| {
                items(w)
                    .into_iter()
                    .map(|(k, v)| match &k {
                        PyValue::Str(k) => (k.clone(), v),
                        other => panic!("a column {other:?}"),
                    })
                    .collect()
            });
            let column_in = call.get("column_in").map(|c| {
                let values = match &arg(&c[1]) {
                    PyValue::List(v) => v.clone(),
                    other => panic!("values {other:?}"),
                };
                (cps(&c[0]), values)
            });
            let order: Vec<PyStr> = call["order_by"]
                .as_array()
                .into_iter()
                .flatten()
                .map(cps)
                .collect();
            PyValue::List(m::membership_rows(
                t.conn(),
                call["table"].as_str().expect("table"),
                where_.as_deref(),
                column_in
                    .as_ref()
                    .map(|(c, v)| (c.as_slice(), v.as_slice())),
                &order,
            )?)
        }
        "next_id" => PyValue::str(&m::next_membership_id(
            t.conn(),
            call["table"].as_str().expect("table"),
            call["prefix"].as_str().expect("prefix"),
        )?),
        "next_number" => PyValue::int(m::next_membership_number(
            t.conn(),
            call["table"].as_str().expect("table"),
            &cps(&call["column"]),
        )?),
        "committed_seq" => {
            let seq = match t {
                Target::Store(s, _) => s.committed_seq(),
                Target::Tx(x, _) => x.committed_seq(),
            };
            seq.map_or(PyValue::None, |s| PyValue::int(i128::from(s)))
        }
        "in_batch" => PyValue::Bool(match t {
            Target::Store(s, _) => s.in_batch(),
            Target::Tx(x, _) => x.in_batch(),
        }),
        "wrote" => PyValue::Bool(m::wrote_record(t.conn(), call["id"].as_str().expect("id"))?),
        "store_id" => {
            let path = match t {
                Target::Store(s, _) => s.path().to_owned(),
                Target::Tx(_, p) => p.to_path_buf(),
            };
            PyValue::str(&m::membership_store_id(t.conn(), &path)?)
        }
        "sql" => {
            for statement in call["statements"].as_array().expect("statements") {
                let params: Vec<Bound> = statement[1]
                    .as_array()
                    .expect("params")
                    .iter()
                    .map(|p| match &arg(p) {
                        PyValue::Str(s) => Bound::Text(unicode::to_string(s).expect("text")),
                        other => panic!("a parameter {other:?}"),
                    })
                    .collect();
                t.conn()
                    .execute(
                        statement[0].as_str().expect("sql"),
                        rusqlite::params_from_iter(params),
                    )
                    .expect("sql");
            }
            PyValue::None
        }
        "fill" => {
            for i in 0..call["n"].as_u64().expect("n") {
                t.conn()
                    .execute(
                        "INSERT INTO project_remotes(project, remote_key) VALUES (?, ?)",
                        [format!("P00{}", i % 7), format!("r{i:05}")],
                    )
                    .expect("fill");
            }
            PyValue::None
        }
        "log" => {
            let kinds = call
                .get("kinds")
                .filter(|k| !k.is_null())
                .map(|k| match &arg(k) {
                    PyValue::List(items) => items.clone(),
                    other => panic!("kinds {other:?}"),
                });
            let since = match &call["since_seq"] {
                Value::Null => 0,
                Value::Bool(b) => i64::from(*b),
                other => other.as_i64().expect("since_seq"),
            };
            let checkout = call.get("checkout").filter(|c| !c.is_null()).map(arg);
            return Ok(Answer::Log(m::membership_log(
                t.conn(),
                kinds.as_deref(),
                since,
                checkout.as_ref(),
            )?));
        }
        "reissue" => {
            let record = call["record"].as_str().expect("record");
            let source = call["source"].as_str().expect("source");
            let (old, new) = match t {
                Target::Store(s, p) => {
                    p.tokens
                        .push(call["token"].as_str().expect("token").to_owned());
                    s.reissue_membership_store_id(*p, record, source, session)?
                        .0
                }
                Target::Tx(x, path) => x.reissue_membership_store_id(
                    path,
                    call["token"].as_str().expect("token"),
                    record,
                    source,
                    session,
                )?,
            };
            PyValue::List(vec![PyValue::str(&old), PyValue::str(&new)])
        }
        "table" => {
            let table = call["table"].as_str().expect("table");
            let conn = t.conn();
            let mut st = conn
                .prepare(&format!("SELECT * FROM {table} ORDER BY rowid"))
                .expect("prepare");
            let width = st.column_count();
            let mut rows = st.query([]).expect("query");
            let mut out = Vec::new();
            while let Some(r) = rows.next().expect("row") {
                let mut row = Vec::with_capacity(width);
                for i in 0..width {
                    row.push(match r.get_ref(i).expect("cell") {
                        rusqlite::types::ValueRef::Null => PyValue::None,
                        rusqlite::types::ValueRef::Integer(n) => PyValue::int(i128::from(n)),
                        rusqlite::types::ValueRef::Real(f) => PyValue::Float(f),
                        rusqlite::types::ValueRef::Text(b) => {
                            PyValue::str(std::str::from_utf8(b).expect("utf-8"))
                        }
                        rusqlite::types::ValueRef::Blob(_) => panic!("a blob in {table}"),
                    });
                }
                out.push(PyValue::List(row));
            }
            return Ok(Answer::Table(out));
        }
        other => panic!("call {other} is not run here"),
    }))
}

/// `scrubbed`: a store's path in a RuntimeError spelled `<store>`.
fn refused(e: &WriteError) -> Value {
    let (class, text) = refusal(e);
    let text = text.map(|t| match (t.strip_prefix("store "), t.find(" carries ")) {
        (Some(_), Some(at)) => format!("store <store>{}", &t[at..]),
        _ => t,
    });
    json!({"class": class, "text": text})
}

fn step_outcome(store: &mut WritableStore, clocks: &mut Clocks, call: &Value) -> Value {
    if call["call"] == "batch" {
        let path = store.path().to_owned();
        let inner = call["steps"].as_array().expect("steps");
        let done = store.batch(clocks, None, |tx| {
            let mut out = Vec::new();
            for c in inner {
                out.push(answer(&one(&mut Target::Tx(tx, &path), c)?));
            }
            Ok::<_, WriteError>(out)
        });
        return match done {
            Ok((out, _)) => json!({"written": true, "returned": out}),
            Err(e) => json!({"written": false, "refusal": refused(&e)}),
        };
    }
    match one(&mut Target::Store(store, clocks), call) {
        Ok(v) => json!({"written": true, "returned": answer(&v)}),
        Err(e) => json!({"written": false, "refusal": refused(&e)}),
    }
}

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("membership-native");
    let root = dir.0.clone();
    let c = root.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    let provider = opened::provider(header);
    let mut store = open_for_writing(&file, &c.join("keys"), &provider)
        .map_err(|e| format!("{name}: the open refused: {e}"))?;
    let parts = differing(&header["opened"], &state(&c, &root, store_id, pragmas));
    if !parts.is_empty() {
        return Err(format!("{name}: the opened store differs in {parts:?}"));
    }
    let mut checked = 0;
    for step in case["steps"].as_array().expect("steps") {
        let label = format!("{name} step {}", step["step"]);
        let the_call = &step["call"];
        let mut clocks = Clocks {
            replay: Replay::of(step),
            tokens: Vec::new(),
        };
        let before_seq = written::last_seq(&file);
        let got = step_outcome(&mut store, &mut clocks, the_call);
        if got != step["outcome"] {
            return Err(format!(
                "{label}: got {got}, the reference {}",
                step["outcome"]
            ));
        }
        if !clocks.replay.drained()
            || !clocks.tokens.is_empty()
            || step["reads"] != json!(clocks.replay.order)
        {
            return Err(format!(
                "{label}: reads {:?}, the reference {}",
                clocks.replay.order, step["reads"]
            ));
        }
        let mut rows = written::log_rows(&file, before_seq);
        for row in rows.as_array_mut().expect("rows") {
            if let Some(text) = row[4].as_str().map(str::to_owned) {
                row[4] = spelled(text);
            }
        }
        if rows != step["rows"] {
            return Err(format!(
                "{label}: rows differ\nwant {}\ngot  {rows}",
                step["rows"]
            ));
        }
        let digest = written::state_digest(&state(&c, &root, store_id, pragmas));
        if digest != step["state"] {
            return Err(format!(
                "{label}: the state differs\nwant {}\ngot  {digest}",
                step["state"]
            ));
        }
        let fts = written::fts_view(&file);
        if fts != step["fts"] {
            return Err(format!(
                "{label}: the index differs: want {}, got {fts}",
                step["fts"]
            ));
        }
        checked += 1;
    }
    store.close().map_err(|e| format!("{name}: close: {e}"))?;
    let got = written::refold(&file);
    if got != case["refold"] {
        return Err(format!(
            "{name}: refold {got}, the reference {}",
            case["refold"]
        ));
    }
    Ok(checked)
}

#[test]
fn every_native_membership_call_answers_as_the_reference() {
    let mut cases = jsonl("store_membership_native.jsonl");
    let header = cases.remove(0);
    let mut failures = Vec::new();
    let mut calls = 0;
    for case in &cases {
        match run(case, &header) {
            Ok(n) => calls += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(calls > 0 || !failures.is_empty(), "no call checked");
    let steps: usize = cases
        .iter()
        .map(|c| c["steps"].as_array().map_or(0, Vec::len))
        .sum();
    assert!(
        !failures.is_empty() || calls == steps,
        "{calls} calls checked of the file's {steps}"
    );
    eprintln!(
        "store_membership_native.jsonl: {} cases, {calls} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_native_membership_call_answers_as_the_reference",
        "store_membership_native.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
