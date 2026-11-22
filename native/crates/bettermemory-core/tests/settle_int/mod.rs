//! Scripted fakes for `recall_settle::interrupted`, as
//! `tests/contract/gen_recall_settle.py` builds them for
//! `recall_settle_interrupted.jsonl`: every call is logged in the
//! generator's order and compact form (a float as `{"f": repr}`, a set as
//! `{"set": sorted}`).

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::BTreeSet;

use bettermemory_core::audit::report::{AuditError, MissHit, MissReport, Verdict};
use bettermemory_core::audit::THRESHOLD_RULE_V1;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::hook::ATTRIBUTION_LOOKBACK_SECONDS;
use bettermemory_core::pyvalue::{repr, PyStr, PyValue};
use bettermemory_core::recall_settle::interrupted::{
    probe_message, settle_interrupted, Fault, Interrupted, Message, ProbeCall, Prober, SearchPool,
    Settings, TranscriptHost,
};
use bettermemory_core::recall_settle::{LoadFailure, Recorder, SettleStore};
use bettermemory_core::transcript::InterruptedTurn;
use serde_json::Value;

pub const T: &str = "2026-01-01T12:00:00+00:00";

pub fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

pub fn dt(iso: &str) -> DateTime {
    DateTime::from_isoformat(&cps(iso)).expect("an isoformat")
}

pub fn s(t: &str) -> PyValue {
    PyValue::Str(cps(t))
}

/// The golden's compact form read back; an object other than `d`, `tu`
/// and `dt` stays a dict of its sorted keys.
pub fn plain(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => PyValue::Int(n.to_string()),
        Value::String(t) => s(t),
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
            PyValue::Dict(map.iter().map(|(k, v)| (s(k), plain(v))).collect())
        }
    }
}

/// A value in the compact form: a float as `{"f": repr}`.
pub fn norm(v: PyValue) -> PyValue {
    match &v {
        PyValue::Float(x) => {
            let text = repr(&PyValue::Float(*x)).unwrap();
            PyValue::Dict(vec![(s("f"), PyValue::Str(text))])
        }
        PyValue::List(items) => PyValue::List(items.iter().cloned().map(norm).collect()),
        PyValue::Tuple(items) => PyValue::Tuple(items.iter().cloned().map(norm).collect()),
        PyValue::Dict(pairs) => PyValue::Dict(
            pairs
                .iter()
                .map(|(k, x)| (norm(k.clone()), norm(x.clone())))
                .collect(),
        ),
        _ => v,
    }
}

fn set_value(set: &BTreeSet<PyStr>) -> PyValue {
    let items = set.iter().cloned().map(PyValue::Str).collect();
    PyValue::Dict(vec![(s("set"), PyValue::List(items))])
}

fn opt(v: Option<&[u32]>) -> PyValue {
    v.map_or(PyValue::None, |x| PyValue::Str(x.to_vec()))
}

fn get<'a>(case: &'a Value, key: &str) -> Option<&'a Value> {
    case.get(key)
}

fn text_or(case: &Value, key: &str, default: Option<&str>) -> Option<PyStr> {
    match get(case, key) {
        None => default.map(cps),
        Some(Value::Null) => None,
        Some(v) => Some(cps(v.as_str().unwrap())),
    }
}

/// The error the generator writes: `{"err": name, "msg": text}`.
pub fn err(name: &str, msg: &[u32]) -> PyValue {
    PyValue::Dict(vec![
        (s("err"), s(name)),
        (s("msg"), PyValue::Str(msg.to_vec())),
    ])
}

/// A `RuntimeError` the expanduser closure raises.
pub type Raised = PyStr;

pub fn fault_value(f: Fault<Raised>) -> PyValue {
    match f {
        Fault::Raised(msg) => err("RuntimeError", &msg),
        Fault::OverflowError => err("OverflowError", &cps("date value out of range")),
        Fault::Audit(AuditError::ValueError(msg)) => err("ValueError", &msg),
        Fault::Os(e) => err(e.class.name(), &cps(e.errno.unwrap_or(""))),
        Fault::NotPorted => s("NotPorted"),
        other => panic!("not in the fixtures: {other:?}"),
    }
}

pub struct Fake {
    pub case: Value,
    pub log: RefCell<Vec<PyValue>>,
    pub calls: RefCell<usize>,
}

impl Fake {
    fn push(&self, items: Vec<PyValue>) {
        self.log.borrow_mut().push(PyValue::List(items));
    }
}

pub struct Store<'a>(pub &'a Fake);

impl SettleStore for Store<'_> {
    fn events_of_kinds(&mut self, kinds: &[&str], since: &DateTime) -> Vec<PyValue> {
        self.0.push(vec![
            s("eok"),
            PyValue::List(kinds.iter().map(|k| s(k)).collect()),
            PyValue::DateTime(*since),
        ]);
        get(&self.0.case, "ev")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(plain).collect())
            .unwrap_or_default()
    }
    fn events_since(&mut self, _since: &DateTime) -> Vec<PyValue> {
        unreachable!()
    }
    fn load_one(&mut self, _id: &[u32]) -> Result<PyStr, LoadFailure> {
        unreachable!()
    }
    fn has_memories(&mut self) -> Option<bool> {
        self.0.push(vec![s("hm")]);
        Some(
            get(&self.0.case, "hm")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        )
    }
}

