//! Every sequence of `membership_legacy.jsonl` (not gated) against the
//! native legacy questions, shown questions, roots and record counts of
//! `Membership` over the generator's recording store. The spelling is
//! `tests/contract/gen_membership_legacy.py`'s.

mod common;

use bettermemory_core::pyvalue::PyValue;
use common::membership_core::model;
use common::membership_legacy::{named, step, tables};
use common::membership_queries::files_of;
use common::membership_rows::{field, int, lines, list, name, same};
use common::parity::Ledger;

/// The `_sequence` of the generator: a model at a fixed UTC time.
fn init() -> PyValue {
    PyValue::Dict(vec![
        (
            PyValue::str("now"),
            PyValue::List(vec![PyValue::str("2026-03-04T05:06:07"), PyValue::int(0)]),
        ),
        (PyValue::str("session"), PyValue::None),
        (PyValue::str("given"), PyValue::Bool(false)),
    ])
}

#[test]
fn membership_legacy_every_sequence() {
    let file = "membership_legacy.jsonl";
    let all = lines(file);
    let mut ledger = Ledger::new("membership_legacy_every_sequence", file);
    let header = &all[0];
    assert_eq!(name(field(header, "seq")), "header");
    let named = named(header);
    let mut total = 0usize;
    let mut seqs = 0usize;
    for l in &all[1..] {
        let seq = name(field(l, "seq"));
        let fail = match field(l, "fail") {
            PyValue::List(f) => Some((name(&f[0]), int(&f[1]) as usize)),
            _ => None,
        };
        let files = files_of(field(l, "files"));
        let mut m = model(&init(), tables(&named, l), fail, false);
        for (i, st) in list(field(l, "steps")).iter().enumerate() {
            let (o, log, rows) = step(&mut m, st, &files);
            let label = format!("{seq} step {i} {:?}", field(st, "op"));
            for (part, got) in [("o", &o), ("log", &log), ("rows", &rows)] {
                let want = field(st, part);
                total += 1;
                assert!(
                    !ledger.differs_fatally(format!("{label} {part}"), same(got, want)),
                    "{label} {part}: got {got:?}, want {want:?}"
                );
            }
        }
        seqs += 1;
    }
    assert!(total > 0);
    assert_eq!(seqs, all.len() - 1);
    ledger.finish(total);
}
