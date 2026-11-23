//! Runs one row of `event_recorder.jsonl` or `event_recorder_python.jsonl`
//! over the native `Recorder` and spells its answer as the generator does.

#![allow(dead_code)]

use crate::common;
use bettermemory_core::credentials::spans::secret_spans;
use bettermemory_core::event_hook::HookScope;
use bettermemory_core::event_recorder::{EventActor, EventSink, Recorder};
use bettermemory_core::identity::{Actor, Caller, Published, Workspace};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_prompt::PyException;
use bettermemory_core::unicode::{cps, to_string};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::rc::Rc;

/// A call the fake store received: the kind and the keywords in order.
pub type Call = (PyStr, Vec<(PyStr, Value)>);

pub struct FakeSink {
    pub fail: Option<PyException>,
    pub published: Option<Actor>,
    pub calls: Rc<RefCell<Vec<Call>>>,
    pub warnings: Rc<RefCell<Vec<PyStr>>>,
}

fn py(value: &PyValue) -> Value {
    match value {
        PyValue::None => json!({"t": "null"}),
        PyValue::Bool(b) => json!({"t": "bool", "v": b}),
        PyValue::Int(i) => json!({"t": "int", "v": i}),
        PyValue::Float(f) => json!({
            "t": "float",
            "bits": format!("{:016x}", f.to_bits()),
            "repr": bettermemory_core::float_repr::repr(*f),
        }),
        PyValue::Str(s) => match to_string(s) {
            Some(t) => json!({"t": "str", "v": t}),
            None => json!({"t": "str", "v_cps": s}),
        },
        PyValue::Tuple(v) => json!({"t": "tuple", "v": v.iter().map(py).collect::<Vec<_>>()}),
        PyValue::List(v) => json!({"t": "list", "v": v.iter().map(py).collect::<Vec<_>>()}),
        PyValue::Dict(v) => json!({
            "t": "dict",
            "v": v.iter().map(|(k, x)| json!([py(k), py(x)])).collect::<Vec<_>>(),
        }),
        other => panic!("no spelling for {other:?}"),
    }
}

impl EventSink for FakeSink {
    fn record_event(
        &mut self,
        kind: &[u32],
        session: PyValue,
        actor: EventActor,
        best_effort: Option<bool>,
        attempts: Option<u32>,
        deadline: Option<f64>,
        fields: Vec<(PyStr, PyValue)>,
    ) -> Result<(), PyException> {
        let mut kw = vec![(cps("session"), py(&session))];
        let actor = match actor {
            EventActor::Published(a) => {
                assert_eq!(Some(&a), self.published.as_ref());
                json!({"t": "published"})
            }
            EventActor::Given(v) => py(&v),
            EventActor::None => py(&PyValue::None),
        };
        kw.push((cps("actor"), actor));
        if let Some(b) = best_effort {
            kw.push((cps("best_effort"), py(&PyValue::Bool(b))));
        }
        if let Some(n) = attempts {
            kw.push((cps("attempts"), py(&PyValue::int(i128::from(n)))));
        }
        if let Some(d) = deadline {
            kw.push((cps("deadline"), py(&PyValue::Float(d))));
        }
        kw.extend(fields.iter().map(|(k, v)| (k.clone(), py(v))));
        self.calls.borrow_mut().push((kind.to_vec(), kw));
        self.fail.clone().map_or(Ok(()), Err)
    }

    fn call_name(&self) -> String {
        "tests.contract.gen_event_recorder._Sink.record_event".to_owned()
    }
}

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn caller(pub_: &Value) -> Caller {
    let mut sources = Vec::new();
    if let Some(s) = pub_.get("source").and_then(Value::as_str) {
        sources.push((cps("session"), cps(s)));
    }
    let client = pub_.get("client").and_then(opt_ref);
    if client.is_some() {
        sources.push((cps("client"), cps("header")));
    }
    sources.sort();
    let actor = Actor {
        client,
        session: pub_.get("session").and_then(opt_ref),
        sources,
        ..Actor::default()
    };
    let workspace = match pub_.get("workspace") {
        Some(ws) => Workspace {
            path: opt(&ws[0]),
            source: cps(ws[1].as_str().unwrap()),
        },
        None => Workspace::default(),
    };
    Caller {
        actor,
        workspace,
        harness_session: pub_.get("harness").and_then(opt_ref),
        ..Caller::default()
    }
}

fn opt_ref(v: &Value) -> Option<PyStr> {
    opt(v)
}

fn text(s: &[u32]) -> Value {
    match to_string(s) {
        Some(t) => json!({"w": t}),
        None => json!({"w_cps": s}),
    }
}

fn message(value: &Value) -> PyStr {
    value
        .as_str()
        .map(cps)
        .unwrap_or_else(|| common::text_field(value, "message"))
}

fn encoded_message(value: &[u32]) -> Value {
    to_string(value).map_or_else(|| json!({"message_cps": value}), |s| json!(s))
}

