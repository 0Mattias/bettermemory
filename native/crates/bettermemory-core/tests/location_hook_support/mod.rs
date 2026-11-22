//! Recording doubles for the `location_hook` tests, as
//! `gen_location_hook.py` writes them: `_hooked`, `os.getcwd`,
//! `object_key`, `_hostname`, `open_machine` and a model, each logging
//! its call, and one round of `resolve_hook` run with them.

#![allow(dead_code)]

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::capture::location::{Location, ObjectKey};
use bettermemory_core::location_hook::{
    resolve_hook, Answered, HookError, HookHands, HookModel, HookRequest, Hooked, NO_MACHINE,
};
use bettermemory_core::location_resolve::{CallerModel, Policy, Process, RecordHome};
use bettermemory_core::location_witness::{ExchangeReads, Exchanges, NeedWitnesses};
use bettermemory_core::machine::MachineIdentity;
use bettermemory_core::membership_records::records::Checkout;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::caller::Resolution;
use bettermemory_core::unicode;
use serde_json::{json, Value};

use crate::common::neutral;

pub type Log = Rc<RefCell<Vec<Value>>>;

pub fn text(t: &str) -> PyStr {
    unicode::cps(t)
}

/// A text as the fixture spells it; a text with a lone surrogate (only
/// the robustness tests log one) as its code points' debug form.
pub fn s(t: &[u32]) -> String {
    unicode::to_string(t).unwrap_or_else(|| format!("{t:?}"))
}

fn os(v: &Option<PyStr>) -> Value {
    json!(v.as_deref().map(s))
}

pub fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(text)
}

pub fn resolution(spec: &Value) -> Resolution {
    let mut r = Resolution {
        outcome: opt(&spec["o"]).unwrap(),
        project: opt(&spec["p"]),
        checkout: opt(&spec["c"]),
        rule: opt(&spec["rule"]).unwrap_or_else(|| text("r")),
        proposal: None,
        candidates: Vec::new(),
        witnesses: Vec::new(),
        chain: Vec::new(),
        tenant: None,
        lines: Vec::new(),
        rows: Vec::new(),
        remint_token: false,
    };
    if let Some(lines) = spec["lines"].as_array() {
        r.lines = lines.iter().map(|l| opt(l).unwrap()).collect();
    }
    r
}

pub fn res_json(r: &Resolution) -> Value {
    json!({
        "o": s(&r.outcome), "p": os(&r.project), "c": os(&r.checkout),
        "rule": s(&r.rule), "lines": r.lines.iter().map(|l| s(l)).collect::<Vec<_>>(),
    })
}

pub fn loc_json(l: &Location) -> Value {
    json!([
        s(&l.machine),
        s(&l.cwd),
        l.state.as_str(),
        s(&l.detail),
        os(&l.session),
        os(&l.git_dir),
        os(&l.token),
        s(&l.token_status),
        l.object_key
            .as_ref()
            .map(|k| json!([s(&k.device), s(&k.inode), os(&k.birth)])),
    ])
}

/// `{"t": tag, "v": v}`, built by moving `v` in: `json!` would serialize
/// a nested `Value` afresh at every level, quadratic in the depth.
fn tagged(tag: &str, v: Value) -> Value {
    let mut m = serde_json::Map::new();
    m.insert("t".into(), Value::String(tag.into()));
    m.insert("v".into(), v);
    Value::Object(m)
}

/// `_common.neutral` of a value an exchange holds.
pub fn to_neutral(v: &PyValue) -> Value {
    match v {
        PyValue::None => json!({"t": "null"}),
        PyValue::Bool(b) => json!({"t": "bool", "v": b}),
        PyValue::Int(t) => json!({"t": "int", "v": t}),
        PyValue::Str(t) => match unicode::to_string(t) {
            Some(text) => json!({"t": "str", "v": text}),
            None => json!({"t": "str", "v_cps": t}),
        },
        PyValue::List(items) => {
            tagged("list", Value::Array(items.iter().map(to_neutral).collect()))
        }
        PyValue::Tuple(items) => tagged(
            "tuple",
            Value::Array(items.iter().map(to_neutral).collect()),
        ),
        PyValue::Dict(items) => tagged(
            "dict",
            Value::Array(
                items
                    .iter()
                    .map(|(k, v)| Value::Array(vec![to_neutral(k), to_neutral(v)]))
                    .collect(),
            ),
        ),
        other => panic!("no exchange holds {other:?}"),
    }
}

/// The double's raise: `OSError` (as FileNotFoundError) or another class.
#[derive(Clone, Debug, PartialEq)]
pub enum Fault {
    Os,
    Other,
}

fn fault(v: &Value) -> Result<(), Fault> {
    match v.as_str() {
        Some("os") => Err(Fault::Os),
        Some("other") => Err(Fault::Other),
        _ => Ok(()),
    }
}

