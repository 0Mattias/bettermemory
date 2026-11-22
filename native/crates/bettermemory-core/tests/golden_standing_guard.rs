//! Every case of `standing_guard.jsonl` against `standing_guard`:
//! `read_words`, `check` and `guard` over stub collaborators that answer
//! from the row's script, each call logged in order and compared.

mod common;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::standing_guard::{check, guard, read_words, GuardDeps, GuardError};
use bettermemory_core::unicode::{cps, to_string};
use common::{assert_count, jsonl};
use serde_json::{json, Value};

fn t(v: &Value) -> PyStr {
    match v {
        Value::String(s) => cps(s),
        _ => common::cps_of(&v["$cps"]),
    }
}

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("a list").iter().map(t).collect()
}

fn pairs(v: &Value) -> Vec<(PyStr, Value)> {
    v["$d"]
        .as_array()
        .expect("a dict")
        .iter()
        .map(|p| (t(&p[0]), p[1].clone()))
        .collect()
}

fn get<'a>(d: &'a [(PyStr, Value)], k: &str) -> Option<&'a Value> {
    d.iter().find(|(key, _)| *key == cps(k)).map(|p| &p.1)
}

/// The generator's `V` read into a Python value.
fn py(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => PyValue::int(n.as_i64().unwrap().into()),
        Value::String(s) => PyValue::str(s),
        Value::Array(a) => PyValue::List(a.iter().map(py).collect()),
        Value::Object(o) if o.contains_key("$cps") => PyValue::Str(t(v)),
        Value::Object(_) => PyValue::Dict(
            pairs(v)
                .into_iter()
                .map(|(k, x)| (PyValue::Str(k), py(&x)))
                .collect(),
        ),
    }
}

/// A Python value written as the generator's `V`.
fn w(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Int(n) => json!(n.parse::<i64>().unwrap()),
        PyValue::Str(s) => match to_string(s) {
            Some(s) => json!(s),
            None => json!({ "$cps": s }),
        },
        PyValue::List(a) | PyValue::Tuple(a) => Value::Array(a.iter().map(w).collect()),
        PyValue::Dict(d) => {
            json!({"$d": d.iter().map(|(k, x)| json!([w(k), w(x)])).collect::<Vec<_>>()})
        }
        other => panic!("no V form for {other:?}"),
    }
}

fn ws(s: &[u32]) -> Value {
    w(&PyValue::Str(s.to_vec()))
}

fn wl(items: &[PyStr]) -> Value {
    Value::Array(items.iter().map(|s| ws(s)).collect())
}

struct Stub {
    sc: Vec<(PyStr, Value)>,
    log: Vec<Value>,
}

impl Stub {
    fn flag(&self, k: &str, default: bool) -> bool {
        get(&self.sc, k).map_or(default, |v| v.as_bool().unwrap())
    }
    fn checkout(&self) -> Result<Option<PyStr>, PyStr> {
        match get(&self.sc, "caller") {
            None => Ok(Some(cps("/c"))),
            Some(c) => {
                let c = pairs(c);
                if let Some(text) = get(&c, "raise") {
                    return Err(t(text));
                }
                let co = get(&c, "co").unwrap();
                Ok((!co.is_null()).then(|| t(co)))
            }
        }
    }
}

impl GuardDeps for Stub {
    type Caller = ();
    type Error = PyStr;
    fn guarded_ids(&mut self, ids: &[PyStr]) -> Vec<PyStr> {
        self.log.push(json!(["ids", wl(ids)]));
        get(&self.sc, "own").map(texts).unwrap_or_default()
    }
    fn asking(&mut self) -> bool {
        self.log.push(json!(["asking"]));
        self.flag("asking", true)
    }
    fn caller(&mut self) -> Result<(Option<PyStr>, ()), PyStr> {
        self.log.push(json!(["caller"]));
        self.checkout().map(|c| (c, ()))
    }
    fn refusal_rows(&mut self, ids: &[PyStr], _: &()) -> PyValue {
        self.log.push(json!(["rows", wl(ids)]));
        match get(&self.sc, "rows") {
            Some(v) => py(v),
            None => PyValue::List(
                ids.iter()
                    .map(|i| PyValue::Dict(vec![(PyValue::str("id"), PyValue::Str(i.clone()))]))
                    .collect(),
            ),
        }
    }
    fn words_seen(&mut self, checkout: &[u32]) -> bool {
        self.log.push(json!(["seen", ws(checkout)]));
        self.flag("seen", false)
    }
    fn words_hold(&mut self, checkout: &[u32], quote: &[u32]) -> bool {
        self.log.push(json!(["holds", ws(checkout), ws(quote)]));
        self.flag("holds", false)
    }
    fn record_row(&mut self, kind: &str, fields: Vec<(&'static str, PyValue)>) {
        let d = PyValue::Dict(
            fields
                .into_iter()
                .map(|(k, v)| (PyValue::str(k), v))
                .collect(),
        );
        self.log.push(json!(["rec", kind, w(&d)]));
    }
}

fn opt_text(v: Option<&Value>) -> Option<PyStr> {
    v.filter(|v| !v.is_null()).map(t)
}

fn guard_err(e: GuardError<PyStr>) -> Value {
    match e {
        GuardError::Caller(m) => json!({"err": "Caller", "m": ws(&m)}),
        GuardError::Value(m) => json!({"err": "Value", "m": ws(&m)}),
    }
}

#[test]
fn every_case_matches_the_reference() {
    let all = jsonl("standing_guard.jsonl");
    let rows = &all[1..];
    assert_eq!(rows.len() as u64, all[0]["cases"].as_u64().unwrap());
    let mut checked = 0;
    for (i, r) in rows.iter().enumerate() {
        let a = pairs(&r["a"]);
        let mut stub = Stub {
            sc: pairs(&r["sc"]),
            log: Vec::new(),
        };
        let said = opt_text(get(&a, "said"));
        let got = match r["k"].as_str().unwrap() {
            "words" => {
                let co = stub.checkout().unwrap();
                let name = t(get(&a, "name").unwrap());
                match read_words(&mut stub, co.as_deref(), said.as_deref(), &name) {
                    Ok(None) => json!({ "ok": null }),
                    Ok(Some(wd)) => json!({"ok": [ws(&wd.quote), wd.checked]}),
                    Err(m) => json!({"err": "Value", "m": ws(&m)}),
                }
            }
            kind => {
                let door = t(get(&a, "door").unwrap());
                let records = texts(get(&a, "records").unwrap());
                if kind == "guard" {
                    match guard(&mut stub, &door, &records, said.as_deref()) {
                        Ok(resp) => json!({ "ok": resp.as_ref().map_or(Value::Null, w) }),
                        Err(e) => guard_err(e),
                    }
                } else {
                    let rule_s = get(&a, "rule_s").filter(|v| !v.is_null()).map(texts);
                    match check(
                        &mut stub,
                        &door,
                        &records,
                        said.as_deref(),
                        rule_s.as_deref(),
                    ) {
                        Ok((resp, held)) => json!({
                            "ok": resp.as_ref().map_or(Value::Null, w),
                            "held": held.map_or(Value::Null, |h| json!([
                                ws(&h.door), wl(&h.records), ws(&h.words.quote), h.words.checked
                            ])),
                        }),
                        Err(e) => guard_err(e),
                    }
                }
            }
        };
        assert_eq!(got, r["o"], "row {i}: answer of {r}");
        assert_eq!(Value::Array(stub.log), r["log"], "row {i}: calls of {r}");
        checked += 1;
    }
    assert_count("standing_guard.jsonl", checked, rows.len());
}
