//! Every case of `membership_shared_ask.jsonl` (not gated) against the
//! asking half of `membership_shared`: `_ask_which`, `_retire_into`,
//! `_asked_at`, `_asked_here`, `_unseen` and `_asked_plan` over the
//! native `Membership` on the generator's recording store. Both the
//! answer and the store calls, in order, are compared.

#[path = "common/membership_shared_ask.rs"]
mod ask;
mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;

use common::membership_rows::{field, lines, name, same};
use common::parity::Ledger;

const FILE: &str = "membership_shared_ask.jsonl";

#[test]
fn membership_shared_ask_every_case() {
    let all = lines(FILE);
    let header = &all[0];
    assert_eq!(name(field(header, "f")), "header");
    let bases = field(header, "base");
    let world = field(header, "world");
    let mut ledger = Ledger::new("membership_shared_ask_every_case", FILE);
    let mut cases = 0;
    let mut checks = 0;
    for (i, raw) in all.iter().enumerate().skip(1) {
        let case = heal::expand(raw, bases);
        let label = format!("case {i} {}", name(field(&case, "f")));
        let (o, calls) = ask::run(&case, world);
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
