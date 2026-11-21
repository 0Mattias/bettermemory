//! Every sequence of `membership_core.jsonl` (not gated) and
//! `membership_core_unicode.jsonl` (gated on the Unicode version) against
//! the native `Membership` over the generator's recording store, and the
//! digest of `canonical`, `chain_of`, `_members` and `named` over seeded
//! merge graphs. The spelling is `tests/contract/gen_membership_core.py`'s.

mod common;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_core::*;
use common::membership_rows::{field, int, lines, list, name, same, Lcg};
use common::parity::Ledger;
use sha2::{Digest, Sha256};

fn check(ledger: &mut Ledger, label: &str, got: &PyValue, want: &PyValue, total: &mut usize) {
    *total += 1;
    assert!(
        !ledger.differs_fatally(label, same(got, want)),
        "{label}: got {got:?}, want {want:?}"
    );
}

fn run_steps(ledger: &mut Ledger, m: &mut Model, seq: &str, steps: &[PyValue], total: &mut usize) {
    for (i, st) in steps.iter().enumerate() {
        let ((o, log, rows), inner, _) = step(m, st);
        let label = format!("{seq} step {i} {:?}", field(st, "op"));
        check(ledger, &format!("{label} o"), &o, field(st, "o"), total);
        check(
            ledger,
            &format!("{label} log"),
            &log,
            field(st, "log"),
            total,
        );
        check(
            ledger,
            &format!("{label} rows"),
            &rows,
            field(st, "rows"),
            total,
        );
        if let Some(want) = st.get("inner") {
            let want = list(want);
            assert_eq!(inner.len(), want.len(), "{label}: inner steps");
            for (j, (o, log, rows)) in inner.iter().enumerate() {
                let l = format!("{label} inner {j}");
                check(ledger, &format!("{l} o"), o, field(&want[j], "o"), total);
                check(
                    ledger,
                    &format!("{l} log"),
                    log,
                    field(&want[j], "log"),
                    total,
                );
                check(
                    ledger,
                    &format!("{l} rows"),
                    rows,
                    field(&want[j], "rows"),
                    total,
                );
            }
        }
    }
}

fn run_file(test: &str, file: &str) -> (usize, usize) {
    let all = lines(file);
    let mut ledger = Ledger::new(test, file);
    let base = all
        .iter()
        .find(|l| name(field(l, "seq")) == "header")
        .map(|h| field(h, "base").clone());
    let mut total = 0;
    let mut seqs = 0;
    for l in &all {
        let seq = name(field(l, "seq"));
        if seq == "header" || seq == "digest" {
            continue;
        }
        let tables = match field(l, "tables") {
            PyValue::Str(_) => tables_of(base.as_ref().expect("a header")),
            other => tables_of(other),
        };
        let fail = match field(l, "fail") {
            PyValue::List(f) => Some((name(&f[0]), int(&f[1]) as usize)),
            _ => None,
        };
        let outer = field(l, "outer") == &PyValue::Bool(true);
        let mut m = model(field(l, "init"), tables, fail, outer);
        run_steps(
            &mut ledger,
            &mut m,
            &seq,
            list(field(l, "steps")),
            &mut total,
        );
        let after = tables_sha(&m.store().0.borrow());
        check(
            &mut ledger,
            &format!("{seq} after"),
            &PyValue::str(&after),
            field(l, "after"),
            &mut total,
        );
        seqs += 1;
    }
    assert!(total > 0 && seqs > 0);
    ledger.finish(total);
    (seqs, total)
}

#[test]
fn membership_core_every_sequence() {
    let (seqs, _) = run_file("membership_core_every_sequence", "membership_core.jsonl");
    let in_file = lines("membership_core.jsonl").len() - 2;
    assert_eq!(seqs, in_file);
}

#[test]
fn membership_core_unicode_every_sequence() {
    let (seqs, _) = run_file(
        "membership_core_unicode_every_sequence",
        "membership_core_unicode.jsonl",
    );
    assert_eq!(seqs, lines("membership_core_unicode.jsonl").len());
}

