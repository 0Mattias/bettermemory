//! Cases a plausible wrong `commits_since_anchor` or walk memo fails:
//! a memo keyed without the files' signature, a walk stored after the
//! files moved or the directories did, a timed-out walk served at 60
//! seconds or under a clock gone back, a replay that does not count its
//! failure or timeout again, `_held_anchor` reading exit 128 as held or a
//! killed check as absent, the commits listed alone where the names were
//! read, an LRU that evicts the newest entry or moves a stored key, a
//! memo that takes two keys differing only in their signature for one, a
//! root no file name holds raising before the pass or where no git runs,
//! and the process's memo not registered with `caches` or cleared by
//! halves.

mod common;

use std::sync::Arc;

use bettermemory_core::caches;
use bettermemory_core::origin_git;
use bettermemory_core::origin_history::{HistoryUnread, ReachableWalk, WalkedValue};
use bettermemory_core::origin_reach::walk::CLEAR_KEY;
use bettermemory_core::origin_reach::{clear_walk_memo, walk_memo, Error, Kept, WalkKey, WalkMemo};
use bettermemory_core::unicode::cps;
use common::origin_reach_walk::{files_of, merged, ok, run_row, text, Run, Shared, A, B, C};
use serde_json::{json, Value};

fn defaults() -> Value {
    common::jsonl("origin_reach_walk.jsonl")[0]["defaults"].clone()
}

fn steps(steps: Value) -> Vec<Value> {
    run_row(&defaults(), steps.as_array().unwrap())
}

fn calls(out: &Value) -> usize {
    out["calls"].as_array().unwrap().len()
}

fn names_timed_out() -> Value {
    json!(["timeout", ok(A, C, false)])
}

#[test]
fn the_key_holds_the_signature() {
    let out = steps(json!([
        {"script": [ok(A, C, true)]},
        {"sig": "s2", "script": [ok(A, C, true)]},
    ]));
    assert_eq!(calls(&out[1]), 1, "another signature is another key");
    assert_eq!(out[1]["memos"][0][0], 2);
}

#[test]
fn a_walk_is_not_stored_when_the_files_moved() {
    for moved in [
        json!({"after": "s2"}),
        json!({"after": null}),
        json!({"held": false}),
    ] {
        let first = merged(&moved, &json!({"script": [ok(A, C, true)]}));
        let out = steps(json!([first, {"script": [ok(A, C, true)]}]));
        assert_eq!(out[0]["memos"][0][0], 0, "{moved}");
        assert_eq!(calls(&out[1]), 1, "{moved}: walked again");
    }
}

#[test]
fn a_timed_out_walk_is_kept_under_sixty_seconds_only() {
    let again = json!(["timeout", ok(A, C, false)]);
    for (now, kept) in [
        (159.999, true),
        (160.0, false),
        (99.999, false),
        (100.0, true),
    ] {
        let out = steps(json!([
            {"script": names_timed_out()},
            {"now": now, "script": if kept { json!([]) } else { again.clone() }},
        ]));
        assert_eq!(calls(&out[1]) == 0, kept, "now {now}");
        // Served or walked again, a timeout is counted either way.
        assert_eq!(out[1]["counts"][2], 2, "now {now}");
    }
}

#[test]
fn a_timed_out_unread_walk_is_dropped_at_sixty_seconds() {
    let out = steps(json!([
        {"script": ["timeout", "timeout"]},
        {"now": 160.0, "script": ["timeout", "timeout"]},
    ]));
    assert_eq!(calls(&out[1]), 2);
    assert_eq!(out[1]["unwalked"][0], 2);
}

