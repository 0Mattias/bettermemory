//! What the tests of `recall_state` share: the scripted store, the
//! entry's and the views' JSON forms as `gen_recall_state.py` writes them,
//! and the generator's sweep draws.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::loads;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_state::{
    closing_view, rebuild_session, view, RebuildError, SegmentView, SessionEvents, SessionRecall,
    STATE_KINDS,
};
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
