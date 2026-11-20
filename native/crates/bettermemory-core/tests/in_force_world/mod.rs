//! The store and admission of `gen_in_force`'s cases, as test doubles
//! that write every call into one shared log, and the sweep's rule
//! (`_draw`).

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use bettermemory_core::in_force::{in_force, InForce, Inbound, LinkReader, RecordView};
use serde_json::{json, Value};

pub type Log = Rc<RefCell<Vec<Value>>>;

pub struct Store {
    pub links: HashMap<String, Vec<Inbound>>,
    pub held: HashSet<String>,
    pub log: Option<Log>,
}

pub struct View {
    pub admit: Option<HashSet<String>>,
    pub log: Option<Log>,
}

fn note(log: &Option<Log>, call: Value) {
    if let Some(log) = log {
        log.borrow_mut().push(call);
    }
}

impl LinkReader for Store {
    fn inbound_links(
        &mut self,
        targets: &[String],
        types: &[&str],
    ) -> HashMap<String, Vec<Inbound>> {
        assert_eq!(types, ["supersedes", "extends"]);
        note(&self.log, json!(["l", targets]));
        targets
            .iter()
            .filter_map(|t| self.links.get(t).map(|r| (t.clone(), r.clone())))
            .collect()
    }

    fn held_ids(&mut self, ids: &[String]) -> HashSet<String> {
        note(&self.log, json!(["h", ids]));
        ids.iter()
            .filter(|r| self.held.contains(*r))
            .cloned()
            .collect()
    }
}

impl RecordView for View {
    fn prime(&mut self, ids: &[String]) {
        note(&self.log, json!(["p", ids]));
    }

    fn admits_id(&mut self, id: &str) -> bool {
        note(&self.log, json!(["a", id]));
        self.admit.as_ref().is_none_or(|a| a.contains(id))
    }
}

/// One case: the inbound table, the admitted ids (None for all), the held
/// ids and the asked ids.
#[derive(Clone, Debug, Default)]
pub struct Case {
    pub links: Vec<(String, Vec<Inbound>)>,
    pub admit: Option<Vec<String>>,
    pub held: Vec<String>,
    pub ids: Vec<String>,
}

pub fn s(x: &str) -> String {
    x.to_owned()
}

pub fn ids(xs: &[&str]) -> Vec<String> {
    xs.iter().map(|x| s(x)).collect()
}

pub fn row(kind: &str, source: &str, stamp: &str) -> Inbound {
    (s(kind), s(source), s(stamp))
}

/// How long one case may take before its test fails. A broken cycle
/// check never ends; the deadline turns that hang into a failure.
pub const DEADLINE: Duration = Duration::from_secs(60);

/// Runs a case, logging every call when `log`, failing when it has not
/// answered within `DEADLINE`.
pub fn run(case: &Case, log: bool) -> (Vec<InForce>, Vec<Value>) {
    run_within(case, log, DEADLINE)
}

/// `run` on its own thread, failing when it has not answered within `limit`.
pub fn run_within(case: &Case, log: bool, limit: Duration) -> (Vec<InForce>, Vec<Value>) {
    let case = case.clone();
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = tx.send(run_here(&case, log));
    });
    rx.recv_timeout(limit)
        .expect("in_force answered before its deadline")
}

fn run_here(case: &Case, log: bool) -> (Vec<InForce>, Vec<Value>) {
    let shared: Option<Log> = log.then(|| Rc::new(RefCell::new(Vec::new())));
    let mut store = Store {
        links: case.links.iter().cloned().collect(),
        held: case.held.iter().cloned().collect(),
        log: shared.clone(),
    };
    let mut view = View {
        admit: case.admit.as_ref().map(|a| a.iter().cloned().collect()),
        log: shared.clone(),
    };
    let out = in_force(&mut store, &case.ids, &mut view);
    drop(store);
    drop(view);
    let calls = shared.map_or_else(Vec::new, |l| l.take());
    (out, calls)
}

/// `[out, calls]` as the generator spells them.
pub fn answer(case: &Case) -> Value {
    let (out, calls) = run(case, true);
    let out: Vec<Value> = out
        .iter()
        .map(|f| json!([f.asked, f.id, f.replaces, f.extended_by, f.held]))
        .collect();
    json!([out, calls])
}

/// The generator's `_draw`: the sweep's cases from `seed`.
pub fn draw(seed: u64, count: usize) -> Vec<Case> {
    let mut x = seed;
    let mut pick = |n: u64| -> usize {
        x = x
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((x >> 33) % n) as usize
    };
    let names = ["", "a", "b", "c", "d", "e", "f", "g"];
    let stamps = ["", "1", "2", "3"];
    let kinds = [
        "supersedes",
        "supersedes",
        "supersedes",
        "extends",
        "extends",
        "contradicts",
    ];
    let mut out = Vec::with_capacity(count);
    for _ in 0..count {
        let mut links = Vec::new();
        for target in &names[1..] {
            let n = pick(4);
            let rows: Vec<Inbound> = (0..n)
                .map(|_| {
                    let k = kinds[pick(6)];
                    let src = names[pick(8)];
                    let st = stamps[pick(4)];
                    row(k, src, st)
                })
                .collect();
            if !rows.is_empty() {
                links.push((s(target), rows));
            }
        }
        let admit = if pick(3) == 0 {
            None
        } else {
            Some(
                names
                    .iter()
                    .filter(|_| pick(4) != 0)
                    .map(|n| s(n))
                    .collect(),
            )
        };
        let held = names
            .iter()
            .filter(|_| pick(5) == 0)
            .map(|n| s(n))
            .collect();
        let n = pick(5);
        let asked = (0..n).map(|_| s(names[pick(8)])).collect();
        out.push(Case {
            links,
            admit,
            held,
            ids: asked,
        });
    }
    out
}