pub struct Rec<'a>(pub &'a Fake);

impl Recorder for Rec<'_> {
    fn record(&mut self, kind: &str, fields: Vec<(PyValue, PyValue)>) {
        self.0
            .push(vec![s("rec"), s(kind), norm(PyValue::Dict(fields))]);
    }
}

/// The report the probe answers for one recorded `rep`.
pub fn report(rep: &Value, session_id: &[u32], now: &DateTime) -> MissReport {
    let verdict = match rep.get("v").and_then(Value::as_str).unwrap_or("ok") {
        "miss" => Verdict::Miss,
        "ok" => Verdict::Ok,
        "no_signal" => Verdict::NoSignal,
        v => panic!("verdict {v}"),
    };
    let hits = rep
        .get("hits")
        .and_then(Value::as_array)
        .map(|hs| {
            hs.iter()
                .map(|h| MissHit {
                    id: cps(h[0].as_str().unwrap()),
                    score: h[1].as_f64().unwrap(),
                    relevance: cps(h[2].as_str().unwrap()),
                    scopes: Vec::new(),
                    snippet: Vec::new(),
                    matched_unique: h[3].as_i64().unwrap(),
                    query_unique: h[4].as_i64().unwrap(),
                    relevance_v2: cps(h[5].as_str().unwrap()),
                })
                .collect()
        })
        .unwrap_or_default();
    let text = |k: &str| rep.get(k).and_then(Value::as_str).map(cps);
    MissReport {
        verdict,
        checked_at: *now,
        session_id: session_id.to_vec(),
        lookback_seconds: ATTRIBUTION_LOOKBACK_SECONDS,
        recent_retrieval_count: rep.get("rrc").and_then(Value::as_i64).unwrap_or(0),
        threshold_rule: cps(THRESHOLD_RULE_V1),
        top_hits: hits,
        probe_query: text("pq"),
        no_signal_reason: text("nsr"),
        suppressed_by: text("sb"),
    }
}

pub fn settings(case: &Value) -> Settings {
    Settings {
        search_mode: text_or(case, "sm", Some("hybrid")).unwrap(),
        half_life_days: get(case, "hl").and_then(Value::as_f64).unwrap_or(30.0),
        conversational: get(case, "conv").and_then(Value::as_bool).unwrap_or(true),
        telemetry_enabled: get(case, "tel").and_then(Value::as_bool).unwrap_or(true),
    }
}

type Pool = SearchPool<PyStr, &'static str>;

