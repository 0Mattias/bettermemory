//! What the tests of `recall_state` share: the scripted store, the
//! entry's and the views' JSON forms as `gen_recall_state.py` writes them,
//! and the generator's sweep draws; for `RecallState`, the scripted store
//! of `recall_state_held.jsonl`, its calls and their forms, and the
//! generator's call draws.

use std::collections::HashMap;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::loads;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_state::{
    closing_view, rebuild_session, view, RebuildError, RecallState, SegmentView, SessionEvents,
    SessionRecall, STATE_IDLE_SECONDS, STATE_KINDS, STATE_MAX_SESSIONS,
};
use bettermemory_core::transcript::CompactMarker;
use bettermemory_core::unicode::{cps, to_string};
use serde_json::{json, Value};

pub const HARNESS: &str = "claude-code";
pub const SESSION: &str = "S1";
pub const NOW: &str = "2026-03-01T09:30:00+00:00";

pub fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).expect("an isoformat")
}

pub fn key() -> (PyStr, PyStr) {
    (cps(HARNESS), cps(SESSION))
}

/// A store whose session read yields `rows`, then raises
/// `OperationalError` after the first `fail` rows when `fail` is set.
pub struct Rows {
    pub rows: Vec<PyValue>,
    pub fail: Option<usize>,
    pub asked: Vec<(PyStr, Vec<String>)>,
}

impl Rows {
    pub fn new(rows: Vec<PyValue>) -> Self {
        Rows {
            rows,
            fail: None,
            asked: Vec::new(),
        }
    }
}

impl SessionEvents for Rows {
    type Error = &'static str;
    fn session_events_of_kinds(
        &mut self,
        session_id: &[u32],
        kinds: &[&str],
    ) -> (Vec<PyValue>, Option<&'static str>) {
        self.asked.push((
            session_id.to_vec(),
            kinds.iter().map(|k| k.to_string()).collect(),
        ));
        match self.fail {
            Some(n) => (
                self.rows.iter().take(n).cloned().collect(),
                Some("OperationalError"),
            ),
            None => (self.rows.clone(), None),
        }
    }
}

pub fn events(text: &str) -> Vec<PyValue> {
    match &loads(&cps(text)).expect("rows as JSON") {
        PyValue::List(rows) => rows.clone(),
        other => panic!("rows are a list: {other:?}"),
    }
}

pub fn event(text: &str) -> PyValue {
    loads(&cps(text)).expect("an event as JSON")
}

pub fn s(text: &[u32]) -> Value {
    Value::String(to_string(text).expect("no surrogate"))
}

fn opt(text: &Option<PyStr>) -> Value {
    text.as_ref().map_or(Value::Null, |t| s(t))
}

fn set<'a>(items: impl IntoIterator<Item = &'a PyStr>) -> Value {
    Value::Array(items.into_iter().map(|t| s(t)).collect())
}

fn seg(v: &PyValue) -> Value {
    match v {
        PyValue::Int(text) => Value::String(text.clone()),
        other => panic!("a segment that is not an int: {other:?}"),
    }
}

fn value_text(v: &PyValue) -> Value {
    match v {
        PyValue::Str(t) => s(t),
        PyValue::None => Value::Null,
        other => panic!("a shown_by value that is not a str or None: {other:?}"),
    }
}

pub fn view_form(v: &SegmentView) -> Value {
    json!({
        "seg": seg(&v.segment),
        "at": v.started_at.isoformat(),
        "shown": set(&v.shown),
        "dl": v.deliveries as i64,
        "by": v.shown_by.iter().map(|(k, (a, b))| json!([s(k), value_text(a), value_text(b)])).collect::<Vec<_>>(),
        "settled": set(&v.settled),
        "roots": set(&v.review_roots),
        "marker": opt(&v.marker),
    })
}

