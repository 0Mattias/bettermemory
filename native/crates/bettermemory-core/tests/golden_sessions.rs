//! Every case of `sessions.jsonl` (not gated) against the native
//! `sessions`: the triggers, and over each hand-built log `_session_of`,
//! `hook_spans`, `runs_beside`, `_latest_in_process_session`,
//! `harness_joined_sessions`, `live_harness_sessions`, `bridged_session`,
//! `joined_sessions` and `_disabled_scopes_from_events` for every argument
//! the row lists; then the seeded sweep by line count and SHA-256. The
//! spelling is the generator's (`tests/contract/gen_sessions.py`).

mod common;

use std::collections::BTreeSet;

use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::sessions as ss;
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

/// A JSON value as `json.loads` reads it.
fn value(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) if n.is_f64() => PyValue::Float(n.as_f64().expect("float")),
        Value::Number(n) => PyValue::Int(n.to_string()),
        Value::String(s) => PyValue::Str(cps(s)),
        Value::Array(items) => PyValue::List(items.iter().map(value).collect()),
        Value::Object(map) => PyValue::Dict(
            map.iter()
                .map(|(k, v)| (PyValue::Str(cps(k)), value(v)))
                .collect(),
        ),
    }
}

fn arg(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn opt(v: &Option<PyStr>) -> Option<&[u32]> {
    v.as_deref()
}

fn err(e: PyError) -> Value {
    json!({ "x": e.name() })
}

fn set(r: Result<BTreeSet<PyStr>, PyError>) -> Value {
    match r {
        Ok(s) => Value::Array(s.iter().map(|x| Value::String(text(x))).collect()),
        Err(e) => err(e),
    }
}

fn one(r: Result<Option<PyStr>, PyError>) -> Value {
    match r {
        Ok(Some(s)) => Value::String(text(&s)),
        Ok(None) => Value::Null,
        Err(e) => err(e),
    }
}

fn spans_value(r: &Result<ss::HookSpans, PyError>) -> Value {
    match r {
        Ok(spans) => Value::Array(
            spans
                .entries()
                .iter()
                .map(|(s, (a, b))| json!([text(s), a.isoformat(), b.isoformat()]))
                .collect(),
        ),
        Err(e) => err(*e),
    }
}

fn args(r: &Value, key: &str) -> Vec<Option<PyStr>> {
    r[key].as_array().expect(key).iter().map(arg).collect()
}

fn check_log(r: &Value) {
    let name = r["name"].as_str().unwrap();
    let events: Vec<PyValue> = r["ev"].as_array().unwrap().iter().map(value).collect();
    let hs = args(r, "hs");
    let wt = args(r, "wt");
    let sv = args(r, "ss");

    let of: Vec<Value> = events
        .iter()
        .map(|e| ss::session_of(e).map_or(Value::Null, |s| Value::String(text(s))))
        .collect();
    assert_eq!(Value::Array(of), r["of"], "of: {name}");

    let spans = ss::hook_spans(&events);
    assert_eq!(spans_value(&spans), r["spans"], "spans: {name}");
    match &spans {
        Ok(spans) => {
            let mut names: Vec<PyStr> = spans.entries().iter().map(|(s, _)| s.clone()).collect();
            names.push(cps("h_absent"));
            let beside: Vec<Value> = names
                .iter()
                .map(|o| {
                    json!(names
                        .iter()
                        .map(|c| ss::runs_beside(spans, o, c))
                        .collect::<Vec<_>>())
                })
                .collect();
            assert_eq!(Value::Array(beside), r["beside"], "beside: {name}");
        }
        Err(_) => assert!(r["beside"].is_null(), "beside: {name}"),
    }

    let latest: Vec<Value> = wt
        .iter()
        .map(|w| one(ss::latest_in_process_session(&events, opt(w))))
        .collect();
    assert_eq!(Value::Array(latest), r["latest"], "latest: {name}");

    let hj: Vec<Value> = hs
        .iter()
        .map(|h| set(ss::harness_joined_sessions(&events, opt(h))))
        .collect();
    assert_eq!(Value::Array(hj), r["hj"], "hj: {name}");

    let live: Vec<Value> = hs
        .iter()
        .map(|h| set(ss::live_harness_sessions(&events, opt(h))))
        .collect();
    assert_eq!(Value::Array(live), r["live"], "live: {name}");

    for (i, h) in hs.iter().enumerate() {
        for (j, w) in wt.iter().enumerate() {
            let b = one(ss::bridged_session(&events, opt(h), opt(w)));
            assert_eq!(b, r["bridged"][i][j], "bridged {h:?} {w:?}: {name}");
            let jn = set(ss::joined_sessions(&events, opt(h), opt(w)));
            assert_eq!(jn, r["joined"][i][j], "joined {h:?} {w:?}: {name}");
        }
    }
    for (i, w) in wt.iter().enumerate() {
        for (j, s) in sv.iter().enumerate() {
            let d = set(ss::disabled_scopes_from_events(&events, opt(w), opt(s)));
            assert_eq!(d, r["disabled"][i][j], "disabled {w:?} {s:?}: {name}");
        }
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

fn line_opt(v: &Option<PyStr>) -> String {
    v.as_ref().map_or("~".to_string(), |s| text(s))
}

fn line_set(r: Result<BTreeSet<PyStr>, PyError>) -> String {
    match r {
        Ok(s) => format!(
            "{{{}}}",
            s.iter().map(|x| text(x)).collect::<Vec<_>>().join(",")
        ),
        Err(e) => format!("!{}", e.name()),
    }
}

fn line_one(r: Result<Option<PyStr>, PyError>) -> String {
    match r {
        Ok(s) => line_opt(&s),
        Err(e) => format!("!{}", e.name()),
    }
}

fn check_sweep(r: &Value) {
    let pools: Vec<(String, Vec<Option<PyValue>>)> = r["pools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| {
            let key = p[0].as_str().unwrap().to_string();
            let pool = p[1]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| (v.as_str() != Some("~")).then(|| value(v)))
                .collect();
            (key, pool)
        })
        .collect();
    let hs = args(r, "hs");
    let wt = args(r, "wt");
    let mut d = Draw(r["seed"].as_u64().unwrap());
    let mut lines: Vec<String> = Vec::new();
    for i in 0..r["count"].as_u64().unwrap() {
        let n = 1 + d.pick(10);
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
        for h in &hs {
            let live = line_set(ss::live_harness_sessions(&events, opt(h)));
            lines.push(format!("{i} h={} live={live}", line_opt(h)));
            for w in &wt {
                let b = line_one(ss::bridged_session(&events, opt(h), opt(w)));
                let j = line_set(ss::joined_sessions(&events, opt(h), opt(w)));
                lines.push(format!(
                    "{i} h={} w={} b={b} j={j}",
                    line_opt(h),
                    line_opt(w)
                ));
            }
        }
        for w in &wt {
            let dis = line_set(ss::disabled_scopes_from_events(&events, opt(w), None));
            lines.push(format!("{i} w={} d={dis}", line_opt(w)));
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
fn sessions_every_case() {
    let lines = common::jsonl("sessions.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    assert_eq!(header["triggers"], json!(ss::OUT_OF_PROCESS_TRIGGERS));
    let mut checked = 0;
    let mut logs = 0;
    for r in &lines[1..] {
        match r["k"].as_str().unwrap() {
            "log" => {
                check_log(r);
                logs += 1;
            }
            "sweep" => check_sweep(r),
            other => panic!("unknown row kind {other}"),
        }
        checked += 1;
    }
    assert!(logs > 0, "no log checked");
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("sessions.jsonl", checked, in_file);
}
