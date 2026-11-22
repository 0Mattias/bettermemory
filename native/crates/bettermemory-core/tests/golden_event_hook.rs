//! Every case of `event_hook.jsonl` (not gated) against the native
//! `event_hook`: `_store_busy` of each class and code, `StoreBusy`'s text
//! for each event, and each run tree's trace (deadlines, suppression,
//! busy timeouts lowered and given back, the error raised).

mod common;
mod event_hook_tree;

use bettermemory_core::event_hook::{is_busy, store_busy_message};
use serde_json::Value;

#[test]
fn every_row() {
    let lines = common::jsonl("event_hook.jsonl");
    let (header, rows) = lines.split_first().unwrap();
    let mut checked = 0;
    for row in rows {
        match row["k"].as_str().unwrap() {
            "busy" => {
                let got = is_busy(row["c"].as_str().unwrap(), row["code"].as_i64());
                assert_eq!(Value::Bool(got), row["r"], "{row}");
            }
            "msg" => {
                let got = store_busy_message(row["e"].as_str().unwrap());
                assert_eq!(row["r"].as_str().unwrap(), got);
            }
            "run" => {
                let got = event_hook_tree::run_row_failing(
                    event_hook_tree::float(&row["w"]),
                    row["f"].as_u64().map(|f| f as usize),
                    &row["t"],
                );
                assert_eq!(&Value::Array(got), &row["r"], "{}", row["t"]);
            }
            other => panic!("unknown row kind {other}"),
        }
        checked += 1;
    }
    assert!(checked > 0);
    assert_eq!(checked, header["count"].as_u64().unwrap() as usize);
}