pub fn entry_form(e: &SessionRecall) -> Value {
    json!({
        "seg": seg(&e.segment),
        "at": e.started_at.isoformat(),
        "seen": e.last_seen.isoformat(),
        "shown": set(&e.shown),
        "dl": e.deliveries as i64,
        "by": e.shown_by.iter().map(|(k, (a, b))| json!([s(k), s(a), opt(b)])).collect::<Vec<_>>(),
        "settled": set(&e.settled),
        "roots": set(&e.review_roots),
        "marker": opt(&e.marker),
        "sig": e.signal.as_ref().map_or(Value::Null, |g| json!({
            "at": g.at.isoformat(),
            "shown": set(&g.shown),
            "dl": g.deliveries as i64,
            "ev": opt(&g.event_id),
        })),
        "view": view_form(&view(e)),
        "closing": view_form(&closing_view(e, None)),
        "kept": e.signal.as_ref().map_or(Value::Null, |g| view_form(&closing_view(e, Some(&g.shown)))),
    })
}

/// `rebuild_session` under the generator's key at `NOW`, as its form.
pub fn rebuild(store: &mut Rows, agent: Option<&[u32]>) -> Value {
    match rebuild_session(store, &key(), dt(NOW), agent) {
        Ok(entry) => entry_form(&entry),
        Err(RebuildError::Raised(e)) => json!({ "err": e.name() }),
        Err(RebuildError::Read(name)) => json!({ "err": name }),
    }
}

pub fn asked_once(store: &Rows) {
    assert_eq!(store.asked.len(), 1, "one session read");
    assert_eq!(store.asked[0].0, cps(SESSION));
    assert_eq!(store.asked[0].1, STATE_KINDS.to_vec());
}

/// The generator's splitmix64.
pub struct Rng(pub u64);

impl Rng {
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    pub fn pick<T: Clone>(&mut self, options: &[T]) -> T {
        let n = options.len() as u64;
        options[(self.next() % n) as usize].clone()
    }
}

fn st(t: &str) -> PyValue {
    PyValue::str(t)
}

fn l(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

fn d(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (st(k), v)).collect())
}

pub fn ts(minute: usize) -> String {
    format!("2026-01-01T12:{minute:02}:00Z")
}

/// The generator's `_DRAWS`, `None` for `_ABSENT`.
fn draws() -> Vec<(&'static str, Vec<Option<PyValue>>)> {
    let i = PyValue::int;
    let big = PyValue::int_from_decimal(&(1u128 << 70).to_string()).unwrap();
    vec![
        (
            "harness",
            vec![None, None, Some(st(HARNESS)), Some(st("codex")), Some(i(7))],
        ),
        (
            "agent_id",
            vec![None, None, Some(st("A")), Some(st("")), Some(st("B"))],
        ),
        (
            "segment",
            vec![
                None,
                None,
                Some(i(0)),
                Some(i(1)),
                Some(i(2)),
                Some(PyValue::Bool(true)),
                Some(big),
            ],
        ),
        (
            "marker",
            vec![None, Some(st("m1")), Some(st("m2")), Some(i(5))],
        ),
        (
            "opened_at",
            vec![
                None,
                None,
                Some(st("2026-01-01T11:00:00+00:00")),
                Some(st("bad")),
            ],
        ),
        (
            "marker_absent",
            vec![None, None, Some(PyValue::Bool(true)), Some(i(1))],
        ),
        (
            "event_id",
            vec![None, Some(st("E1")), Some(st("E2")), Some(i(3))],
        ),
        (
            "ids",
            vec![
                None,
                Some(l(vec![st("a"), st("b")])),
                Some(l(vec![st("c")])),
                Some(st("d")),
                Some(l(vec![st("a"), i(5)])),
            ],
        ),
        (
            "shown_ids",
            vec![
                None,
                None,
                Some(PyValue::None),
                Some(l(vec![st("b")])),
                Some(st("c")),
            ],
        ),
        (
            "top_hits",
            vec![
                None,
                Some(l(vec![d(vec![("id", st("a"))])])),
                Some(l(vec![])),
                Some(l(vec![d(vec![("id", i(4))])])),
                Some(l(vec![d(vec![("id", st("e"))]), d(vec![("id", st("f"))])])),
            ],
        ),
        (
            "surface",
            vec![
                None,
                Some(st("brief")),
                Some(st("recall")),
                Some(st("other")),
                Some(st("brief")),
                Some(l(vec![st("x")])),
            ],
        ),
        (
            "withdrawn",
            vec![
                None,
                Some(l(vec![st("a")])),
                Some(l(vec![st("b"), st("c")])),
                Some(st("x")),
            ],
        ),
        ("brief_event_id", vec![None, Some(st("B1")), Some(i(2))]),
        (
            "root_ids",
            vec![
                None,
                Some(l(vec![st("r1")])),
                Some(l(vec![st("r2"), st("r1")])),
            ],
        ),
    ]
}

