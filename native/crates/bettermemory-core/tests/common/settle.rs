//! Scripted fakes for `recall_settle`, as `tests/contract/gen_recall_settle.py`
//! builds them: every call they see is logged in the generator's order and
//! read back in its compact form (`_plain`).

use std::collections::{BTreeMap, BTreeSet};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_settle::{
    self as rs, Key, LoadFailure, Recorder, SegmentView, Sessions, SettleError, SettleState,
    SettleStore, Shown,
};
use serde_json::Value;

pub fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

pub fn dt(iso: &str) -> DateTime {
    DateTime::from_isoformat(&cps(iso)).expect("an isoformat")
}

/// `_plain`'s form, or a plain JSON event, as a `PyValue`.
pub fn plain(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => PyValue::Int(n.to_string()),
        Value::String(s) => PyValue::Str(cps(s)),
        Value::Array(items) => PyValue::List(items.iter().map(plain).collect()),
        Value::Object(map) => {
            if let (1, Some(Value::Array(pairs))) = (map.len(), map.get("d")) {
                return PyValue::Dict(pairs.iter().map(|p| (plain(&p[0]), plain(&p[1]))).collect());
            }
            if let (1, Some(Value::Array(items))) = (map.len(), map.get("tu")) {
                return PyValue::Tuple(items.iter().map(plain).collect());
            }
            if let (1, Some(Value::String(iso))) = (map.len(), map.get("dt")) {
                return PyValue::DateTime(dt(iso));
            }
            PyValue::Dict(
                map.iter()
                    .map(|(k, v)| (PyValue::Str(cps(k)), plain(v)))
                    .collect(),
            )
        }
    }
}

fn st(s: &[u32]) -> PyValue {
    PyValue::Str(s.to_vec())
}

fn list(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

fn key_value(key: &Key) -> PyValue {
    list(vec![st(&key.0), st(&key.1)])
}

fn ids(v: &[PyStr]) -> PyValue {
    list(v.iter().map(|s| st(s)).collect())
}

fn strs(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .map(|a| a.iter().map(|x| cps(x.as_str().unwrap())).collect())
        .unwrap_or_default()
}

fn view(v: &Value) -> SegmentView {
    let by: BTreeMap<PyStr, (PyValue, PyValue)> = v["by"]
        .as_object()
        .map(|m| {
            m.iter()
                .map(|(k, x)| (cps(k), (plain(&x[0]), plain(&x[1]))))
                .collect()
        })
        .unwrap_or_default();
    SegmentView {
        started_at: dt(v["at"].as_str().unwrap_or(T)),
        segment: if v.get("n").is_some() {
            plain(&v["n"])
        } else {
            PyValue::int(1)
        },
        settled: strs(&v["settled"]).into_iter().collect(),
        review_roots: strs(&v["roots"]).into_iter().collect(),
        shown_by: by,
    }
}

pub const T: &str = "2026-01-01T12:00:00+00:00";

/// The fakes of one case, and their log.
pub struct Fakes<'a> {
    pub case: &'a Value,
    pub log: Vec<PyValue>,
}

impl<'a> Fakes<'a> {
    pub fn new(case: &'a Value) -> Self {
        Fakes {
            case,
            log: Vec::new(),
        }
    }

    fn events(&self, name: &str) -> Vec<PyValue> {
        self.case[name]
            .as_array()
            .map(|a| a.iter().map(plain).collect())
            .unwrap_or_default()
    }
}

impl SettleStore for Fakes<'_> {
    fn events_of_kinds(&mut self, kinds: &[&str], since: &DateTime) -> Vec<PyValue> {
        self.log.push(list(vec![
            PyValue::str("eok"),
            list(kinds.iter().map(|k| PyValue::str(k)).collect()),
            PyValue::DateTime(*since),
        ]));
        self.events("ev")
    }

    fn events_since(&mut self, since: &DateTime) -> Vec<PyValue> {
        self.log
            .push(list(vec![PyValue::str("es"), PyValue::DateTime(*since)]));
        self.events("recent")
    }

    fn load_one(&mut self, id: &[u32]) -> Result<PyStr, LoadFailure> {
        self.log.push(list(vec![PyValue::str("load"), st(id)]));
        let name: String = id.iter().map(|&c| char::from_u32(c).unwrap()).collect();
        match self.case["bodies"].get(&name).and_then(Value::as_str) {
            None | Some("!nf") => Err(LoadFailure::NotFound),
            Some("!tomb") => Err(LoadFailure::Tombstoned),
            Some(body) => Ok(cps(body)),
        }
    }
}

