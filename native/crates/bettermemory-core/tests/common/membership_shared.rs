//! The spelling of `membership_shared.jsonl`
//! (`tests/contract/gen_membership_shared.py`): checkouts, sightings,
//! stamps, question and log rows decoded, and the generator's sweep
//! (`_Lcg`, `_draw`, `_sweep_line`) made again.

#![allow(dead_code)]

use std::collections::BTreeSet;

use bettermemory_core::capture::location::{Location, ObjectKey};
use bettermemory_core::capture::{Stamp, State};
use bettermemory_core::json;
use bettermemory_core::log::LogRow;
use bettermemory_core::membership_records::records::{Checkout, Proposal};
use bettermemory_core::membership_shared as ms;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;

pub fn cps(s: &str) -> PyStr {
    unicode::cps(s)
}

pub fn lines(name: &str) -> Vec<PyValue> {
    let text = std::fs::read_to_string(super::common::golden_path(name))
        .unwrap_or_else(|e| panic!("cannot read golden file {name}: {e}"));
    text.lines()
        .map(|l| json::loads(&cps(l)).expect("a JSON line"))
        .collect()
}

pub fn field<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no field {k} in {v:?}"))
}

pub fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

pub fn name(v: &PyValue) -> String {
    match v {
        PyValue::Str(t) => unicode::to_string(t).expect("a name"),
        other => panic!("not a name: {other:?}"),
    }
}

pub fn int(v: &PyValue) -> i128 {
    match v {
        PyValue::Int(t) => t.parse().expect("an int"),
        other => panic!("not an int: {other:?}"),
    }
}

fn int_text(v: &PyValue) -> String {
    match v {
        PyValue::Int(t) => t.clone(),
        other => panic!("not an int: {other:?}"),
    }
}

pub fn stamp(v: &PyValue) -> Stamp {
    let s = list(v);
    let PyValue::Str(n) = &s[0] else {
        panic!("a stamp name")
    };
    Stamp {
        name: n.clone(),
        numbers: [
            int_text(&s[1]),
            int_text(&s[2]),
            int_text(&s[3]),
            int_text(&s[4]),
        ],
    }
}

pub fn stamps(v: &PyValue) -> Vec<Stamp> {
    list(v).iter().map(stamp).collect()
}

fn ticks(v: Option<&PyValue>) -> Option<bool> {
    match v {
        Some(PyValue::Bool(b)) => Some(*b),
        _ => None,
    }
}

/// A checkout with the fields the rules read; the rest are placeholders.
pub fn make_checkout(
    machine: &str,
    token: Option<PyStr>,
    stamps: Vec<Stamp>,
    ticks: Option<bool>,
    key: Option<(&str, &str)>,
) -> Checkout {
    Checkout {
        id: cps("k1"),
        project: cps("p1"),
        machine: cps(machine),
        kind: cps("primary"),
        object_key: key.map(|(d, i)| ObjectKey {
            device: cps(d),
            inode: cps(i),
            birth: None,
        }),
        common_object_key: None,
        git_dir: None,
        token,
        token_status: cps("own"),
        stamps,
        last_head: None,
        branch: None,
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: cps("t0"),
        views: Vec::new(),
        sessions: Vec::new(),
        replaced_by: None,
        ticks,
    }
}

pub fn checkout(v: &PyValue) -> Checkout {
    let machine = v.get("m").map_or("m1".to_owned(), name);
    let token = match v.get("t") {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let st = v.get("s").map(stamps).unwrap_or_default();
    let key = match v.get("o") {
        Some(PyValue::List(o)) => Some((name(&o[0]), name(&o[1]))),
        _ => None,
    };
    make_checkout(
        &machine,
        token,
        st,
        ticks(v.get("x")),
        key.as_ref().map(|(d, i)| (d.as_str(), i.as_str())),
    )
}

pub fn make_location(stamps: Vec<Stamp>, ticks: Option<bool>) -> Location {
    Location {
        machine: cps("m1"),
        cwd: cps("/w"),
        state: State::Repo,
        root: None,
        kind: None,
        git_dir: None,
        object_key: None,
        common_object_key: None,
        root_object_key: None,
        head: None,
        branch: None,
        ref_tips: Vec::new(),
        remote_keys: BTreeSet::new(),
        alias_keys: BTreeSet::new(),
        shallow: false,
        token: None,
        token_written: None,
        token_status: cps("own"),
        stamps,
        ticks,
        files: Vec::new(),
        file_tracked: None,
        evicted: false,
        detail: Vec::new(),
        manifest: None,
        session: None,
    }
}

pub fn location(v: &PyValue) -> Location {
    make_location(
        v.get("s").map(stamps).unwrap_or_default(),
        ticks(v.get("x")),
    )
}

fn payload(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) => unicode::to_string(s).expect("a payload"),
        _ => {
            let r = list(field(v, "rep"));
            let mut out = name(&r[0]);
            out.push_str(&name(&r[1]).repeat(int(&r[2]) as usize));
            out.push_str(&name(&r[3]));
            out
        }
    }
}

