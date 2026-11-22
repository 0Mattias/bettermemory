//! Every case of `location_hook.jsonl` (class i): `resolve_hook` with the
//! generator's recording doubles, round by round, against
//! `location_hook`.

mod common;
mod location_hook_support;

use common::jsonl;
use common::parity::Ledger;
use location_hook_support::run_case;

#[test]
fn location_hook_every_case() {
    let file = "location_hook.jsonl";
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty());
    let mut ledger = Ledger::new("location_hook_every_case", file);
    for c in cases {
        let name = c["k"].as_str().unwrap().to_string();
        ledger.case(name.clone(), || assert_eq!(run_case(c), c["r"], "{name}"));
    }
    ledger.finish(cases.len());
}