impl Recorder for Fakes<'_> {
    fn record(&mut self, kind: &str, fields: Vec<(PyValue, PyValue)>) {
        self.log.push(list(vec![
            PyValue::str("rec"),
            PyValue::str(kind),
            PyValue::Dict(fields),
        ]));
    }
}

impl SettleState for Fakes<'_> {
    fn segment(&mut self, key: &Key, now: &DateTime) -> SegmentView {
        self.log.push(list(vec![
            PyValue::str("seg"),
            key_value(key),
            PyValue::DateTime(*now),
        ]));
        view(&self.case["seg"])
    }

    fn take_closed(&mut self, key: &Key) -> Option<SegmentView> {
        self.log
            .push(list(vec![PyValue::str("take"), key_value(key)]));
        let closed = &self.case["closed"];
        (!closed.is_null()).then(|| view(closed))
    }

    fn note_settled(&mut self, key: &Key, v: &[PyStr], now: &DateTime) {
        self.log.push(list(vec![
            PyValue::str("settled"),
            key_value(key),
            ids(v),
            PyValue::DateTime(*now),
        ]));
    }

    fn note_review_roots(&mut self, key: &Key, v: &[PyStr], now: &DateTime) {
        self.log.push(list(vec![
            PyValue::str("roots"),
            key_value(key),
            ids(v),
            PyValue::DateTime(*now),
        ]));
    }
}

/// `joined_sessions`, logged into a log of its own and merged in order.
struct Joins<'a> {
    case: &'a Value,
    log: Vec<PyValue>,
}

impl Sessions for Joins<'_> {
    fn joined_sessions(
        &mut self,
        events: &[PyValue],
        harness_session: &[u32],
        worktree_root: Option<&[u32]>,
    ) -> BTreeSet<PyStr> {
        self.log.push(list(vec![
            PyValue::str("joined"),
            PyValue::int(events.len() as i128),
            st(harness_session),
            worktree_root.map_or(PyValue::None, st),
        ]));
        strs(&self.case["joined"]).into_iter().collect()
    }
}

fn opt(case: &Value, name: &str) -> Option<PyStr> {
    case[name].as_str().map(cps)
}

fn answer_list(r: Result<Vec<PyStr>, SettleError>) -> PyValue {
    match r {
        Ok(v) => ids(&v),
        Err(e) => err(e),
    }
}

fn err(e: SettleError) -> PyValue {
    let name = match e {
        SettleError::OverflowError => "OverflowError",
        SettleError::TypeError => "TypeError",
    };
    PyValue::Dict(vec![(PyValue::str("err"), PyValue::str(name))])
}

