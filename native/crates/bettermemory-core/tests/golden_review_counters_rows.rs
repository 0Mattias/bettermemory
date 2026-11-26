//! Every row of `review_counters_rows.jsonl` against `review_counters`:
//! the tables, `_str`, `_ids`, `_score`, `_session_of`, `_bucket_value`,
//! `IssuedRow`, `DecidedRow` and `GradeRow` (`from_event` and their
//! properties) and `is_review_row`; and the sweep of drawn events by count
//! and digest, drawn here by the generator's rule from the header's pools.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::json;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::review::{REVIEW_ISSUED, UNDECLARED};
use bettermemory_core::review_counters::{
    self as rc, CountersError, DecidedRow, GradeRow, IssuedRow,
};
use bettermemory_core::unicode;
use common::neutral;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "review_counters_rows.jsonl";

/// `_t`: ASCII text as itself, else its code points.
fn t(s: &[u32]) -> Value {
    if s.iter().all(|&c| c < 0x80) {
        Value::String(unicode::to_string(s).expect("ASCII"))
    } else {
        json!({ "cps": s })
    }
}

fn ot(s: Option<&PyStr>) -> Value {
    s.map_or(Value::Null, |s| t(s))
}

fn list(items: &[PyStr]) -> Value {
    Value::Array(items.iter().map(|s| t(s)).collect())
}

fn pairs(items: &[(PyStr, PyStr)]) -> Value {
    Value::Array(items.iter().map(|(a, b)| json!([t(a), t(b)])).collect())
}

fn dt(d: Option<&DateTime>) -> Value {
    d.map_or(Value::Null, |d| Value::String(d.isoformat()))
}

fn f(x: Option<f64>) -> Value {
    x.map_or(Value::Null, |x| Value::String(float_repr::repr(x)))
}

fn tried<T>(r: Result<T, CountersError>, spell: impl FnOnce(T) -> Value) -> Value {
    match r {
        Ok(v) => spell(v),
        Err(e) => json!({ "x": e.name() }),
    }
}

fn issued(row: Option<IssuedRow>) -> Value {
    let Some(row) = row else { return Value::Null };
    json!({
        "id": t(&row.review_id),
        "parent": ot(row.parent_review_id.as_ref()),
        "ts": dt(Some(&row.ts)),
        "session": ot(row.session.as_ref()),
        "client": t(&row.client),
        "model": t(&row.model),
        "related": list(&row.related),
        "pre": row.pre_answered,
        "chars": row.response_chars.clone().map_or(Value::Null, Value::String),
        "roster": list(&row.roster),
        "chain": t(row.chain_key()),
    })
}

fn decided(row: Option<DecidedRow>) -> Value {
    let Some(row) = row else { return Value::Null };
    json!({
        "id": t(&row.review_id),
        "ts": dt(Some(&row.ts)),
        "session": ot(row.session.as_ref()),
        "client": t(&row.client),
        "status": t(&row.status),
        "action": t(&row.action),
        "relation": pairs(&row.relation),
        "un": f(row.r#unsafe),
        "se": f(row.secret),
        "mem": ot(row.memory_id.as_ref()),
        "links": list(&row.links),
        "upd": list(&row.update_suggested),
        "filings": pairs(&row.filings),
        "cred": list(&row.credential_kinds),
        "matches": list(&row.matches),
        "committed": row.committed(),
        "zero": row.zero_attestation(),
    })
}

fn grade(row: Option<GradeRow>) -> Value {
    let Some(row) = row else { return Value::Null };
    json!({
        "id": t(&row.review_id),
        "ts": dt(Some(&row.ts)),
        "sample": dt(row.sample_ts.as_ref()),
        "ra": row.right_answers,
        "rx": row.right_action,
        "pre": row.pre_answered,
    })
}

/// The generator's `_answer`.
fn answer(ev: &PyValue) -> Value {
    json!({
        "issued": tried(IssuedRow::from_event(ev), issued),
        "decided": tried(DecidedRow::from_event(ev), decided),
        "grade": tried(GradeRow::from_event(ev), grade),
        "is": tried(rc::is_review_row(ev), Value::Bool),
        "session": tried(rc::session_of(ev), |s| ot(s.as_ref())),
        "client": tried(rc::bucket_value(ev, "client"), |s| t(&s)),
        "model": tried(rc::bucket_value(ev, "model"), |s| t(&s)),
    })
}

/// `event[key] = value`: an existing key keeps its place, a new one goes
/// last.
fn set(event: &mut PyValue, key: &str, value: PyValue) {
    let PyValue::Dict(items) = event else {
        panic!("a dict base")
    };
    let k = PyValue::str(key);
    match items.iter().position(|(have, _)| *have == k) {
        Some(i) => items[i].1 = value,
        None => items.push((k, value)),
    }
}

fn event_of(row: &Value, header: &Value) -> PyValue {
    if let Some(ev) = row.get("ev") {
        return neutral(ev);
    }
    if let Some(text) = row.get("json") {
        return json::loads(&unicode::cps(text.as_str().expect("text"))).expect("JSON");
    }
    let base = row["base"].as_str().expect("a base");
    let mut event = neutral(&header["bases"][base]);
    for pair in row
        .get("set")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        set(
            &mut event,
            pair[0].as_str().expect("a key"),
            neutral(&pair[1]),
        );
    }
    for key in row
        .get("drop")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let k = PyValue::str(key.as_str().expect("a key"));
        let PyValue::Dict(items) = &mut event else {
            unreachable!()
        };
        let before = items.len();
        items.retain(|(have, _)| *have != k);
        assert_eq!(items.len() + 1, before, "drop {k:?}");
    }
    event
}

