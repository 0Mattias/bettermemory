//! Every case of `audit_probe.jsonl` (not gated) against the native
//! `audit::probe` and `search::lane::relevance_label_v2`: the thin-message
//! gate, the v2 label, `probe_for_miss` with a recorded ranker and a fake
//! admission (its report, the ranker's calls, what the admission was
//! primed with and asked), `is_duplicate_audit` and
//! `_count_recent_retrievals`.

mod common;

use bettermemory_core::audit::probe::{ack_tokens, too_thin_to_probe};
use bettermemory_core::search::lane::{relevance_label_v2, V2_HIGH_MATCHED_FLOOR};
use common::audit_probe::{memories, run_count, run_dup, run_probe, text};
use serde_json::{json, Value};

#[test]
fn every_case() {
    let rows = common::jsonl("audit_probe.jsonl");
    let header = &rows[0];
    let mems = memories(header);
    let mut checked = 0;
    for row in &rows[1..] {
        let name = row.get("name").cloned().unwrap_or(Value::Null);
        match row["k"].as_str().unwrap() {
            "ack" => assert_eq!(row["o"], json!(ack_tokens())),
            "thin" => assert_eq!(
                row["o"],
                json!(too_thin_to_probe(&text(&row["msg"]))),
                "thin {}",
                row["msg"]
            ),
            "v2floor" => assert_eq!(row["o"], json!(V2_HIGH_MATCHED_FLOOR)),
            "v2" => {
                let (m, q) = (row["m"].as_i64().unwrap(), row["q"].as_i64().unwrap());
                assert_eq!(row["o"], json!(relevance_label_v2(m, q)), "v2 {m} {q}");
            }
            "probe" => {
                let got = run_probe(row, &mems);
                for key in ["o", "calls", "primed", "asked"] {
                    assert_eq!(got[key], row[key], "probe {name} {key}");
                }
            }
            "dup" => assert_eq!(run_dup(row), row["o"], "dup {name}"),
            "count" => {
                let got = run_count(row);
                for key in ["o", "primed", "asked"] {
                    assert_eq!(got[key], row[key], "count {name} {key}");
                }
            }
            other => panic!("unknown row kind {other}"),
        }
        checked += 1;
    }
    common::assert_count(
        "audit_probe.jsonl",
        checked,
        header["count"].as_u64().unwrap() as usize,
    );
}
