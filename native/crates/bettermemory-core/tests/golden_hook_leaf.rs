//! Every case of `hook_leaf.jsonl` (not gated) against the native `hook`.
//! The spelling is the generator's (`tests/contract/gen_hook_leaf.py`).

mod common;

use std::cell::RefCell;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::hook::{self, EmitError, RowSink, Turn};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};

/// A text: a string, `{"c": cps}` or `{"r": [[text, n], ...]}`.
fn text(v: &Value) -> PyStr {
    if let Some(s) = v.as_str() {
        return unicode::cps(s);
    }
    if let Some(c) = v.get("c") {
        return c
            .as_array()
            .expect("code points")
            .iter()
            .map(|x| x.as_u64().expect("a code point") as u32)
            .collect();
    }
    let mut out = Vec::new();
    for pair in v["r"].as_array().expect("a recipe") {
        let piece = text(&pair[0]);
        for _ in 0..pair[1].as_u64().expect("a count") {
            out.extend_from_slice(&piece);
        }
    }
    out
}

fn spelled(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => json!(s),
        None => json!({ "c": t }),
    }
}

fn opt_text(v: &Value) -> Option<PyStr> {
    (!v.is_null()).then(|| text(v))
}

fn parsed(v: &Value) -> PyValue {
    json::loads(&text(v)).expect("a JSON text")
}

fn list(v: &PyValue) -> Vec<PyValue> {
    match v {
        PyValue::List(items) => items.clone(),
        _ => panic!("a list"),
    }
}

fn dumped(v: &PyValue) -> Value {
    let t = json::dumps(v, DumpOptions::DEFAULT).expect("dumps");
    json!(unicode::to_string(&t).expect("ASCII"))
}

fn now(case: &Value) -> DateTime {
    DateTime::from_isoformat(&text(&case["now"])).expect("a clock")
}

fn ids(set: impl IntoIterator<Item = PyStr>) -> PyValue {
    PyValue::List(set.into_iter().map(PyValue::Str).collect())
}

fn err(name: &str) -> Value {
    json!({ "e": name })
}

struct Rec {
    rows: Vec<PyValue>,
    fail_at: usize,
}

impl RowSink for Rec {
    type Error = &'static str;
    fn record(&mut self, kind: &str, fields: Vec<(PyStr, PyValue)>) -> Result<(), &'static str> {
        if self.rows.len() + 1 == self.fail_at {
            return Err("OSError");
        }
        let pairs = fields
            .into_iter()
            .map(|(k, v)| PyValue::List(vec![PyValue::Str(k), v]))
            .collect();
        self.rows.push(PyValue::List(vec![
            PyValue::str(kind),
            PyValue::List(pairs),
        ]));
        Ok(())
    }
}

fn emit(case: &Value) -> Value {
    let events = list(&parsed(&case["ev"]));
    let bodies = parsed(&case["bodies"]);
    let gone = parsed(&case["gone"]);
    let boom: Vec<PyStr> = case["boom"]
        .as_array()
        .expect("boom")
        .iter()
        .map(text)
        .collect();
    let clock = now(case);
    let sid = text(&case["sid"]);
    let reply = text(&case["reply"]);
    let wt = opt_text(&case["wt"]);
    let model = opt_text(&case["model"]);
    let server = case.get("server").map(text);
    let latest = opt_text(&case["latest"]);
    let turn = Turn {
        recent_events: &events,
        session_id: &sid,
        assistant_response: &reply,
        worktree_root: wt.as_ref(),
        client_model: model.as_ref(),
        server_session: server.as_ref(),
        now: &clock,
    };
    let joins = RefCell::new(Vec::new());
    let mut rec = Rec {
        rows: Vec::new(),
        fail_at: case["rfail"].as_u64().expect("rfail") as usize,
    };
    let got = hook::emit_hook_attributions(
        &turn,
        |id: &PyStr| {
            if boom.contains(id) {
                return Err("OSError");
            }
            if let PyValue::List(g) = &gone {
                if g.contains(&PyValue::Str(id.clone())) {
                    return Ok(None);
                }
            }
            let PyValue::Dict(items) = &bodies else {
                panic!("bodies")
            };
            let body = items
                .iter()
                .find(|(k, _)| *k == PyValue::Str(id.clone()))
                .map(|(_, v)| v);
            match body {
                Some(PyValue::Str(b)) => Ok(Some(b.clone())),
                _ => panic!("no body"),
            }
        },
        &mut rec,
        |evs: &[PyValue], root: Option<&PyStr>| {
            joins.borrow_mut().push(PyValue::List(vec![
                PyValue::int(evs.len() as i128),
                root.map_or(PyValue::None, |r| PyValue::Str(r.clone())),
            ]));
            latest.clone()
        },
    );
    let head = match got {
        Ok(set) => (PyValue::str("ret"), ids(set)),
        Err(EmitError::Effect(name)) => (PyValue::str("e"), PyValue::str(name)),
        Err(EmitError::Scan(s)) => (PyValue::str("e"), PyValue::str(s.name())),
    };
    let out = PyValue::Dict(vec![
        head,
        (PyValue::str("rows"), PyValue::List(rec.rows)),
        (PyValue::str("joins"), PyValue::List(joins.into_inner())),
    ]);
    dumped(&out)
}

fn answer(case: &Value) -> Value {
    match case["f"].as_str().expect("a function") {
        "payload" => match hook::read_payload(&text(&case["a"])) {
            Ok(v) => dumped(&v),
            Err(e) => err(e.name()),
        },
        "text" => {
            let p = parsed(&case["p"]);
            match hook::payload_text(&p, case["n"].as_str().expect("a name")) {
                Some(s) => spelled(s),
                None => Value::Null,
            }
        }
        "pending" => {
            let events = list(&parsed(&case["ev"]));
            let used: Vec<PyStr> = case["u"]
                .as_array()
                .expect("used")
                .iter()
                .map(text)
                .collect();
            let used_refs: Vec<&[u32]> = used.iter().map(|u| u.as_slice()).collect();
            match hook::pending_retrievals(
                &events,
                &now(case),
                &text(&case["r"]),
                &used_refs,
                case["l"].as_i64().expect("lookback"),
            ) {
                Ok(set) => dumped(&ids(set)),
                Err(e) => err(e.name()),
            }
        }
        "emit" => emit(case),
        other => panic!("unknown function {other}"),
    }
}

#[test]
fn golden_hook_leaf() {
    let file = "hook_leaf.jsonl";
    let lines = common::jsonl(file);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    assert_eq!(
        header["lookback"],
        json!(hook::ATTRIBUTION_LOOKBACK_SECONDS)
    );
    assert_eq!(header["cap"], json!(hook::STDIN_PAYLOAD_CAP_BYTES));
    let count = header["count"].as_u64().expect("a count") as usize;
    assert!(count > 0);
    let mut ledger = Ledger::new("golden_hook_leaf", file);
    for (i, case) in lines[1..].iter().enumerate() {
        let got = answer(case);
        let label = format!("line {}", i + 2);
        assert!(
            !ledger.differs_fatally(&label, got == case["o"]),
            "{label} {case}: got {got}"
        );
    }
    assert_eq!(lines.len() - 1, count);
    ledger.finish(count);
}
