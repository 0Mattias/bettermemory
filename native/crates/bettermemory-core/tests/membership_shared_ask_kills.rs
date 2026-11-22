//! Cases a plausible wrong port of the asking half of `membership_shared`
//! fails: `RETIRE_AFTER` other than 180 days, a strict bound read as
//! inclusive, the oldest sighting read for the newest, a second
//! retirement that writes again, and `_asked_at`'s ranking by id alone.

#[path = "common/membership_shared_ask.rs"]
mod ask;
mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;

use bettermemory_core::json;
use bettermemory_core::membership::shared_ask::retire_after_us;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use common::membership_rows::{field, lines, list, name};

fn run(case: &str) -> (PyValue, PyValue) {
    let all = lines("membership_shared_ask.jsonl");
    let header = &all[0];
    let case = heal::expand(
        &json::loads(&cps(case)).expect("a case"),
        field(header, "base"),
    );
    ask::run(&case, field(header, "world"))
}

fn view(k: &str, machine: &str, path: &str, seen: &str) -> String {
    format!(
        r#"{{"$d": [["checkout", "{k}"], ["machine", "{machine}"], ["path", "{path}"], ["first_seen", "t"], ["last_seen", "{seen}"], ["seen_order", 1]]}}"#
    )
}

fn unseen_there(seen: &[&str]) -> PyValue {
    let views: Vec<String> = seen
        .iter()
        .enumerate()
        .map(|(i, s)| view("K2", "M2", &format!("/p{i}"), s))
        .collect();
    let case = format!(
        r#"{{"f": "unseen", "k": "K2", "tables": {{"checkouts": [{{"$k": ["K1", {{"id": "K2", "machine": "M2"}}]}}], "checkout_views": [{}]}}}}"#,
        views.join(", ")
    );
    run(&case).0
}

#[test]
fn retire_after_is_180_days() {
    assert_eq!(retire_after_us(), 180 * 86_400 * 1_000_000);
}

#[test]
fn a_sighting_exactly_retire_after_ago_is_still_seen() {
    assert_eq!(
        unseen_there(&["2025-09-05T05:06:07Z"]),
        PyValue::Bool(false)
    );
    assert_eq!(
        unseen_there(&["2025-09-05T05:06:06.999999Z"]),
        PyValue::Bool(true)
    );
}

#[test]
fn the_newest_sighting_decides() {
    let old = "2025-01-01T00:00:00Z";
    let recent = "2026-01-01T00:00:00Z";
    assert_eq!(unseen_there(&[old, recent]), PyValue::Bool(false));
    assert_eq!(unseen_there(&[recent, old]), PyValue::Bool(false));
    assert_eq!(unseen_there(&["junk", old]), PyValue::Bool(true));
    assert_eq!(unseen_there(&["junk"]), PyValue::Bool(false));
}

#[test]
fn a_second_retirement_writes_nothing() {
    let case = r#"{"f": "retire_into", "idle": "K5", "kept": "K1", "by": "heal", "log": [["checkout_retired", "{\"into\": \"K9\"}"]], "x": {}}"#;
    let (o, calls) = run(case);
    assert_eq!(o, PyValue::None);
    let calls = list(&calls);
    assert_eq!(calls.len(), 1);
    assert_eq!(name(&list(&calls[0])[0]), "log");
}

#[test]
fn this_machines_checkout_is_asked_before_a_lower_id_elsewhere() {
    let q = r#"{"$d": [["id", "Q001"], ["checkout", "K2"], ["kind", "import"], ["candidates_json", ["P3"]], ["evidence_json", {"$d": [["imported_project", "P3"]]}], ["reason", "r"], ["line", "l"], ["state", "open"], ["answer", null], ["answered_by", null], ["signature_json", null], ["opened", "t"]]}"#;
    let case = format!(
        r#"{{"f": "asked_at", "idle": "K2", "kept": "K1", "pr": "Q001", "live": ["/w/z"], "x": {{"proposals": [{q}], "checkouts": [{{"$k": ["K1", {{}}]}}, {{"$k": ["K1", {{"id": "K2", "project": "P2"}}]}}, {{"$k": ["K1", {{"id": "K7", "project": "P3"}}]}}, {{"$k": ["K1", {{"id": "K4", "project": "P3", "machine": "M2"}}]}}], "checkout_views": [{}, {}]}}}}"#,
        view("K7", "M1", "/w/z", "2026-01-01T00:00:00Z"),
        view("K4", "M2", "/x/t", "2026-01-01T00:00:00Z")
    );
    assert_eq!(run(&case).0, PyValue::str("K7"));
}
