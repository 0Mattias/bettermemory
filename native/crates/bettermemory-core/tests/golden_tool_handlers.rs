//! Every case of `tool_handlers.jsonl` (not gated) against
//! `tool_handlers`: each scripted body through `refusals_as_tool_errors`
//! and, with the store's commits moved or not, through
//! `kept_or_retryable`. The spelling is the generator's
//! (`tests/contract/gen_tool_handlers.py`).

mod common;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::schema::Absent;
use bettermemory_core::tool_handlers::{
    kept_or_retryable, kept_write_refusal, refusals_as_tool_errors, retryable_refusal, wrap_code,
    Failure, Refusal, RefusalClass, SqliteFailure,
};
use bettermemory_core::unicode::cps;
use common::tool_handlers::{bundle, retryable, Bundle, TestStore};
use serde_json::{json, Map, Value};

pub fn py(v: &Value) -> PyValue {
    match v {
        Value::String(s) => PyValue::str(s),
        Value::Number(n) => PyValue::Int(n.to_string()),
        Value::Array(a) => PyValue::List(a.iter().map(py).collect()),
        Value::Object(o) => {
            PyValue::Dict(o.iter().map(|(k, v)| (PyValue::str(k), py(v))).collect())
        }
        other => panic!("no value {other}"),
    }
}

fn back(v: &PyValue) -> Value {
    let text = |s: &[u32]| {
        s.iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect::<String>()
    };
    match v {
        PyValue::Str(s) => json!(text(s)),
        PyValue::Int(n) => serde_json::from_str(n).unwrap(),
        PyValue::List(a) => Value::Array(a.iter().map(back).collect()),
        PyValue::Dict(d) => {
            let mut m = Map::new();
            for (k, v) in d {
                let PyValue::Str(k) = k else { panic!("a key") };
                m.insert(text(k), back(v));
            }
            Value::Object(m)
        }
        other => panic!("no spelling {other:?}"),
    }
}

fn code_of(name: &str) -> &'static str {
    match name {
        "store_busy" => "store_busy",
        "store_full" => "store_full",
        other => panic!("no code {other}"),
    }
}

fn s(v: &Value) -> Vec<u32> {
    cps(v.as_str().unwrap())
}

fn body(b: &Value) -> Result<PyValue, Failure> {
    let o = b.as_object().unwrap();
    let refusal = |class, t: &Value| {
        Err(Failure::Tool(Refusal {
            class,
            text: s(t),
            code: None,
        }))
    };
    if let Some(v) = o.get("ok") {
        return Ok(py(v));
    }
    if let Some(t) = o.get("value") {
        return Err(Failure::Value(s(t)));
    }
    if let Some(kind) = o.get("absent") {
        let kind = if kind == "NotFound" {
            Absent::NotFound
        } else {
            Absent::Message
        };
        let args = o["args"].as_array().unwrap().iter().map(py).collect();
        return Err(Failure::Absent(kind, args));
    }
    if let Some(t) = o.get("sqlite") {
        let code = match o.get("wide") {
            Some(d) => Some(wrap_code(d.as_str().unwrap()).unwrap()),
            None => o.get("code").and_then(Value::as_i64),
        };
        return Err(Failure::Sqlite(SqliteFailure { code, text: s(t) }));
    }
    if let Some(t) = o.get("tool") {
        return refusal(RefusalClass::ToolError, t);
    }
    if let Some(t) = o.get("refused") {
        return refusal(RefusalClass::Refused, t);
    }
    if let Some(p) = o.get("retry") {
        return Err(Failure::Tool(retryable_refusal(
            code_of(p[0].as_str().unwrap()),
            &s(&p[1]),
        )));
    }
    if let Some(p) = o.get("kept") {
        return Err(Failure::Tool(kept_write_refusal(
            code_of(p[0].as_str().unwrap()),
            &s(&p[1]),
        )));
    }
    Err(Failure::Other(s(&o["other"])))
}

fn outcome(got: Result<PyValue, Failure>, b: &Value) -> Value {
    match got {
        Ok(v) => json!({"r": back(&v)}),
        Err(Failure::Tool(r)) => json!({
            "cls": r.class.name(),
            "text": r.text.iter().map(|&c| char::from_u32(c).unwrap()).collect::<String>(),
            "code": r.code,
            "retryable": r.class.retryable(),
        }),
        Err(Failure::Sqlite(_)) => json!({"raised": "OperationalError"}),
        Err(Failure::Other(_)) => json!({"raised": "RuntimeError"}),
        Err(Failure::Value(_)) => json!({"raised": "ValueError"}),
        Err(Failure::Absent(..)) => {
            let name = b["absent"].as_str().unwrap();
            json!({"raised": format!("{name}Error")})
        }
        Err(other) => panic!("not raised by the reference: {other:?}"),
    }
}

#[test]
fn tool_handlers_every_case() {
    let lines = common::jsonl("tool_handlers.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "tool_handlers");
    let mut checked = 0;
    for r in &lines[1..] {
        let got = match r["k"].as_str().unwrap() {
            "w" => refusals_as_tool_errors(|| body(&r["b"]), &retryable),
            "kr" => {
                let deps: Bundle = bundle(TestStore::new(1), None, None);
                let moved = r["moved"].as_bool().unwrap();
                kept_or_retryable(
                    &|d: &Bundle| {
                        if moved {
                            d.store.commit();
                        }
                        body(&r["b"])
                    },
                    &deps,
                    &retryable,
                )
            }
            other => panic!("unknown row kind {other}"),
        };
        assert_eq!(outcome(got, &r["b"]), r["o"], "{r}");
        checked += 1;
    }
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("tool_handlers.jsonl", checked, in_file);
}
