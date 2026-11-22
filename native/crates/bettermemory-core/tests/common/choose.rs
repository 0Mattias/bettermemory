//! Scripted fakes for `recall::choose`, as `tests/contract/gen_recall_choose.py`
//! builds them: every call is logged in the generator's order, in its
//! compact form (`gen_recall_settle._plain`), and `run` answers a case as
//! the generator writes its `o`.

use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::rc::Rc;

use bettermemory_core::audit::report::{MissHit, MissReport, Verdict};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::in_force::{Inbound, LinkReader, RecordView};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall::choose::{
    choose_hits, deliver_recall, ChoiceMemory, ChooseError, ChooseStore, ClaimState, Held,
    HookResult, PromptTurnView, Window,
};
use bettermemory_core::recall_settle::{Key, Sessions};
use bettermemory_core::store::provenance::TrustRow;
use serde_json::{json, Value};

pub use super::settle::{cps, dt, plain, T};

type Log = Rc<RefCell<Vec<PyValue>>>;

fn st(s: &[u32]) -> PyValue {
    PyValue::Str(s.to_vec())
}

fn ids(v: &[PyStr]) -> PyValue {
    PyValue::List(v.iter().map(|s| st(s)).collect())
}

fn sids(v: &[String]) -> PyValue {
    PyValue::List(v.iter().map(|s| PyValue::str(s)).collect())
}

pub fn text(s: &[u32]) -> String {
    s.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn push(log: &Log, items: Vec<PyValue>) {
    log.borrow_mut().push(PyValue::List(items));
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| a.iter().map(|x| x.as_str().unwrap().to_owned()).collect())
        .unwrap_or_default()
}

pub struct Store {
    case: Value,
    log: Log,
}

impl LinkReader for Store {
    fn inbound_links(
        &mut self,
        targets: &[String],
        types: &[&str],
    ) -> HashMap<String, Vec<Inbound>> {
        push(
            &self.log,
            vec![
                PyValue::str("inbound"),
                sids(targets),
                sids(&types.iter().map(|t| t.to_string()).collect::<Vec<_>>()),
            ],
        );
        let mut out = HashMap::new();
        for t in targets {
            if let Some(rows) = self.case["links"].get(t).and_then(Value::as_array) {
                let rows = rows
                    .iter()
                    .map(|r| {
                        let s = strs(r);
                        (s[0].clone(), s[1].clone(), s[2].clone())
                    })
                    .collect();
                out.insert(t.clone(), rows);
            }
        }
        out
    }

    fn held_ids(&mut self, asked: &[String]) -> HashSet<String> {
        push(&self.log, vec![PyValue::str("held"), sids(asked)]);
        let held = strs(&self.case["held"]);
        asked.iter().filter(|i| held.contains(i)).cloned().collect()
    }
}

impl ChooseStore for Store {
    fn events_of_kinds(&mut self, _kinds: &[&str], since: &DateTime) -> Vec<PyValue> {
        push(
            &self.log,
            vec![PyValue::str("eok"), PyValue::DateTime(*since)],
        );
        self.case["ev"]
            .as_array()
            .map(|a| a.iter().map(plain).collect())
            .unwrap_or_default()
    }

    fn load_many(&mut self, wanted: &[PyStr]) -> Vec<ChoiceMemory> {
        push(&self.log, vec![PyValue::str("load"), ids(wanted)]);
        let opt = |v: &Value| v.as_str().map(dt);
        wanted
            .iter()
            .filter_map(|id| {
                let m = self.case["mem"].get(text(id))?;
                Some(ChoiceMemory {
                    id: id.clone(),
                    body: cps(m[0].as_str().unwrap()),
                    updated: opt(&m[1]),
                    last_verified_at: opt(&m[2]),
                })
            })
            .collect()
    }

    fn trust_rows(&mut self, wanted: &[PyStr]) -> Option<HashMap<PyStr, TrustRow>> {
        push(&self.log, vec![PyValue::str("trust"), ids(wanted)]);
        let trust = self.case.get("trust").unwrap_or(&Value::Null);
        if trust.is_null() && self.case.get("trust").is_some() {
            return None;
        }
        Some(
            wanted
                .iter()
                .filter_map(|id| {
                    let label = trust.get(text(id))?.as_str()?;
                    Some((
                        id.clone(),
                        TrustRow {
                            label: label.to_owned(),
                            verified_at: None,
                        },
                    ))
                })
                .collect(),
        )
    }

    fn contradicted_at(&mut self, wanted: &[PyStr]) -> Vec<(PyStr, DateTime)> {
        push(&self.log, vec![PyValue::str("contra"), ids(wanted)]);
        // `contra` is a list of `[id, stamp]` pairs, so its order is the
        // reference's dict order.
        let Some(pairs) = self.case["contra"].as_array() else {
            return Vec::new();
        };
        pairs
            .iter()
            .map(|p| (p[0].as_str().unwrap(), p[1].as_str().unwrap()))
            .filter(|(k, _)| wanted.contains(&cps(k)))
            .map(|(k, v)| (cps(k), dt(v)))
            .collect()
    }