/// The answer and the log of one function row.
pub fn run(kind: &str, case: &Value) -> (PyValue, Vec<PyValue>) {
    let now = dt(case["now"].as_str().unwrap_or(T));
    let sid = opt(case, "sid").unwrap_or_else(|| cps("S"));
    let harness = opt(case, "h");
    let mut f = Fakes::new(case);
    let answer = match kind {
        "shown" => {
            let reply = opt(case, "reply").unwrap_or_else(|| cps(REPLY));
            let sessions = strs(&case["ss"]);
            let exclude = strs(&case["ex"]);
            let pid = opt(case, "pid");
            let cm = opt(case, "cm");
            let args = Shown {
                session_id: &sid,
                assistant_response: &reply,
                sessions: &sessions,
                harness: harness.as_deref(),
                exclude: &exclude,
                prompt_id: pid.as_deref(),
                client_model: cm.as_deref(),
                now: &now,
            };
            // One value plays store, recorder and state, as in the generator.
            let shared = std::cell::RefCell::new(Fakes::new(case));
            let out = rs::settle_shown(
                &mut Proxy(&shared),
                &mut Proxy(&shared),
                &mut Proxy(&shared),
                &args,
            );
            f.log = shared.into_inner().log;
            ids(&out)
        }
        "unused" => {
            let k = strs(&case["key"]);
            let key: Key = if k.is_empty() {
                (cps("claude-code"), cps("S"))
            } else {
                (k[0].clone(), k[1].clone())
            };
            let wt = opt(case, "wt");
            let tf = opt(case, "tf").unwrap_or_else(|| cps("stop_hook"));
            let tel = case["tel"].as_bool().unwrap_or(true);
            let shared = std::cell::RefCell::new(Fakes::new(case));
            let mut joins = JoinsProxy(&shared, case);
            let out = rs::settle_unused(
                &mut Proxy(&shared),
                &mut Proxy(&shared),
                &mut joins,
                &key,
                &now,
                wt.as_deref(),
                &tf,
                tel,
                |spec| {
                    shared.borrow_mut().log.push(list(vec![
                        PyValue::str("recorder"),
                        st(spec.session_id),
                        PyValue::Bool(spec.enabled),
                        spec.worktree_root.map_or(PyValue::None, st),
                        st(spec.triggered_from),
                    ]));
                    Proxy(&shared)
                },
            );
            let a = answer_list(out);
            f.log = shared.into_inner().log;
            a
        }
        "prev" => match rs::previous_audit_at(&mut f, &sid, &now) {
            Ok(Some(d)) => PyValue::DateTime(d),
            Ok(None) => PyValue::None,
            Err(e) => err(e),
        },
        "review" => {
            let sessions: BTreeSet<PyStr> = if case.get("ss").is_some() {
                strs(&case["ss"]).into_iter().collect()
            } else {
                [cps("S")].into_iter().collect()
            };
            let since = dt(case["since"].as_str().unwrap_or(T));
            let shared = std::cell::RefCell::new(Fakes::new(case));
            let out = rs::review_unanswered(
                &mut Proxy(&shared),
                &mut Proxy(&shared),
                &mut Proxy(&shared),
                &sid,
                &sessions,
                &since,
                harness.as_deref(),
                &now,
                |root| {
                    shared
                        .borrow_mut()
                        .log
                        .push(list(vec![PyValue::str("chain"), st(root)]));
                    let name: String = root.iter().map(|&c| char::from_u32(c).unwrap()).collect();
                    match case["chains"].get(&name) {
                        Some(v) => (v[0].as_bool().unwrap(), v[1].as_bool().unwrap()),
                        None => (true, false),
                    }
                },
            );
            let a = answer_list(out);
            f.log = shared.into_inner().log;
            a
        }
        other => panic!("unknown row kind {other}"),
    };
    (answer, f.log)
}

/// The generator's `REPLY`.
pub const REPLY: &str =
    "I will run the integration suite with pytest fixtures for the database setup, as before.";

/// One shared `Fakes` behind each trait.
pub struct Proxy<'r, 'a>(pub &'r std::cell::RefCell<Fakes<'a>>);

impl SettleStore for Proxy<'_, '_> {
    fn events_of_kinds(&mut self, kinds: &[&str], since: &DateTime) -> Vec<PyValue> {
        self.0.borrow_mut().events_of_kinds(kinds, since)
    }
    fn events_since(&mut self, since: &DateTime) -> Vec<PyValue> {
        self.0.borrow_mut().events_since(since)
    }
    fn load_one(&mut self, id: &[u32]) -> Result<PyStr, LoadFailure> {
        self.0.borrow_mut().load_one(id)
    }
}

impl Recorder for Proxy<'_, '_> {
    fn record(&mut self, kind: &str, fields: Vec<(PyValue, PyValue)>) {
        self.0.borrow_mut().record(kind, fields)
    }
}

impl SettleState for Proxy<'_, '_> {
    fn segment(&mut self, key: &Key, now: &DateTime) -> SegmentView {
        self.0.borrow_mut().segment(key, now)
    }
    fn take_closed(&mut self, key: &Key) -> Option<SegmentView> {
        self.0.borrow_mut().take_closed(key)
    }
    fn note_settled(&mut self, key: &Key, v: &[PyStr], now: &DateTime) {
        self.0.borrow_mut().note_settled(key, v, now)
    }
    fn note_review_roots(&mut self, key: &Key, v: &[PyStr], now: &DateTime) {
        self.0.borrow_mut().note_review_roots(key, v, now)
    }
}

struct JoinsProxy<'r, 'a>(&'r std::cell::RefCell<Fakes<'a>>, &'a Value);

impl Sessions for JoinsProxy<'_, '_> {
    fn joined_sessions(
        &mut self,
        events: &[PyValue],
        harness_session: &[u32],
        worktree_root: Option<&[u32]>,
    ) -> BTreeSet<PyStr> {
        let mut j = Joins {
            case: self.1,
            log: Vec::new(),
        };
        let out = j.joined_sessions(events, harness_session, worktree_root);
        self.0.borrow_mut().log.extend(j.log);
        out
    }
}
