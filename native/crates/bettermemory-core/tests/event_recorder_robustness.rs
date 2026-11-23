//! Hostile input for `Recorder::record`: many fields, long kinds and
//! texts, values nested deep, float edges for the deadline. No panic in
//! debug or release, and time linear in the number of fields.

mod common;
mod event_recorder_run;
mod timing;

use bettermemory_core::event_hook::HookScope;
use bettermemory_core::event_recorder::{EventActor, EventSink, Recorder};
use bettermemory_core::identity::Published;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_prompt::PyException;
use bettermemory_core::unicode::cps;
use serde_json::{json, Value};

fn row(rc: Value, kind: &str, f: Vec<Value>, dl: Value) -> Value {
    json!({
        "rc": rc, "pub": {"session": "q", "source": "transport", "harness": "h"},
        "sup": false, "dl": dl, "cap": "/w/c", "fail": null, "kind": kind, "f": f,
    })
}

fn fields(n: usize) -> Vec<Value> {
    (0..n)
        .map(|i| json!([format!("f{i}"), {"t": "int", "v": i.to_string()}]))
        .collect()
}

fn timed(n: usize) -> std::time::Duration {
    let on = json!(["p", true, "/w", "a", "t"]);
    let r = row(on, "write", vec![], Value::Null);
    let (mut rec, _) = event_recorder_run::recorder(&r);
    let published = Published::default();
    let scope = HookScope::new();
    let fields: Vec<(PyStr, PyValue)> = (0..n)
        .map(|i| (cps(&format!("f{i}")), PyValue::int(i as i128)))
        .collect();
    let kind = cps("write");
    timing::once(|| rec.record(&scope, &published, &kind, fields).unwrap())
}

#[test]
fn many_fields_grow_linearly() {
    timing::grows_within(
        "record over 2,000 and 20,000 fields",
        timing::TENFOLD,
        &mut || timed(2_000),
        &mut || timed(20_000),
    );
}

#[test]
fn telemetry_off_over_many_fields() {
    let off = json!(["p", false, null, "", ""]);
    let r = event_recorder_run::run(&row(off, "turn_audited", fields(100_000), Value::Null));
    assert_eq!(r["calls"].as_array().unwrap().len(), 1);
}

struct Count(usize);

impl EventSink for Count {
    fn record_event(
        &mut self,
        _: &[u32],
        _: PyValue,
        _: EventActor,
        _: Option<bool>,
        _: Option<u32>,
        _: Option<f64>,
        fields: Vec<(PyStr, PyValue)>,
    ) -> Result<(), PyException> {
        self.0 += fields.len();
        Ok(())
    }
}

/// A kind of a million code points and a value nested 200,000 deep, each
/// on both classes and with telemetry off: the recorder never walks a
/// value, so neither reaches the stack.
#[test]
fn long_kind_and_deep_value() {
    let mut deep = PyValue::None;
    for _ in 0..200_000 {
        deep = PyValue::List(vec![deep]);
    }
    for enabled in [true, false] {
        let mut rec = Recorder {
            store: Count(0),
            session_id: cps("p"),
            enabled,
            worktree_root: None,
            attribution: cps("a"),
            triggered_from: Vec::new(),
            secret_spans: Box::new(|_: &[u32]| Vec::new()),
            declaration: Box::new(|_: &Published| Ok(None)),
            capture_worktree: Box::new(|| Ok(None)),
            warn: Box::new(|_| panic!("the store accepted the row")),
        };
        let scope = HookScope::new();
        let published = Published::default();
        for kind in [cps(&"k".repeat(1_000_000)), cps("write")] {
            let fields = vec![(cps("x"), deep.clone()), (cps("id"), deep.clone())];
            rec.record(&scope, &published, &kind, fields).unwrap();
        }
        assert!(rec.store.0 > 0);
    }
}

#[test]
fn deadline_float_edges() {
    for at in [f64::MAX, f64::MIN, 0.0, -0.0, 1e-300] {
        let on = json!(["p", true, null, "", ""]);
        let r = event_recorder_run::run(&row(on, "write", vec![], json!(at)));
        assert_eq!(r["calls"].as_array().unwrap().len(), 1);
    }
}

#[test]
fn a_long_probe_query_off() {
    let off = json!(["p", false, null, "", ""]);
    let text = "a".repeat(1_000_000);
    let f = vec![json!(["probe_query", {"t": "str", "v": text}])];
    let r = event_recorder_run::run(&row(off, "turn_audited", f, Value::Null));
    assert_eq!(r["calls"].as_array().unwrap().len(), 1);
}
