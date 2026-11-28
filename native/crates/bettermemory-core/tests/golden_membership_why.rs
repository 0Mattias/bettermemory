//! Every sequence of `membership_why.jsonl` (not gated) against the native
//! `Membership::why` over the generator's recording store and scripted
//! membership log, and the digest of its seeded sweep of 1,000 stores. The
//! spelling is `tests/contract/gen_membership_why.py`'s.

mod common;

use bettermemory_core::pyvalue::PyValue;
use common::membership_rows::{field, int, lines, list, name, same, Lcg};
use common::membership_why::{digest_line, step, tables, why_model, Pools};
use common::parity::Ledger;
use sha2::{Digest, Sha256};

#[test]
fn membership_why_every_sequence() {
    let file = "membership_why.jsonl";
    let all = lines(file);
    let mut ledger = Ledger::new("membership_why_every_sequence", file);
    let header = &all[0];
    assert_eq!(name(field(header, "seq")), "header");
    let mut total = 0usize;
    let mut steps = 0usize;
    let mut seqs = 0usize;
    for l in &all[1..all.len() - 1] {
        let seq = name(field(l, "seq"));
        let fail = match field(l, "fail") {
            PyValue::List(f) => Some((name(&f[0]), int(&f[1]) as usize)),
            _ => None,
        };
        let mut m = why_model(tables(header, l), fail);
        for (i, st) in list(field(l, "steps")).iter().enumerate() {
            let (o, log) = step(&mut m, st);
            let label = format!("{seq} step {i} {:?}", field(st, "a"));
            for (part, got) in [("o", &o), ("log", &log)] {
                let want = field(st, part);
                total += 1;
                assert!(
                    !ledger.differs_fatally(format!("{label} {part}"), same(got, want)),
                    "{label} {part}: got {got:?}, want {want:?}"
                );
            }
            steps += 1;
        }
        seqs += 1;
    }
    assert!(steps > 0, "no step checked");
    assert_eq!(seqs, all.len() - 2);
    ledger.finish(total);
}

#[test]
fn membership_why_seeded_sweep() {
    let file = "membership_why.jsonl";
    let all = lines(file);
    let row = all.last().expect("the digest row");
    assert_eq!(name(field(row, "seq")), "digest");
    let mut ledger = Ledger::new("membership_why_seeded_sweep", file);
    let pools = Pools::of(row);
    let asked: Vec<String> = list(field(row, "asked")).iter().map(name).collect();
    let mut rng = Lcg(int(field(row, "seed")) as u64);
    let mut h = Sha256::new();
    let mut count = 0usize;
    for _ in 0..int(field(row, "stores")) {
        let mut m = why_model(pools.store(&mut rng, &asked), None);
        for rid in &asked {
            let start = m.store().0.borrow().log.len();
            let got = m.why(&bettermemory_core::unicode::cps(rid));
            let log = m.store().0.borrow().log[start..].to_vec();
            h.update(digest_line(rid, &got, &log).as_bytes());
            count += 1;
        }
    }
    let want = int(field(row, "count")) as usize;
    assert!(count > 0, "no case checked");
    assert_eq!(count, want, "cases checked");
    let digest: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    assert!(
        !ledger.differs_fatally(
            "sweep sha256".to_string(),
            digest == name(field(row, "sha256"))
        ),
        "the sweep's digest differs"
    );
    ledger.finish(1);
}