/// Runs one case of kind `probe` or `int`: the answer in compact form and
/// the log.
pub fn run(kind: &str, case: &Value) -> (PyValue, Vec<PyValue>) {
    let fake = Fake {
        case: case.clone(),
        log: RefCell::new(Vec::new()),
        calls: RefCell::new(0),
    };
    let f = &fake;
    let now = dt(get(case, "now").and_then(Value::as_str).unwrap_or(T));
    let scopes: BTreeSet<PyStr> = match get(case, "xs") {
        Some(Value::Array(a)) => a.iter().map(|x| cps(x.as_str().unwrap())).collect(),
        _ => [cps("s1"), cps("s2")].into_iter().collect(),
    };
    let recent: Vec<PyValue> = get(case, "recent")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(plain).collect())
        .unwrap_or_default();
    let sid = text_or(case, "sid", Some("S")).unwrap();
    let ss = text_or(case, "ss", None);
    let wt = text_or(case, "wt", None);
    let adm = cps("ADM");
    let cfg = settings(case);
    let mut pool = |_: &mut Store<'_>, q: &[u32], xs: &BTreeSet<PyStr>, a: &PyStr| {
        f.push(vec![
            s("pool"),
            PyValue::Str(q.to_vec()),
            PyValue::List(xs.iter().cloned().map(PyValue::Str).collect()),
            PyValue::Str(a.clone()),
        ]);
        let sizes = get(&f.case, "pool").and_then(Value::as_array);
        let n = sizes.map_or(1, |a| {
            a[(*f.calls.borrow()).min(a.len() - 1)].as_u64().unwrap()
        });
        Ok::<Pool, Raised>(SearchPool {
            memories: (0..n).map(|i| cps(&format!("r{i}"))).collect(),
            corpus_stats_provider: "CSP",
        })
    };
    let mut probe = |c: ProbeCall<'_, PyStr, &'static str, PyStr>| {
        let kw = vec![
            (s("recent_events"), PyValue::List(c.recent_events.to_vec())),
            (s("session_id"), PyValue::Str(c.session_id.to_vec())),
            (s("retrieval_session_id"), opt(c.retrieval_session_id)),
            (s("now"), PyValue::DateTime(*c.now)),
            (
                s("lookback_seconds"),
                PyValue::int(c.lookback_seconds.into()),
            ),
            (s("admission"), PyValue::Str(c.admission.clone())),
            (s("worktree_root"), opt(c.worktree_root)),
            (s("excluded_scopes"), set_value(c.excluded_scopes)),
            (s("mode"), PyValue::Str(c.mode.clone())),
            (s("half_life_days"), PyValue::Float(c.half_life_days)),
            (s("conversational"), PyValue::Bool(c.conversational)),
            (s("corpus_stats_provider"), s(c.corpus_stats_provider)),
            (s("floor"), PyValue::Bool(c.floor)),
            (s("searched"), PyValue::Bool(c.searched)),
        ];
        f.push(vec![
            s("probe"),
            PyValue::List(c.memories.into_iter().map(PyValue::Str).collect()),
            PyValue::Str(c.user_message.to_vec()),
            norm(PyValue::Dict(kw)),
        ]);
        let reps = get(&f.case, "reps").and_then(Value::as_array);
        let empty = serde_json::json!({});
        let i = *f.calls.borrow();
        let rep = reps.map_or(&empty, |a| &a[i.min(a.len() - 1)]);
        *f.calls.borrow_mut() += 1;
        Ok::<MissReport, Raised>(report(rep, c.session_id, c.now))
    };
    let mut prober = Prober {
        resolve_pool: &mut pool,
        probe: &mut probe,
    };
    let mut store = Store(f);
    let answer = if kind == "probe" {
        let msg = text_or(case, "msg", Some("how do we deploy")).unwrap();
        let floor = get(case, "floor").and_then(Value::as_bool).unwrap_or(true);
        match probe_message(
            &mut store,
            &cfg,
            &mut prober,
            &Message {
                user_message: &msg,
                session_id: &sid,
                server_session: ss.as_deref(),
                admission: &adm,
                excluded_scopes: &scopes,
                recent: &recent,
                worktree_root: wt.as_deref(),
                now: &now,
                floor,
            },
        ) {
            Ok(r) => norm(r.to_dict()),
            Err(e) => fault_value(e),
        }
    } else {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/t", Node::Dir);
        if get(case, "file").and_then(Value::as_bool).unwrap_or(true) {
            let node = match get(case, "node").and_then(Value::as_str) {
                None => Node::File(b"{}\n".to_vec()),
                Some("fifo") => Node::Fifo,
                Some("socket") => Node::Socket,
                Some("device") => Node::Device,
                Some("dir") => Node::Dir,
                Some(other) => panic!("no node {other}"),
            };
            fs.insert(b"/t/ok.jsonl", node);
        }
        if get(case, "locked")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            fs.set_mode(b"/t", 0);
        }
        // The expanded path as code points, in place of the path itself.
        let xp: Option<PyStr> = get(case, "xp")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(|c| c.as_u64().unwrap() as u32).collect());
        let nohome = get(case, "nohome")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mut expand = |p: &[u32]| {
            f.push(vec![s("path"), PyValue::Str(p.to_vec())]);
            if nohome {
                Err(cps("Could not determine home directory."))
            } else {
                Ok(xp.clone().unwrap_or_else(|| p.to_vec()))
            }
        };
        let turns_now = now;
        let mut turns = |_: &[u8], since: &DateTime| {
            f.push(vec![s("turns"), PyValue::DateTime(*since)]);
            let list = get(&f.case, "turns").and_then(Value::as_array);
            Ok(list
                .map(|a| {
                    a.iter()
                        .map(|t| {
                            let o = |i: usize| t[i].as_str().map(cps);
                            InterruptedTurn {
                                prompt_id: o(0),
                                prompt: o(1),
                                reply: o(2),
                                model: o(3),
                                at: turns_now,
                            }
                        })
                        .collect()
                })
                .unwrap_or_default())
        };
        let mut host = TranscriptHost {
            expanduser: &mut expand,
            fs: &fs,
            turns: &mut turns,
        };
        let tp = text_or(case, "tp", Some("/t/ok.jsonl"));
        let pa = get(case, "pa").and_then(Value::as_str).map(dt);
        let tf = text_or(case, "tf", Some("stop_hook")).unwrap();
        let ex = text_or(case, "ex", None);
        let got = settle_interrupted(
            &mut store,
            &cfg,
            &mut prober,
            &mut host,
            &Interrupted {
                session_id: &sid,
                transcript_path: tp.as_deref(),
                admission: &adm,
                excluded_scopes: &scopes,
                recent: &recent,
                server_session: ss.as_deref(),
                worktree_root: wt.as_deref(),
                now: &now,
                previous_audit: pa.as_ref(),
                triggered_from: &tf,
                exclude_prompt_id: ex.as_deref(),
            },
            |spec| {
                f.push(vec![
                    s("recorder"),
                    PyValue::Str(spec.session_id.to_vec()),
                    PyValue::Bool(spec.enabled),
                    opt(spec.worktree_root),
                    PyValue::Str(spec.triggered_from.to_vec()),
                ]);
                Rec(f)
            },
        );
        match got {
            Ok(ids) => PyValue::List(ids.into_iter().map(PyValue::Str).collect()),
            Err(e) => fault_value(e),
        }
    };
    let log = fake.log.borrow().clone();
    (answer, log)
}