/// A recording `Membership`.
pub struct Fake {
    pub rnd: Value,
    pub log: Log,
    pub machine: PyStr,
}

impl CallerModel for Fake {
    type Error = Fault;
    fn record_machine(&mut self, identity: &MachineIdentity) -> Result<(), Fault> {
        self.log
            .borrow_mut()
            .push(json!(["record", s(&identity.id)]));
        fault(&self.rnd["record"])
    }
    fn resolve(&mut self, loc: &Location, strict: bool) -> Result<Resolution, Fault> {
        self.log
            .borrow_mut()
            .push(json!(["resolve", loc_json(loc), strict, null]));
        fault(&self.rnd["resolve"])?;
        Ok(self.answer())
    }
    fn session_resolution(
        &mut self,
        _: &Location,
        _: &Resolution,
        _: &[u32],
    ) -> Result<Option<Resolution>, Fault> {
        unreachable!("a hook has no stand")
    }
    fn unresolved_home(&mut self, _: &Location, _: Option<&[u32]>) -> Result<RecordHome, Fault> {
        unreachable!("a hook asks no home")
    }
    fn add_witness(
        &mut self,
        _: &[u32],
        _: &[u32],
        _: &[u32],
        _: Option<&[u32]>,
    ) -> Result<(), Fault> {
        unreachable!("a hook adds no witness")
    }
}

impl Fake {
    fn answer(&self) -> Resolution {
        let spec = &self.rnd["res"];
        if spec.is_null() {
            resolution(&json!({"o": "silent", "p": "p", "c": "k"}))
        } else {
            resolution(spec)
        }
    }
}

impl HookModel for Fake {
    fn machine(&self) -> PyStr {
        self.machine.clone()
    }
    fn is_os_error(error: &Fault) -> bool {
        *error == Fault::Os
    }
    fn checkouts_of(&mut self, token: &[u32]) -> Vec<(PyStr, Option<ObjectKey>)> {
        self.log.borrow_mut().push(json!(["checkouts", s(token)]));
        let empty = Vec::new();
        let rows = self.rnd["checkouts"].as_array().unwrap_or(&empty);
        rows.iter()
            .map(|row| {
                let key = row[1].as_array().map(|k| ObjectKey {
                    device: opt(&k[0]).unwrap(),
                    inode: opt(&k[1]).unwrap(),
                    birth: None,
                });
                (opt(&row[0]).unwrap(), key)
            })
            .collect()
    }
    fn resolve_declared(
        &mut self,
        loc: &Location,
        strict: bool,
        declared: &mut ExchangeReads,
    ) -> Result<Result<Resolution, NeedWitnesses>, Fault> {
        let mut held: Vec<(String, Value)> = declared
            .reads
            .iter()
            .map(|(f, sp)| (s(f), os(sp)))
            .collect();
        held.sort_by(|a, b| a.0.cmp(&b.0));
        let held: Vec<Value> = held.into_iter().map(|(f, v)| json!([f, v])).collect();
        self.log
            .borrow_mut()
            .push(json!(["resolve", loc_json(loc), strict, held]));
        fault(&self.rnd["resolve"])?;
        if let Some(folders) = self.rnd["reads"].as_array() {
            for f in folders {
                let f = opt(f).unwrap();
                let mut spelled = text("s:");
                spelled.extend_from_slice(&f);
                declared.set_read(f, Some(spelled));
            }
        }
        if !self.rnd["need"].is_null() {
            return Ok(Err(NeedWitnesses {
                question: neutral(&self.rnd["need"]),
            }));
        }
        Ok(Ok(self.answer()))
    }
}

