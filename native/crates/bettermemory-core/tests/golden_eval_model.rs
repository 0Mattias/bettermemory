//! Every row of `eval_model.jsonl` and `eval_model_python.jsonl` against
//! `eval_model`: `RateCI.from_counts` and `to_dict`, `_wilson_interval`,
//! `ColdEndorsementMemoriesRow`, `parse_since`, `parse_timestamp_utc`,
//! `parse_window_bound`, `resolve_window`, `_show_in_window`,
//! `_canonical_top_hit_id` and `_silent_miss_from_event` (as `to_dict`);
//! the header's constants; and the sweep of drawn counts through
//! `RateCI.from_counts` by count and digest, drawn here by the
//! generator's rule.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_model::{self as em, Bound, EvalModelError};
use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{neutral, text_field};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "eval_model.jsonl";
const PYTHON: &str = "eval_model_python.jsonl";

/// `_common.neutral` of a value `to_dict` answers.
fn to_neutral(v: &PyValue) -> Value {
    match v {
        PyValue::None => json!({"t": "null"}),
        PyValue::Bool(b) => json!({"t": "bool", "v": b}),
        PyValue::Int(t) => json!({"t": "int", "v": t}),
        PyValue::Float(f) => float_neutral(*f),
        PyValue::Str(s) => match unicode::to_string(s) {
            Some(text) => json!({"t": "str", "v": text}),
            None => json!({"t": "str", "v_cps": s}),
        },
        PyValue::List(items) => {
            json!({"t": "list", "v": items.iter().map(to_neutral).collect::<Vec<_>>()})
        }
        other => panic!("no answer holds {other:?}"),
    }
}

fn float_neutral(f: f64) -> Value {
    json!({"t": "float", "bits": format!("{:016x}", f.to_bits()), "repr": float_repr::repr(f)})
}

/// `to_dict`'s values (neutral), its keys checked against the header's.
fn values(d: &PyValue, keys: &Value) -> Value {
    let PyValue::Dict(items) = d else {
        panic!("a dict")
    };
    let got: Vec<Value> = items
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(s) => json!(unicode::to_string(s).expect("an ASCII key")),
            other => panic!("a str key, not {other:?}"),
        })
        .collect();
    assert_eq!(&Value::Array(got), keys, "to_dict's keys");
    Value::Array(items.iter().map(|(_, v)| to_neutral(v)).collect())
}

/// A text as `_common.text_field` writes it under `name`.
fn text_json(name: &str, s: &[u32]) -> Map<String, Value> {
    let mut m = Map::new();
    match unicode::to_string(s) {
        Some(t) => m.insert(name.into(), json!(t)),
        None => m.insert(format!("{name}_cps"), json!(s)),
    };
    m
}

/// An exception as the generator's `_err` writes it.
fn err(e: &EvalModelError) -> Value {
    let text = e.message();
    let mut m = Map::new();
    m.insert("x".into(), json!(e.name()));
    if text.len() > 300 {
        let utf8 = unicode::to_string(&text).expect("a long message without a surrogate");
        m.insert("msg_len".into(), json!(text.len()));
        m.insert(
            "msg_sha256".into(),
            json!(common::hex(&Sha256::digest(utf8.as_bytes()))),
        );
    } else {
        m.extend(text_json("msg", &text));
    }
    Value::Object(m)
}

/// `{"r": answer}` or the exception.
fn answer<T>(r: Result<T, EvalModelError>, spell: impl FnOnce(T) -> Value) -> Value {
    match r {
        Ok(v) => json!({ "r": spell(v) }),
        Err(e) => err(&e),
    }
}

/// The answer keys of a row.
fn want(row: &Value) -> Value {
    let keys = ["r", "x", "msg", "msg_cps", "msg_len", "msg_sha256"];
    let m: Map<String, Value> = keys
        .iter()
        .filter_map(|k| row.get(*k).map(|v| ((*k).to_owned(), v.clone())))
        .collect();
    Value::Object(m)
}

fn fields(dt: &DateTime) -> Value {
    json!([
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.microsecond,
        dt.offset_us
    ])
}

fn opt_fields(dt: Option<DateTime>) -> Value {
    dt.as_ref().map_or(Value::Null, fields)
}

fn dt(v: &Value) -> DateTime {
    let f = v.as_array().expect("datetime fields");
    let u = |i: usize| f[i].as_u64().expect("a field") as u32;
    DateTime::new(
        f[0].as_i64().expect("year"),
        u(1),
        u(2),
        u(3),
        u(4),
        u(5),
        u(6),
        f[7].as_i64(),
    )
    .expect("a datetime")
}

fn opt_dt(v: &Value) -> Option<DateTime> {
    (!v.is_null()).then(|| dt(v))
}

