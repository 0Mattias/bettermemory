//! Every case of `recall_counters.jsonl` (not gated) against the native
//! `recall_counters`: the constants, the readers over each event, `_ids`,
//! `_attested` and `_in_window`, `compute_surface_report` over each
//! hand-built log in its window and with its pick, then the seeded sweep
//! by line count and SHA-256. The spelling is the generator's
//! (`tests/contract/gen_recall_counters.py`): a tuple is `{"$t": [...]}`,
//! an int past 64 bits `{"$i": "<decimal>"}`, and a section's rate dicts
//! are `[numerator, denominator]` in the compact text, without `note`.

mod common;

use bettermemory_core::datetime::{parse_event_ts, DateTime};
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::recall_counters as rc;
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

fn ids_json(ids: &[PyStr]) -> Value {
    Value::Array(ids.iter().map(|s| Value::String(text(s))).collect())
}

fn bound(v: &Value) -> Option<DateTime> {
    v.as_str()
        .map(|t| parse_event_ts(&cps(t)).expect("a bound parses"))
}

fn stamp(v: &Value) -> Option<DateTime> {
    v.as_str().and_then(|t| parse_event_ts(&cps(t)))
}

const RATE_KEYS: [&str; 6] = [
    "numerator",
    "denominator",
    "rate",
    "ci95_lower",
    "ci95_upper",
    "torn_read",
];

/// The generator's `_compact`: each rate dict as `[numerator,
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

fn spell(v: &PyValue, options: DumpOptions) -> String {
    text(&dumps(v, options).expect("the section dumps"))
}

const COMPACT: DumpOptions = DumpOptions {
    item_separator: ",",
    key_separator: ":",
    ..DumpOptions::DEFAULT
};

/// The generator's `_pick` of a spec `{"in": [...], "raise": rule}`.
fn pick_of(spec: &Value) -> impl FnMut(&PyValue) -> Result<bool, PyError> {
    let wanted: Vec<Option<PyStr>> = spec["in"]
        .as_array()
        .expect("in")
        .iter()
        .map(|v| v.as_str().map(cps))
        .collect();
    let raise = spec["raise"].as_str().map(cps);
    move |row: &PyValue| {
        let none = PyValue::None;
        let rule = row.get("rule").unwrap_or(&none);
        let is = |want: &Option<PyStr>| match (want, rule) {
            (None, PyValue::None) => true,
            (Some(w), PyValue::Str(s)) => s == w,
            _ => false,
        };
        if raise.is_some() && is(&raise) {
            return Err(PyError::ValueError);
        }
        Ok(wanted.iter().any(is))
    }
}

fn report(
    events: &[PyValue],
    start: Option<DateTime>,
    end: Option<DateTime>,
    pick: Option<&Value>,
) -> Result<Option<PyValue>, PyError> {
    match pick {
        None => rc::compute_surface_report::<PyError>(events, start, end, None),
        Some(spec) => {
            let mut pick = pick_of(spec);
            rc::compute_surface_report(events, start, end, Some(&mut pick))
        }
    }
}

fn check_report(r: &Value, note: &str) {
    let name = r["name"].as_str().unwrap();
    let events: Vec<PyValue> = r["ev"].as_array().unwrap().iter().map(value).collect();
    let pick = (!r["pick"].is_null()).then(|| &r["pick"]);
    let got = match report(&events, bound(&r["start"]), bound(&r["end"]), pick) {
        Ok(None) => Value::Null,
        Ok(Some(section)) => {
            assert_eq!(
                section.get("note"),
                Some(&PyValue::Str(cps(note))),
                "note: {name}"
            );
            Value::String(spell(&compact(&section), COMPACT))
        }
        Err(e) => json!({ "x": e.name() }),
    };
    assert_eq!(got, r["out"], "report: {name}");
}

fn check_read(r: &Value) {
    let event = value(&r["ev"]);
    let row = match rc::is_surface_row(&event) {
        Ok(b) => json!(b),
        Err(e) => json!({ "x": e.name() }),
    };
    assert_eq!(row, r["row"], "is_surface_row: {}", r["ev"]);
    assert_eq!(
        json!(rc::is_surface_show(&event)),
        r["show"],
        "show: {}",
        r["ev"]
    );
    assert_eq!(
        json!(rc::surface_of(&event)),
        r["surface"],
        "surface: {}",
        r["ev"]
    );
    assert_eq!(
        ids_json(&rc::shown_ids(&event)),
        r["shown"],
        "shown: {}",
        r["ev"]
    );
    let session = rc::session_of(&event).map(|s| text(s));
    assert_eq!(json!(session), r["session"], "session: {}", r["ev"]);
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

fn check_sweep(r: &Value) {
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
    let picks = r["picks"].as_array().unwrap();
    let mut d = Draw(r["seed"].as_u64().unwrap());
    let mut lines: Vec<String> = Vec::new();
    for i in 0..r["count"].as_u64().unwrap() {
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
        let p = d.pick(picks.len());
        let (start, end) = (bound(&windows[w][0]), bound(&windows[w][1]));
        for (name, pick) in [("-".to_string(), None), (format!("p={p}"), Some(&picks[p]))] {
            let answer = match report(&events, start, end, pick) {
                Ok(None) => "~".to_string(),
                Ok(Some(section)) => spell(&section, DumpOptions::DEFAULT),
                Err(e) => format!("!{}", e.name()),
            };
            lines.push(format!("{i} w={w} {name}={answer}"));
        }
    }
    assert_eq!(
        lines.len() as u64,
        r["lines"].as_u64().unwrap(),
        "sweep lines"
    );
    let digest = Sha256::digest(lines.join("\n").as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(hex, r["sha256"].as_str().unwrap(), "sweep digest");
}

#[test]
fn recall_counters_every_case() {
    let lines = common::jsonl("recall_counters.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    assert_eq!(header["recall_grade"], rc::RECALL_GRADE);
    assert_eq!(header["strata"], json!(rc::STRATA));
    assert_eq!(header["surfaces"], json!(rc::SURFACES));
    assert_eq!(
        [rc::STRATUM_DELIVERY, rc::STRATUM_BRIEF, rc::STRATUM_SILENT],
        rc::STRATA
    );
    let note = header["note"].as_str().unwrap();
    assert_eq!(note, rc::SILENT_MISS_NOTE);
    for kind in header["kinds"].as_array().unwrap() {
        let event = PyValue::Dict(vec![(PyValue::Str(cps("kind")), value(kind))]);
        assert_eq!(rc::is_surface_row(&event), Ok(true), "kind {kind}");
    }
    let mut checked = 0;
    let mut reports = 0;
    for r in &lines[1..] {
        match r["k"].as_str().unwrap() {
            "read" => check_read(r),
            "ids" => assert_eq!(ids_json(&rc::ids_of(&value(&r["v"]))), r["out"], "{r}"),
            "att" => {
                let id = cps(r["id"].as_str().unwrap());
                let got = rc::attested(&value(&r["ev"]), &id);
                assert_eq!(json!(got), r["out"], "{r}");
            }
            "win" => {
                let got = rc::in_window(stamp(&r["ts"]), bound(&r["start"]), bound(&r["end"]));
                assert_eq!(json!(got), r["out"], "{r}");
            }
            "report" => {
                check_report(r, note);
                reports += 1;
            }
            "sweep" => check_sweep(r),
            other => panic!("unknown row kind {other}"),
        }
        checked += 1;
    }
    assert!(reports > 0, "no report checked");
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("recall_counters.jsonl", checked, in_file);
}
