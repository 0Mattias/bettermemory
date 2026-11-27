//! Every row of `review_counters_report.jsonl` against
//! `review_counters::compute_review_report` and `ReviewReport::to_dict`,
//! and the sweep of drawn logs by count and digest, drawn here by the
//! generator's rule (`_report_log` over `_chain_log`).

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::review_counters::{self as rc, CountersError};
use bettermemory_core::unicode::{self, cps};
use common::parity::Ledger;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "review_counters_report.jsonl";

/// The generator's `_t`: an ASCII text as itself, otherwise its code
/// points.
fn t(s: &[u32]) -> Value {
    match unicode::to_string(s).filter(|s| s.is_ascii()) {
        Some(text) => Value::String(text),
        None => json!({ "cps": s }),
    }
}

/// A JSON value of the header's logs as the Python value `json` gives.
fn pv(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => PyValue::int(i128::from(i)),
            None => PyValue::Float(n.as_f64().expect("a float")),
        },
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

fn iso(v: &Value) -> Option<DateTime> {
    v.as_str().map(|text| {
        let dt = DateTime::from_isoformat(&cps(text)).expect("an isoformat");
        assert_eq!(dt.isoformat(), text);
        dt
    })
}

/// The generator's `_rspell`.
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
        other => panic!("not in a report: {other:?}"),
    }
}

fn x(e: CountersError) -> Value {
    json!({ "x": e.name() })
}

/// The generator's `_report`.
fn report(
    events: &[PyValue],
    start: Option<&DateTime>,
    end: Option<&DateTime>,
    now: &DateTime,
) -> Value {
    rc::compute_review_report(events, start, end, now)
        .and_then(|r| r.to_dict())
        .map_or_else(x, |d| spell(&d))
}

#[test]
fn review_counters_report_every_row() {
    let lines = common::jsonl(NAME);
    let header = &lines[0];
    assert_eq!(
        header["count"].as_u64().expect("count") as usize,
        lines.len() - 1
    );
    let rows: Vec<&Value> = lines[1..].iter().filter(|r| r["k"] != "sweep").collect();
    assert_eq!(rows.len() + 2, lines.len(), "one sweep row");
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new("golden_review_counters_report", NAME);
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        assert_eq!(row["k"], "report");
        let log: Vec<PyValue> = header["logs"][row["log"].as_str().expect("a log name")]
            .as_array()
            .expect("a log")
            .iter()
            .map(event)
            .collect();
        let got = report(
            &log,
            iso(&row["start"]).as_ref(),
            iso(&row["end"]).as_ref(),
            &iso(&row["now"]).expect("now"),
        );
        let want = &row["r"];
        if ledger.differs_fatally(format!("row {i}"), got == *want) {
            failures.push(format!(
                "row {i} {}\n native    {got}\n reference {want}",
                row["log"]
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

/// The generator's `_report_log`.
fn report_log(draw: &mut Draw) -> Vec<PyValue> {
    let mut events = chain_log(draw);
    for ev in &mut events {
        let PyValue::Dict(items) = ev else {
            unreachable!("a drawn event is a dict")
        };
        let issued = items
            .iter()
            .any(|(k, v)| *k == PyValue::str("kind") && *v == PyValue::str("review_issued"));
        items.push((
            PyValue::str("client"),
            PyValue::str(draw.of(&["c2", "c1", "c10"])),
        ));
        items.push((PyValue::str("model"), PyValue::str(draw.of(&["m2", "m1"]))));
        if issued {
            let chars = match draw.pick(5) {
                0 => None,
                1 => Some(PyValue::int(5)),
                2 => Some(PyValue::int(12)),
                3 => Some(PyValue::int(40)),
                _ => Some(PyValue::Bool(true)),
            };
            if let Some(chars) = chars {
                items.push((PyValue::str("response_chars"), chars));
            }
        }
    }
    for _ in 0..draw.pick(3) {
        let rid = draw.of(&["a", "b", "c", "d"]);
        let ts = sweep_ts(draw);
        let sample_ts = sweep_ts(draw);
        let right_answers = draw.pick(2) == 1;
        let right_action = draw.pick(2) == 1;
        let pre = draw.pick(2) == 1;
        events.push(dict(vec![
            ("kind", PyValue::str("review_grade")),
            ("review_id", PyValue::str(rid)),
            ("ts", PyValue::str(&ts)),
            ("sample_ts", PyValue::str(&sample_ts)),
            ("right_answers", PyValue::Bool(right_answers)),
            ("right_action", PyValue::Bool(right_action)),
            ("pre_answered", PyValue::Bool(pre)),
        ]));
    }
    events
}

/// The generator's `_report_answer`, as its JSON line.
fn report_answer(events: &[PyValue]) -> String {
    let (start, end, now) = (at_minutes(0), at_minutes(45), at_minutes(70));
    let mut out = Map::new();
    out.insert("w".into(), report(events, Some(&start), Some(&end), &now));
    out.insert("a".into(), report(events, None, None, &at_minutes(50)));
    serde_json::to_string(&Value::Object(out)).expect("JSON")
}

#[test]
fn review_counters_report_sweep() {
    let lines = common::jsonl(NAME);
    let sweep = lines.last().expect("a sweep row");
    assert_eq!(sweep["k"], "sweep");
    let pools = &lines[0]["pools"];
    assert_eq!(pools["clients"], json!(["c2", "c1", "c10"]));
    assert_eq!(pools["models"], json!(["m2", "m1"]));
    assert_eq!(pools["chars"], json!([null, 5, 12, 40, true]));
    let count = sweep["count"].as_u64().expect("count");
    assert!(count > 0);
    let mut draw = Draw(sweep["seed"].as_u64().expect("seed"));
    let mut sha = Sha256::new();
    for _ in 0..count {
        let line = report_answer(&report_log(&mut draw));
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
