//! Every case of `eval_model_split.jsonl` (not gated) against the native
//! `eval_model::split`: `surface_rule_split` and `_with_rule_split` over
//! each hand-built log in its window, then the seeded sweep by line count
//! and SHA-256. The spelling is the generator's
//! (`tests/contract/gen_eval_model.py`, `generate_split`): a tuple is
//! `{"$t": [...]}`, an int past 64 bits `{"$i": "<decimal>"}`, a bound is
//! `fromisoformat` text (naive when it has no offset), and an answer's
//! rate dicts are `[numerator, denominator]` in its compact text, without
//! `note`.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_model::{self as em, EvalModelError};
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_counters as rc;
use bettermemory_core::recall_state::RULE;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn text(s: &[u32]) -> String {
    s.iter()
        .map(|&c| char::from_u32(c).expect("scalar"))
        .collect()
}

/// A JSON value as the generator spelled it, tags read back.
fn value(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) if n.is_f64() => PyValue::Float(n.as_f64().expect("float")),
        Value::Number(n) => PyValue::Int(n.to_string()),
        Value::String(s) => PyValue::Str(cps(s)),
        Value::Array(items) => PyValue::List(items.iter().map(value).collect()),
        Value::Object(map) => {
            if map.len() == 1 {
                if let Some(Value::Array(items)) = map.get("$t") {
                    return PyValue::Tuple(items.iter().map(value).collect());
                }
                if let Some(Value::String(digits)) = map.get("$i") {
                    return PyValue::Int(digits.clone());
                }
            }
            PyValue::Dict(
                map.iter()
                    .map(|(k, v)| (PyValue::Str(cps(k)), value(v)))
                    .collect(),
            )
        }
    }
}

/// A bound as `datetime.fromisoformat` reads the generator's text.
fn bound(v: &Value) -> Option<DateTime> {
    v.as_str()
        .map(|t| DateTime::from_isoformat(&cps(t)).expect("a bound parses"))
}

const RATE_KEYS: [&str; 6] = [
    "numerator",
    "denominator",
    "rate",
    "ci95_lower",
    "ci95_upper",
    "torn_read",
];

/// `gen_recall_counters._compact`: each rate dict as `[numerator,
/// denominator]`, `note` left out.
fn compact(v: &PyValue) -> PyValue {
    match v {
        PyValue::Dict(items) => {
            let keys: Vec<String> = items
                .iter()
                .map(|(k, _)| match k {
                    PyValue::Str(s) => text(s),
                    other => panic!("a str key, not {other:?}"),
                })
                .collect();
            if keys == RATE_KEYS {
                return PyValue::List(vec![items[0].1.clone(), items[1].1.clone()]);
            }
            PyValue::Dict(
                items
                    .iter()
                    .zip(&keys)
                    .filter(|(_, k)| *k != "note")
                    .map(|((k, v), _)| (k.clone(), compact(v)))
                    .collect(),
            )
        }
        PyValue::List(items) => PyValue::List(items.iter().map(compact).collect()),
        other => other.clone(),
    }
}

const COMPACT: DumpOptions = DumpOptions {
    item_separator: ",",
    key_separator: ":",
    ..DumpOptions::DEFAULT
};

fn spell(v: &PyValue, options: DumpOptions) -> String {
    text(&dumps(v, options).expect("the answer dumps"))
}

/// The split as the list `[by_rule, membership_delta]`.
fn split(
    events: &[PyValue],
    start: Option<&DateTime>,
    end: Option<&DateTime>,
) -> Result<Option<PyValue>, EvalModelError> {
    let (by_rule, delta) = em::surface_rule_split(events, start, end)?;
    Ok(Some(PyValue::List(vec![by_rule, delta.to_dict()])))
}

/// The row's `out`: null, `{"x": class}` or the compact text.
fn out(answer: Result<Option<PyValue>, EvalModelError>) -> Value {
    match answer {
        Ok(None) => Value::Null,
        Ok(Some(v)) => json!(spell(&compact(&v), COMPACT)),
        Err(e) => json!({ "x": e.name() }),
    }
}

