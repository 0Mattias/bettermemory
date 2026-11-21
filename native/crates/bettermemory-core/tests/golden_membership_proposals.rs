//! Every sequence of `membership_proposals.jsonl` (not gated) against the
//! native `Membership` over the generator's recording store: the machine
//! rows and the proposals. The spelling is
//! `tests/contract/gen_membership_proposals.py`'s.

mod common;

use bettermemory_core::pyvalue::PyValue;
use common::membership_proposals::{after, model_of, step};
use common::membership_rows::{field, lines, list, name, same};
use common::parity::Ledger;

const FILE: &str = "membership_proposals.jsonl";

#[test]
fn membership_proposals_every_sequence() {
    let all = lines(FILE);
    let mut ledger = Ledger::new("membership_proposals_every_sequence", FILE);
    let mut total = 0usize;
    let mut check = |ledger: &mut Ledger, label: String, got: &PyValue, want: &PyValue| {
        total += 1;
        assert!(
            !ledger.differs_fatally(&label, same(got, want)),
            "{label}: got {got:?}, want {want:?}"
        );
    };
    for l in &all {
        let seq = name(field(l, "seq"));
        let mut m = model_of(l);
        for (i, s) in list(field(l, "steps")).iter().enumerate() {
            let (o, log, rows) = step(&mut m, s);
            let label = format!("{seq} step {i} {}", name(field(s, "op")));
            check(&mut ledger, format!("{label} o"), &o, field(s, "o"));
            check(&mut ledger, format!("{label} log"), &log, field(s, "log"));
            check(
                &mut ledger,
                format!("{label} rows"),
                &rows,
                field(s, "rows"),
            );
        }
        check(
            &mut ledger,
            format!("{seq} after"),
            &after(&m),
            field(l, "after"),
        );
    }
    let want: usize = all
        .iter()
        .map(|l| 1 + 3 * list(field(l, "steps")).len())
        .sum();
    assert!(total > 0 && !all.is_empty());
    assert_eq!(total, want);
    ledger.finish(total);
}
