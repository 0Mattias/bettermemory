//! Every sequence of `membership_writes.jsonl` (not gated) against the
//! native `Membership` over the generator's recording store: each step's
//! answer, its store calls in order and the rows it collected, and the
//! tables after the sequence. The spelling is
//! `tests/contract/gen_membership_writes.py`'s.

mod common;

use bettermemory_core::pyvalue::PyValue;
use common::membership_core::{model, tables_of, tables_sha};
use common::membership_rows::{field, int, lines, list, name, same};
use common::membership_writes::{histories, step};
use common::parity::Ledger;

const FILE: &str = "membership_writes.jsonl";

fn check(ledger: &mut Ledger, label: &str, got: &PyValue, want: &PyValue, total: &mut usize) {
    *total += 1;
    assert!(
        !ledger.differs_fatally(label, same(got, want)),
        "{label}: got {got:?}, want {want:?}"
    );
}

#[test]
fn membership_writes_every_sequence() {
    let all = lines(FILE);
    let mut ledger = Ledger::new("membership_writes_every_sequence", FILE);
    let header = all
        .iter()
        .find(|l| name(field(l, "seq")) == "header")
        .expect("a header");
    let hist = histories(field(header, "histories"));
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
        let mut m = model(field(l, "init"), tables, fail, false);
        for (i, st) in list(field(l, "steps")).iter().enumerate() {
            let ((o, log, rows), inner) = step(&mut m, &hist, st);
            let label = format!("{seq} step {i} {:?}", field(st, "op"));
            check(
                &mut ledger,
                &format!("{label} o"),
                &o,
                field(st, "o"),
                &mut total,
            );
            check(
                &mut ledger,
                &format!("{label} log"),
                &log,
                field(st, "log"),
                &mut total,
            );
            check(
                &mut ledger,
                &format!("{label} rows"),
                &rows,
                field(st, "rows"),
                &mut total,
            );
            if let Some(want) = st.get("inner") {
                let want = list(want);
                assert_eq!(inner.len(), want.len(), "{label}: inner steps");
                for (j, (o, log, rows)) in inner.iter().enumerate() {
                    let w = &want[j];
                    let l = format!("{label} inner {j}");
                    check(&mut ledger, &format!("{l} o"), o, field(w, "o"), &mut total);
                    check(
                        &mut ledger,
                        &format!("{l} log"),
                        log,
                        field(w, "log"),
                        &mut total,
                    );
                    check(
                        &mut ledger,
                        &format!("{l} rows"),
                        rows,
                        field(w, "rows"),
                        &mut total,
                    );
                }
            }
        }
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
    assert!(total > 0);
    assert_eq!(seqs, all.len() - 1, "every sequence checked");
    ledger.finish(total);
}
