//! Cases a plausible wrong port of the store-reading half of `standing`
//! fails. The body sha memo is process-wide, so the tests here take turns.

#[path = "standing_reads_world/mod.rs"]
mod world;

use std::sync::{Mutex, MutexGuard};

use bettermemory_core::standing::{self, ReadError};
use bettermemory_core::unicode;
use serde_json::{json, Value};

static TURN: Mutex<()> = Mutex::new(());

fn turn() -> MutexGuard<'static, ()> {
    TURN.lock().unwrap_or_else(|e| e.into_inner())
}

fn id(c: char) -> String {
    format!("01K{}{c}", "0".repeat(22))
}

fn case(recs: Value, extra: Value) -> Value {
    let mut c = json!({
        "recs": recs, "bodies": {}, "tau": true, "links": {}, "returned": [],
        "unanswered": null, "candidates": null, "tombs": {}, "mems": [],
        "admit": null, "deny": [], "ops": []
    });
    for (k, v) in extra.as_object().expect("extra") {
        c[k] = v.clone();
    }
    c
}

fn rec(rid: &str, p: Value, mac: Value, upd: &str) -> Value {
    json!([rid, null, upd, ["global"], null, mac, p, "b"])
}

fn bodies(ids: &[&String]) -> Value {
    Value::Object(ids.iter().map(|i| ((*i).clone(), json!("b"))).collect())
}

fn reads_of(out: &Value, op: usize) -> usize {
    out[op][1]
        .as_array()
        .expect("calls")
        .iter()
        .filter(|c| c[0] == "b")
        .count()
}

#[test]
fn a_row_without_mac_is_hashed_every_time() {
    let _t = turn();
    let a = id('A');
    let c = case(
        json!([rec(&a, json!(0.5), Value::Null, "1")]),
        json!({"bodies": bodies(&[&a]), "ops": [["answers", [a]], ["answers", [a]], ["count"]]}),
    );
    let out = world::answer(&c);
    assert_eq!((reads_of(&out, 0), reads_of(&out, 1)), (1, 1));
    assert_eq!(out[2][0], json!(0));
}

#[test]
fn a_hit_moves_to_the_end_of_the_memo() {
    let _t = turn();
    let (a, b, cc) = (id('A'), id('B'), id('C'));
    let c = case(
        json!([
            rec(&a, json!(0.5), json!("m"), "3"),
            rec(&b, json!(0.5), json!("m"), "2"),
            rec(&cc, json!(0.5), json!("m"), "1")
        ]),
        json!({"bodies": bodies(&[&a, &b, &cc]), "bound": 2, "ops": [
            ["answers", [a]], ["answers", [b]], ["answers", [a]], ["answers", [cc]],
            ["answers", [a]], ["answers", [b]]]}),
    );
    let out = world::answer(&c);
    // First in, first out would pop A when C came in.
    assert_eq!(reads_of(&out, 4), 0, "A was moved to the end by its hit");
    assert_eq!(reads_of(&out, 5), 1, "B was the oldest");
}

#[test]
fn the_threshold_is_inclusive_and_read_of_the_current_answer() {
    let _t = turn();
    let a = id('A');
    for (p, want) in [
        (0.3, true),
        (0.29999999999999993, false),
        (0.30000000000000004, true),
    ] {
        let c = case(
            json!([rec(&a, json!(p), json!("m"), "1")]),
            json!({"bodies": bodies(&[&a]), "ops": [["standing", null, false]]}),
        );
        let got = world::answer(&c)[0][0].clone();
        assert_eq!(got == json!([a]), want, "probability {p}");
    }
    // An answer for another body is no answer.
    let c = case(
        json!([[a, null, "1", ["global"], null, "m", 1.0, "other"]]),
        json!({"bodies": bodies(&[&a]), "ops": [["standing", null, false], ["backlog"]]}),
    );
    let out = world::answer(&c);
    assert_eq!(out[0][0], json!([]));
    assert_eq!(out[1][0], json!([[a], 1]));
}

#[test]
fn the_pre_filter_reads_no_admission() {
    let _t = turn();
    let a = id('A');
    let c = case(
        json!([rec(&a, Value::Null, json!("m"), "1")]),
        json!({"bodies": bodies(&[&a]), "ops": [["standing", null, false]]}),
    );
    let out = world::answer(&c);
    assert_eq!(out[0][1], json!([["c", 0.3], ["r", null]]));
}

#[test]
fn held_only_when_asked_and_history_before_held() {
    let _t = turn();
    let (a, b) = (id('A'), id('B'));
    let c = case(
        json!([rec(&a, json!(1.0), json!("m"), "1")]),
        json!({"bodies": bodies(&[&a]), "returned": [a], "ops": [
            ["standing", null, false], ["standing", null, true], ["guarded", [a]],
            ["makes", a, null, 1.0, false]]}),
    );
    let out = world::answer(&c);
    assert_eq!(out[0][0], json!([]));
    assert_eq!(out[1][0], json!([a]));
    assert_eq!(out[2][0], json!([a]));
    assert_eq!(out[3][0], json!(true));
    let c = case(
        json!([rec(&a, json!(1.0), json!("m"), "1")]),
        json!({"bodies": bodies(&[&a]), "returned": [a], "links": {&a: [["supersedes", b, "2"]]},
               "ops": [["makes", a, null, 1.0, true]]}),
    );
    assert_eq!(world::answer(&c)[0][0], json!(false));
}

