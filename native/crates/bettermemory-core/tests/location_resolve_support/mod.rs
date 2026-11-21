//! Recording doubles for the `location_resolve` tests, as
//! `gen_location_resolve.py` writes them: a model, a capture cache, a
//! re-mint, `_earlier_tree` and `_machineless`, each logging its call.

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

use bettermemory_core::capture::location::{to_location, Location};
use bettermemory_core::capture::{Capture, Kind, State};
use bettermemory_core::checkout_token::Lineage;
use bettermemory_core::location_resolve::{
    Caller, CallerModel, Capturer, Hands, Machineless, Policy, Process, RecordHome, Request,
    TokenWriter,
};
use bettermemory_core::machine::{MachineFacts, MachineIdentity};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::store::caller::Resolution;
use bettermemory_core::unicode;
use serde_json::{json, Value};

pub type Log = Rc<RefCell<Vec<Value>>>;

pub fn text(t: &str) -> PyStr {
    unicode::cps(t)
}

pub fn s(t: &[u32]) -> String {
    unicode::to_string(t).expect("text")
}

pub fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(text)
}

fn os(v: &Option<PyStr>) -> Value {
    json!(v.as_deref().map(s))
}

pub fn identity(id: &str) -> MachineIdentity {
    MachineIdentity {
        id: text(id),
        facts: MachineFacts {
            hardware: None,
            hostname: None,
            id_file: None,
        },
        predecessor: None,
        minted: false,
        cloned: false,
        facts_updated: false,
        recorded: None,
    }
}

pub fn resolution(spec: &Value) -> Resolution {
    Resolution {
        outcome: opt(&spec["o"]).unwrap(),
        project: opt(&spec["p"]),
        checkout: opt(&spec["c"]),
        rule: opt(&spec["rule"]).unwrap_or_else(|| text("r")),
        proposal: None,
        candidates: Vec::new(),
        witnesses: Vec::new(),
        chain: Vec::new(),
        tenant: None,
        lines: spec["lines"]
            .as_array()
            .map(|a| a.iter().map(|l| opt(l).unwrap()).collect())
            .unwrap_or_default(),
        rows: Vec::new(),
        remint_token: false,
    }
}

pub fn res_json(r: &Resolution) -> Value {
    json!({
        "o": s(&r.outcome), "p": os(&r.project), "c": os(&r.checkout),
        "rule": s(&r.rule), "lines": r.lines.iter().map(|l| s(l)).collect::<Vec<_>>(),
    })
}

pub fn loc_json(l: &Location) -> Value {
    json!({
        "machine": s(&l.machine), "cwd": s(&l.cwd), "state": l.state.as_str(),
        "kind": l.kind.map(Kind::as_str), "git_dir": os(&l.git_dir), "head": os(&l.head),
        "session": os(&l.session), "detail": s(&l.detail),
    })
}

pub fn caller_json<M>(c: &Caller<M>) -> Value {
    json!({"loc": loc_json(&c.location), "history": os(&c.history), "res": res_json(&c.resolution)})
}

fn state(t: &str) -> State {
    *State::ALL.iter().find(|x| x.as_str() == t).unwrap()
}

pub fn capture_of(cwd: &[u32], spec: &Value) -> Capture {
    let mut c = Capture::new(cwd.to_vec(), state(spec["state"].as_str().unwrap()));
    c.kind = spec["kind"]
        .as_str()
        .map(|k| *Kind::ALL.iter().find(|x| x.as_str() == k).unwrap());
    c.git_dir = opt(&spec["git_dir"]);
    c.common_dir = opt(&spec["common_dir"]);
    c.head = opt(&spec["head"]);
    c.evicted = spec["evicted"].as_bool().unwrap_or(false);
    c.detail = opt(&spec["detail"]).unwrap_or_default();
    c.file_errors = spec["file_errors"]
        .as_array()
        .map(|a| a.iter().map(|e| opt(e).unwrap()).collect())
        .unwrap_or_default();
    c
}

/// The recording model.
pub struct Fake<'a> {
    pub case: Value,
    pub log: Log,
    pub writer: Option<TokenWriter<'a>>,
}

