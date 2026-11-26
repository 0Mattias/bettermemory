//! Every row of `eval_model_rules.jsonl` against `eval_model::rules`: the
//! six rules over hand-built `top_hits`, `_collect_replayable_audits`
//! over scripted streams (under each of the header's spans, and at the
//! calendar's edges), `_probe_query_display`, `to_dict` of the sweep row
//! and the three widening types; the registries' names and descriptions;
//! and the sweep of drawn `(top_hits, recent)` pairs through all six rules
//! by count and digest, drawn here by the generator's rule.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_model::{self as em, EvalModelError, ThresholdRule};
use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::neutral;
use common::parity::Ledger;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "eval_model_rules.jsonl";

/// `_common.neutral` of a value.
fn to_neutral(v: &PyValue) -> Value {
    match v {
        PyValue::None => json!({"t": "null"}),
        PyValue::Bool(b) => json!({"t": "bool", "v": b}),
        PyValue::Int(t) => json!({"t": "int", "v": t}),
        PyValue::Float(f) => json!({
            "t": "float",
            "bits": format!("{:016x}", f.to_bits()),
            "repr": float_repr::repr(*f),
        }),
        PyValue::Str(s) => match unicode::to_string(s) {
            Some(text) => json!({"t": "str", "v": text}),
            None => json!({"t": "str", "v_cps": s}),
        },
        PyValue::List(items) => {
            json!({"t": "list", "v": items.iter().map(to_neutral).collect::<Vec<_>>()})
        }
        PyValue::Dict(items) => json!({
            "t": "dict",
            "v": items
                .iter()
                .map(|(k, v)| json!([to_neutral(k), to_neutral(v)]))
                .collect::<Vec<_>>(),
        }),
        other => panic!("no answer holds {other:?}"),
    }
}

/// An exception as the generator's `_err` writes it (every text here is
/// short and ASCII).
fn err(e: &EvalModelError) -> Value {
    let text = unicode::to_string(&e.message()).expect("an ASCII message");
    json!({"x": e.name(), "msg": text})
}

fn all_rules() -> Vec<ThresholdRule> {
    em::THRESHOLD_RULES
        .iter()
        .chain(em::WIDENING_RULES.iter())
        .copied()
        .collect()
}

fn rule_answer(rule: &ThresholdRule, hits: &[PyValue], recent: &str) -> Value {
    match (rule.check)(hits, recent) {
        Ok(b) => json!(b),
        Err(e) => err(&e),
    }
}

fn list(v: PyValue) -> Vec<PyValue> {
    match v {
        PyValue::List(ref items) => items.clone(),
        other => panic!("a list, not {other:?}"),
    }
}

