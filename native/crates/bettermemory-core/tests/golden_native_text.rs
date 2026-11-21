//! Every case of `native_text.jsonl` (not gated) against `native_text`:
//! `has_surrogates`, `utf8_text`, `origin_projection` and a digest of
//! `utf8_text` over every code point.

mod common;

use bettermemory_core::native_text as nt;
use bettermemory_core::pyvalue::PyValue;
use common::parity::Ledger;
use serde_json::Value;
use sha2::{Digest, Sha256};

fn check(r: &Value) {
    match r["k"].as_str().unwrap() {
        "has" => assert_eq!(
            nt::has_surrogates(&common::neutral(&r["v"])),
            r["r"].as_bool().unwrap()
        ),
        "utf8" => assert_eq!(
            nt::utf8_text(&common::text_field(r, "t")),
            r["r"].as_str().unwrap()
        ),
        "proj" => {
            let whole = common::neutral(&r["o"]);
            let PyValue::Dict(origin) = &whole else {
                panic!("an origin is a dict")
            };
            match nt::origin_projection(origin) {
                Err(e) => assert_eq!(e.name(), r["raises"].as_str().expect("an answer")),
                Ok((legacy, native)) => {
                    assert!(r.get("raises").is_none(), "the reference raised");
                    // Debug text, not `==`: a NaN equals nothing, and its rendering
                    // compares as the value it is.
                    assert_eq!(
                        format!("{:?}", PyValue::Dict(legacy)),
                        format!("{:?}", common::neutral(&r["legacy"]))
                    );
                    let want = r["native"].as_str().map(bettermemory_core::unicode::cps);
                    assert_eq!(native, want);
                }
            }
        }
        "sweep" => {
            let mut digest = Sha256::new();
            let mut count = 0u64;
            for c in 0..0x110000u32 {
                digest.update(nt::utf8_text(&[c]).as_bytes());
                count += 1;
            }
            assert_eq!(count, r["count"].as_u64().unwrap());
            let sha: String = digest
                .finalize()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            assert_eq!(sha, r["sha"].as_str().unwrap());
        }
        other => panic!("unknown row kind {other}"),
    }
}

#[test]
fn native_text_every_case() {
    let file = "native_text.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    let mut ledger = Ledger::new("native_text_every_case", file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, r), || check(r));
    }
    ledger.finish(rows.len());
}
