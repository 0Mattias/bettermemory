//! Every row of `origin_dates_memo.jsonl` against `origin_dates::memo`:
//! `instant` over the listed dates and the seeded sweep, and each
//! scenario of `commit_author_timestamps` over one memo, every step
//! checking the answer, the calls in order, the failures and timeouts
//! noted and the memo after it.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_dates::memo::instant;
use bettermemory_core::origin_history::TimestampsMemo;
use common::origin_dates_memo::{memo_of, run_step};
use common::{assert_count, jsonl};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const LO: i128 = -62_135_596_800_000_000;
const SPAN: i128 = 253_402_300_799_999_999 - LO + 1;

fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

fn sweep(seed: u64, count: usize) -> Vec<i64> {
    let mut x = seed;
    (0..count)
        .map(|_| {
            x = x
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            i64::try_from(LO + i128::from(x) % SPAN).unwrap()
        })
        .collect()
}

#[test]
fn every_row() {
    let rows = jsonl("origin_dates_memo.jsonl");
    let mut checked = 0;
    for row in &rows {
        match row["f"].as_str().unwrap() {
            "instant" => {
                let at: Vec<u32> = row["at"].as_str().unwrap().chars().map(u32::from).collect();
                let stamp = DateTime::from_isoformat(&at).unwrap();
                assert_eq!(json!(bits(instant(&stamp).unwrap())), row["r"], "{row}");
            }
            "instant_sweep" => {
                let us = sweep(
                    row["seed"].as_u64().unwrap(),
                    row["count"].as_u64().unwrap() as usize,
                );
                assert_eq!(us.len() as u64, row["count"].as_u64().unwrap());
                let mut h = Sha256::new();
                for u in us {
                    let stamp = DateTime::from_unix_micros_utc(u).unwrap();
                    h.update(instant(&stamp).unwrap().to_bits().to_be_bytes());
                }
                let hex: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
                assert_eq!(json!(hex), row["sha256"]);
            }
            "scenario" => {
                let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
                let steps = row["steps"].as_array().unwrap();
                assert!(!steps.is_empty());
                for step in steps {
                    let (r, s) = run_step(&mut memo, step);
                    let s = s.0.borrow();
                    let name = &row["name"];
                    if r != step["r"] {
                        panic!("{name}: answer {} want {}", trim(&r), trim(&step["r"]));
                    }
                    assert_eq!(json!(s.calls), step["c"], "{name}");
                    assert_eq!(json!(s.failed), step["n"], "{name}");
                    assert_eq!(json!(s.timed), step["t"], "{name}");
                    assert_eq!(memo_of(&memo), step["m"], "{name}");
                    assert!(s.answers.is_empty() && s.cuts.is_empty() && s.held.is_empty());
                }
            }
            other => panic!("unknown row {other}"),
        }
        checked += 1;
    }
    assert_count("origin_dates_memo.jsonl", checked, rows.len());
}

fn trim(v: &Value) -> String {
    let t = v.to_string();
    t.chars().take(300).collect()
}