/// The base audit with `[key, value-or-null]` changes applied in order
/// (an existing key keeps its place, a new one goes last, null drops).
fn with_changes(base: &PyValue, changes: &Value) -> PyValue {
    let PyValue::Dict(base) = base else {
        panic!("a dict base")
    };
    let mut items = base.clone();
    for pair in changes.as_array().expect("changes") {
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

fn events(spelled: &Value, base: &PyValue) -> Vec<PyValue> {
    spelled
        .as_array()
        .expect("events")
        .iter()
        .map(|e| match e.get("n") {
            Some(n) => neutral(n),
            None => with_changes(base, &e["w"]),
        })
        .collect()
}

fn walk_answer(evs: &[PyValue], since: Option<i128>, now: &DateTime) -> Value {
    match em::collect_replayable_audits(evs, since, now) {
        Err(e) => err(&e),
        Ok(w) => {
            let rows: Vec<Value> = w
                .rows
                .iter()
                .map(|r| {
                    assert!(std::ptr::eq(r.event, &evs[r.index]));
                    let Some(PyValue::List(hits)) = r.event.get("top_hits") else {
                        panic!("a row's event holds a list")
                    };
                    assert!(std::ptr::eq(r.top_hits, hits.as_slice()));
                    json!([r.index, r.recent])
                })
                .collect();
            assert_eq!(w.with_features, rows.len() as i64);
            json!({"r": {
                "total": w.total_events_scanned,
                "in_window": w.events_in_window,
                "without": w.without_features,
                "repeats": w.repeats_skipped,
                "retracted": w.cutoff_retracted,
                "rows": rows,
            }})
        }
    }
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

fn span(v: &Value) -> Option<i128> {
    v.as_str().map(|s| s.parse().expect("an i128 span"))
}

fn opt_str(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        PyValue::Str(s) => Some(s.clone()),
        other => panic!("a str or None, not {other:?}"),
    }
}

fn s(v: &PyValue) -> PyStr {
    opt_str(v).expect("a str")
}

fn opt_int(v: &PyValue) -> Option<String> {
    match v {
        PyValue::None => None,
        PyValue::Int(t) => Some(t.clone()),
        other => panic!("an int or None, not {other:?}"),
    }
}

fn i(v: &PyValue) -> i64 {
    opt_int(v).expect("an int").parse().expect("an i64")
}

fn opt_f(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::None => None,
        PyValue::Float(f) => Some(*f),
        other => panic!("a float or None, not {other:?}"),
    }
}

fn scopes(v: &PyValue) -> Option<Vec<PyStr>> {
    match v {
        PyValue::None => None,
        PyValue::List(items) => Some(items.iter().map(s).collect()),
        other => panic!("a list or None, not {other:?}"),
    }
}

fn turn(f: &[PyValue]) -> em::WideningFlaggedTurn {
    em::WideningFlaggedTurn {
        ts: opt_str(&f[0]),
        session_id: opt_str(&f[1]),
        client_model: opt_str(&f[2]),
        probe_query_preview: opt_str(&f[3]),
        probe_query_len: opt_int(&f[4]),
        probe_query_hash: opt_str(&f[5]),
        top_hit_id: s(&f[6]),
        top_hit_score: opt_f(&f[7]),
        relevance_v1: opt_str(&f[8]),
        relevance_v2: opt_str(&f[9]),
        matched_unique: opt_int(&f[10]),
        query_unique: opt_int(&f[11]),
        v1_also_flagged: matches!(f[12], PyValue::Bool(true)),
        memory_status: s(&f[13]),
        memory_summary: opt_str(&f[14]),
        memory_scopes: scopes(&f[15]),
    }
}

fn rollup(f: &[PyValue]) -> em::WideningMemoryRollup {
    em::WideningMemoryRollup {
        memory_id: s(&f[0]),
        count: i(&f[1]),
        distinct_sessions: i(&f[2]),
        status: s(&f[3]),
        summary: opt_str(&f[4]),
        scopes: scopes(&f[5]),
        score_min: opt_f(&f[6]),
        score_max: opt_f(&f[7]),
    }
}

fn to_dict(kind: &str, f: &[PyValue]) -> PyValue {
    match kind {
        "sweep_row" => em::ThresholdSweepRow {
            rule: s(&f[0]),
            description: s(&f[1]),
            would_flag: i(&f[2]),
            delta_from_v1: i(&f[3]),
            delta_pct: opt_f(&f[4]),
        }
        .to_dict(),
        "turn" => turn(f).to_dict(),
        "rollup" => rollup(f).to_dict(),
        "detail" => em::WideningRuleDetail {
            rule: s(&f[0]),
            description: s(&f[1]),
            flagged_total: i(&f[2]),
            beyond_v1: i(&f[3]),
            turns: list(f[4].clone())
                .into_iter()
                .map(|t| turn(&list(t)))
                .collect(),
            by_memory: list(f[5].clone())
                .into_iter()
                .map(|m| rollup(&list(m)))
                .collect(),
        }
        .to_dict(),
        other => panic!("no type {other}"),
    }
}

/// The answer keys of a row.
fn want(row: &Value, keys: &[&str]) -> Value {
    let m: Map<String, Value> = keys
        .iter()
        .filter_map(|k| row.get(*k).map(|v| ((*k).to_owned(), v.clone())))
        .collect();
    Value::Object(m)
}

/// The port's answer to a row and the reference's.
fn check(row: &Value, header: &Value) -> (Value, Value) {
    let base = neutral(&header["audit_base"]);
    match row["k"].as_str().expect("a kind") {
        "rule" => {
            let hits = list(neutral(&row["hits"]));
            let recent = row["recent"].as_str().expect("recent");
            let got: Vec<Value> = all_rules()
                .iter()
                .map(|r| rule_answer(r, &hits, recent))
                .collect();
            (Value::Array(got), row["r"].clone())
        }
        "walk" => {
            let evs = events(&row["events"], &base);
            let now = dt(&header["now"]);
            let got: Vec<Value> = header["walk_sinces"]
                .as_array()
                .expect("spans")
                .iter()
                .map(|since| walk_answer(&evs, span(since), &now))
                .collect();
            (Value::Array(got), row["r"].clone())
        }
        "walk_at" => {
            let evs = events(&row["events"], &base);
            let got = walk_answer(&evs, span(&row["since"]), &dt(&row["now"]));
            (got, want(row, &["r", "x", "msg"]))
        }
        "probe" => {
            let value = row.get("v").map(neutral);
            let (p, n, h) = em::probe_query_display(value.as_ref());
            let got = json!([
                to_neutral(&p.map_or(PyValue::None, PyValue::Str)),
                to_neutral(&n.map_or(PyValue::None, PyValue::Int)),
                to_neutral(&h.map_or(PyValue::None, PyValue::Str)),
            ]);
            (got, row["r"].clone())
        }
        "dict" => {
            let fields = list(neutral(&row["in"]));
            let got = to_dict(row["type"].as_str().expect("a type"), &fields);
            (to_neutral(&got), row["r"].clone())
        }
        other => panic!("no row kind {other}"),
    }
}

#[test]
fn eval_model_rules_every_row() {
    let lines = common::jsonl(NAME);
    let header = &lines[0];
    let rows = &lines[1..lines.len() - 1];
    assert_eq!(
        header["count"].as_u64().expect("count") as usize,
        rows.len()
    );
    let mut ledger = Ledger::new("golden_eval_model_rules", NAME);
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        let (got, want) = check(row, header);
        if ledger.differs_fatally(format!("row {i}"), got == want) {
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
fn eval_model_rules_registries() {
    let lines = common::jsonl(NAME);
    let header = &lines[0];
    for (key, registry) in [
        ("threshold_rules", &em::THRESHOLD_RULES[..]),
        ("widening_rules", &em::WIDENING_RULES[..]),
    ] {
        let want = header[key].as_array().expect("a registry");
        assert_eq!(want.len(), registry.len());
        for (w, rule) in want.iter().zip(registry) {
            assert_eq!(w[0], rule.name, "the key");
            assert_eq!(w[1], rule.name);
            assert_eq!(w[2], rule.description);
            let found = em::rule_named(registry, rule.name).expect("by name");
            assert_eq!(found.name, rule.name);
        }
        assert!(em::rule_named(registry, "v9_none").is_none());
    }
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

/// A sweep table: each entry a value, or None for a key left out.
fn table(v: &Value) -> Vec<Option<PyValue>> {
    v.as_array()
        .expect("a table")
        .iter()
        .map(|e| (!e.is_null()).then(|| neutral(e)))
        .collect()
}

struct Tables {
    rels: Vec<Option<PyValue>>,
    scores: Vec<Option<PyValue>>,
    non_dicts: Vec<Option<PyValue>>,
    recents: Vec<String>,
}

fn draw_score(d: &mut Draw, t: &Tables) -> Option<PyValue> {
    match d.pick(4) {
        0 => Some(PyValue::int(d.pick(200) as i128 - 50)),
        1 => Some(PyValue::Float((d.pick(1601) as i64 - 400) as f64 / 8.0)),
        2 => Some(PyValue::Bool(d.pick(2) == 1)),
        _ => t.scores[d.pick(t.scores.len())].clone(),
    }
}

fn draw_hit(d: &mut Draw, t: &Tables) -> PyValue {
    if d.pick(10) == 0 {
        return t.non_dicts[d.pick(t.non_dicts.len())]
            .clone()
            .expect("a value");
    }
    let mut items = Vec::new();
    for key in ["relevance", "relevance_v2"] {
        if let Some(v) = t.rels[d.pick(t.rels.len())].clone() {
            items.push((PyValue::str(key), v));
        }
    }
    if let Some(score) = draw_score(d, t) {
        items.push((PyValue::str("score"), score));
    }
    PyValue::Dict(items)
}

#[test]
fn eval_model_rules_sweep() {
    let lines = common::jsonl(NAME);
    let sweep = lines.last().expect("the sweep");
    assert_eq!(sweep["k"], "sweep");
    let count = sweep["count"].as_u64().expect("count");
    assert!(count > 0);
    let t = Tables {
        rels: table(&sweep["rels"]),
        scores: table(&sweep["scores"]),
        non_dicts: table(&sweep["non_dicts"]),
        recents: sweep["recents"]
            .as_array()
            .expect("recents")
            .iter()
            .map(|r| r.as_str().expect("decimal").to_owned())
            .collect(),
    };
    let rules = all_rules();
    let mut d = Draw(sweep["seed"].as_u64().expect("seed"));
    let mut lines_out = Vec::new();
    for _ in 0..count {
        let n = d.pick(4);
        let hits: Vec<PyValue> = (0..n).map(|_| draw_hit(&mut d, &t)).collect();
        let recent = &t.recents[d.pick(t.recents.len())];
        let words: Vec<String> = rules
            .iter()
            .map(|r| match (r.check)(&hits, recent) {
                Ok(true) => "1".to_owned(),
                Ok(false) => "0".to_owned(),
                Err(e) => format!(
                    "{}:{}",
                    e.name(),
                    unicode::to_string(&e.message()).expect("ASCII")
                ),
            })
            .collect();
        lines_out.push(words.join(" "));
    }
    assert_eq!(lines_out.len() as u64, count);
    let digest = common::hex(&Sha256::digest(lines_out.join("\n").as_bytes()));
    assert_eq!(digest, sweep["sha256"].as_str().expect("sha256"));
}