fn check_row(r: &Value) {
    let events: Vec<PyValue> = r["ev"].as_array().unwrap().iter().map(value).collect();
    let (start, end) = (bound(&r["start"]), bound(&r["end"]));
    let answer = match r["k"].as_str().unwrap() {
        "split" => split(&events, start.as_ref(), end.as_ref()),
        "with" => {
            let spelled = &r["surfaces"];
            let surfaces = if spelled.is_null() {
                None
            } else if spelled.get("report").is_some() {
                rc::compute_surface_report::<EvalModelError>(&events, start, end, None)
                    .expect("the log's section")
            } else if let Some(pairs) = spelled.get("pairs") {
                let pairs = pairs.as_array().unwrap().iter();
                let pairs = pairs.map(|p| (value(&p[0]), value(&p[1])));
                Some(PyValue::Dict(pairs.collect()))
            } else {
                Some(value(&spelled["value"]))
            };
            em::with_rule_split(surfaces.as_ref(), &events, start.as_ref(), end.as_ref())
        }
        other => panic!("unknown row kind {other}"),
    };
    assert_eq!(out(answer), r["out"], "{}: {}", r["k"], r["name"]);
}

/// The generator's `_Draw`.
struct Draw(u64);

impl Draw {
    fn nxt(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }

    fn pick(&mut self, n: usize) -> usize {
        (self.nxt() % n as u64) as usize
    }
}

fn sweep_answer(answer: Result<Option<PyValue>, EvalModelError>) -> String {
    match answer {
        Ok(None) => "~".to_string(),
        Ok(Some(v)) => spell(&v, DumpOptions::DEFAULT),
        Err(e) => format!("!{}", e.name()),
    }
}

fn check_sweep(r: &Value) -> usize {
    let pools: Vec<(String, Vec<Option<PyValue>>)> = r["pools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| {
            let pool = p[1].as_array().unwrap().iter();
            let pool = pool.map(|v| (v.as_str() != Some("~")).then(|| value(v)));
            (p[0].as_str().unwrap().to_string(), pool.collect())
        })
        .collect();
    let windows = r["windows"].as_array().unwrap();
    let mut d = Draw(r["seed"].as_u64().unwrap());
    let mut lines: Vec<String> = Vec::new();
    let count = r["count"].as_u64().unwrap();
    for i in 0..count {
        let n = 3 + d.pick(14);
        let mut events = Vec::with_capacity(n);
        for _ in 0..n {
            let mut fields = Vec::new();
            for (key, pool) in &pools {
                if let Some(v) = &pool[d.pick(pool.len())] {
                    fields.push((PyValue::Str(cps(key)), v.clone()));
                }
            }
            events.push(PyValue::Dict(fields));
        }
        let w = d.pick(windows.len());
        let (start, end) = (bound(&windows[w][0]), bound(&windows[w][1]));
        let (s, e) = (start.as_ref(), end.as_ref());
        lines.push(format!(
            "{i} w={w} s={}",
            sweep_answer(split(&events, s, e))
        ));
        let with = rc::compute_surface_report::<EvalModelError>(&events, start, end, None)
            .and_then(|section| em::with_rule_split(section.as_ref(), &events, s, e));
        lines.push(format!("{i} w={w} t={}", sweep_answer(with)));
    }
    assert!(count > 0, "an empty sweep");
    assert_eq!(
        lines.len() as u64,
        r["lines"].as_u64().unwrap(),
        "sweep lines"
    );
    let digest = Sha256::digest(lines.join("\n").as_bytes());
    assert_eq!(
        common::hex(&digest),
        r["sha256"].as_str().unwrap(),
        "sweep digest"
    );
    count as usize
}

#[test]
fn eval_model_split_every_case() {
    let lines = common::jsonl("eval_model_split.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "eval_model_split");
    assert_eq!(header["rule"], RULE);
    assert_eq!(header["earlier"], em::EARLIER_RULES);
    assert_eq!(header["surfaces"], json!(rc::SURFACES));
    let mut checked = 0;
    let mut sweeps = 0;
    for r in &lines[1..] {
        if r["k"] == "sweep" {
            assert!(check_sweep(r) > 0);
            sweeps += 1;
        } else {
            check_row(r);
        }
        checked += 1;
    }
    assert_eq!(sweeps, 1, "one sweep");
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("eval_model_split.jsonl", checked, in_file);
}
