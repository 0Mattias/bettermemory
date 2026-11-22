//! Cases a plausible wrong port of the heal half of `membership_shared`
//! fails: the later `checkout_retired` row winning, the fold's exclusion
//! of the checkout itself, stamps compared as sets, the order of
//! `_healed`'s reads and the KeyError `_folded` raises.

mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;

use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use common::membership_rows::{field, lines, list, name};

fn run(case: &str) -> (PyValue, PyValue) {
    let all = lines("membership_shared_heal.jsonl");
    let bases = field(&all[0], "base");
    let case = heal::expand(&json::loads(&cps(case)).expect("a case"), bases);
    heal::run(&case)
}

fn kinds(calls: &PyValue) -> Vec<String> {
    list(calls)
        .iter()
        .map(|c| {
            let c = list(c);
            if name(&c[0]) == "rows" {
                format!("rows:{}", name(&c[1]))
            } else {
                name(&c[0])
            }
        })
        .collect()
}

const RETIRED: &str = r#"{"$k": ["K2", {"retired": 1}]}"#;

#[test]
fn retired_into_takes_the_latest_row_even_when_it_names_none() {
    let (o, _) = run(&format!(
        r#"{{"f": "retired_into", "k": {RETIRED}, "log": [["checkout_retired", "{{\"into\": \"K1\"}}"], ["checkout_retired", "{{\"into\": null}}"]]}}"#
    ));
    assert_eq!(o, PyValue::None);
    let (o, _) = run(&format!(
        r#"{{"f": "retired_into", "k": {RETIRED}, "log": [["checkout_retired", "{{\"into\": \"K1\"}}"], ["checkout_retired", "[1]"], ["checkout_retired", "{{bad"]]}}"#
    ));
    assert_eq!(o, PyValue::str("K1"));
}

#[test]
fn retired_into_reads_nothing_for_a_live_checkout() {
    let (o, calls) = run(
        r#"{"f": "retired_into", "k": {"$k": ["K2", {}]}, "log": [["checkout_retired", "{\"into\": \"K1\"}"]]}"#,
    );
    assert_eq!(o, PyValue::None);
    assert!(list(&calls).is_empty());
}

#[test]
fn folded_follows_chains_and_leaves_the_checkout_out() {
    let (o, _) = run(
        r#"{"f": "folded", "checkout": "K2", "log": [["checkout_retired", "{\"checkout\": \"K2\", \"into\": \"K1\"}"], ["checkout_retired", "{\"checkout\": \"K3\", \"into\": \"K2\"}"], ["checkout_retired", "{\"checkout\": \"K1\", \"into\": \"K3\"}"]]}"#,
    );
    assert_eq!(
        o,
        PyValue::List(vec![PyValue::str("K1"), PyValue::str("K3")])
    );
}

#[test]
fn folded_raises_key_error_for_a_row_without_its_checkout() {
    let (o, _) = run(
        r#"{"f": "folded", "checkout": "K1", "log": [["checkout_retired", "{\"into\": \"K1\"}"]]}"#,
    );
    assert_eq!(name(&list(field(&o, "raised"))[0]), "KeyError");
    let (o, _) =
        run(r#"{"f": "folded", "checkout": "K1", "log": [["checkout_retired", "{\"into\": 0}"]]}"#);
    assert_eq!(o, PyValue::List(Vec::new()));
}

#[test]
fn restamped_compares_stamps_as_sets() {
    let k = r#"{"$k": ["K1", {"stamps_json": [["description", 7, 5000000123, 6000000007, 10], ["info/exclude", 8, 3000000001, 4000000001, 3], {"ticks": false}]}]}"#;
    let same = r#"[["info/exclude", 8, 3000000001, 4000000001, 3], ["description", 7, 5000000123, 6000000007, 10], ["info/exclude", 8, 3000000001, 4000000001, 3]]"#;
    let (_, calls) = run(&format!(
        r#"{{"f": "restamped", "k": {k}, "loc": {{"s": {same}}}, "how": "birth"}}"#
    ));
    assert!(list(&calls).is_empty(), "a reordered set is the same");
    let (_, calls) = run(&format!(
        r#"{{"f": "restamped", "k": {k}, "loc": {{"s": {same}, "x": true}}, "how": "birth"}}"#
    ));
    assert_eq!(kinds(&calls), ["put"], "ticks that differ are written");
    let (_, calls) = run(&format!(
        r#"{{"f": "restamped", "k": {k}, "loc": {{"s": {same}, "x": true}}, "how": "view"}}"#
    ));
    assert!(
        list(&calls).is_empty(),
        "only birth, token and remint restamp"
    );
}

const R: &str = "0123456789ABCYDG2G2Y15QCNN";

fn healed(extra_tables: &str, later_project: &str) -> (PyValue, PyValue) {
    run(&format!(
        r#"{{"f": "healed", "k": {{"$k": ["K1", {{}}]}}, "later": {{"$k": ["K2", {{"project": "{later_project}"}}]}}, "loc": {{"t": "{R}", "s": [["description", 7, 5000000123, 6000000007, 10]], "x": false}}, "log": [["checkout_registered", "{{\"checkout\": \"K2\", \"rule\": \"join:T\"}}"]], "tables": {{"checkouts": [{{"$k": ["K1", {{}}]}}, {{"$k": ["K2", {{}}]}}, {{"$k": ["K3", {{}}]}}]{extra_tables}}}}}"#
    ))
}

#[test]
fn healed_reads_homes_last_and_only_on_later() {
    let (o, calls) = healed("", "P2");
    assert_eq!(o, PyValue::str("heal"));
    assert_eq!(
        kinds(&calls),
        [
            "rows:checkouts",
            "rows:projects",
            "rows:proposals",
            "log",
            "rows:homes"
        ]
    );
    let homes = list(&calls).last().cloned().expect("a call");
    let want = json::loads(&cps(r#"{"$d": [["checkout", "K2"]]}"#)).expect("a where");
    assert_eq!(list(&homes)[2], want);
    let (o, _) = healed(r#", "homes": [{"$d": [["checkout", "K2"]]}]"#, "P2");
    assert_eq!(o, PyValue::str("ask"));
    let (o, _) = healed(r#", "homes": [{"$d": [["checkout", "K1"]]}]"#, "P2");
    assert_eq!(o, PyValue::str("heal"));
}

#[test]
fn healed_asks_nothing_where_later_is_not_in_a_carrier_project() {
    let (o, calls) = healed("", "P7");
    assert_eq!(o, PyValue::str(""));
    assert_eq!(kinds(&calls), ["rows:checkouts", "rows:projects"]);
}

/// `_chosen` lets what the log reader raises out. A `_NAMING` row whose
/// payload nests 10,000 deep makes `json.loads` raise RecursionError
/// under CPython 3.13 on macOS (the 9,999th container); the depth at which
/// the decoder stops follows the release and the system (3.14 on Linux
/// reads it), so this case is pinned here and not in the ungated golden
/// file.
#[test]
fn chosen_lets_the_log_readers_raise_out() {
    let deep = "[".repeat(10_000) + &"]".repeat(10_000);
    let (o, _) = run(&format!(
        r#"{{"f": "chosen", "k": {{"$k": ["K1", {{}}]}}, "later": {{"$k": ["K2", {{}}]}}, "loc": {{"t": "{R}", "s": [["description", 7, 5000000123, 6000000007, 10]], "x": false}}, "log": [["checkout_registered", "{deep}"]]}}"#
    ));
    assert_eq!(name(&list(field(&o, "raised"))[0]), "RecursionError");
}
