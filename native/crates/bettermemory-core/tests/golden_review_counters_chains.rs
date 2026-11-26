//! Every row of `review_counters_chains.jsonl` against
//! `review_counters::chains`: `build_chains`, `chains_in_window`, the
//! `_Tally` of a window and its `to_dict`, the rates, `_share`, `_median`
//! and `_iso_week`; and the sweep of drawn logs by count and digest, drawn
//! here by the generator's rule (`_chain_log`).

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::review::ACTION_COMMIT;
use bettermemory_core::review_counters::{self as rc, CountersError, ReviewChain, Tally};
use bettermemory_core::unicode::{self, cps};
use common::parity::Ledger;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "review_counters_chains.jsonl";

/// An ASCII text (every text of the file is).
fn t(s: &[u32]) -> Value {
    Value::String(
        unicode::to_string(s)
            .filter(|s| s.is_ascii())
            .expect("ASCII"),
    )
}

/// A JSON value of the header's logs as the Python value `json` gives.
fn pv(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => PyValue::int(i128::from(n.as_i64().expect("an int"))),
        Value::String(s) => PyValue::str(s),
        Value::Array(items) => PyValue::List(items.iter().map(pv).collect()),
        Value::Object(map) => {
            PyValue::Dict(map.iter().map(|(k, v)| (PyValue::str(k), pv(v))).collect())
        }
    }
}

/// An event of a log: a JSON text when it is a string opening a dict.
fn event(v: &Value) -> PyValue {
    match v.as_str() {
        Some(text) if text.starts_with('{') => json::loads(&cps(text)).expect("JSON"),
        _ => pv(v),
    }
}

fn log(header: &Value, name: &str) -> Vec<PyValue> {
    header["logs"][name]
        .as_array()
        .expect("a log")
        .iter()
        .map(event)
        .collect()
}

fn iso(v: &Value) -> Option<DateTime> {
    v.as_str().map(|text| {
        let dt = DateTime::from_isoformat(&cps(text)).expect("an isoformat");
        assert_eq!(dt.isoformat(), text);
        dt
    })
}

/// The generator's `_chain`.
fn chain(c: &ReviewChain) -> Value {
    let ids: Vec<Value> = c.review_ids().iter().map(|s| t(s)).collect();
    let rel: Vec<Value> = c.related().iter().map(|s| t(s)).collect();
    let rows: Vec<Value> = c
        .issued
        .iter()
        .map(|r| json!([t(&r.review_id), r.ts.isoformat()]))
        .collect();
    json!({
        "key": t(&c.key),
        "ids": ids,
        "rel": rel,
        "rows": rows,
        "state": c.state.as_str(),
        "closed": c.closed,
        "absorbed": c.absorbed,
        "retries": c.standing_retries,
        "calls": c.calls(),
        "d": c.decided.as_ref().map_or(Value::Null, |d| {
            json!([t(&d.review_id), d.ts.isoformat(), t(&d.action)])
        }),
        "out": c.outcome().map_or(Value::Null, |a| t(a)),
        "session": c.session().map_or(Value::Null, |s| t(s)),
        "client": t(c.client()),
        "model": t(c.model()),
        "pre": c.pre_answered(),
    })
}

/// The generator's `_spell`: a dict as its pairs, a float as its repr.
fn spell(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Int(text) => Value::Number(text.parse().expect("a count")),
        PyValue::Float(x) => Value::String(float_repr::repr(*x)),
        PyValue::Str(s) => t(s),
        PyValue::Dict(items) => Value::Array(
            items
                .iter()
                .map(|(k, v)| json!([spell(k), spell(v)]))
                .collect(),
        ),
        other => panic!("not in a tally: {other:?}"),
    }
}

fn x(e: CountersError) -> Value {
    json!({ "x": e.name() })
}

fn chains_of(r: Result<Vec<ReviewChain>, CountersError>) -> Value {
    r.map_or_else(x, |cs| Value::Array(cs.iter().map(chain).collect()))
}