/// One round of `resolve_hook` with the round's doubles: what the
/// fixture records of it, but the stands.
pub fn round(process: &Process, exchanges: &mut Exchanges, rnd: &Value, log: &Log) -> Value {
    let posted = (!rnd["posted"].is_null()).then(|| neutral(&rnd["posted"]));
    let cwd = opt(&rnd["cwd"]);
    let session = opt(&rnd["session"]);
    let store = text("/s");
    let policy = Policy {
        write_token: rnd["wt"].as_bool().unwrap_or(true),
        strict: rnd["strict"].as_bool().unwrap_or(false),
    };
    let req = HookRequest {
        store_path: &store,
        policy,
        cwd: cwd.as_deref(),
        posted: posted.as_ref(),
        session: session.as_deref(),
    };
    let fake = |machine: &[u32], sess: Option<&[u32]>, live: bool, writer: bool| {
        log.borrow_mut()
            .push(json!(["model", s(machine), sess.map(s), live, writer]));
        fault(&rnd["make"])?;
        Ok(Fake {
            rnd: rnd.clone(),
            log: log.clone(),
            machine: machine.to_vec(),
        })
    };
    let hands = HookHands {
        getcwd: || {
            log.borrow_mut().push(json!(["getcwd"]));
            opt(&rnd["g"])
        },
        object_key: |path: &[u32]| {
            log.borrow_mut().push(json!(["key", s(path)]));
            rnd["here"]
                .as_array()
                .map(|k| (opt(&k[0]).unwrap(), opt(&k[1]).unwrap(), opt(&k[2])))
        },
        hostname: || {
            log.borrow_mut().push(json!(["host"]));
            match rnd.get("local") {
                Some(v) if !v.is_null() => match &neutral(v) {
                    PyValue::Str(t) => Some(t.clone()),
                    _ => None,
                },
                _ => None,
            }
        },
        hooked: |cwd: Option<PyStr>| {
            log.borrow_mut().push(json!([
                "hooked",
                cwd.as_deref().map(s),
                session.as_deref().map(s)
            ]));
            fault(&rnd["hooked"])?;
            Ok(resolution(&json!({"o": "silent", "p": "h", "c": "hk"})))
        },
        open_machine: || {
            log.borrow_mut().push(json!(["open"]));
            match rnd["open"].as_str() {
                None => Err(Fault::Os),
                Some("other") => Err(Fault::Other),
                Some(id) => Ok(MachineIdentity {
                    id: text(id),
                    facts: bettermemory_core::machine::MachineFacts {
                        hardware: None,
                        hostname: None,
                        id_file: None,
                    },
                    predecessor: None,
                    minted: false,
                    cloned: false,
                    facts_updated: false,
                    recorded: None,
                }),
            }
        },
        new_model: |id: &MachineIdentity, sess: Option<&[u32]>, answered: Option<Answered<'_>>| {
            let a = answered.is_some();
            if rnd["probe_writer"].as_bool() == Some(true) {
                let said = match answered.as_ref().map(|w| w.writer.call(&bare_checkout())) {
                    None => "absent",
                    Some(Ok(None)) => "declined",
                    Some(Ok(Some(_))) => "answered",
                    Some(Err(_)) => "asked",
                };
                log.borrow_mut().push(json!(["writer", said]));
            }
            fake(&id.id, sess, a, a)
        },
        no_machine: |sess: Option<&[u32]>| fake(&text(NO_MACHINE), sess, false, false),
        exchanges,
        now: 0.0,
        fresh: [0xab; 16],
    };
    match resolve_hook::<Fake, _, _, _, _, _, _, _>(process, req, hands) {
        Ok(Hooked::Resolved(res)) => json!({"res": res_json(&res)}),
        Ok(Hooked::Asked(ex)) => json!({"ask": to_neutral(&ex.question)}),
        Err(HookError::Model(Fault::Os)) => json!({"raised": "FileNotFoundError"}),
        Err(HookError::Model(Fault::Other)) => json!({"raised": "_Other"}),
        Err(HookError::Raised(e)) => json!({"raised": e.name()}),
    }
}

/// Every stand the process keeps, as [session, machine, path, checkout].
pub fn stands(process: &Process) -> Value {
    let rows: Vec<Value> = process
        .stands
        .keys()
        .iter()
        .map(|(store, sess)| {
            let st = process.stands.stand_of(store, Some(sess)).unwrap();
            json!([
                s(sess),
                s(&st.machine),
                s(&st.path),
                os(&st.resolution.checkout)
            ])
        })
        .collect();
    json!(rows)
}

/// A checkout with no token: `AnsweredToken.__call__` on it shows the
/// writer's `posted` flag, declining for a posted mapping without
/// `remint_ok` and asking otherwise.
fn bare_checkout() -> Checkout {
    Checkout {
        id: text("k1"),
        project: text("p1"),
        machine: text("mc"),
        kind: text("primary"),
        object_key: None,
        common_object_key: None,
        git_dir: Some(text("/w/.git")),
        token: None,
        token_status: text("absent"),
        stamps: Vec::new(),
        last_head: None,
        branch: None,
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: text("t0"),
        views: Vec::new(),
        sessions: Vec::new(),
        replaced_by: None,
        ticks: None,
    }
}

/// A case's rounds run in one process with one table of exchanges.
pub fn run_case(c: &Value) -> Value {
    let process = Process::default();
    let mut exchanges = Exchanges::new();
    let mut out = Vec::new();
    for rnd in c["rounds"].as_array().unwrap() {
        let log: Log = Rc::new(RefCell::new(Vec::new()));
        let got = round(&process, &mut exchanges, rnd, &log);
        let mut r = json!({"calls": log.borrow().clone()});
        for (k, v) in got.as_object().unwrap() {
            r[k] = v.clone();
        }
        r["stands"] = stands(&process);
        out.push(r);
    }
    json!(out)
}