/// A row's spec: `spec` (text, code points or null) or `spec_runs`.
fn spec(row: &Value) -> Option<PyStr> {
    if let Some(runs) = row.get("spec_runs") {
        let mut out = Vec::new();
        for run in runs.as_array().expect("runs") {
            let c = unicode::cps(run[0].as_str().expect("a character"));
            assert_eq!(c.len(), 1);
            out.extend(std::iter::repeat_n(
                c[0],
                run[1].as_u64().expect("a count") as usize,
            ));
        }
        return Some(out);
    }
    if row.get("spec").is_some_and(Value::is_null) {
        return None;
    }
    Some(text_field(row, "spec"))
}

fn bound_json(b: Option<Bound>) -> Value {
    match b {
        None => Value::Null,
        Some(Bound::Span(us)) => json!({"span": us.to_string()}),
        Some(Bound::At(at)) => json!({"at": fields(&at)}),
    }
}

fn bound_of(v: &Value) -> Option<Bound> {
    if v.is_null() {
        return None;
    }
    if let Some(span) = v.get("span") {
        return Some(Bound::Span(
            span.as_str().expect("decimal").parse().expect("i128"),
        ));
    }
    Some(Bound::At(dt(&v["at"])))
}

fn i64_of(v: &Value) -> i64 {
    v.as_i64().expect("an i64")
}

/// The event of an `event` row: `ev`, or the base with `ev_with` applied.
fn event(row: &Value, base: &PyValue) -> PyValue {
    if let Some(ev) = row.get("ev") {
        return neutral(ev);
    }
    let PyValue::Dict(base) = base else {
        panic!("a dict base")
    };
    let mut items = base.clone();
    for pair in row["ev_with"].as_array().expect("ev_with") {
        let key = PyValue::str(pair[0].as_str().expect("a key"));
        let at = items.iter().position(|(k, _)| *k == key);
        match (&pair[1], at) {
            (Value::Null, Some(i)) => {
                items.remove(i);
            }
            (Value::Null, None) => {}
            (v, Some(i)) => items[i].1 = neutral(v),
            (v, None) => items.push((key, neutral(v))),
        }
    }
    PyValue::Dict(items)
}

/// The native answer to a row, in the row's own answer keys.
fn check(row: &Value, header: &Value) -> (Value, Value) {
    let kind = row["k"].as_str().expect("a kind");
    let got = match kind {
        "rate" => {
            let r = em::RateCI::from_counts(i64_of(&row["n"]), i64_of(&row["d"]));
            json!({"r": values(&r.to_dict(), &header["rate_keys"])})
        }
        "wilson" => {
            let z = match neutral(&row["z"]) {
                PyValue::Float(f) => f,
                other => panic!("a float z, not {other:?}"),
            };
            let (lo, hi) = em::wilson_interval(i64_of(&row["kk"]), i64_of(&row["n"]), z);
            json!({"r": [float_neutral(lo), float_neutral(hi)]})
        }
        "cold" => {
            let i = &row["in"];
            let r = em::ColdEndorsementMemoriesRow {
                id: text_field(i, "id"),
                scopes: i["scopes"]
                    .as_array()
                    .expect("scopes")
                    .iter()
                    .map(|s| text_field(s, "v"))
                    .collect(),
                summary: text_field(i, "summary"),
                retrieval_count: i64_of(&i["retrieval_count"]),
                auto_applied_count: i64_of(&i["auto_applied_count"]),
                explicit_applied_count: i64_of(&i["explicit_applied_count"]),
            };
            json!({"r": values(&r.to_dict(), &header["cold_keys"])})
        }
        "since" => answer(em::parse_since(spec(row).as_deref()), |s| {
            s.map_or(Value::Null, |us| json!(us.to_string()))
        }),
        "ts" => answer(
            em::parse_timestamp_utc(&spec(row).expect("a spec"), flag(row)),
            |d| fields(&d),
        ),
        "bound" => answer(
            em::parse_window_bound(spec(row).as_deref(), flag(row), &dt(&row["now"])),
            bound_json,
        ),
        "resolve" => answer(
            em::resolve_window(
                bound_of(&row["since"]).as_ref(),
                bound_of(&row["until"]).as_ref(),
                &dt(&row["now"]),
            ),
            |(s, e)| json!([opt_fields(s), opt_fields(e)]),
        ),
        "window" => answer(
            em::show_in_window(
                &neutral(&row["show"]),
                opt_dt(&row["start"]).as_ref(),
                opt_dt(&row["end"]).as_ref(),
            ),
            |b| json!(b),
        ),
        "event" => {
            let ev = event(row, &neutral(&header["event_base"]));
            let top = answer(em::canonical_top_hit_id(&ev), |r| {
                r.map_or(Value::Null, |s| Value::Object(text_json("v", &s)))
            });
            let miss = answer(em::silent_miss_from_event(&ev), |c| {
                c.map_or(Value::Null, |c| values(&c.to_dict(), &header["miss_keys"]))
            });
            return (
                json!({"top": top, "miss": miss}),
                json!({"top": row["top"], "miss": row["miss"]}),
            );
        }
        other => panic!("unknown kind {other}"),
    };
    (got, want(row))
}