    fn standing_ids<V: RecordView>(&mut self, _admission: &mut V, asked: &[PyStr]) -> Vec<PyStr> {
        push(&self.log, vec![PyValue::str("standing"), ids(asked)]);
        let standing = strs(&self.case["standing"]);
        asked
            .iter()
            .filter(|i| standing.contains(&text(i)))
            .cloned()
            .collect()
    }

    fn pending_conflict_pairs(&mut self) -> Vec<(PyStr, PyStr, PyStr)> {
        push(&self.log, vec![PyValue::str("pairs")]);
        self.case["pairs"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|p| {
                        let s = strs(p);
                        (cps(&s[0]), cps(&s[1]), cps(&s[2]))
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}

pub struct View {
    deny: Vec<String>,
    log: Log,
}

impl RecordView for View {
    fn prime(&mut self, primed: &[String]) {
        push(&self.log, vec![PyValue::str("prime"), sids(primed)]);
    }

    fn admits_id(&mut self, id: &str) -> bool {
        push(&self.log, vec![PyValue::str("admits"), PyValue::str(id)]);
        !self.deny.iter().any(|d| d == id)
    }
}

pub struct Turn {
    pub store: Store,
    pub view: View,
    pub key: Key,
    pub now: DateTime,
    pub wt: Option<PyStr>,
    pub remint: Option<PyValue>,
}

impl PromptTurnView for Turn {
    type Store = Store;
    type View = View;
    fn parts(&mut self) -> (&mut Store, &mut View) {
        (&mut self.store, &mut self.view)
    }
    fn surface(&self) -> &[u32] {
        &[112, 114, 111, 109, 112, 116, 95, 114, 101, 99, 97, 108, 108]
    }
    fn key(&self) -> &Key {
        &self.key
    }
    fn now(&self) -> &DateTime {
        &self.now
    }
    fn worktree_root(&self) -> Option<&[u32]> {
        self.wt.as_deref()
    }
    fn remint(&self) -> Option<&PyValue> {
        self.remint.as_ref()
    }
}

pub struct Joins {
    case: Value,
    log: Log,
}

impl Sessions for Joins {
    fn joined_sessions(
        &mut self,
        events: &[PyValue],
        hs: &[u32],
        wt: Option<&[u32]>,
    ) -> BTreeSet<PyStr> {
        push(
            &self.log,
            vec![
                PyValue::str("joined"),
                PyValue::int(events.len() as i128),
                st(hs),
                wt.map_or(PyValue::None, st),
            ],
        );
        strs(&self.case["joined"]).iter().map(|s| cps(s)).collect()
    }
}

pub struct Claims {
    keep: Option<Vec<String>>,
    log: Log,
}

impl ClaimState for Claims {
    fn claim_delivery(
        &mut self,
        key: &Key,
        shown: &[PyStr],
        now: &DateTime,
        event_id: &[u32],
    ) -> (Vec<PyStr>, i64) {
        push(
            &self.log,
            vec![
                PyValue::str("claim"),
                ids(&[key.0.clone(), key.1.clone()]),
                ids(shown),
                PyValue::DateTime(*now),
                st(event_id),
            ],
        );
        let kept = match &self.keep {
            None => shown.to_vec(),
            Some(k) => shown
                .iter()
                .filter(|i| k.contains(&text(i)))
                .cloned()
                .collect(),
        };
        (kept, 4)
    }
}

pub fn held_json(h: &[Held]) -> Value {
    Value::Array(
        h.iter()
            .map(|e| {
                let mut m = serde_json::Map::new();
                m.insert("id".into(), json!(text(&e.id)));
                m.insert("reason".into(), json!(text(&e.reason)));
                if let Some(r) = &e.relation {
                    m.insert("relation".into(), json!(text(r)));
                }
                Value::Object(m)
            })
            .collect(),
    )
}

fn held_py(h: &[Held]) -> PyValue {
    PyValue::List(
        h.iter()
            .map(|e| {
                let mut d = vec![
                    (PyValue::str("id"), st(&e.id)),
                    (PyValue::str("reason"), st(&e.reason)),
                ];
                if let Some(r) = &e.relation {
                    d.push((PyValue::str("relation"), st(r)));
                }
                PyValue::Dict(d)
            })
            .collect(),
    )
}

pub fn response(r: &HookResult) -> Value {
    let mut m = serde_json::Map::new();
    m.insert("stdout".into(), json!(text(&r.stdout)));
    m.insert("surface".into(), json!(text(&r.surface)));
    m.insert(
        "ids".into(),
        json!(r.ids.iter().map(|i| text(i)).collect::<Vec<_>>()),
    );
    m.insert("chars".into(), json!(r.chars));
    m.insert("rule".into(), json!(text(&r.rule)));
    m.insert(
        "event_id".into(),
        r.event_id.as_ref().map_or(Value::Null, |e| json!(text(e))),
    );
    if let Some(PyValue::Dict(d)) = &r.membership {
        if !d.is_empty() {
            m.insert(
                "membership".into(),
                super::rows::value(r.membership.as_ref().unwrap()),
            );
        }
    }
    Value::Object(m)
}

pub fn report(hits: &Value) -> MissReport {
    MissReport {
        verdict: Verdict::Miss,
        checked_at: dt(T),
        session_id: cps("H"),
        lookback_seconds: 0,
        recent_retrieval_count: 0,
        threshold_rule: Vec::new(),
        top_hits: hits
            .as_array()
            .unwrap()
            .iter()
            .map(|h| MissHit {
                id: cps(h[0].as_str().unwrap()),
                score: 0.0,
                relevance: Vec::new(),
                scopes: Vec::new(),
                snippet: cps(h[1].as_str().unwrap()),
                matched_unique: 0,
                query_unique: 0,
                relevance_v2: Vec::new(),
            })
            .collect(),
        probe_query: None,
        no_signal_reason: None,
        suppressed_by: None,
    }
}

/// One `choose` case: (the log, the answer), both as the generator writes.
pub fn run(case: &Value) -> (PyValue, Value) {
    run_report(case, &report(&case["hits"]))
}

/// `run` over a report built by the caller (ids JSON cannot spell).
pub fn run_report(case: &Value, rep: &MissReport) -> (PyValue, Value) {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let mut turn = Turn {
        store: Store {
            case: case.clone(),
            log: log.clone(),
        },
        view: View {
            deny: strs(&case["deny"]),
            log: log.clone(),
        },
        key: (cps("claude-code"), cps("H")),
        now: dt(T),
        wt: case["wt"].as_str().map(cps),
        remint: (!case["remint"].is_null()).then(|| plain(&case["remint"])),
    };
    let mut joins = Joins {
        case: case.clone(),
        log: log.clone(),
    };
    let shown: Vec<PyStr> = strs(&case["shown"]).iter().map(|s| cps(s)).collect();
    let started = dt(case["at"].as_str().unwrap_or(T));
    let own = dt(case["own"].as_str().unwrap_or(T));
    let recent = vec![PyValue::Dict(vec![(PyValue::str("n"), PyValue::int(1))])];
    let window = Window {
        shown: &shown,
        started_at: &started,
        own_since: &own,
        recent: &recent,
    };
    let alog = log.clone();
    let admit = |hits: &[PyStr], surface: &[u32]| {
        push(&alog, vec![PyValue::str("admit"), ids(hits), st(surface)]);
        match &case["admit"] {
            Value::Null => Ok((hits.to_vec(), Vec::new())),
            Value::String(_) => Err(ChooseError::Raised("ValueError".into())),
            a => Ok((
                strs(&a[0]).iter().map(|s| cps(s)).collect(),
                a[1].as_array()
                    .unwrap()
                    .iter()
                    .map(|p| {
                        let s = strs(p);
                        (cps(&s[0]), cps(&s[1]))
                    })
                    .collect(),
            )),
        }
    };
    let out = match choose_hits(&mut turn, &mut joins, rep, &window, admit) {
        Err(ChooseError::TypeError) => json!({"err": "TypeError"}),
        Err(ChooseError::Raised(name)) => json!({ "err": name }),
        Err(ChooseError::NotUnicode) => json!({"err": "NotUnicode"}),
        Ok(choice) => {
            let chosen: Vec<Value> = choice
                .chosen
                .iter()
                .map(|(hit, m, row, e)| {
                    json!([
                        text(&hit.id),
                        text(&m.id),
                        row.label,
                        e.as_ref().map(|e| json!([e.asked, e.id]))
                    ])
                })
                .collect();
            let mut out = json!({
                "chosen": chosen,
                "held": held_json(&choice.suppressed),
                "rc3": choice.rc3_candidates.iter().map(|m| text(&m.id)).collect::<Vec<_>>(),
            });
            if case["deliver"].as_bool() == Some(true) {
                let mut claims = Claims {
                    keep: case.get("claim").map(strs),
                    log: log.clone(),
                };
                let (l1, l2, l3) = (log.clone(), log.clone(), log.clone());
                let res = deliver_recall(
                    &mut turn,
                    &mut claims,
                    &choice,
                    || {
                        push(&l1, vec![PyValue::str("mint")]);
                        cps("01HZZZZZZZZZZZZZZZZZZZZZZE")
                    },
                    |d| {
                        push(
                            &l2,
                            vec![
                                PyValue::str("record"),
                                st(d.block),
                                ids(d.shown_ids),
                                st(d.event_id),
                                PyValue::int(i128::from(d.segment)),
                            ],
                        );
                        d.event_id.to_vec()
                    },
                    |reason, entries| {
                        push(
                            &l3,
                            vec![
                                PyValue::str("hold"),
                                PyValue::str(reason),
                                held_py(&entries),
                            ],
                        );
                        HookResult::new(Vec::new(), "prompt_recall", Vec::new(), 0, None, None)
                    },
                );
                out["result"] = response(&res);
            }
            out
        }
    };
    let logged = PyValue::List(log.borrow().clone());
    (logged, out)
}