fn window_tally(
    events: &[PyValue],
    start: Option<&DateTime>,
    end: Option<&DateTime>,
    now: &DateTime,
    chars: Vec<String>,
) -> Value {
    let run = || -> Result<Value, CountersError> {
        let mut tally = Tally::default();
        for c in rc::chains_in_window(events, start, end, now)? {
            tally.add(&c);
        }
        tally.response_chars.extend(chars);
        Ok(spell(&tally.to_dict()?))
    };
    run().unwrap_or_else(x)
}

fn texts(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("texts")
        .iter()
        .map(|s| s.as_str().expect("a text").to_string())
        .collect()
}

fn check(row: &Value, header: &Value) -> Value {
    let logged = || log(header, row["log"].as_str().expect("a log name"));
    let now = || iso(&row["now"]).expect("now");
    match row["k"].as_str().expect("a kind") {
        "chains" => chains_of(rc::build_chains(
            &logged(),
            &now(),
            iso(&row["horizon"]).as_ref(),
        )),
        "window" => chains_of(rc::chains_in_window(
            &logged(),
            iso(&row["start"]).as_ref(),
            iso(&row["end"]).as_ref(),
            &now(),
        )),
        "tally" => window_tally(
            &logged(),
            iso(&row["start"]).as_ref(),
            iso(&row["end"]).as_ref(),
            &now(),
            texts(&row["chars"]),
        ),
        "rate" => {
            let mut tally = Tally::default();
            tally
                .actions
                .add(&cps(ACTION_COMMIT), row["committed"].as_u64().expect("n"));
            for pair in row["filings"].as_array().expect("filings") {
                let detector = cps(pair[0].as_str().expect("a detector"));
                tally.filings.add(&detector, pair[1].as_u64().expect("n"));
            }
            tally.committed_calls = row["calls"].as_u64().expect("calls");
            let dict = tally.to_dict().expect("no median");
            json!([
                spell(&tally.filings_per_100()),
                spell(dict.get("calls_per_committed_write").expect("a key")),
            ])
        }
        "share" => {
            let n = row["n"].as_u64().expect("n");
            let d = row["d"].as_u64().expect("d");
            rc::share(n, d).map_or(Value::Null, |f| Value::String(float_repr::repr(f)))
        }
        "median" => match rc::median(&texts(&row["v"])) {
            Ok(m) => m.map_or(Value::Null, |f| Value::String(float_repr::repr(f))),
            Err(e) => x(e),
        },
        "week" => rc::iso_week(&iso(&row["ts"]).expect("a time")).map_or_else(x, Value::String),
        other => panic!("unknown row kind {other}"),
    }
}

#[test]
fn review_counters_chains_every_row() {
    let lines = common::jsonl(NAME);
    let header = &lines[0];
    assert_eq!(
        header["count"].as_u64().expect("count") as usize,
        lines.len() - 1
    );
    assert_eq!(header["answer_window_us"], json!(rc::ANSWER_WINDOW_US));
    let rows: Vec<&Value> = lines[1..].iter().filter(|r| r["k"] != "sweep").collect();
    assert_eq!(rows.len() + 2, lines.len(), "one sweep row");
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new("golden_review_counters_chains", NAME);
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

    fn of<'a>(&mut self, pool: &[&'a str]) -> &'a str {
        pool[self.pick(pool.len() as u64) as usize]
    }
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        pairs
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn strs(items: &[&str]) -> PyValue {
    PyValue::List(items.iter().map(|s| PyValue::str(s)).collect())
}

/// The generator's `_sw_ts`.
fn sweep_ts(draw: &mut Draw) -> String {
    let minute = [0, 5, 29, 30, 31, 45, 60, 61][draw.pick(8) as usize];
    let us = draw.pick(2);
    let offset = draw.of(&["Z", "+01:00"]);
    let hour = 10 + minute / 60 + u64::from(offset == "+01:00");
    format!("2026-11-21T{hour:02}:{:02}:00.{us:06}{offset}", minute % 60)
}