#[test]
fn every_replay_counts_again() {
    // A walk whose names failed: a failure again.
    let failed = json!([128, "", "fatal: bad revision\n"]);
    let out = steps(json!([
        {"walked": true, "script": [failed.clone(), ok(A, C, false)]},
        {"walked": true},
    ]));
    assert_eq!(calls(&out[1]), 0);
    assert_eq!(out[1]["counts"][1], 2);
    // Its names timed out: a timeout again, from the pass and the memo.
    for sig in [Value::Null, json!("s1")] {
        let out = steps(json!([
            {"sig": sig, "walked": true, "script": names_timed_out()},
            {"sig": sig, "walked": true},
        ]));
        assert_eq!(calls(&out[1]), 0);
        assert_eq!(
            out[1]["counts"].as_array().unwrap()[1..3],
            [json!(2), json!(2)]
        );
    }
    // Given up on: counted, and given up on again.
    let out = steps(json!([
        {"sig": null, "walked": true, "script": [failed.clone(), failed.clone(), [0, "", ""]]},
        {"sig": null, "walked": true},
    ]));
    assert_eq!(out[1]["counts"][1], 3);
    assert_eq!(out[1]["unwalked"], json!([2, "error"]));
    // A None after a failure: a failure again; after none, nothing.
    let out = steps(json!([
        {"walked": true, "script": [failed.clone(), failed, [128, "", ""]]},
        {"walked": true},
        {"anchor": B, "walked": true, "script": [[0, format!("\x01>{C}\n\x01-{A}\n"), ""]]},
        {"anchor": B, "walked": true},
    ]));
    assert_eq!(out[1]["counts"][1], 3);
    assert_eq!(out[3]["counts"][1], 3);
    assert_eq!(calls(&out[3]), 0);
}

#[test]
fn the_check_reads_exit_128_as_absent_and_a_signal_as_held() {
    let failed = json!([128, "", "fatal: bad revision\n"]);
    for (code, unwalked) in [(128, 0), (1, 0), (0, 1), (-9, 1), (-15, 1)] {
        let out = steps(json!([
            {"script": [failed.clone(), failed.clone(), [code, "", ""]]},
        ]));
        assert_eq!(out[0]["result"], Value::Null);
        assert_eq!(out[0]["unwalked"][0], unwalked, "exit {code}");
    }
}

#[test]
fn names_read_are_not_listed_alone() {
    let out = steps(json!([{"script": [ok(A, C, true)]}]));
    assert_eq!(calls(&out[0]), 1);
    assert_eq!(out[0]["result"][2], true, "listed");
    assert_eq!(out[0]["result"][1], json!([["f.txt", [C]]]));
}

fn key(n: u32) -> WalkKey {
    WalkKey {
        root: cps("top"),
        anchor: cps(&format!("{n:040x}")),
        head: cps(C),
        files: files_of("s1"),
    }
}

fn walk(n: u32) -> Arc<ReachableWalk> {
    Arc::new(ReachableWalk {
        anchor: cps(&format!("{n:040x}")),
        head: cps(C),
        commits: vec![cps(C)],
        touched: Default::default(),
        listed: true,
        timed_out: false,
    })
}

fn anchors(keys: &[WalkKey]) -> Vec<u32> {
    keys.iter()
        .map(|k| u32::from_str_radix(&text(&k.anchor), 16).unwrap())
        .collect()
}

#[test]
fn the_lru_evicts_the_least_recently_used() {
    let memo = WalkMemo::with_cap(3);
    for n in 0..3 {
        memo.store(&key(n), walk(n));
    }
    assert!(memo.recall(&key(0)).is_some());
    memo.store(&key(3), walk(3));
    assert_eq!(anchors(&memo.memo_keys()), [2, 0, 3]);
    // A key stored again keeps its place and takes the new walk.
    memo.store(&key(2), walk(9));
    assert_eq!(anchors(&memo.memo_keys()), [2, 0, 3]);
    assert_eq!(
        memo.recall(&key(2)).unwrap().anchor,
        cps(&format!("{:040x}", 9))
    );
    assert!(memo.recall(&key(1)).is_none());
}

#[test]
fn the_timed_out_table_moves_and_evicts() {
    let memo = WalkMemo::with_cap(2);
    let unread = Kept::Unread(HistoryUnread::Timeout);
    memo.keep_timed_out(&key(0), unread.clone(), 1.0);
    memo.keep_timed_out(&key(1), unread.clone(), 2.0);
    assert!(memo
        .recall_timed_out(&key(0), &mut || Ok(3.0))
        .unwrap()
        .is_some());
    memo.keep_timed_out(&key(2), unread.clone(), 4.0);
    let held: Vec<_> = memo.timed_out_entries().into_iter().map(|e| e.0).collect();
    assert_eq!(anchors(&held), [0, 2]);
    // Kept again: moved to the end with its new stamp.
    memo.keep_timed_out(&key(0), unread, 5.0);
    let held = memo.timed_out_entries();
    assert_eq!(
        anchors(&held.iter().map(|e| e.0.clone()).collect::<Vec<_>>()),
        [2, 0]
    );
    assert_eq!(held[1].1, 5.0);
    // The clock is read only where an entry is held; NaN drops it.
    let mut read = 0;
    assert!(memo
        .recall_timed_out(&key(7), &mut || {
            read += 1;
            Ok(0.0)
        })
        .unwrap()
        .is_none());
    assert_eq!(read, 0);
    assert!(memo
        .recall_timed_out(&key(2), &mut || Ok(f64::NAN))
        .unwrap()
        .is_none());
    assert_eq!(memo.timed_out_entries().len(), 1);
}

