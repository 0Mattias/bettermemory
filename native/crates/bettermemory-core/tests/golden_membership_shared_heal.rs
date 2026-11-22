//! Every case of `membership_shared_heal.jsonl` (not gated) against the
//! heal half of `membership_shared`: `_carriers`, `_restamped`,
//! `_healed`, `_chosen`, `retired_into` and `_folded` over the native
//! `Membership` on the generator's recording store. Both the answer and
//! the store calls, in order, are compared.

mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;

use common::membership_rows::{field, lines, name, same};
use common::parity::Ledger;

const FILE: &str = "membership_shared_heal.jsonl";

#[test]
fn membership_shared_heal_every_case() {
    let all = lines(FILE);
    let header = &all[0];
    assert_eq!(name(field(header, "f")), "header");
    let bases = field(header, "base");
    let mut ledger = Ledger::new("membership_shared_heal_every_case", FILE);
    let mut cases = 0;
    let mut checks = 0;
    for (i, raw) in all.iter().enumerate().skip(1) {
        let case = heal::expand(raw, bases);
        let label = format!("case {i} {}", name(field(&case, "f")));
        let (o, calls) = heal::run(&case);
        for (what, got, want) in [
            ("o", &o, field(&case, "o")),
            ("calls", &calls, field(&case, "calls")),
        ] {
            checks += 1;
            assert!(
                !ledger.differs_fatally(format!("{label} {what}"), same(got, want)),
                "{label} {what}: got {got:?}, want {want:?}"
            );
        }
        cases += 1;
    }
    assert!(cases > 0);
    assert_eq!(cases, all.len() - 1);
    assert_eq!(checks, 2 * cases);
    ledger.finish(checks);
}