/// The generator's `_chain_log`.
fn chain_log(draw: &mut Draw) -> Vec<PyValue> {
    let mut events = Vec::new();
    for _ in 0..1 + draw.pick(8) {
        let write = draw.pick(3) == 2;
        let rid = draw.of(&["a", "b", "c", "d"]);
        let ts = sweep_ts(draw);
        let session = draw.of(&["s1", "s2"]);
        let mut ev = vec![
            ("ts", PyValue::str(&ts)),
            ("review_id", PyValue::str(rid)),
            ("session", PyValue::str(session)),
        ];
        let keys: Vec<&str> = ["x", "y"]
            .into_iter()
            .filter(|_| draw.pick(2) == 1)
            .collect();
        if write {
            ev.push(("kind", PyValue::str("write")));
            ev.push(("status", PyValue::str("committed")));
            let action = draw.of(&[
                "committed",
                "committed",
                "refused_standing",
                "refused_secret",
                "odd",
            ]);
            ev.push(("review_action", PyValue::str(action)));
            let relation = dict(
                keys.iter()
                    .map(|k| (*k, PyValue::str("unrelated")))
                    .collect(),
            );
            let second = PyValue::int(i128::from(draw.pick(2)));
            ev.push((
                "review_answers",
                dict(vec![
                    ("relation", relation),
                    ("unsafe", PyValue::int(0)),
                    ("secret", second),
                ]),
            ));
            match draw.pick(3) {
                0 => {}
                i => ev.push((
                    "review_filings",
                    PyValue::List(vec![dict(vec![
                        ("id", PyValue::str("t")),
                        ("detector", PyValue::str(["review", "zz"][i as usize - 1])),
                    ])]),
                )),
            }
            if draw.pick(2) == 1 {
                ev.push(("supersedes_reviewed", strs(&["l"])));
            }
        } else {
            ev.push(("kind", PyValue::str("review_issued")));
            match draw.pick(3) {
                0 => {}
                i => ev.push(("parent_review_id", PyValue::str(["a", "b"][i as usize - 1]))),
            }
            ev.push(("related", strs(&keys)));
            ev.push(("pre_answered", PyValue::Bool(draw.pick(3) == 0)));
            if draw.pick(6) == 0 {
                ev.push(("roster", strs(&["m"])));
            }
        }
        events.push(dict(ev));
    }
    events
}

fn at_minutes(m: u32) -> DateTime {
    DateTime::new(2026, 11, 21, 10 + m / 60, m % 60, 0, 0, Some(0)).expect("a time")
}

/// The generator's `_sweep_answer`, as its JSON line.
fn sweep_answer(events: &[PyValue]) -> String {
    let (start, end, now) = (at_minutes(0), at_minutes(45), at_minutes(70));
    let mut out = Map::new();
    out.insert(
        "b".into(),
        chains_of(rc::build_chains(events, &at_minutes(50), None)),
    );
    out.insert(
        "w".into(),
        chains_of(rc::chains_in_window(events, Some(&start), Some(&end), &now)),
    );
    out.insert(
        "t".into(),
        window_tally(events, Some(&start), Some(&end), &now, Vec::new()),
    );
    serde_json::to_string(&Value::Object(out)).expect("JSON")
}

#[test]
fn review_counters_chains_sweep() {
    let lines = common::jsonl(NAME);
    let sweep = lines.last().expect("a sweep row");
    assert_eq!(sweep["k"], "sweep");
    let count = sweep["count"].as_u64().expect("count");
    assert!(count > 0);
    let mut draw = Draw(sweep["seed"].as_u64().expect("seed"));
    let mut sha = Sha256::new();
    for _ in 0..count {
        let line = sweep_answer(&chain_log(&mut draw));
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