impl CallerModel for Fake<'_> {
    type Error = ();
    fn record_machine(&mut self, identity: &MachineIdentity) -> Result<(), ()> {
        self.log
            .borrow_mut()
            .push(json!(["record", s(&identity.id)]));
        if self.case["fails"].as_bool() == Some(true) {
            return Err(());
        }
        Ok(())
    }
    fn resolve(&mut self, loc: &Location, strict: bool) -> Result<Resolution, ()> {
        self.log
            .borrow_mut()
            .push(json!(["resolve", s(&loc.cwd), strict]));
        for token in self.case["writes"].as_array().cloned().unwrap_or_default() {
            let lineage = opt(&token).map(|t| Lineage {
                token: t,
                device: text("1"),
                inode: text("2"),
            });
            let got = (self.writer.as_mut().unwrap())(lineage);
            let got = got.map(|(t, st)| json!([os(&t), s(&st)]));
            self.log.borrow_mut().push(json!(["wrote", got]));
        }
        let spec = match &self.case["res"] {
            Value::Null => json!({"o": "silent", "p": "p", "c": "k"}),
            v => v.clone(),
        };
        Ok(resolution(&spec))
    }
    fn session_resolution(
        &mut self,
        loc: &Location,
        stand: &Resolution,
        path: &[u32],
    ) -> Result<Option<Resolution>, ()> {
        self.log
            .borrow_mut()
            .push(json!(["sr", s(&loc.cwd), os(&stand.checkout), s(path)]));
        Ok(match &self.case["sr"] {
            Value::Null => None,
            v => Some(resolution(v)),
        })
    }
    fn unresolved_home(
        &mut self,
        loc: &Location,
        session: Option<&[u32]>,
    ) -> Result<RecordHome, ()> {
        self.log
            .borrow_mut()
            .push(json!(["unresolved", s(&loc.cwd), session.map(s)]));
        Ok(RecordHome {
            project: text("prov"),
            checkout: None,
            rule: text("b6"),
        })
    }
    fn add_witness(
        &mut self,
        project: &[u32],
        head: &[u32],
        source: &[u32],
        history: Option<&[u32]>,
    ) -> Result<(), ()> {
        self.log.borrow_mut().push(json!([
            "witness",
            s(project),
            s(head),
            s(source),
            history.map(s)
        ]));
        Ok(())
    }
}

/// The recording capture cache.
pub struct Caps {
    pub log: Log,
    pub caps: VecDeque<Value>,
}

impl Capturer for Caps {
    fn capture(&mut self, cwd: &[u32], write_token: bool, budget: f64, use_cache: bool) -> Capture {
        self.log
            .borrow_mut()
            .push(json!(["capture", s(cwd), write_token, budget, use_cache]));
        let spec = self.caps.pop_front().expect("a capture");
        capture_of(cwd, &spec)
    }
    fn forget(&mut self, cwd: &[u32]) {
        self.log.borrow_mut().push(json!(["forget", s(cwd)]));
    }
}

pub fn caps(case: &Value, log: &Log) -> Caps {
    Caps {
        log: log.clone(),
        caps: case["caps"].as_array().cloned().unwrap_or_default().into(),
    }
}

pub fn policy(case: &Value) -> Policy {
    Policy {
        write_token: case["wt"].as_bool().unwrap_or(true),
        strict: case["strict"].as_bool().unwrap_or(false),
    }
}

pub fn request(case: &Value) -> Request {
    Request {
        store_path: text("/s"),
        policy: policy(case),
        cwd: opt(&case["cwd"]),
        session: opt(&case["session"]),
        identity: match case.get("id") {
            None => Some(identity("m1")),
            Some(Value::Null) => None,
            Some(v) => Some(identity(v.as_str().unwrap())),
        },
        budget: case["budget"].as_f64().unwrap_or(0.3),
    }
}

