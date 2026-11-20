//! Cases a plausible wrong `in_force` fails: the empty id falling through
//! `newest_head`'s `or`, the id breaking a stamp tie, the last stamp read
//! for a source, the cycle's end, an extender read as its head, a held
//! head, a link type walked but never followed, the order of reads.

#[path = "in_force_world/mod.rs"]
mod world;

use bettermemory_core::in_force::InForce;
use serde_json::json;
use world::{ids, row, run, s, Case};

const S: &str = "supersedes";
const E: &str = "extends";

type Rows<'a> = &'a [(&'a str, &'a str, &'a str)];

fn case(asked: &[&str], links: &[(&str, Rows)], held: &[&str]) -> Case {
    Case {
        links: links
            .iter()
            .map(|(t, rows)| (s(t), rows.iter().map(|(k, x, st)| row(k, x, st)).collect()))
            .collect(),
        admit: None,
        held: ids(held),
        ids: ids(asked),
    }
}

fn one(asked: &str, id: &str, extended_by: Option<&str>, held: bool) -> InForce {
    InForce {
        asked: s(asked),
        id: s(id),
        replaces: (asked != id).then(|| s(asked)),
        extended_by: extended_by.map(s),
        held,
    }
}

fn got(c: &Case) -> Vec<InForce> {
    run(c, false).0
}

#[test]
fn the_empty_id_falls_through_to_the_newest_held() {
    let c = case(&["a"], &[("a", &[(S, "", "1"), (S, "c", "2")])], &["c"]);
    assert_eq!(got(&c), vec![one("a", "c", None, true)]);
}

#[test]
fn a_stamp_tie_goes_to_the_greater_id() {
    let c = case(&["a"], &[("a", &[(S, "c", "1"), (S, "b", "1")])], &[]);
    assert_eq!(got(&c), vec![one("a", "c", None, false)]);
}

#[test]
fn a_source_carries_the_last_stamp_read() {
    let c = case(
        &["x", "a"],
        &[
            ("a", &[(S, "b", "5"), (S, "c", "4")]),
            ("x", &[(S, "b", "1")]),
        ],
        &[],
    );
    assert_eq!(
        got(&c),
        vec![one("x", "b", None, false), one("a", "b", None, false)]
    );
}

#[test]
fn a_cycle_ends_where_it_closes() {
    let c = case(
        &["a"],
        &[
            ("a", &[(S, "b", "2")]),
            ("b", &[(S, "c", "3")]),
            ("c", &[(S, "b", "4")]),
        ],
        &[],
    );
    assert_eq!(got(&c), vec![one("a", "c", None, false)]);
}

#[test]
fn an_extender_is_named_by_its_head_and_never_held() {
    let c = case(
        &["a"],
        &[
            ("a", &[(E, "b", "2"), (E, "d", "1")]),
            ("b", &[(S, "c", "3")]),
        ],
        &["c"],
    );
    assert_eq!(got(&c), vec![one("a", "a", Some("d"), false)]);
}

#[test]
fn an_extender_whose_head_is_the_record_is_not_named() {
    let c = case(
        &["a"],
        &[
            ("a", &[(S, "b", "2")]),
            ("b", &[(E, "c", "3")]),
            ("c", &[(S, "b", "4")]),
        ],
        &[],
    );
    assert_eq!(got(&c), vec![one("a", "b", None, false)]);
}

#[test]
fn a_held_head_holds_the_slot() {
    let c = case(&["a"], &[("a", &[(S, "b", "2")])], &["b"]);
    assert_eq!(got(&c), vec![one("a", "b", None, true)]);
}

#[test]
fn another_link_type_is_walked_and_never_followed() {
    let c = case(
        &["a"],
        &[
            ("a", &[("contradicts", "b", "9"), (S, "c", "1")]),
            ("b", &[(S, "d", "1")]),
        ],
        &[],
    );
    let (out, calls) = run(&c, true);
    assert_eq!(out, vec![one("a", "c", None, false)]);
    assert_eq!(calls[4], json!(["l", ["b", "c"]]));
}

#[test]
fn nothing_asked_reads_no_held_and_duplicates_reach_held() {
    let (out, calls) = run(&case(&[], &[], &[]), true);
    assert!(out.is_empty() && calls.is_empty());
    let (out, calls) = run(&case(&["b", "a", "b"], &[], &[]), true);
    assert_eq!(
        out,
        vec![one("b", "b", None, false), one("a", "a", None, false)]
    );
    assert_eq!(calls.last(), Some(&json!(["h", ["b", "a", "b"]])));
}
