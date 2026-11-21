//! Every case of `daemon_client.jsonl` (not gated) against
//! `daemon_client`: the constants in the header and `hook_budget_seconds`
//! with the harness lookup per event.

mod common;

use bettermemory_core::daemon_client as dc;
use bettermemory_core::float_repr::repr;
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};

#[test]
fn daemon_client_every_case() {
    let file = "daemon_client.jsonl";
    let lines = common::jsonl(file);
    let (h, rows) = (&lines[0], &lines[1..]);
    assert_eq!(h["cases"].as_u64().unwrap() as usize, rows.len());
    assert_eq!(h["store"], dc::STORE_FILENAME);
    assert_eq!(h["start"], repr(dc::START_TIMEOUT_SECONDS));
    assert_eq!(h["hook"], repr(dc::HOOK_TIMEOUT_SECONDS));
    assert_eq!(h["margin"], repr(dc::HOOK_MARGIN_SECONDS));
    let table: Vec<Value> = dc::HOOK_HARNESS_TIMEOUT_SECONDS
        .iter()
        .map(|(k, v)| json!([k, repr(*v)]))
        .collect();
    assert_eq!(Value::Array(table), h["harness"]);
    let mut ledger = Ledger::new("daemon_client_every_case", file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, r), || {
            let event = common::text_field(r, "e");
            // An event the port cannot spell as Rust text (a lone
            // surrogate) is no key of the table: the fallback.
            let text = unicode::to_string(&event).unwrap_or_default();
            let budget = if unicode::to_string(&event).is_some() {
                dc::hook_budget_seconds(&text)
            } else {
                dc::hook_budget_seconds("\u{0}not an event")
            };
            assert_eq!(repr(budget), r["budget"].as_str().unwrap());
            let harness = unicode::to_string(&event)
                .and_then(|t| dc::harness_timeout(&t))
                .map(repr);
            assert_eq!(harness.as_deref(), r["harness"].as_str());
        });
    }
    ledger.finish(rows.len());
}
