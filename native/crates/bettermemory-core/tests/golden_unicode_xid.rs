//! `unicode_xid.jsonl`: the digest of `str.isidentifier` alone and after
//! `"a"` over every code point, whole and per 0x1000 block, and each whole
//! string case.

mod common;

use bettermemory_core::unicode::xid::{is_identifier, is_xid_continue, is_xid_start};
use common::parity::Ledger;
use common::{jsonl, text_field};
use sha2::{Digest, Sha256};

fn hex_digest(hasher: Sha256) -> String {
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// The generator's answer character for one code point.
fn answer(cp: u32) -> u8 {
    let alone = is_identifier(&[cp]);
    let after = is_identifier(&[u32::from(b'a'), cp]);
    b"0123"[usize::from(alone) | usize::from(after) << 1]
}

#[test]
fn unicode_xid_every_record() {
    let lines = jsonl("unicode_xid.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "unicode_xid");
    let count = header["count"].as_u64().expect("count") as u32;
    let block_size = header["block_size"].as_u64().expect("block size") as u32;
    assert_eq!(count, 0x110000);
    assert_eq!(block_size, 0x1000);
    let mut whole = Sha256::new();
    let mut blocks = Vec::new();
    let mut checked = 0u32;
    for start in (0..count).step_by(block_size as usize) {
        let text: Vec<u8> = (start..start + block_size).map(answer).collect();
        checked += block_size;
        whole.update(&text);
        let mut block = Sha256::new();
        block.update(&text);
        blocks.push(hex_digest(block));
    }
    assert_eq!(checked, count, "code points answered");
    let expected: Vec<&str> = header["block_sha256"]
        .as_array()
        .expect("blocks")
        .iter()
        .map(|b| b.as_str().unwrap())
        .collect();
    let mut failures = Vec::new();
    if hex_digest(whole) != header["sha256"] {
        failures.push("sha256".to_string());
    }
    assert_eq!(expected.len(), blocks.len(), "block count");
    for (i, (got, want)) in blocks.iter().zip(&expected).enumerate() {
        if got != want {
            failures.push(format!("block U+{:04X}", i as u32 * block_size));
        }
    }
    let cases = &lines[1..];
    for case in cases {
        let input = text_field(case, "input");
        if is_identifier(&input) != case["isidentifier"] {
            failures.push(format!("case {input:X?}"));
        }
    }
    assert!(!cases.is_empty(), "no case");
    // The ASCII fast paths agree with the tables' own rule.
    assert!(is_xid_start(u32::from(b'A')) && is_xid_continue(u32::from(b'9')));
    Ledger::new("unicode_xid_every_record", "unicode_xid.jsonl")
        .settle(1 + expected.len() + cases.len(), &failures);
}