pub fn log_row(kind: &str, payload: String) -> LogRow {
    LogRow {
        seq: 1,
        ts: String::new(),
        session: None,
        kind: kind.to_owned(),
        payload,
        prev_mac: String::new(),
        mac: String::new(),
    }
}

pub fn log_rows(v: &PyValue) -> Vec<LogRow> {
    list(v)
        .iter()
        .map(|r| {
            let r = list(r);
            log_row(&name(&r[0]), payload(&r[1]))
        })
        .collect()
}

pub fn proposal(kind: &PyValue, evidence: &PyValue) -> Proposal {
    let PyValue::Str(k) = kind else {
        panic!("a kind")
    };
    Proposal {
        id: cps("p1"),
        checkout: Some(cps("k1")),
        kind: k.clone(),
        candidates: Vec::new(),
        evidence: evidence.clone(),
        reason: Vec::new(),
        line: Vec::new(),
        state: cps("open"),
        answer: None,
        answered_by: None,
        signature: None,
        opened: cps("t0"),
    }
}

/// The generator's spelling of what a question row's read raised.
pub fn row_error(e: &ms::RowError) -> PyStr {
    cps(&match e {
        ms::RowError::Key(k) => format!("KeyError:{k}"),
        ms::RowError::Py(p) => p.name().to_owned(),
        ms::RowError::AttributeError => "AttributeError".to_owned(),
    })
}

/// The generator's `_Lcg`.
pub struct Lcg(pub u64);

impl Lcg {
    pub fn next(&mut self, bound: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) % bound
    }
}

/// The generator's `_TIMES`.
pub fn times() -> Vec<String> {
    let s: i128 = 1_000_000_000;
    let huge = "1".to_owned() + &"0".repeat(40);
    let p127 = "170141183460469231731687303715884105728";
    let mut out: Vec<String> = [
        0,
        100,
        s,
        s + 1,
        s + 100,
        2 * s,
        2 * s - 1,
        3 * s + 500,
        -s,
        -100,
    ]
    .iter()
    .map(i128::to_string)
    .collect();
    out.push(huge.clone());
    out.push(format!("{}100", &huge[..huge.len() - 3]));
    out.push(format!("{}999999999", &huge[..huge.len() - 9]));
    out.push(format!("-{huge}"));
    out.push(p127.to_owned());
    out.push("170141183460469231731687303715884105828".to_owned());
    out
}

pub fn draw(rng: &mut Lcg) -> Vec<Stamp> {
    let t = times();
    let names = ["description", "info/exclude", "commondir"];
    let n = rng.next(3) + 1;
    (0..n)
        .map(|_| {
            let nm = names[rng.next(3) as usize];
            let ino = (rng.next(2) + 1).to_string();
            let m = t[rng.next(t.len() as u64) as usize].clone();
            let c = t[rng.next(t.len() as u64) as usize].clone();
            let size = rng.next(2).to_string();
            Stamp {
                name: cps(nm),
                numbers: [ino, m, c, size],
            }
        })
        .collect()
}

pub fn sweep_line(a: &[Stamp], b: &[Stamp]) -> String {
    let ka = make_checkout("m1", None, a.to_vec(), None, None);
    let lb = make_location(b.to_vec(), None);
    let ea = ms::exact(a);
    let eb = ms::exact(b);
    let bits = [
        ms::same_files(a, b),
        ms::read_here(&ka, &lb),
        ms::moved_under(&ka, &lb),
        ms::ticked(a),
        ms::in_ticks_at(&ka, &lb),
        ms::carried_in_ticks(&ka, &make_checkout("m1", None, b.to_vec(), None, None), &lb),
        ea.intersection(&eb).next().is_some(),
    ];
    bits.iter().map(|&x| if x { '1' } else { '0' }).collect()
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
