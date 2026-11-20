//! Every case of `machine_identity.jsonl` and `machine_identity_python.jsonl`
//! against `machine`: `MachineFacts.from_dict`/`to_dict`, `_identity_from`,
//! `identity_to_json`, `new_machine_id` and `is_clone`, the two sweeps by
//! their digests, and the recursion bound of `str()` in `from_dict`.

mod common;

use bettermemory_core::machine::{self as m, MachineFacts};
use bettermemory_core::pyvalue::{PyError, PyValue};
use common::parity::Ledger;
use common::{jsonl, neutral, unhex};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// `_common.neutral` of the values these answers hold.
fn to_neutral(v: &PyValue) -> Value {
    match v {
        PyValue::None => json!({"t": "null"}),
        PyValue::Bool(b) => json!({"t": "bool", "v": b}),
        PyValue::Int(t) => json!({"t": "int", "v": t}),
        PyValue::Str(s) => match s
            .iter()
            .map(|&c| char::from_u32(c))
            .collect::<Option<String>>()
        {
            Some(text) => json!({"t": "str", "v": text}),
            None => json!({"t": "str", "v_cps": s}),
        },
        PyValue::List(items) => {
            json!({"t": "list", "v": items.iter().map(to_neutral).collect::<Vec<_>>()})
        }
        PyValue::Dict(items) => json!({"t": "dict", "v": items
            .iter()
            .map(|(k, v)| json!([to_neutral(k), to_neutral(v)]))
            .collect::<Vec<_>>()}),
        other => panic!("no answer holds {other:?}"),
    }
}

fn answer(r: Result<Option<PyValue>, PyError>) -> Value {
    match r {
        Ok(None) => json!({"r": null}),
        Ok(Some(v)) => json!({"r": to_neutral(&v)}),
        Err(e) => json!({"x": e.name()}),
    }
}

fn opt_text(v: &Value) -> Option<Vec<u32>> {
    match neutral(v) {
        PyValue::None => None,
        PyValue::Str(ref s) => Some(s.clone()),
        other => panic!("a fact {other:?}"),
    }
}

fn facts(side: &Value) -> MachineFacts {
    let id_file = match neutral(&side["v"][2]) {
        PyValue::None => None,
        PyValue::Tuple(ref p) => {
            let s = |v: &PyValue| match v {
                PyValue::Str(s) => Some(s.clone()),
                PyValue::None => None,
                other => panic!("an id file part {other:?}"),
            };
            Some((
                s(&p[0]).expect("device"),
                s(&p[1]).expect("inode"),
                s(&p[2]),
            ))
        }
        other => panic!("an id file {other:?}"),
    };
    MachineFacts {
        hardware: opt_text(&side["v"][0]),
        hostname: opt_text(&side["v"][1]),
        id_file,
    }
}

const HARDWARE: [Option<&str>; 4] = [None, Some(""), Some("A"), Some("B")];
const HOSTNAMES: [Option<&str>; 4] = [None, Some(""), Some("h"), Some("g")];
const ID_FILES: [Option<(&str, &str, Option<&str>)>; 4] = [
    None,
    Some(("1", "2", None)),
    Some(("1", "2", Some("3"))),
    Some(("1", "3", None)),
];

fn all_facts() -> Vec<MachineFacts> {
    let t = |s: &str| s.chars().map(u32::from).collect::<Vec<u32>>();
    let mut out = Vec::new();
    for h in HARDWARE {
        for n in HOSTNAMES {
            for f in ID_FILES {
                out.push(MachineFacts {
                    hardware: h.map(t),
                    hostname: n.map(t),
                    id_file: f.map(|(d, i, b)| (t(d), t(i), b.map(t))),
                });
            }
        }
    }
    out
}

fn sweep_bytes(i: u32) -> [u8; 17] {
    let mut h = Sha256::new();
    h.update(b"machine-id ");
    h.update(i.to_be_bytes());
    let d = h.finalize();
    d[..17].try_into().expect("17 bytes")
}

fn sha_hex(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn nest(kind: &str, n: u64) -> PyValue {
    let mut v = PyValue::int(1);
    for _ in 0..n {
        v = match kind {
            "l" => PyValue::List(vec![v]),
            "t" => PyValue::Tuple(vec![v]),
            _ => PyValue::Dict(vec![(PyValue::str("k"), v)]),
        };
    }
    v
}

fn run(row: &Value) -> (Value, Value) {
    let want = |keys: &[&str]| {
        let mut o = serde_json::Map::new();
        for k in keys {
            if let Some(v) = row.get(*k) {
                o.insert((*k).into(), v.clone());
            }
        }
        Value::Object(o)
    };
    match row["f"].as_str().expect("f") {
        "fd" => {
            let a = neutral(&row["a"]);
            let got = answer(MachineFacts::from_dict(&a).map(|f| f.map(|f| f.to_dict())));
            (got, want(&["r", "x"]))
        }
        "id" => {
            let a = neutral(&row["a"]);
            let got = answer(m::identity_from(&a).map(|i| i.map(|i| m::identity_to_json(&i))));
            (got, want(&["r", "x"]))
        }
        "cl" => {
            let pair = &row["a"]["v"];
            let got = m::is_clone(&facts(&pair[0]), &facts(&pair[1]));
            (json!({"r": got}), want(&["r"]))
        }
        "cl_sweep" => {
            let all = all_facts();
            let mut bits = Vec::new();
            for r in &all {
                for s in &all {
                    bits.push(if m::is_clone(r, s) { b'1' } else { b'0' });
                }
            }
            (
                json!({"n": bits.len(), "sha": sha_hex(&bits)}),
                want(&["n", "sha"]),
            )
        }
        "nid" => {
            let b: [u8; 17] = unhex(row["b"].as_str().expect("b")).try_into().expect("17");
            (json!({"r": m::new_machine_id(&b)}), want(&["r"]))
        }
        "nid_sweep" => {
            let n = row["n"].as_u64().expect("n") as u32;
            let ids: Vec<String> = (0..n).map(|i| m::new_machine_id(&sweep_bytes(i))).collect();
            (
                json!({"n": n, "sha": sha_hex(ids.join("\n").as_bytes())}),
                want(&["n", "sha"]),
            )
        }
        "deep" => {
            let mut parts = vec![PyValue::int(1), PyValue::int(1), PyValue::int(1)];
            let w = row["w"].as_u64().expect("w") as usize;
            parts[w] = nest(row["k"].as_str().expect("k"), row["n"].as_u64().expect("n"));
            let raw = PyValue::Dict(vec![(PyValue::str("id_file"), PyValue::List(parts))]);
            let got = match MachineFacts::from_dict(&raw) {
                Ok(_) => json!({"r": "ok"}),
                Err(e) => json!({"x": e.name()}),
            };
            (got, want(&["r", "x"]))
        }
        other => panic!("unknown row {other}"),
    }
}

fn check_file(test: &str, name: &str) {
    let lines = jsonl(name);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    assert_eq!(count, lines.len() - 1, "{name}: header count");
    assert!(count > 0, "{name}: no case");
    let ledger = Ledger::new(test, name);
    let mut failures = Vec::new();
    for (i, row) in lines[1..].iter().enumerate() {
        let (got, want) = run(row);
        if got != want {
            failures.push(format!("row {i} {}: native {got} want {want}", row["f"]));
        }
    }
    ledger.settle(count, &failures);
}

#[test]
fn machine_identity_every_case() {
    check_file("golden_machine_identity", "machine_identity.jsonl");
}

#[test]
fn machine_identity_python_every_case() {
    check_file("golden_machine_identity", "machine_identity_python.jsonl");
}
