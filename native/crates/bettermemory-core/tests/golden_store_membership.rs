//! The membership tables' store methods against `store_membership.jsonl`:
//! each case replayed from the store `store_create.json` pins, its steps
//! as `gen_store_membership._run` runs them (a batch's inner calls inside
//! one transaction), each answer compared as the reference's
//! `encoded_json` text (or its count and SHA-256 past 40 rows), then the
//! step's log rows, clock reads, state digest and index, and the refold
//! at the end. A step the native build refuses (a surrogate's native
//! write) ends its case's replay. POSIX only, as the fixture is;
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

/// The step's clocks; no store id is drawn.
struct Clocks {
    replay: Replay,
}

impl Provider for Clocks {
    fn store_id(&mut self) -> io::Result<String> {
        self.replay.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.replay.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.replay.membership_store_id()
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

/// `answer`: the value's `encoded_json`, or past 40 rows its count and
/// digest.
fn answer(value: &PyValue) -> Value {
    let text = json::dumps(value, DumpOptions::SORTED_ASCII_COMPACT).expect("dumps");
    let text = unicode::to_string(&text).expect("ascii");
    match value {
        PyValue::List(rows) if rows.len() > 40 => {
            json!({"count": rows.len(), "sha256": sha(text.as_bytes())})
        }
        _ => Value::String(text),
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

fn one(t: &mut Target<'_, '_>, call: &Value) -> Result<PyValue, WriteError> {
    let session = call["session"].as_str();
    let what = call["call"].as_str().expect("call");
    Ok(match what {
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
        other => panic!("call {other} is not run here"),
    })
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
    let dir = scratch("membership");
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
    let mut whole = true;
    for step in case["steps"].as_array().expect("steps") {
        let label = format!("{name} step {}", step["step"]);
        let the_call = &step["call"];
        if the_call.get("native").and_then(Value::as_str) == Some("reference_only") {
            whole = false;
            break;
        }
        let mut clocks = Clocks {
            replay: Replay::of(step),
        };
        let before_seq = written::last_seq(&file);
        let got = step_outcome(&mut store, &mut clocks, the_call);
        if got != step["outcome"] {
            return Err(format!(
                "{label}: got {got}, the reference {}",
                step["outcome"]
            ));
        }
        if !clocks.replay.drained() || step["reads"] != json!(clocks.replay.order) {
            return Err(format!(
                "{label}: reads {:?}, the reference {}",
                clocks.replay.order, step["reads"]
            ));
        }
        let rows = written::log_rows(&file, before_seq);
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
    if whole {
        let got = written::refold(&file);
        if got != case["refold"] {
            return Err(format!(
                "{name}: refold {got}, the reference {}",
                case["refold"]
            ));
        }
    }
    Ok(checked)
}

#[test]
fn every_membership_call_answers_as_the_reference() {
    let mut cases = jsonl("store_membership.jsonl");
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
    eprintln!(
        "store_membership.jsonl: {} cases, {calls} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_membership_call_answers_as_the_reference",
        "store_membership.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
