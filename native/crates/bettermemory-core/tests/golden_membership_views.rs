//! Every case of `membership_views.jsonl` (not gated) against the native
//! `Membership`, unit membership-3: each step's answer, store calls,
//! liveness reads and history calls. The spelling is
//! `tests/contract/gen_membership_views.py`'s.

mod common;

use common::membership_core::tables_of;
use common::membership_rows::{field, lines, list, name, same};
use common::membership_views::{step, Run};
use common::parity::Ledger;

#[test]
fn membership_views_every_step() {
    let file = "membership_views.jsonl";
    let mut ledger = Ledger::new("membership_views_every_step", file);
    let mut total = 0;
    let mut steps_in_file = 0;
    for l in lines(file) {
        let case = name(field(&l, "case"));
        let mut run = Run::new(tables_of(field(&l, "tables")), field(&l, "live"));
        for (i, st) in list(field(&l, "steps")).iter().enumerate() {
            steps_in_file += 1;
            let (o, log, lv, hc) = step(&mut run, st);
            let label = format!("{case} step {i} {:?}", field(st, "op"));
            for (what, got, want) in [
                ("o", &o, field(st, "o")),
                ("log", &log, field(st, "log")),
                ("lv", &lv, field(st, "lv")),
                ("hc", &hc, field(st, "hc")),
            ] {
                total += 1;
                assert!(
                    !ledger.differs_fatally(format!("{label} {what}"), same(got, want)),
                    "{label} {what}: got {got:?}, want {want:?}"
                );
            }
        }
    }
    assert!(steps_in_file > 0);
    assert_eq!(total, steps_in_file * 4);
    ledger.finish(total);
}
