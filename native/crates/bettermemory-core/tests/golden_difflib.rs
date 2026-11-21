//! Every case of `difflib.jsonl` (not gated)
//! against the native `difflib`: `history._body_diff`, `unified_diff` over
//! line lists, `str.splitlines`, and the seeded random sweeps compared by
//! count and digest (`tests/contract/gen_difflib.py`).

mod common;

use bettermemory_core::difflib::{body_diff, splitlines, unified_diff_lines};
use bettermemory_core::pyvalue::PyStr;
use common::parity::Ledger;
use serde_json::Value;
use sha2::{Digest, Sha256};

fn cps(v: &Value) -> PyStr {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap() as u32)
        .collect()
}

fn lines(v: &Value) -> Vec<PyStr> {
    v.as_array().unwrap().iter().map(cps).collect()
}

fn s(t: &str) -> PyStr {
    t.chars().map(u32::from).collect()
}

/// The generator's `_Rng`: xorshift64*.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: u64) -> u64 {
        (self.next() >> 33) % n
    }
}

/// The generator's `_near`.
fn near(rng: &mut Rng, n: usize, c: usize) -> (Vec<PyStr>, Vec<PyStr>) {
    let mut b: Vec<PyStr> = (0..n)
        .map(|_| s(&format!("x{}", rng.below(1000))))
        .collect();
    let p = s("P");
    for _ in 0..c {
        let i = rng.below(n as u64) as usize;
        b[i] = p.clone();
    }
    while b.iter().filter(|l| **l == p).count() < c {
        let i = rng.below(n as u64) as usize;
        if b[i] != p {
            b[i] = p.clone();
        }
    }
    let mut a = Vec::new();
    for line in &b {
        let r = rng.below(16);
        if r < 2 {
            continue;
        }
        if r < 4 {
            a.push(s(&format!("y{}", rng.below(1000))));
            continue;
        }
        if r == 4 {
            a.push(p.clone());
        }
        a.push(line.clone());
    }
    (a, b)
}

/// The generator's `_small`.
fn small(rng: &mut Rng) -> (Vec<PyStr>, Vec<PyStr>) {
    let one = |rng: &mut Rng| {
        let len = rng.below(13);
        (0..len)
            .map(|_| vec![u32::from(b"abcd"[rng.below(4) as usize])])
            .collect::<Vec<PyStr>>()
    };
    let a = one(rng);
    let b = one(rng);
    (a, b)
}

fn bulk(r: &Value) -> String {
    let mut rng = Rng(r["seed"].as_u64().unwrap());
    let (n, c) = (
        r["n"].as_u64().unwrap() as usize,
        r["c"].as_u64().unwrap() as usize,
    );
    let mut h = Sha256::new();
    for _ in 0..r["count"].as_u64().unwrap() {
        let (a, b) = if r["kind"] == "near" {
            near(&mut rng, n, c)
        } else {
            small(&mut rng)
        };
        let out: String = unified_diff_lines(&a, &b, &s("a"), &s("b"))
            .iter()
            .map(|&x| char::from_u32(x).unwrap())
            .collect();
        h.update(out.as_bytes());
        h.update([0u8]);
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

fn check(r: &Value) {
    match r["k"].as_str().unwrap() {
        "body" => {
            let before = match &r["before"] {
                Value::Null => None,
                v => Some(cps(v)),
            };
            let got = body_diff(
                before.as_deref(),
                &cps(&r["after"]),
                &cps(&r["old"]),
                &cps(&r["new"]),
            );
            assert_eq!(got, cps(&r["out"]));
        }
        "lines" => {
            let got = unified_diff_lines(&lines(&r["a"]), &lines(&r["b"]), &s("o"), &s("n"));
            assert_eq!(got, cps(&r["out"]));
        }
        "split" => assert_eq!(splitlines(&cps(&r["t"])), lines(&r["out"])),
        "bulk" => assert_eq!(bulk(r), r["sha"].as_str().unwrap()),
        other => panic!("unknown row kind {other}"),
    }
}

#[test]
fn difflib_every_case() {
    let file = "difflib.jsonl";
    let rows = common::jsonl(file);
    let header = &rows[0];
    let rows = &rows[1..];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, rows.len());
    let mut ledger = Ledger::new("difflib_every_case", file);
    for (i, r) in rows.iter().enumerate() {
        let label = format!("line {} {}", i + 2, r["k"]);
        ledger.case(label, || check(r));
    }
    ledger.finish(rows.len());
}
