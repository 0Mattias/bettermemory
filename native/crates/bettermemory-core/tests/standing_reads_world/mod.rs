//! The in-memory store and admission of `gen_standing_reads.py`, built
//! from a case's JSON, logging every read as the generator does.

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::in_force::{Inbound, LinkReader, RecordView};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{LinkType, Memory, MemoryLink};
use bettermemory_core::standing::{self, Admission, ReadError, StandingRow, StandingStore};
use bettermemory_core::store::caller::ModelError;
use bettermemory_core::unicode;
use serde_json::{json, Value};

type Log = Rc<RefCell<Vec<Value>>>;

pub struct World {
    pub log: Log,
    pub tau: bool,
    pub recs: Vec<StandingRow>,
    pub bodies: HashMap<String, PyStr>,
    pub links: HashMap<String, Vec<Inbound>>,
    pub returned: Vec<String>,
    pub unanswered: Option<Vec<String>>,
    pub candidates: Option<Vec<String>>,
    pub tombs: HashMap<String, Vec<(String, String)>>,
    pub mems: Vec<(String, Vec<String>, String, String)>,
}

pub struct View {
    pub log: Log,
    pub admit: Option<Vec<String>>,
    pub deny: Vec<Option<String>>,
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|x| x.as_str().expect("a text").to_owned())
        .collect()
}

fn opt_strs(v: &Value) -> Option<Vec<String>> {
    (!v.is_null()).then(|| strs(v))
}

fn opt_str(v: &Value) -> Option<String> {
    v.as_str().map(str::to_owned)
}

pub fn sha_hex(text: &str) -> String {
    standing::body_sha256(&unicode::cps(text)).expect("encodable")
}

fn wanted(ids: Option<&[String]>) -> Value {
    match ids {
        None => Value::Null,
        Some(ids) => {
            let mut seen = HashSet::new();
            json!(ids
                .iter()
                .filter(|i| seen.insert(i.as_str()))
                .collect::<Vec<_>>())
        }
    }
}

pub fn build(c: &Value) -> (World, View) {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let recs = c["recs"]
        .as_array()
        .expect("recs")
        .iter()
        .map(|r| StandingRow {
            id: r[0].as_str().expect("id").to_owned(),
            category: opt_str(&r[1]),
            updated: r[2].as_str().expect("updated").to_owned(),
            scopes: strs(&r[3]).iter().map(|s| unicode::cps(s)).collect(),
            recorded: r[4].as_str().map(unicode::cps),
            log_mac: opt_str(&r[5]),
            probability: r[6].as_f64(),
            body_sha256: r[7].as_str().map(sha_hex),
        })
        .collect();
    let bodies = c["bodies"]
        .as_object()
        .expect("bodies")
        .iter()
        .map(|(k, v)| (k.clone(), unicode::cps(v.as_str().expect("body"))))
        .collect();
    let links = c["links"]
        .as_object()
        .expect("links")
        .iter()
        .map(|(t, rows)| {
            let rows = rows
                .as_array()
                .expect("rows")
                .iter()
                .map(|r| {
                    let r = strs(r);
                    (r[0].clone(), r[1].clone(), r[2].clone())
                })
                .collect();
            (t.clone(), rows)
        })
        .collect();
    let tombs = c["tombs"]
        .as_object()
        .expect("tombs")
        .iter()
        .map(|(k, v)| {
            let pairs = v
                .as_array()
                .expect("links")
                .iter()
                .map(|p| {
                    let p = strs(p);
                    (p[0].clone(), p[1].clone())
                })
                .collect();
            (k.clone(), pairs)
        })
        .collect();
    let mems = c["mems"]
        .as_array()
        .expect("mems")
        .iter()
        .map(|m| {
            (
                m[0].as_str().expect("id").to_owned(),
                strs(&m[1]),
                m[2].as_str().expect("updated").to_owned(),
                m[3].as_str().expect("body").to_owned(),
            )
        })
        .collect();
    let world = World {
        log: log.clone(),
        tau: c["tau"].as_bool().expect("tau"),
        recs,
        bodies,
        links,
        returned: strs(&c["returned"]),
        unanswered: opt_strs(&c["unanswered"]),
        candidates: opt_strs(&c["candidates"]),
        tombs,
        mems,
    };
    let view = View {
        log,
        admit: opt_strs(&c["admit"]),
        deny: c["deny"]
            .as_array()
            .expect("deny")
            .iter()
            .map(opt_str)
            .collect(),
    };
    (world, view)
}