#[test]
fn the_review_off_holds_nothing() {
    let _t = turn();
    let a = id('A');
    let c = case(
        json!([rec(&a, json!(1.0), json!("m"), "1")]),
        json!({"bodies": bodies(&[&a]), "returned": [a], "tau": false,
               "tombs": {id('D'): [["supersedes", a]]},
               "ops": [["held", null], ["removal", [id('D')]], ["standing", null, false]]}),
    );
    let out = world::answer(&c);
    assert_eq!(out[0], json!([[], []]));
    assert_eq!(out[1][0], json!([]));
    assert_eq!(out[2][0], json!([a]));
}

#[test]
fn refusal_rows_keys_dates_and_dedup() {
    let _t = turn();
    let (a, b) = (id('A'), id('B'));
    let c = case(
        json!([]),
        json!({"admit": [a], "mems": [
            [a, ["global"], "2026-01-01T23:30:00-05:00", " x "],
            [b, ["global"], "2026-01-02T00:30:00+02:00", "y"]],
            "ops": [["refusal", [b, a, b]]]}),
    );
    let out = world::answer(&c);
    assert_eq!(
        out[0][0],
        json!([
            [
                ["id", b],
                ["scopes", ["global"]],
                ["updated", "2026-01-01"],
                ["text", null],
                ["admitted", false]
            ],
            [
                ["id", a],
                ["scopes", ["global"]],
                ["updated", "2026-01-02"],
                ["text", "x"]
            ]
        ])
    );
    assert_eq!(out[0][1][0], json!(["p", [b, a, b]]));
    assert_eq!(out[0][1][1], json!(["g", [b, a]]));
}

#[test]
fn held_by_removal_keeps_first_occurrences_of_supersedes_only() {
    let _t = turn();
    let (a, b, cc, d) = (id('A'), id('B'), id('C'), id('D'));
    let c = case(
        json!([
            rec(&a, json!(1.0), json!("m"), "1"),
            rec(&b, json!(1.0), json!("m"), "2"),
            rec(&cc, json!(1.0), json!("m"), "3")
        ]),
        json!({"bodies": bodies(&[&a, &b, &cc]), "returned": [a, b, cc],
               "tombs": {&d: [["supersedes", cc], ["extends", b], ["supersedes", a], ["supersedes", cc]]},
               "ops": [["removal", [id('E'), d]]]}),
    );
    assert_eq!(world::answer(&c)[0][0], json!([cc, a]));
}

#[test]
fn a_repeated_row_keeps_its_first_place_and_last_answer() {
    let _t = turn();
    let (a, b) = (id('A'), id('B'));
    let c = case(
        json!([
            rec(&a, json!(0.1), json!("m1"), "3"),
            rec(&b, json!(0.2), json!("m"), "2"),
            rec(&a, json!(0.9), json!("m2"), "1")
        ]),
        json!({"bodies": bodies(&[&a, &b]), "ops": [["answers", [a, b]]]}),
    );
    assert_eq!(world::answer(&c)[0][0], json!([[a, 0.9], [b, 0.2]]));
}

#[test]
fn a_surrogate_body_and_a_naive_stamp_raise() {
    let _t = turn();
    let a = id('A');
    let c = case(json!([rec(&a, json!(0.5), json!("m"), "1")]), json!({}));
    let (mut w, mut v) = world::build(&c);
    w.bodies.insert(a.clone(), vec![0x61, 0xD800]);
    standing::clear_shas();
    assert_eq!(
        standing::answers(&mut w, std::slice::from_ref(&a)),
        Err(ReadError::Unencodable)
    );
    assert_eq!(
        standing::body_sha256(&unicode::cps("")).expect("sha").len(),
        64
    );
    let c = case(
        json!([]),
        json!({"mems": [[a, ["global"], "2026-01-01T00:00:00", "x"]]}),
    );
    let (mut w, _) = world::build(&c);
    assert_eq!(
        standing::refusal_rows(&mut w, std::slice::from_ref(&a), &mut v),
        Err(ReadError::NaiveUpdated)
    );
}

#[test]
fn makes_standing_reads_the_category_of_the_first_row() {
    let _t = turn();
    let a = id('A');
    // Two rows for one id, newest first: the first is ambient, the last
    // has no category. The reference reads `current[0]["category"]`.
    let c = case(
        json!([
            [a, "ambient", "2", ["global"], null, "m", null, "b"],
            [a, null, "1", ["global"], null, "m", null, "b"]
        ]),
        json!({"bodies": bodies(&[&a]), "ops": [["makes", a, "ambient", null, false]]}),
    );
    assert_eq!(world::answer(&c)[0][0], json!(false));
}
