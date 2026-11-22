//! Every case of `membership_records_identity.jsonl` (not gated) and
//! `membership_records_identity_python.jsonl` (gated on the Python
//! release) against `membership_records::identity`: `from_json`'s answer
//! or ValueError text, `to_json` and `digest`, and the seeded sweep. The
//! spelling of each row and the sweep's rule are the generator's
//! (`tests/contract/gen_membership_records.py`, unit 4).

mod common;

use bettermemory_core::json::{self, dumps, DumpOptions};
use bettermemory_core::membership_records::identity::PortableIdentity;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use common::parity::Ledger;
use sha2::{Digest, Sha256};

const FILE: &str = "membership_records_identity.jsonl";
const PYTHON: &str = "membership_records_identity_python.jsonl";

fn rows(file: &str) -> Vec<PyValue> {
    let text = std::fs::read_to_string(common::golden_path(file))
        .unwrap_or_else(|e| panic!("cannot read golden file {file}: {e}"));
    text.lines()
        .map(|l| json::loads(&unicode::cps(l)).expect("a JSON line"))
        .collect()
}

fn field<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no field {k}"))
}

fn sorted(v: &PyValue) -> Vec<u32> {
    let options = DumpOptions {
        sort_keys: true,
        ..DumpOptions::DEFAULT
    };
    dumps(v, options).expect("encodes")
}

/// Checks one identity row; panics with the difference.
fn check(row: &PyValue) {
    let raw = field(row, "a");
    let out = field(row, "o");
    let got = PortableIdentity::from_json(raw);
    match (out.get("raised"), got) {
        (Some(PyValue::List(r)), Err(text)) => {
            assert_eq!(r[0], PyValue::str("ValueError"), "{raw:?}");
            assert_eq!(r[1], PyValue::Str(text), "{raw:?}");
        }
        (None, Ok(p)) => {
            assert_eq!(sorted(&p.to_json()), sorted(field(out, "json")), "{raw:?}");
            assert_eq!(PyValue::str(&p.digest()), *field(out, "digest"), "{raw:?}");
        }
        (want, got) => panic!("{raw:?}: want {want:?} {out:?}, got {got:?}"),
    }
}

struct Rng(u64);

impl Rng {
    fn next(&mut self, bound: u64) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((self.0 >> 32) % bound) as usize
    }
}

fn texts(items: &[&str]) -> Vec<PyValue> {
    items.iter().map(|s| PyValue::str(s)).collect()
}

/// `_identity_doc`, drawn alike.
fn identity_doc(rng: &mut Rng) -> PyValue {
    let names = ["", "p", "Proj", "a b"];
    let (a40, b40, z64) = ("a".repeat(40), "b".repeat(40), "0".repeat(64));
    let sp = format!(" {}", "c".repeat(40));
    let (up, a39, f41) = ("A".repeat(40), "a".repeat(39), "f".repeat(41));
    let nl = format!("{}\n", "1".repeat(64));
    let witnesses = [&a40, &b40, &z64, &sp, &up, &a39, &f41, &nl, &a40];
    let remotes = texts(&["r1", "local:x", "r0", "r1", "local:"]);
    let files = texts(&["f2", "f1", "f2"]);
    let flags = [
        None,
        Some(PyValue::Bool(true)),
        Some(PyValue::Bool(false)),
        Some(PyValue::int(1)),
    ];
    let parents = [
        None,
        Some(PyValue::None),
        Some(PyValue::int(0)),
        Some(PyValue::int(3)),
        Some(PyValue::int(-1)),
        Some(PyValue::Bool(true)),
    ];
    let mut doc = vec![(PyValue::str("name"), PyValue::str(names[rng.next(4)]))];
    for (key, pool) in [("remotes", &remotes), ("file_ids", &files)] {
        if rng.next(4) != 0 {
            let n = rng.next(4);
            let items = (0..n)
                .map(|_| pool[rng.next(pool.len() as u64)].clone())
                .collect();
            doc.push((PyValue::str(key), PyValue::List(items)));
        }
    }
    if rng.next(4) != 0 {
        let n = rng.next(5);
        let items = (0..n)
            .map(|_| PyValue::str(witnesses[rng.next(witnesses.len() as u64)]))
            .collect();
        doc.push((PyValue::str("witnesses"), PyValue::List(items)));
    }
    for key in ["folder", "boundary"] {
        if let Some(v) = &flags[rng.next(4)] {
            doc.push((PyValue::str(key), v.clone()));
        }
    }
    if let Some(v) = &parents[rng.next(6)] {
        doc.push((PyValue::str("parent"), v.clone()));
    }
    PyValue::Dict(doc)
}

fn sweep(seed: u64, n: usize) -> String {
    let mut rng = Rng(seed);
    let lines: Vec<String> = (0..n)
        .map(
            |_| match PortableIdentity::from_json(&identity_doc(&mut rng)) {
                Ok(p) => format!("{}|{}", p.digest(), p.parent.as_deref().unwrap_or("None")),
                Err(text) => format!(
                    "!{}",
                    text.iter()
                        .map(|&c| char::from_u32(c).expect("ASCII"))
                        .collect::<String>()
                ),
            },
        )
        .collect();
    common::hex(&Sha256::digest(lines.join("\n").as_bytes()))
}

#[test]
fn membership_records_identity_every_case() {
    let all = rows(FILE);
    let mut checked = 0;
    for row in &all {
        match field(row, "f") {
            PyValue::Str(f) if *f == unicode::cps("identity") => check(row),
            PyValue::Str(f) if *f == unicode::cps("identity_sweep") => {
                let PyValue::Int(seed) = field(row, "seed") else {
                    panic!("seed")
                };
                let PyValue::Int(n) = field(row, "n") else {
                    panic!("n")
                };
                let got = sweep(seed.parse().unwrap(), n.parse().unwrap());
                assert_eq!(PyValue::str(&got), *field(row, "o"), "identity sweep");
            }
            other => panic!("unknown row {other:?}"),
        }
        checked += 1;
    }
    common::assert_count(FILE, checked, all.len());
}

#[test]
fn membership_records_identity_python_every_case() {
    let all = rows(PYTHON);
    let mut ledger = Ledger::new("membership_records_identity_python_every_case", PYTHON);
    for (i, row) in all.iter().enumerate() {
        ledger.case(format!("row {i}"), || check(row));
    }
    ledger.finish(all.len());
}