impl World {
    fn call(&self, v: Value) {
        self.log.borrow_mut().push(v);
    }
}

impl LinkReader for World {
    fn inbound_links(
        &mut self,
        targets: &[String],
        _types: &[&str],
    ) -> HashMap<String, Vec<Inbound>> {
        self.call(json!(["l", targets]));
        targets
            .iter()
            .filter_map(|t| self.links.get(t).map(|r| (t.clone(), r.clone())))
            .collect()
    }

    fn held_ids(&mut self, ids: &[String]) -> HashSet<String> {
        let tau = self.tau;
        standing::held_ids(self, Some(ids), tau).expect("held_ids")
    }
}

impl StandingStore for World {
    fn body_of(&mut self, id: &str) -> Option<PyStr> {
        self.call(json!(["b", id]));
        self.bodies.get(id).cloned()
    }

    fn standing_rows(&mut self, ids: Option<&[String]>) -> Vec<StandingRow> {
        let w = wanted(ids);
        self.call(json!(["r", w]));
        let set: Option<HashSet<&str>> = ids.map(|ids| ids.iter().map(String::as_str).collect());
        let mut out: Vec<StandingRow> = self
            .recs
            .iter()
            .filter(|r| set.as_ref().is_none_or(|set| set.contains(r.id.as_str())))
            .cloned()
            .collect();
        out.sort_by(|a, b| (&b.updated, &b.id).cmp(&(&a.updated, &a.id)));
        out
    }

    fn digest_unanswered_ids(&mut self) -> Option<Vec<String>> {
        self.call(json!(["u"]));
        self.unanswered.clone()
    }

    fn returned_rows(&mut self, ids: Option<&[String]>) -> Vec<String> {
        let w = wanted(ids);
        self.call(json!(["m", w]));
        match ids {
            None => self.returned.clone(),
            Some(_) => strs(&w)
                .into_iter()
                .filter(|i| self.returned.contains(i))
                .collect(),
        }
    }

    fn standing_candidate_ids(&mut self, tau: Option<f64>) -> Option<Vec<String>> {
        self.call(json!(["c", tau]));
        self.candidates.clone()
    }

    fn get_many(&mut self, ids: &[String]) -> Vec<Memory> {
        self.call(json!(["g", ids]));
        ids.iter()
            .filter_map(|i| self.mems.iter().find(|m| &m.0 == i))
            .map(|(id, scopes, updated, body)| {
                let dt = DateTime::from_isoformat(&unicode::cps(updated)).expect("iso");
                let payload = PyValue::Dict(vec![
                    (PyValue::str("id"), PyValue::str(id)),
                    (PyValue::str("created"), PyValue::DateTime(dt)),
                    (PyValue::str("updated"), PyValue::DateTime(dt)),
                    (
                        PyValue::str("scopes"),
                        PyValue::List(scopes.iter().map(|s| PyValue::str(s)).collect()),
                    ),
                    (PyValue::str("confidence"), PyValue::str("high")),
                    (PyValue::str("source"), PyValue::str("inferred")),
                    (PyValue::str("body"), PyValue::str(body)),
                ]);
                Memory::from_payload(&payload).expect("a memory")
            })
            .collect()
    }

    fn tombstone_links(&mut self, id: &str) -> Result<Vec<MemoryLink>, ReadError> {
        self.call(json!(["t", id]));
        let Some(pairs) = self.tombs.get(id) else {
            return Err(ReadError::NotFound(id.to_owned()));
        };
        Ok(pairs
            .iter()
            .map(|(t, target)| MemoryLink {
                link_type: LinkType::MEMBERS
                    .iter()
                    .find(|(v, _)| v == t)
                    .expect("a link type")
                    .1,
                target_id: unicode::cps(target),
                note: None,
            })
            .collect())
    }
}