pub fn machineless_caller<'a>(case: &Value, log: &Log, session: Option<PyStr>) -> Caller<Fake<'a>> {
    let cap = Capture::new(text("<x>"), State::Indeterminate);
    Caller {
        location: to_location(&cap, &text("<no machine>"), session.as_deref()),
        history: None,
        resolution: resolution(&json!({"o": "indeterminate", "rule": "machineless"})),
        model: Fake {
            case: case.clone(),
            log: log.clone(),
            writer: None,
        },
    }
}

/// Runs `f` with the case's doubles.
#[allow(clippy::type_complexity)]
pub fn with_hands<'c, R>(
    case: &Value,
    log: &Log,
    capturer: &'c mut Caps,
    _req: &Request,
    f: impl FnOnce(
        Hands<
            'c,
            Caps,
            Fake<'static>,
            Box<dyn FnOnce() -> Result<MachineIdentity, String>>,
            Box<
                dyn FnOnce(
                    &MachineIdentity,
                    Option<&[u32]>,
                    Option<TokenWriter<'static>>,
                ) -> Result<Fake<'static>, ()>,
            >,
            Box<dyn FnMut(&[u32], bool, bool, Option<Lineage>) -> (Option<PyStr>, PyStr)>,
            Box<dyn FnOnce(&Fake<'static>, &Capture) -> Result<bool, ()>>,
            Box<dyn FnOnce(Machineless<'_, String>) -> Result<Caller<Fake<'static>>, ()>>,
        >,
    ) -> R,
) -> R {
    let (c1, l1) = (case.clone(), log.clone());
    let open_machine: Box<dyn FnOnce() -> Result<MachineIdentity, String>> = Box::new(move || {
        l1.borrow_mut().push(json!(["open"]));
        match c1["open"].as_str() {
            Some(id) => Ok(identity(id)),
            None => Err("no state".into()),
        }
    });
    let (c2, l2) = (case.clone(), log.clone());
    let new_model: Box<
        dyn FnOnce(
            &MachineIdentity,
            Option<&[u32]>,
            Option<TokenWriter<'static>>,
        ) -> Result<Fake<'static>, ()>,
    > = Box::new(move |id, session, writer| {
        l2.borrow_mut().push(json!([
            "model",
            s(&id.id),
            session.map(s),
            writer.is_some()
        ]));
        if c2["model_raises"].as_bool() == Some(true) {
            return Err(());
        }
        Ok(Fake {
            case: c2,
            log: l2,
            writer,
        })
    });
    let (c3, l3) = (case.clone(), log.clone());
    let remint: Box<dyn FnMut(&[u32], bool, bool, Option<Lineage>) -> (Option<PyStr>, PyStr)> =
        Box::new(move |git_dir, policy, bare, lineage| {
            l3.borrow_mut().push(json!([
                "remint",
                s(git_dir),
                policy,
                bare,
                lineage.map(|l| s(&l.token))
            ]));
            match c3["remint"].as_array() {
                Some(a) => (opt(&a[0]), opt(&a[1]).unwrap()),
                None => (Some(text("T")), text("written")),
            }
        });
    let (c4, l4) = (case.clone(), log.clone());
    let earlier_tree: Box<dyn FnOnce(&Fake<'static>, &Capture) -> Result<bool, ()>> =
        Box::new(move |_, cap| {
            l4.borrow_mut().push(json!(["earlier", s(&cap.cwd)]));
            if c4["early_raises"].as_bool() == Some(true) {
                return Err(());
            }
            Ok(c4["early"].as_bool().unwrap_or(false))
        });
    let (c5, l5) = (case.clone(), log.clone());
    let machineless: Box<dyn FnOnce(Machineless<'_, String>) -> Result<Caller<Fake<'static>>, ()>> =
        Box::new(move |m| {
            l5.borrow_mut().push(json!([
                "machineless",
                m.error,
                m.cwd.map(s),
                m.session.map(s),
                m.stand.map(|st| s(&st.path))
            ]));
            Ok(machineless_caller(&c5, &l5, m.session.map(<[u32]>::to_vec)))
        });
    f(Hands {
        open_machine,
        capturer,
        new_model,
        remint,
        earlier_tree,
        machineless,
        _model: std::marker::PhantomData,
    })
}

pub fn new_process() -> Process {
    Process::default()
}
