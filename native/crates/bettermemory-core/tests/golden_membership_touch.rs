//! Every sequence of `membership_touch.jsonl` (not gated) against the
//! native `Membership` over the generator's recording store: each step's
//! answer, its store calls in order, the rows it collected and the
//! model's `_remint`, and the tables after the sequence. The spelling is
//! `tests/contract/gen_membership_touch.py`'s.

mod common;

use bettermemory_core::pyvalue::PyValue;
use common::membership_core::{tables_of, tables_sha};
use common::membership_rows::{field, int, lines, list, name, same};
use common::membership_touch::{clock, model, states, step, writer};
use common::parity::Ledger;

const FILE: &str = "membership_touch.jsonl";
const PARTS: [&str; 4] = ["o", "log", "rows", "remint"];

#[test]
fn membership_touch_every_sequence() {
    let all = lines(FILE);
    let mut ledger = Ledger::new("membership_touch_every_sequence", FILE);
    let header = all
        .iter()
        .find(|l| name(field(l, "seq")) == "header")
        .expect("a header");
    let now = clock(field(header, "now"));
    let mut total = 0;
    let mut seqs = 0;
    for l in &all {
        let seq = name(field(l, "seq"));
        if seq == "header" {
            continue;
        }
        let tables = match field(l, "tables") {
            PyValue::Str(_) => tables_of(field(header, "base")),
            other => tables_of(other),
        };
        let fail = match field(l, "fail") {
            PyValue::List(f) => Some((name(&f[0]), int(&f[1]) as usize)),
            _ => None,
        };
        let mut m = model(
            tables,
            fail,
            states(field(l, "live")),
            writer(field(l, "writer")),
            now,
        );
        for (i, st) in list(field(l, "steps")).iter().enumerate() {
            let got = step(&mut m, st);
            for (part, g) in PARTS.iter().zip(got.iter()) {
                let label = format!("{seq} step {i} {part}");
                let want = field(st, part);
                total += 1;
                assert!(
                    !ledger.differs_fatally(&label, same(g, want)),
                    "{label}: got {g:?}, want {want:?}"
                );
            }
        }
        let after = PyValue::str(&tables_sha(&m.store().0.borrow()));
        total += 1;
        let label = format!("{seq} after");
        assert!(
            !ledger.differs_fatally(&label, same(&after, field(l, "after"))),
            "{label}"
        );
        seqs += 1;
    }
    assert!(total > 0);
    assert_eq!(seqs, all.len() - 1, "every sequence checked");
    ledger.finish(total);
}