impl RecordView for View {
    fn prime(&mut self, ids: &[String]) {
        self.log.borrow_mut().push(json!(["p", ids]));
    }

    fn admits_id(&mut self, id: &str) -> bool {
        self.log.borrow_mut().push(json!(["a", id]));
        self.admit
            .as_ref()
            .is_none_or(|a| a.iter().any(|x| x == id))
    }
}

impl Admission for View {
    fn admits_row(
        &mut self,
        scopes: &[PyStr],
        recorded: Option<&[u32]>,
    ) -> Result<bool, ModelError> {
        let scopes: Vec<String> = scopes
            .iter()
            .map(|s| unicode::to_string(s).expect("text"))
            .collect();
        let recorded = recorded.map(|r| unicode::to_string(r).expect("text"));
        self.log.borrow_mut().push(json!(["w", scopes, recorded]));
        Ok(!self.deny.contains(&recorded))
    }
}

fn py_json(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Str(s) => json!(unicode::to_string(s).expect("text")),
        PyValue::List(items) => Value::Array(items.iter().map(py_json).collect()),
        PyValue::Dict(pairs) => Value::Array(
            pairs
                .iter()
                .map(|(k, v)| json!([py_json(k), py_json(v)]))
                .collect(),
        ),
        other => panic!("unexpected value {other:?}"),
    }
}

fn ids_of(v: &Value) -> Option<Vec<String>> {
    opt_strs(v)
}

/// One op's result as the generator writes it.
pub fn op(world: &mut World, view: &mut View, op: &Value) -> Value {
    let name = op[0].as_str().expect("op");
    match name {
        "answers" => {
            let got = standing::answers(world, &strs(&op[1])).expect("answers");
            json!(got
                .into_iter()
                .map(|(k, v)| json!([k, v]))
                .collect::<Vec<_>>())
        }
        "backlog" => {
            let got = standing::backlog(world, view).expect("backlog");
            json!([got.ids, got.count()])
        }
        "guarded" => json!(standing::guarded_ids(world, &strs(&op[1])).expect("guarded")),
        "makes" => json!(standing::makes_standing(
            world,
            op[1].as_str().expect("id"),
            op[2].as_str(),
            op[3].as_f64(),
            op[4].as_bool().expect("changed"),
        )
        .expect("makes")),
        "held" => {
            let tau = world.tau;
            let ids = ids_of(&op[1]);
            let mut got: Vec<String> = standing::held_ids(world, ids.as_deref(), tau)
                .expect("held")
                .into_iter()
                .collect();
            got.sort();
            json!(got)
        }
        "standing" => {
            let ids = ids_of(&op[1]);
            json!(standing::standing_ids(
                world,
                view,
                ids.as_deref(),
                op[2].as_bool().expect("held")
            )
            .expect("standing"))
        }
        "refusal" => {
            let rows = standing::refusal_rows(world, &strs(&op[1]), view).expect("refusal");
            Value::Array(rows.iter().map(py_json).collect())
        }
        "removal" => {
            let tau = world.tau;
            json!(standing::held_by_removal(world, &strs(&op[1]), tau).expect("removal"))
        }
        "count" => json!(standing::sha_count()),
        other => panic!("unknown op {other}"),
    }
}

/// Every op of the case on one memo, cleared first: `[[result, calls]]`.
pub fn answer(c: &Value) -> Value {
    let (mut world, mut view) = build(c);
    standing::set_sha_bound(
        c.get("bound")
            .and_then(Value::as_u64)
            .unwrap_or(standing::SHA_BOUND as u64) as usize,
    );
    standing::clear_shas();
    let mut out = Vec::new();
    for o in c["ops"].as_array().expect("ops") {
        world.log.borrow_mut().clear();
        let result = op(&mut world, &mut view, o);
        let calls = Value::Array(world.log.borrow().clone());
        out.push(json!([result, calls]));
    }
    standing::clear_shas();
    standing::set_sha_bound(standing::SHA_BOUND);
    Value::Array(out)
}