fn flag(row: &Value) -> &str {
    row["flag"].as_str().expect("a flag")
}

fn every_row(test: &str, name: &str, trailer: usize) {
    let lines = common::jsonl(name);
    let header = &lines[0];
    let rows = &lines[1..lines.len() - trailer];
    assert_eq!(
        header["count"].as_u64().expect("count") as usize,
        rows.len()
    );
    let mut ledger = Ledger::new(test, name);
    let mut failures = Vec::new();
    let full = common::jsonl(NAME);
    for (i, row) in rows.iter().enumerate() {
        let (got, want) = check(row, &full[0]);
        if ledger.differs_fatally(format!("row {i}"), got == want) {
            failures.push(format!(
                "row {i} {row}\n native    {got}\n reference {want}"
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{name}: {} rows differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    ledger.finish(rows.len());
}

#[test]
fn eval_model_every_row() {
    every_row("golden_eval_model", NAME, 1);
}

#[test]
fn eval_model_python_every_row() {
    every_row("golden_eval_model_python", PYTHON, 0);
}

#[test]
fn eval_model_constants() {
    let h = &common::jsonl(NAME)[0];
    assert_eq!(h["default_since"], em::DEFAULT_SINCE_SPEC);
    assert_eq!(h["min_retrievals"], em::DEFAULT_ENDORSEMENT_MIN_RETRIEVALS);
    assert_eq!(h["silent_miss_limit"], em::DEFAULT_SILENT_MISS_LIMIT);
    assert_eq!(h["wilson_z"], float_neutral(em::WILSON_Z));
    assert_eq!(h["since_pattern"], em::SINCE_PATTERN);
    assert_eq!(
        h["since_flags"], 32,
        "re.UNICODE, the default of a str pattern"
    );
    let units: Vec<Value> = em::SINCE_UNIT_TO_SECONDS
        .iter()
        .map(|(u, s)| json!([u.to_string(), s]))
        .collect();
    assert_eq!(h["units"], Value::Array(units));
    assert_eq!(h["earliest_start"], fields(&em::earliest_start()));
    assert_eq!(
        em::earliest_start().instant_us(),
        Some(em::EARLIEST_START_US)
    );
    assert_eq!(h["earlier_rules"], em::EARLIER_RULES);
}

/// The generator's `_Draw`.
struct Draw(u64);

impl Draw {
    fn nxt(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0 >> 33
    }

    fn pick(&mut self, n: u64) -> u64 {
        self.nxt() % n
    }

    fn wide(&mut self) -> u128 {
        let a = u128::from(self.nxt());
        let b = u128::from(self.nxt());
        let c = u128::from(self.nxt());
        (a << 62) ^ (b << 31) ^ c
    }

    fn count(&mut self) -> i128 {
        let width = self.pick(64);
        let value = (self.wide() & ((1u128 << width) - 1)) as i128;
        if self.pick(8) == 0 {
            -value
        } else {
            value
        }
    }

    fn pair(&mut self) -> (i64, i64) {
        let n = self.count();
        let k = match self.pick(4) {
            0 => self.count(),
            1 => (self.wide() % (n.unsigned_abs() + 1)) as i128,
            2 => n - i128::from(self.pick(4) as u8),
            _ => n + i128::from(self.pick(4) as u8),
        };
        let k = k.clamp(i128::from(i64::MIN), i128::from(i64::MAX));
        (k as i64, i64::try_from(n).expect("an i64 count"))
    }
}

#[test]
fn eval_model_sweep() {
    let lines = common::jsonl(NAME);
    let sweep = lines.last().expect("the sweep");
    assert_eq!(sweep["k"], "sweep");
    let count = sweep["count"].as_u64().expect("count");
    assert!(count > 0);
    let mut d = Draw(sweep["seed"].as_u64().expect("seed"));
    let mut text = Vec::new();
    let mut seen = 0;
    for _ in 0..count {
        let (k, n) = d.pair();
        let r = em::RateCI::from_counts(k, n);
        let part = |v: Option<f64>| v.map_or("-".to_string(), |f| format!("{:016x}", f.to_bits()));
        text.push(format!(
            "{k} {n} {} {} {} {}",
            part(r.rate),
            part(r.lower),
            part(r.upper),
            u8::from(r.torn_read)
        ));
        seen += 1;
    }
    assert_eq!(seen, count);
    let digest = common::hex(&Sha256::digest(text.join("\n").as_bytes()));
    assert_eq!(digest, sweep["sha256"].as_str().expect("sha256"));
}