#[test]
fn the_memo_is_read_before_the_pass() {
    let out = steps(json!([
        {"walked": true, "script": [ok(A, C, true)]},
        {"walked": true},
    ]));
    assert_eq!(out[1]["reads"], json!(["sig+ top"]));
    assert_eq!(out[1]["counts"][1], 0, "a memo hit counts nothing");
}

#[test]
fn the_memo_tells_keys_apart_by_their_signature() {
    let memo = WalkMemo::new();
    let other = WalkKey {
        files: files_of("s2"),
        ..key(0)
    };
    memo.store(&key(0), walk(0));
    memo.store(&other, walk(9));
    assert_eq!(memo.memo_keys(), [key(0), other.clone()]);
    assert_eq!(memo.recall(&key(0)).unwrap().anchor, walk(0).anchor);
    assert_eq!(memo.recall(&other).unwrap().anchor, walk(9).anchor);
    let unread = Kept::Unread(HistoryUnread::Timeout);
    memo.keep_timed_out(&other, unread.clone(), 1.0);
    assert!(memo
        .recall_timed_out(&key(0), &mut || Ok(1.0))
        .unwrap()
        .is_none());
    assert_eq!(
        memo.recall_timed_out(&other, &mut || Ok(1.0)).unwrap(),
        Some(unread)
    );
}

#[test]
fn a_root_no_file_name_holds_is_unkeyed_and_raises_only_at_git() {
    let lone = json!([116, 111, 112, 0xD800]);
    // The anchor at the head: the empty walk, with no signature read, set
    // in the pass and served from it.
    let out = steps(json!([
        {"anchor": C, "toplevel_cps": lone, "walked": true},
        {"anchor": C, "toplevel_cps": lone, "walked": true},
    ]));
    for step in &out {
        assert_eq!(step["result"], json!([[], [], true, false]));
        assert_eq!(step["reads"], json!([]));
        assert_eq!(step["calls"], json!([]));
        assert_eq!(step["memos"], json!([[0], [0]]));
        assert_eq!(step["counts"], json!([0, 0, 0, 0, 0]));
    }
    // Another anchor raises where git would run, after the pass.
    let step = merged(&defaults(), &json!({"toplevel_cps": lone, "walked": true}));
    let mut run = Run::new(Shared::default());
    let encode = Err(Error::Git(origin_git::Error::UnicodeEncode));
    assert_eq!(run.since(&step).0, encode);
    let within = (common::cps_of(&lone), cps(A), cps(C));
    run.walked.insert(within, WalkedValue::Failed(true));
    let (answer, counts) = run.since(&step);
    assert_eq!(answer, Ok(None), "the pass is read first");
    assert_eq!(counts[1], 1, "its failure counted again");
}

#[test]
fn the_process_memo_is_registered_for_clearing() {
    let memo = walk_memo();
    assert!(caches::keys().contains(&CLEAR_KEY));
    let fill = || {
        memo.store(&key(0), walk(0));
        memo.keep_timed_out(&key(1), Kept::Unread(HistoryUnread::Timeout), 1.0);
        assert_eq!(memo.memo_keys().len(), 1);
        assert_eq!(memo.timed_out_entries().len(), 1);
    };
    fill();
    caches::clear_all();
    assert!(memo.memo_keys().is_empty());
    assert!(memo.timed_out_entries().is_empty());
    fill();
    clear_walk_memo();
    assert!(memo.memo_keys().is_empty());
    assert!(memo.timed_out_entries().is_empty());
}