fn check(row: &Value, header: &Value) -> Value {
    match row["k"].as_str().expect("a kind") {
        "ev" => answer(&event_of(row, header)),
        "str" => ot(rc::str_of(Some(&neutral(&row["v"]))).as_ref()),
        "ids" => list(&rc::ids(Some(&neutral(&row["v"])))),
        "score" => tried(rc::score(Some(&neutral(&row["v"]))), f),
        other => panic!("unknown row kind {other}"),
    }
}

#[test]
fn review_counters_rows_every_row() {
    let lines = common::jsonl(NAME);
    let header = &lines[0];
    let rows: Vec<&Value> = lines[1..].iter().filter(|r| r["k"] != "sweep").collect();
    assert_eq!(
        header["count"].as_u64().expect("count") as usize,
        lines.len() - 1
    );
    assert_eq!(rows.len() + 2, lines.len(), "one sweep row");
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new("golden_review_counters_rows", NAME);
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        let got = check(row, header);
        let want = &row["r"];
        if ledger.differs_fatally(format!("row {i}"), got == *want) {
            failures.push(format!(
                "row {i} {row}\n native    {got}\n reference {want}"
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{NAME}: {} rows differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    ledger.finish(rows.len());
}

#[test]
fn review_counters_rows_constants() {
    let lines = common::jsonl(NAME);
    let h = &lines[0];
    assert_eq!(h["answer_window_us"], json!(rc::ANSWER_WINDOW_US));
    assert_eq!(h["review_grade"], rc::REVIEW_GRADE);
    assert_eq!(h["review_issued"], REVIEW_ISSUED);
    assert_eq!(h["undeclared"], UNDECLARED);
    assert_eq!(h["refusal_actions"], json!(rc::REFUSAL_ACTIONS));
    assert_eq!(h["detectors"], json!(rc::DETECTORS));
    let table: Vec<[&str; 2]> = rc::STATUS_ACTION.iter().map(|&(k, v)| [k, v]).collect();
    assert_eq!(h["status_action"], json!(table));
}

/// The generator's `_Draw`.
struct Draw(u64);

impl Draw {
    fn pick(&mut self, n: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 33) % n
    }
}

#[test]
fn review_counters_rows_sweep() {
    let lines = common::jsonl(NAME);
    let pools: Vec<(PyValue, Vec<PyValue>)> = lines[0]["pools"]
        .as_array()
        .expect("pools")
        .iter()
        .map(|p| {
            let values = p[1]
                .as_array()
                .expect("a pool")
                .iter()
                .map(neutral)
                .collect();
            (PyValue::str(p[0].as_str().expect("a field")), values)
        })
        .collect();
    let sweep = lines.last().expect("a sweep row");
    assert_eq!(sweep["k"], "sweep");
    let count = sweep["count"].as_u64().expect("count");
    assert!(count > 0);
    let mut draw = Draw(sweep["seed"].as_u64().expect("seed"));
    let mut sha = Sha256::new();
    for _ in 0..count {
        let mut items = Vec::new();
        for (key, pool) in &pools {
            let i = draw.pick(pool.len() as u64 + 1) as usize;
            if i < pool.len() {
                items.push((key.clone(), pool[i].clone()));
            }
        }
        let line = serde_json::to_string(&answer(&PyValue::Dict(items))).expect("JSON");
        assert!(line.is_ascii());
        sha.update(line.as_bytes());
        sha.update(b"\n");
    }
    let digest: String = sha.finalize().iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(
        digest,
        sweep["sha256"].as_str().expect("sha256"),
        "the sweep of {count}"
    );
}