fn kind_value(kind: &[u32]) -> Value {
    match to_string(kind) {
        Some(t) => json!({"kind": t}),
        None => json!({"kind_cps": kind}),
    }
}

#[derive(Debug)]
pub struct NoRaise;
impl bettermemory_core::event_hook::Raised for NoRaise {
    fn class_name(&self) -> &str {
        "NoRaise"
    }
    fn sqlite_code(&self) -> Option<i64> {
        None
    }
}

/// Builds the row's recorder and returns its store calls with it.
pub fn recorder(row: &Value) -> (Recorder<FakeSink>, Rc<RefCell<Vec<Call>>>) {
    let rc = &row["rc"];
    let calls = Rc::new(RefCell::new(Vec::new()));
    let warnings = Rc::new(RefCell::new(Vec::new()));
    let fail = row["fail"].as_array().map(|f| PyException {
        class: f[0].as_str().unwrap().to_owned(),
        message: message(&f[1]),
    });
    let warning_error = row.get("warning_error").map(message);
    let published_actor = row["pub"]
        .as_object()
        .map(|_| caller(&row["pub"]).actor)
        .filter(|a| !a.is_empty());
    let cap = row["cap"].clone();
    let rec = Recorder {
        store: FakeSink {
            fail,
            published: published_actor,
            calls: calls.clone(),
            warnings: warnings.clone(),
        },
        session_id: cps(rc[0].as_str().unwrap()),
        enabled: rc[1].as_bool().unwrap(),
        worktree_root: opt(&rc[2]),
        attribution: cps(rc[3].as_str().unwrap()),
        triggered_from: cps(rc[4].as_str().unwrap()),
        secret_spans: Box::new(|t: &[u32]| {
            secret_spans(t)
                .into_iter()
                .map(|(a, b, k)| (a, b, cps(k)))
                .collect()
        }),
        declaration: Box::new(|p: &Published| {
            Ok(p.current()
                .map(|c| &c.workspace)
                .filter(|w| w.declared())
                .map(|w| (w.path.clone().unwrap(), w.source.clone())))
        }),
        capture_worktree: Box::new(move || match &cap {
            Value::Object(o) => Err(PyException {
                class: "RuntimeError".to_owned(),
                message: message(&o["raise"]),
            }),
            other => Ok(opt(other)),
        }),
        warn: Box::new(move |warning| {
            warnings.borrow_mut().push(warning);
            match &warning_error {
                None => Ok(()),
                Some(message) => Err(PyException {
                    class: "RuntimeError".to_owned(),
                    message: message.clone(),
                }),
            }
        }),
    };
    (rec, calls)
}

/// The row's answer, spelt as the generator's `r`.
pub fn run(row: &Value) -> Value {
    let (mut rec, calls) = recorder(row);
    let mut published = Published::default();
    if row["pub"].is_object() {
        let _ = published.publish(caller(&row["pub"]));
    }
    let kind = common::text_field(row, "kind");
    let fields: Vec<(PyStr, PyValue)> = row["f"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| (cps(p[0].as_str().unwrap()), common::neutral(&p[1])))
        .collect();
    let sfr = rec.session_for_request(&published);
    let wfr = match rec.worktree_for_request(&published) {
        Ok(w) => w.map_or(Value::Null, |w| json!(to_string(&w).unwrap())),
        Err(e) => json!({"raise": encoded_message(&e.message)}),
    };
    let mut scope = HookScope::new();
    let mut out = (false, Value::Null);
    let mut body = |scope: &mut HookScope| {
        let records = rec.records(scope, &kind);
        let raised = match rec.record(scope, &published, &kind, fields.clone()) {
            Ok(()) => Value::Null,
            Err(e) => json!([e.class, encoded_message(&e.message)]),
        };
        out = (records, raised);
    };
    let dl = row["dl"].as_f64();
    let mut timed = |scope: &mut HookScope| match dl {
        None => body(scope),
        Some(at) => {
            let mut clock = || at;
            scope
                .within_hook_budget::<(), NoRaise>("stop", None, &mut clock, |s, _| {
                    body(s);
                    Ok(())
                })
                .unwrap();
        }
    };
    if row["sup"].as_bool().unwrap() {
        scope.suppressed(|s| timed(s));
    } else {
        timed(&mut scope);
    }
    let spelt: Vec<Value> = calls
        .borrow()
        .iter()
        .map(|(k, kw)| {
            json!([
                kind_value(k),
                kw.iter()
                    .map(|(n, v)| json!([to_string(n).unwrap(), v]))
                    .collect::<Vec<_>>()
            ])
        })
        .collect();
    json!({
        "calls": spelt,
        "raised": out.1,
        "records": out.0,
        "sfr": to_string(&sfr).unwrap(),
        "warn": rec.store.warnings.borrow().iter().map(|w| text(w)).collect::<Vec<_>>(),
        "wfr": wfr,
    })
}