/// The generator's `_draw_rows`: the rows and the scope.
pub fn draw_rows(rng: &mut Rng) -> (Vec<PyValue>, Option<PyStr>) {
    let table = draws();
    let agent = rng.pick(&[None, Some(cps("A"))]);
    let count = (rng.next() % 9) as usize;
    let mut rows = Vec::with_capacity(count);
    for i in 0..count {
        let mut row = vec![(st("kind"), st(rng.pick(&STATE_KINDS)))];
        let stamp = rng.pick(&[
            Some(ts(i)),
            Some(ts(i)),
            Some(ts(i)),
            None,
            Some("bad".to_string()),
        ]);
        if let Some(t) = stamp {
            row.push((st("ts"), st(&t)));
        }
        for (name, options) in &table {
            if let Some(v) = rng.pick(options) {
                row.push((st(name), v));
            }
        }
        rows.push(PyValue::Dict(row));
    }
    (rows, agent)
}

// -- RecallState: the scripted call sequences of `recall_state_held.jsonl` --

pub type Key = (PyStr, PyStr);

/// The generator's `_HeldStore`: each session's rows, a raise after the
/// first `fail[session]` rows, and calls made on the state while a
/// session's first read runs (outside the state's lock).
pub struct HeldStore<'a> {
    pub rows: HashMap<PyStr, Vec<PyValue>>,
    pub fail: HashMap<PyStr, usize>,
    pub during: HashMap<PyStr, Vec<Value>>,
    pub state: &'a RecallState,
    pub reads: usize,
}

impl<'a> HeldStore<'a> {
    /// The store a case's spec names (`rows`, `fail`, `during`).
    pub fn new(spec: &Value, state: &'a RecallState) -> Self {
        let texts = |name: &str| spec.get(name).and_then(Value::as_object).cloned();
        HeldStore {
            rows: texts("rows")
                .unwrap_or_default()
                .iter()
                .map(|(s, t)| (cps(s), events(t.as_str().expect("rows as text"))))
                .collect(),
            fail: texts("fail")
                .unwrap_or_default()
                .iter()
                .map(|(s, n)| (cps(s), n.as_u64().expect("a count") as usize))
                .collect(),
            during: texts("during")
                .unwrap_or_default()
                .iter()
                .map(|(s, c)| (cps(s), c.as_array().expect("calls").clone()))
                .collect(),
            state,
            reads: 0,
        }
    }
}

impl SessionEvents for HeldStore<'_> {
    type Error = &'static str;
    fn session_events_of_kinds(
        &mut self,
        session_id: &[u32],
        kinds: &[&str],
    ) -> (Vec<PyValue>, Option<&'static str>) {
        assert_eq!(kinds, STATE_KINDS);
        self.reads += 1;
        for step in self.during.remove(session_id).unwrap_or_default() {
            let state = self.state;
            held_call(state, self, &step);
        }
        let rows = self.rows.get(session_id).cloned().unwrap_or_default();
        match self.fail.get(session_id) {
            Some(&n) => (rows.into_iter().take(n).collect(), Some("OperationalError")),
            None => (rows, None),
        }
    }
}

/// A list of ids from its JSON form.
pub fn ids_of(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .expect("ids")
        .iter()
        .map(|i| cps(i.as_str().expect("an id")))
        .collect()
}

fn marker_of(step: &Value, at: DateTime) -> Option<CompactMarker> {
    step.get("marker")
        .and_then(Value::as_str)
        .map(|k| CompactMarker { key: cps(k), at })
}

fn when(step: &Value, name: &str) -> DateTime {
    dt(step[name].as_str().expect("a datetime"))
}

fn err<E>(e: RebuildError<E>) -> Value
where
    E: Into<&'static str>,
{
    match e {
        RebuildError::Raised(e) => json!({ "err": e.name() }),
        RebuildError::Read(name) => json!({ "err": name.into() }),
    }
}

fn seg_text(v: &PyValue) -> Value {
    match v {
        PyValue::Int(text) => Value::String(text.clone()),
        other => panic!("a segment that is not an int: {other:?}"),
    }
}

