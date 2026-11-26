//! Scripted fakes for `recall_prompt`, as `tests/contract/gen_recall_prompt.py`
//! writes them: every call the fakes see goes into one log, in order.

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::BTreeSet;
use std::rc::Rc;

use bettermemory_core::audit::report::{Entry, MissHit, MissReport, Verdict};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{repr_str, PyStr, PyValue};
use bettermemory_core::recall_prompt::{
    self as rp, Delivery, DeliveryGate, PromptError, PromptSessions, PromptTurn, PyException,
};
use bettermemory_core::recall_settle::{
    Key, LoadFailure, Recorder, RecorderSpec, SegmentView, SettleError, SettleState, SettleStore,
};
use serde_json::Value;

pub const T: &str = "2026-01-01T12:00:00+00:00";

pub type Log = Rc<RefCell<Vec<PyValue>>>;
pub type Turn = PromptTurn<Fake, PyStr, PyStr, Option<PyStr>>;

pub fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

pub fn dt(iso: &str) -> DateTime {
    DateTime::from_isoformat(&cps(iso)).expect("an isoformat")
}

/// The generator's `_plain` form (and plain case JSON) as a `PyValue`.
pub fn pv(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) if n.is_f64() => PyValue::Float(n.as_f64().unwrap()),
        Value::Number(n) => PyValue::Int(n.to_string()),
        Value::String(s) => PyValue::Str(cps(s)),
        Value::Array(items) => PyValue::List(items.iter().map(pv).collect()),
        Value::Object(map) => {
            if let (1, Some(Value::Array(pairs))) = (map.len(), map.get("d")) {
                return PyValue::Dict(pairs.iter().map(|p| (pv(&p[0]), pv(&p[1]))).collect());
            }
            if let (1, Some(Value::String(iso))) = (map.len(), map.get("dt")) {
                return PyValue::DateTime(dt(iso));
            }
            PyValue::Dict(
                map.iter()
                    .map(|(k, v)| (PyValue::Str(cps(k)), pv(v)))
                    .collect(),
            )
        }
    }
}

fn st(s: &[u32]) -> PyValue {
    PyValue::Str(s.to_vec())
}

fn ost(s: Option<&[u32]>) -> PyValue {
    s.map_or(PyValue::None, st)
}

