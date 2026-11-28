//! `origin_reach_walk.jsonl`: `commits_since_anchor`, its walk memo and
//! `_held_anchor` over call sequences with scripted git replies, scripted
//! reads and a scripted clock: each step's answer, the five tallies, the
//! walks given up on, every git call, every in-process read in order and
//! both memos; the memos at their caps; and a seeded sweep of call
//! sequences by count and digest.

mod common;

use bettermemory_core::origin_reach::walk_args;
use bettermemory_core::unicode::cps;
use common::origin_reach_walk::{
    given_up, merged, run_row, text, walk_answer, Mix, Run, Shared, A, B, C, D,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "origin_reach_walk.jsonl";

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

#[test]
fn walk_argv_is_the_reference_s() {
    let rows = common::jsonl(FILE);
    for (names, key) in [(true, "names"), (false, "listing")] {
        let argv: Vec<String> = walk_args(&cps(A), &cps(C), names)
            .iter()
            .map(|a| match text(a) {
                a if a.starts_with("-O") => "-O<devnull>".to_owned(),
                a => a,
            })
            .collect();
        assert_eq!(json!(argv), rows[0]["argv"][key]);
    }
}

#[test]
fn every_sequence() {
    let rows = common::jsonl(FILE);
    let defaults = &rows[0]["defaults"];
    assert_eq!(rows[0]["rows"].as_u64().unwrap() as usize, rows.len() - 1);
    let sequences: Vec<&Value> = rows[1..].iter().filter(|r| r["label"] != "sweep").collect();
    let mut ledger = Ledger::new("every_sequence", FILE);
    for row in &sequences {
        let label = row["label"].as_str().unwrap();
        ledger.case(label, || {
            let steps = row["steps"].as_array().unwrap();
            let out = run_row(defaults, steps);
            assert_eq!(out.len(), row["out"].as_array().unwrap().len(), "{label}");
            for (i, (got, want)) in out.iter().zip(row["out"].as_array().unwrap()).enumerate() {
                assert_eq!(got, want, "{label} step {i}");
            }
        });
    }
    ledger.finish(sequences.len());
    assert!(sequences.len() >= 50);
}

/// `_sweep`: the same draws in the same order, and the digest over each
/// sequence's answers, tallies, walks given up on and memo lengths.
fn sweep(seed: u64, sequences: u64, shifts: &[f64], defaults: &Value) -> (u64, String) {
    let mut digest = Sha256::new();
    let mut steps = 0;
    for i in 0..sequences {
        let world = Shared::default();
        world.borrow_mut().mix = Some(Mix::new(seed + i));
        let mut run = Run::new(world.clone());
        let draw = |n: u64| world.borrow_mut().mix.as_mut().unwrap().below(n);
        let mut now = 1000.0;
        let mut seen = Vec::new();
        for _ in 0..1 + draw(6) {
            let anchor = [A, B, C][draw(3) as usize];
            let head = [C, D][draw(2) as usize];
            let passed = draw(2) == 0;
            now += shifts[draw(shifts.len() as u64) as usize];
            let step = merged(
                defaults,
                &json!({"anchor": anchor, "head": head, "walked": passed, "now": now}),
            );
            let (answer, totals) = run.since(&step);
            let answer = answer.unwrap();
            let mut entry = vec![walk_answer(answer.as_ref()), totals];
            entry.extend(given_up(&run.unwalked).as_array().unwrap().iter().cloned());
            seen.push(json!(entry));
            steps += 1;
        }
        let memo = run.memo.memo_keys().len();
        let kept = run.memo.timed_out_entries().len();
        seen.push(json!([memo, kept]));
        let line = serde_json::to_string(&json!(seen)).unwrap();
        digest.update(line.as_bytes());
        digest.update(b"\n");
    }
    (steps, hex(&digest.finalize()))
}

#[test]
fn sweep_by_count_and_digest() {
    let rows = common::jsonl(FILE);
    let row = rows.iter().find(|r| r["label"] == "sweep").unwrap();
    let shifts: Vec<f64> = row["shifts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_f64().unwrap())
        .collect();
    let mut ledger = Ledger::new("sweep_by_count_and_digest", FILE);
    ledger.case("sweep", || {
        let (steps, digest) = sweep(
            row["seed"].as_u64().unwrap(),
            row["sequences"].as_u64().unwrap(),
            &shifts,
            &rows[0]["defaults"],
        );
        assert_eq!(steps, row["steps"].as_u64().unwrap());
        assert_eq!(digest, row["sha256"].as_str().unwrap());
    });
    ledger.finish(1);
    assert_eq!(row["sequences"], 2000);
}