fn texts(ids: &[PyStr]) -> Value {
    Value::Array(ids.iter().map(|i| s(i)).collect())
}

/// The generator's `_held_call`: one call on the state, as its form.
pub fn held_call(state: &RecallState, store: &mut HeldStore<'_>, step: &Value) -> Value {
    let op = step["op"].as_str().expect("an op");
    let key: Key = match step.get("key").and_then(Value::as_array) {
        Some(k) => (cps(k[0].as_str().unwrap()), cps(k[1].as_str().unwrap())),
        None => (Vec::new(), Vec::new()),
    };
    let text = |name: &str| step.get(name).and_then(Value::as_str).map(cps);
    let ev = text("ev");
    match op {
        "seg" => state
            .segment(store, &key, when(step, "now"))
            .map_or_else(err, |v| view_form(&v)),
        "open" => {
            let now = when(step, "now");
            let source = text("source").expect("a source");
            state
                .open_segment(store, &key, &source, now, marker_of(step, now).as_ref())
                .map_or_else(err, |(v, opened)| json!([view_form(&v), opened]))
        }
        "pend" => state
            .pending_compaction(store, &key, when(step, "now"))
            .map_or_else(err, |p| p.map_or(Value::Null, |at| json!(at.isoformat()))),
        "resolve" => {
            let at = when(step, "at");
            state
                .resolve_compaction(&key, at, marker_of(step, at).as_ref())
                .map_or(Value::Null, |v| view_form(&v))
        }
        "attr" => state
            .attribute_compaction(&key, when(step, "at"))
            .map_or(Value::Null, |(ids, ev)| {
                json!([texts(&ids), ev.map_or(Value::Null, |e| s(&e))])
            }),
        "take" => state
            .take_closed(&key)
            .map_or(Value::Null, |v| view_form(&v)),
        "shown" => {
            state.note_shown(
                &key,
                &ids_of(&step["ids"]),
                step["delivery"].as_bool().expect("a bool"),
                when(step, "now"),
                text("surface").as_deref(),
                ev.as_deref(),
            );
            Value::Null
        }
        "claim" => {
            let (kept, seg) = state.claim_delivery(
                &key,
                &ids_of(&step["ids"]),
                when(step, "now"),
                ev.as_deref(),
            );
            json!([texts(&kept), seg_text(&seg)])
        }
        "brief" => {
            let (ks, kw, seg) = state.claim_brief(
                &key,
                &ids_of(&step["ids"]),
                &ids_of(&step["work"]),
                when(step, "now"),
                ev.as_deref(),
            );
            json!([texts(&ks), texts(&kw), seg_text(&seg)])
        }
        "forget" => {
            state.forget(&key);
            Value::Null
        }
        "settled" => {
            state.note_settled(&key, &ids_of(&step["ids"]), when(step, "now"));
            Value::Null
        }
        "roots" => {
            state.note_review_roots(&key, &ids_of(&step["ids"]), when(step, "now"));
            Value::Null
        }
        "len" => json!(state.len()),
        other => panic!("unknown op {other}"),
    }
}

/// The generator's `_held_form`: `entry_form` without its views, with the
/// segment the entry last left.
pub fn held_form(e: &SessionRecall) -> Value {
    let mut form = entry_form(e);
    let map = form.as_object_mut().expect("an object");
    for name in ["view", "closing", "kept"] {
        map.remove(name);
    }
    map.insert(
        "closed".into(),
        e.closed.as_ref().map_or(Value::Null, view_form),
    );
    form
}

/// The state a case's spec names (`max`, `idle`, `agent`).
pub fn new_state(spec: &Value) -> RecallState {
    RecallState::new(
        spec.get("max")
            .and_then(Value::as_u64)
            .map_or(STATE_MAX_SESSIONS, |n| n as usize),
        spec.get("idle")
            .and_then(Value::as_i64)
            .unwrap_or(STATE_IDLE_SECONDS),
        spec.get("agent").and_then(Value::as_str).map(cps),
    )
}

/// The keys a state holds, in order, as the generator writes them.
pub fn keys_form(state: &RecallState) -> Value {
    Value::Array(
        state
            .keys()
            .iter()
            .map(|(h, k)| json!([s(h), s(k)]))
            .collect(),
    )
}