fn t(v: Option<&[u32]>) -> String {
    v.map_or("~".to_string(), |s| {
        String::from_utf8(s.iter().map(|&c| c as u8).collect()).expect("ASCII")
    })
}

fn joined(v: &[PyStr]) -> String {
    v.iter().map(|s| t(Some(s))).collect::<Vec<_>>().join(",")
}

fn project(pid: &str, name: &PyValue, merged: Option<&str>, parent: Option<&str>) -> PyValue {
    let o = |v: Option<&str>| v.map_or(PyValue::None, PyValue::str);
    PyValue::Dict(vec![
        (PyValue::str("id"), PyValue::str(pid)),
        (PyValue::str("name"), name.clone()),
        (PyValue::str("merged_into"), o(merged)),
        (PyValue::str("provisional"), PyValue::None),
        (PyValue::str("parent"), o(parent)),
        (PyValue::str("tenant"), PyValue::None),
        (PyValue::str("folder"), PyValue::int(0)),
        (PyValue::str("boundary"), PyValue::int(0)),
        (PyValue::str("minted"), PyValue::str("2026-01-01T00:00:00")),
    ])
}

#[test]
fn membership_core_digest() {
    let all = lines("membership_core.jsonl");
    let d = all.last().expect("the digest");
    assert_eq!(name(field(d, "seq")), "digest");
    let names = list(field(d, "names"));
    let scopes: Vec<Vec<PyStr>> = list(field(d, "scopes"))
        .iter()
        .map(|s| list(s).iter().map(|x| cps(&name(x))).collect())
        .collect();
    let mut rng = Lcg(int(field(d, "seed")) as u64);
    let init = PyValue::Dict(vec![
        (
            PyValue::str("now"),
            PyValue::List(vec![PyValue::str("2026-01-01T00:00:00"), PyValue::int(0)]),
        ),
        (PyValue::str("session"), PyValue::None),
        (PyValue::str("given"), PyValue::Bool(false)),
    ]);
    let mut h = Sha256::new();
    let mut count = 0usize;
    for _ in 0..int(field(d, "graphs")) {
        let n = 1 + rng.next(9);
        let ids: Vec<String> = (1..=n).map(|i| format!("P{i:03}")).collect();
        let mut links: Vec<Option<&str>> = vec![None, None, None, Some("global"), Some("PX")];
        links.extend(ids.iter().map(|s| Some(s.as_str())));
        let mut rows = Vec::new();
        for pid in &ids {
            let nm = &names[rng.next(names.len())];
            let merged = links[rng.next(links.len())];
            let parent = links[rng.next(links.len())];
            rows.push(project(pid, nm, merged, parent));
        }
        let mut m = model(
            &init,
            [("projects".to_string(), rows)].into_iter().collect(),
            None,
            false,
        );
        let mut asks: Vec<Option<PyStr>> = ids.iter().map(|s| Some(cps(s))).collect();
        asks.extend([Some(cps("global")), Some(cps("PX")), None]);
        for a in &asks {
            let c = m.canonical(a.as_deref()).expect("canonical");
            h.update(format!("C|{}|{}\n", t(a.as_deref()), t(c.as_deref())).as_bytes());
            let chain = m.chain_of(a.as_deref()).expect("chain_of");
            h.update(format!("H|{}|{}\n", t(a.as_deref()), joined(&chain)).as_bytes());
            count += 2;
        }
        for id in &ids {
            let members = m.members(&cps(id)).expect("members");
            h.update(format!("M|{id}|{}\n", joined(&members)).as_bytes());
            count += 1;
        }
        for sc in &scopes {
            let got: Vec<PyStr> = m.named(sc).expect("named").into_iter().collect();
            h.update(format!("N|{}|{}\n", joined(sc), joined(&got)).as_bytes());
            count += 1;
        }
    }
    assert!(count > 0);
    assert_eq!(count as i128, int(field(d, "count")));
    assert_eq!(common::hex(&h.finalize()), name(field(d, "sha256")));
}