fn l(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

fn set_value(set: &BTreeSet<PyStr>) -> PyValue {
    l(set.iter().map(|s| st(s)).collect())
}

pub fn opt(case: &Value, name: &str) -> Option<PyStr> {
    case[name].as_str().map(cps)
}

fn strs(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .map(|a| a.iter().map(|x| cps(x.as_str().unwrap())).collect())
        .unwrap_or_default()
}

fn entries(v: &Value) -> Vec<Entry> {
    v.as_array()
        .map(|a| {
            a.iter()
                .map(|e| {
                    e.as_object()
                        .unwrap()
                        .iter()
                        .map(|(k, x)| (cps(k), cps(x.as_str().unwrap())))
                        .collect()
                })
                .collect()
        })
        .unwrap_or_default()
}

/// The report `_report` builds from a case's `rep`.
pub fn report(spec: &Value) -> MissReport {
    let verdict = match spec["v"].as_str().unwrap_or("miss") {
        "miss" => Verdict::Miss,
        "ok" => Verdict::Ok,
        _ => Verdict::NoSignal,
    };
    let top_hits = spec["hits"]
        .as_array()
        .map(|hits| {
            hits.iter()
                .map(|h| MissHit {
                    id: cps(h[0].as_str().unwrap()),
                    score: h[1].as_f64().unwrap(),
                    relevance: cps(h[2].as_str().unwrap()),
                    scopes: strs(&h[3]),
                    snippet: cps(h[4].as_str().unwrap()),
                    matched_unique: h[5].as_i64().unwrap(),
                    query_unique: h[6].as_i64().unwrap(),
                    relevance_v2: cps(h[7].as_str().unwrap()),
                })
                .collect()
        })
        .unwrap_or_default();
    MissReport {
        verdict,
        checked_at: dt(T),
        session_id: cps("S"),
        lookback_seconds: 300,
        recent_retrieval_count: 0,
        threshold_rule: cps("r1"),
        top_hits,
        probe_query: opt(spec, "pq"),
        no_signal_reason: opt(spec, "nsr"),
        suppressed_by: None,
    }
}

/// The store, the joins, the gate and the state, over one case and log.
#[derive(Clone)]
pub struct Fake {
    pub case: Value,
    pub log: Log,
}

impl Fake {
    fn push(&self, v: PyValue) {
        self.log.borrow_mut().push(v);
    }
    fn events(&self, name: &str) -> Vec<PyValue> {
        self.case[name]
            .as_array()
            .map(|a| a.iter().map(pv).collect())
            .unwrap_or_default()
    }
}

impl SettleStore for Fake {
    fn events_of_kinds(&mut self, kinds: &[&str], since: &DateTime) -> Vec<PyValue> {
        self.push(l(vec![
            PyValue::str("eok"),
            l(kinds.iter().map(|k| PyValue::str(k)).collect()),
            PyValue::DateTime(*since),
        ]));
        self.events("audits")
    }
    fn events_since(&mut self, since: &DateTime) -> Vec<PyValue> {
        self.push(l(vec![PyValue::str("es"), PyValue::DateTime(*since)]));
        self.events("recent")
    }
    fn load_one(&mut self, _id: &[u32]) -> Result<PyStr, LoadFailure> {
        unreachable!("recall_prompt loads no record")
    }
}

impl PromptSessions for Fake {
    fn bridged_session(
        &mut self,
        events: &[PyValue],
        harness_session: &[u32],
        worktree_root: Option<&[u32]>,
    ) -> Option<PyStr> {
        self.push(l(vec![
            PyValue::str("bridge"),
            PyValue::int(events.len() as i128),
            st(harness_session),
            ost(worktree_root),
        ]));
        opt(&self.case, "bridge")
    }
    fn disabled_scopes(
        &mut self,
        events: &[PyValue],
        worktree_root: Option<&[u32]>,
        server_session: Option<&[u32]>,
    ) -> BTreeSet<PyStr> {
        self.push(l(vec![
            PyValue::str("scopes"),
            PyValue::int(events.len() as i128),
            ost(worktree_root),
            ost(server_session),
        ]));
        strs(&self.case["scopes"]).into_iter().collect()
    }
}

impl DeliveryGate<PyStr, PyStr, Option<PyStr>, PyStr> for Fake {
    fn delivery_membership(
        &mut self,
        resolution: Option<&PyStr>,
        admission: &PyStr,
    ) -> Vec<(PyValue, PyValue)> {
        self.push(l(vec![
            PyValue::str("dm"),
            ost(resolution.map(|r| r.as_slice())),
            st(admission),
        ]));
        match self.case["dm"].as_array() {
            Some(pairs) => {
                // As `dict(pairs)`: a repeated key keeps its first place.
                let mut out: Vec<(PyValue, PyValue)> = Vec::new();
                for p in pairs {
                    let (k, v) = (pv(&p[0]), pv(&p[1]));
                    match out.iter_mut().find(|(x, _)| *x == k) {
                        Some(slot) => slot.1 = v,
                        None => out.push((k, v)),
                    }
                }
                out
            }
            None => vec![
                (PyValue::str("checkout"), PyValue::None),
                (PyValue::str("membership_rule"), PyValue::str("r")),
            ],
        }
    }
    fn rc3_admitted(
        &mut self,
        memories: &[PyStr],
        caller: &Option<PyStr>,
        project_only: bool,
    ) -> Vec<PyStr> {
        self.push(l(vec![
            PyValue::str("rc3"),
            l(memories.iter().map(|m| st(m)).collect()),
            ost(caller.as_deref()),
            PyValue::Bool(project_only),
        ]));
        strs(&self.case["rc3"])
    }
}

impl SettleState for Fake {
    fn segment(&mut self, _key: &Key, _now: &DateTime) -> SegmentView {
        unreachable!()
    }
    fn take_closed(&mut self, _key: &Key) -> Option<SegmentView> {
        unreachable!()
    }
    fn note_settled(&mut self, _key: &Key, _ids: &[PyStr], _now: &DateTime) {
        unreachable!()
    }
    fn note_review_roots(&mut self, _key: &Key, _roots: &[PyStr], _now: &DateTime) {
        unreachable!()
    }
    fn forget(&mut self, key: &Key) {
        self.push(l(vec![
            PyValue::str("forget"),
            l(vec![st(&key.0), st(&key.1)]),
        ]));
    }
}

/// The recorder the factory builds.
pub struct Rec {
    pub fake: Fake,
}

impl Recorder for Rec {
    fn record(&mut self, _kind: &str, _fields: Vec<(PyValue, PyValue)>) {
        unreachable!("recall_prompt writes through try_record")
    }
    fn suppressed(&self) -> bool {
        self.fake.case["sup"].as_bool().unwrap_or(false)
    }
    fn try_record(
        &mut self,
        kind: &str,
        fields: Vec<(PyValue, PyValue)>,
    ) -> Result<(), PyException> {
        self.fake.push(l(vec![
            PyValue::str("rec"),
            PyValue::str(kind),
            PyValue::Dict(fields),
        ]));
        match opt(&self.fake.case, "rraise") {
            Some(message) => Err(PyException {
                class: "OSError".into(),
                message,
            }),
            None => Ok(()),
        }
    }
}

pub fn factory(fake: &Fake) -> impl FnOnce(&RecorderSpec<'_>) -> Rec + '_ {
    move |spec| {
        fake.push(l(vec![
            PyValue::str("recorder"),
            st(spec.session_id),
            PyValue::Bool(spec.enabled),
            ost(spec.worktree_root),
            st(spec.triggered_from),
        ]));
        Rec { fake: fake.clone() }
    }
}

pub fn turn(case: &Value, log: &Log) -> Turn {
    let key = case["key"]
        .as_array()
        .map(|k| (cps(k[0].as_str().unwrap()), cps(k[1].as_str().unwrap())))
        .unwrap_or_else(|| (cps("claude-code"), cps("S")));
    let search_mode = match &case["sm"] {
        Value::Null if case.get("sm").is_some() => None,
        Value::Null => Some(PyStr::new()),
        v => Some(cps(v.as_str().unwrap())),
    };
    PromptTurn {
        store: Fake {
            case: case.clone(),
            log: log.clone(),
        },
        telemetry: case["tel"].as_bool().unwrap_or(true),
        search_mode,
        mode: opt(case, "mode").unwrap_or_else(|| cps("project")),
        query: opt(case, "q").unwrap_or_else(|| cps("how do I run the tests")),
        resolution: opt(case, "res"),
        surface: opt(case, "surface").unwrap_or_else(|| cps(rp::PROMPT_RECALL)),
        admission: cps("adm"),
        remint: None,
        now: dt(case["now"].as_str().unwrap_or(T)),
        key,
        caller: opt(case, "wt"),
        prompt_id: opt(case, "pid"),
        transcript_path: opt(case, "tp"),
    }
}

/// `Admission.on` over a named admission.
pub fn on(log: &Log) -> impl FnMut(&PyStr, &str) -> PyStr + '_ {
    move |name, surface| {
        log.borrow_mut()
            .push(l(vec![PyValue::str("on"), st(name), PyValue::str(surface)]));
        let mut out = name.clone();
        out.push(u32::from('@'));
        out.extend(cps(surface));
        out
    }
}

/// The exception the case's `raise` makes, as Python names and words it.
fn raised(spec: &Value) -> PyException {
    let a = spec.as_array().unwrap();
    let class = a[0].as_str().unwrap();
    match (class, a.len()) {
        ("OSError", 1) => PyException {
            class: class.into(),
            message: PyStr::new(),
        },
        ("OSError", 3) => PyException {
            class: "FileNotFoundError".into(),
            message: cps(&format!("[Errno {}] {}", a[1], a[2].as_str().unwrap())),
        },
        ("KeyError", _) => PyException {
            class: class.into(),
            message: repr_str(&cps(a[1].as_str().unwrap())),
        },
        (_, 1) => PyException {
            class: class.into(),
            message: PyStr::new(),
        },
        _ => PyException {
            class: class.into(),
            message: cps(a[1].as_str().unwrap()),
        },
    }
}

fn err(name: &str) -> PyValue {
    PyValue::Dict(vec![(PyValue::str("err"), PyValue::str(name))])
}

fn settle_err(e: SettleError) -> PyValue {
    err(match e {
        SettleError::OverflowError => "OverflowError",
        SettleError::TypeError => "TypeError",
    })
}

fn prompt_err(e: PromptError) -> PyValue {
    match e {
        PromptError::Audit(_) => err("ValueError"),
        PromptError::Raised(exc) => err(&exc.class),
    }
}

/// One row: the step over the case, its answer and the log.
pub fn run(kind: &str, case: &Value) -> (PyValue, Vec<PyValue>) {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let mut t = turn(case, &log);
    let mut fake = t.store.clone();
    let recent: Vec<PyValue> = fake.events("recent");
    let ss = opt(case, "bridge");
    let excluded: BTreeSet<PyStr> = strs(&case["scopes"]).into_iter().collect();
    let mut minted = 0;
    let mut mint = move || {
        minted += 1;
        format!("E{minted}")
    };
    let answer = match kind {
        "win" => {
            let seg = &case["seg"];
            let view = SegmentView {
                started_at: dt(seg["at"].as_str().unwrap_or(T)),
                segment: if seg.get("n").is_some() {
                    pv(&seg["n"])
                } else {
                    PyValue::int(0)
                },
                settled: BTreeSet::new(),
                review_roots: BTreeSet::new(),
                shown_by: Default::default(),
                shown: Default::default(),
                deliveries: 0,
                marker: None,
            };
            match rp::prompt_window(&mut t, &view, &mut fake) {
                Ok(w) => l(vec![
                    PyValue::DateTime(w.own_since),
                    PyValue::int(w.recent.len() as i128),
                    ost(w.server_session.as_deref()),
                    set_value(&w.excluded_scopes),
                ]),
                Err(e) => settle_err(e),
            }
        }
        "settle" => {
            let mut settle = |_store: &mut Fake, a: rp::SettleArgs<'_, PyStr>| {
                log.borrow_mut().push(l(vec![
                    PyValue::str("settle"),
                    st(a.session_id),
                    ost(a.transcript_path),
                    st(&a.admission),
                    set_value(a.excluded_scopes),
                    PyValue::int(a.recent.len() as i128),
                    ost(a.server_session),
                    ost(a.worktree_root),
                    PyValue::DateTime(*a.now),
                    a.previous_audit.map_or(PyValue::None, PyValue::DateTime),
                    PyValue::str(a.triggered_from),
                    ost(a.exclude_prompt_id),
                ]));
                match case.get("raise") {
                    Some(spec) => Err(raised(spec)),
                    None => Ok(()),
                }
            };
            let warning = rp::settle_before_probe(
                &mut t,
                &recent,
                ss.as_deref(),
                &excluded,
                &mut on(&log),
                &mut settle,
            );
            if let Some(text) = &warning {
                log.borrow_mut()
                    .push(l(vec![PyValue::str("warn"), st(text)]));
            }
            PyValue::None
        }
        "probe" => {
            let got = rp::prompt_probe(
                &mut t,
                &recent,
                ss.as_deref(),
                &excluded,
                &mut on(&log),
                |_store: &mut Fake, a: rp::ProbeArgs<'_, PyStr>| {
                    log.borrow_mut().push(l(vec![
                        PyValue::str("probe"),
                        st(a.user_message),
                        st(a.session_id),
                        ost(a.server_session),
                        st(&a.admission),
                        set_value(a.excluded_scopes),
                        PyValue::int(a.recent.len() as i128),
                        ost(a.worktree_root),
                        PyValue::DateTime(*a.now),
                        PyValue::Bool(a.floor),
                    ]));
                    Ok::<_, ()>(report(&case["rep"]))
                },
            );
            got.unwrap().to_dict()
        }
        "hold" => {
            let rep = report(&case["rep"]);
            let segment = case["seg"].as_i64().unwrap_or(1);
            let cause = opt(case, "cause").unwrap_or_else(|| cps("no_hits"));
            let held = entries(&case["held"]);
            let mut gate = fake.clone();
            match rp::record_hold(
                &t,
                &rep,
                segment,
                &cause,
                &held,
                &mut gate,
                factory(&fake),
                &mut mint,
            ) {
                Ok(()) => PyValue::None,
                Err(e) => prompt_err(e),
            }
        }
        "deliv" => {
            let rep = report(&case["rep"]);
            let chosen: Vec<rp::Chosen<PyStr, ()>> = strs(&case["chosen"])
                .into_iter()
                .map(|id| {
                    (
                        MissHit {
                            id,
                            score: 0.0,
                            relevance: PyStr::new(),
                            scopes: Vec::new(),
                            snippet: PyStr::new(),
                            matched_unique: 0,
                            query_unique: 0,
                            relevance_v2: PyStr::new(),
                        },
                        PyStr::new(),
                        (),
                        None,
                    )
                })
                .collect();
            let block = opt(case, "block").unwrap_or_else(|| cps("block"));
            let shown = strs(&case["shown"]);
            let eid = opt(case, "eid").unwrap_or_else(|| cps("D1"));
            let supp = entries(&case["supp"]);
            let cand = strs(&case["cand"]);
            let d = Delivery {
                block: &block,
                shown_ids: &shown,
                event_id: &eid,
                segment: case["seg"].as_i64().unwrap_or(1),
                chosen: &chosen,
                suppressed: &supp,
                rc3_candidates: &cand,
            };
            let mut state = fake.clone();
            let mut gate = fake.clone();
            match rp::record_delivery(
                &t,
                &rep,
                &mut state,
                &d,
                &mut gate,
                factory(&fake),
                &mut mint,
            ) {
                Ok(fields) => fields,
                Err(e) => prompt_err(e),
            }
        }
        other => panic!("unknown kind {other}"),
    };
    let out = log.borrow().clone();
    (answer, out)
}