/// The generator's `_run`: each step's `{o, keys, reads}`, then every held
/// entry as `[key, form]`.
pub fn run(spec: &Value, steps: &[Value]) -> (Vec<Value>, Value) {
    let state = new_state(spec);
    let mut store = HeldStore::new(spec, &state);
    let mut out = Vec::with_capacity(steps.len());
    for step in steps {
        let before = store.reads;
        let answer = held_call(&state, &mut store, step);
        out.push(json!({
            "o": answer,
            "keys": keys_form(&state),
            "reads": store.reads - before,
        }));
    }
    let end = state
        .entries()
        .iter()
        .map(|((h, k), e)| json!([[s(h), s(k)], held_form(e)]))
        .collect();
    (out, Value::Array(end))
}

pub const T0: &str = "2026-03-01T09:30:00+00:00";
pub const T0_IST: &str = "2026-03-01T15:00:00+05:30";

/// `base` plus `clock` microseconds, as isoformat text.
pub fn at_text(clock: i128, base: &str) -> String {
    dt(base).minus_micros(-clock).expect("in range").isoformat()
}

/// The generator's `_draw_calls` over its header's tables: the spec and
/// the steps of one sequence.
pub fn draw_calls(rng: &mut Rng, header: &Value) -> (Value, Vec<Value>) {
    let ops: Vec<String> = serde_json::from_value(header["ops"].clone()).unwrap();
    let steps_us: Vec<i128> = header["steps_us"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| i128::from(v.as_i64().unwrap()))
        .collect();
    let ids: Vec<Value> = header["ids"].as_array().unwrap().clone();
    let keys: Vec<Value> = header["keys"].as_array().unwrap().clone();
    let max = rng.pick(&[0u64, 1, 2, 3, 1000]);
    let idle = rng.pick(&[0i64, 60, 3600]);
    let agent = rng.pick(&[None, None, Some("A")]);
    let mut spec = json!({ "max": max, "idle": idle, "agent": agent, "rows": header["store"] });
    if let Some(fail) = rng.pick(&[None, None, Some("S2"), Some("S3")]) {
        let mut map = serde_json::Map::new();
        map.insert(fail.to_string(), json!(if fail == "S2" { 0 } else { 3 }));
        spec["fail"] = Value::Object(map);
    }
    let count = rng.next() % 12;
    let (mut clock, mut clocks) = (0i128, Vec::new());
    let mut steps = Vec::new();
    for _ in 0..count {
        let op = rng.pick(&ops);
        let key = rng.pick(&keys);
        clock += rng.pick(&steps_us);
        clocks.push(clock);
        let base = rng.pick(&[T0, T0, T0_IST]);
        let source = rng.pick(&["clear", "compact", "compact", "startup", "resume", ""]);
        let marker = rng.pick(&[None, None, Some("m1"), Some("m2")]);
        let these = rng.pick(&ids);
        let work = rng.pick(&ids);
        let ev = rng.pick(&[None, Some("E1"), Some("E2")]);
        let surface = rng.pick(&[None, Some(""), Some("brief"), Some("recall"), Some("other")]);
        let delivery = rng.pick(&[false, true]);
        let at_clock = clocks[(rng.next() % clocks.len() as u64) as usize] + rng.pick(&[0, 0, 1]);
        let at_base = rng.pick(&[T0, T0_IST]);
        let mut step = json!({ "op": &op, "key": key, "now": at_text(clock, base) });
        let op = op.as_str();
        if op == "open" {
            step["source"] = json!(source);
        }
        if let Some(m) = marker.filter(|_| matches!(op, "open" | "resolve")) {
            step["marker"] = json!(m);
        }
        if matches!(op, "resolve" | "attr") {
            step["at"] = json!(at_text(at_clock, at_base));
        }
        if matches!(op, "shown" | "claim" | "brief" | "settled" | "roots") {
            step["ids"] = these;
        }
        if op == "brief" {
            step["work"] = work;
        }
        if let Some(e) = ev.filter(|_| matches!(op, "shown" | "claim" | "brief")) {
            step["ev"] = json!(e);
        }
        if op == "shown" {
            step["delivery"] = json!(delivery);
            if let Some(s) = surface {
                step["surface"] = json!(s);
            }
        }
        steps.push(step);
    }
    (spec, steps)
}
