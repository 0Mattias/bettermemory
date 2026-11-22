//! Hostile sizes for the store-reading half of `standing`: many rows,
//! long bodies, a memo past its bound. No panic, and time that grows in
//! proportion to the rows.

mod timing;
#[path = "standing_reads_world/mod.rs"]
mod world;

use std::sync::{Mutex, MutexGuard};

use bettermemory_core::standing;
use serde_json::{json, Value};

static TURN: Mutex<()> = Mutex::new(());

fn turn() -> MutexGuard<'static, ()> {
    TURN.lock().unwrap_or_else(|e| e.into_inner())
}

fn many(n: usize, body: &str) -> Value {
    let ids: Vec<String> = (0..n).map(|i| format!("01K{i:023}")).collect();
    let recs: Vec<Value> = ids
        .iter()
        .enumerate()
        .map(|(i, r)| {
            json!([
                r,
                null,
                format!("{i:08}"),
                ["global"],
                null,
                format!("m{i}"),
                0.5,
                body
            ])
        })
        .collect();
    let bodies: serde_json::Map<String, Value> =
        ids.iter().map(|r| (r.clone(), json!(body))).collect();
    json!({
        "recs": recs, "bodies": bodies, "tau": true, "links": {}, "returned": [],
        "unanswered": null, "candidates": null, "tombs": {}, "mems": [],
        "admit": null, "deny": [], "ops": []
    })
}

#[test]
fn the_memo_stays_at_its_bound() {
    let _t = turn();
    let c = many(standing::SHA_BOUND + 300, "b");
    let (mut w, mut v) = world::build(&c);
    standing::clear_shas();
    let got = standing::backlog(&mut w, &mut v).expect("backlog");
    assert_eq!(got.count(), 0);
    assert_eq!(standing::sha_count(), standing::SHA_BOUND);
    standing::clear_shas();
    assert_eq!(standing::sha_count(), 0);
}

#[test]
fn a_long_body_hashes() {
    let _t = turn();
    let body = "\u{e9}".repeat(1 << 20);
    let c = many(2, &body);
    let (mut w, mut v) = world::build(&c);
    standing::clear_shas();
    let ids = standing::standing_ids(&mut w, &mut v, None, false).expect("standing");
    assert_eq!(ids.len(), 2);
    standing::clear_shas();
}

#[test]
fn standing_ids_grow_with_the_rows() {
    let _t = turn();
    let small = many(2_000, "b");
    let large = many(20_000, "b");
    let run = |c: &Value| {
        let (mut w, mut v) = world::build(c);
        standing::clear_shas();
        let got = standing::standing_ids(&mut w, &mut v, None, false).expect("standing");
        assert!(!got.is_empty());
    };
    timing::grows_within(
        "standing_ids: 2,000 and 20,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
    standing::clear_shas();
}

#[test]
fn answers_grow_with_the_rows() {
    let _t = turn();
    let small = many(4_000, "b");
    let large = many(40_000, "b");
    // Only answers is timed: building the world is linear and, timed with
    // it, hides a quadratic answers at these sizes.
    let run = |c: &Value| {
        let (mut w, _) = world::build(c);
        let ids: Vec<String> = c["recs"]
            .as_array()
            .expect("recs")
            .iter()
            .map(|r| r[0].as_str().expect("id").to_owned())
            .collect();
        standing::clear_shas();
        timing::once(|| {
            let got = standing::answers(&mut w, &ids).expect("answers");
            assert_eq!(got.len(), ids.len());
        })
    };
    timing::grows_within(
        "answers: 4,000 and 40,000 rows",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
    standing::clear_shas();
}
