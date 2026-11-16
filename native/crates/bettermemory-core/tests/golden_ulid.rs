//! Every case of `ulid.jsonl`: the text `models.generate_ulid` mints for
//! each pair of milliseconds and random bytes, the milliseconds it takes
//! from each clock float, and `is_valid_ulid` of each text, against
//! `store::mint`. Then properties over a rule-built set of pairs.

mod common;

use bettermemory_core::store::mint::{self, MAX_MS};
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

fn bytes10(hex: &str) -> [u8; 10] {
    let v = common::unhex(hex);
    v.try_into().expect("ten bytes")
}

fn check(failures: &mut Vec<String>, what: String, got: &str, want: &str) {
    if got != want {
        failures.push(format!("{what}: native {got} reference {want}"));
    }
}

fn cps(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

#[test]
fn ulid_every_case() {
    let lines = jsonl("ulid.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "ulid");
    let ledger = Ledger::new("golden_ulid", "ulid.jsonl");
    let mut failures = Vec::new();
    let (mut pairs, mut floats, mut texts, mut valid) = (0u64, 0u64, 0u64, 0u64);
    for record in &lines[1..] {
        let pair = |ms: &Value, hex: &Value, text: &Value| {
            let ms = ms.as_u64().expect("ms");
            let hex = hex.as_str().expect("bytes");
            (
                format!("ms {ms} bytes {hex}"),
                mint::ulid(ms, &bytes10(hex)),
                text.as_str().expect("text").to_string(),
            )
        };
        let mut found = Vec::new();
        if let Some(row) = record.as_array() {
            found.push(pair(&row[0], &row[1], &row[2]));
        } else if let Some(group) = record.get("m") {
            for row in group.as_array().expect("rows") {
                found.push(pair(&row[0], &record["r"], &row[1]));
            }
        } else if let Some(group) = record.get("b") {
            for row in group.as_array().expect("rows") {
                found.push(pair(&record["ms"], &row[0], &row[1]));
            }
        } else if let Some(bits) = record["f"].as_str() {
            floats += 1;
            let seconds = f64::from_bits(u64::from_str_radix(bits, 16).expect("bits"));
            let got = match mint::millis(seconds) {
                Ok(ms) => mint::ulid(ms, &[0; 10]),
                Err(e) => e.name().to_string(),
            };
            let want = record
                .get("t")
                .or_else(|| record.get("x"))
                .and_then(Value::as_str)
                .expect("a result");
            check(
                &mut failures,
                format!("clock {bits} ({seconds:e})"),
                &got,
                want,
            );
        } else {
            texts += 1;
            let text: Vec<u32> = match record.get("v") {
                Some(v) => cps(v.as_str().expect("text")),
                None => {
                    let at = record["at"].as_u64().expect("at") as usize;
                    let mut t = vec![u32::from(b'0'); 26];
                    t[at] = record["c"].as_u64().expect("c") as u32;
                    t
                }
            };
            let want = record["ok"].as_bool().expect("ok");
            let got = mint::is_valid_ulid(&text);
            valid += u64::from(want);
            check(
                &mut failures,
                format!("text {text:?}"),
                &got.to_string(),
                &want.to_string(),
            );
            // The reverse read: a valid text names its first ten characters.
            if got && mint::ulid_ms(&text).is_none() {
                failures.push(format!("text {text:?}: valid but no milliseconds"));
            }
        }
        for (what, got, want) in found {
            pairs += 1;
            check(&mut failures, what, &got, &want);
        }
    }
    assert_eq!(header["pairs"].as_u64(), Some(pairs));
    assert_eq!(header["floats"].as_u64(), Some(floats));
    assert_eq!(header["texts"].as_u64(), Some(texts));
    assert_eq!(header["valid"].as_u64(), Some(valid));
    ledger.settle((pairs + floats + texts) as usize, &failures);
}

/// Pairs by a fixed rule: edges and a linear congruential walk.
fn rule_pairs() -> Vec<(u64, [u8; 10])> {
    let mut out = vec![
        (0, [0; 10]),
        (MAX_MS, [0xff; 10]),
        (1, [0; 10]),
        (0, [0xff; 10]),
    ];
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut next = || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        state
    };
    for k in 0..3000u64 {
        let ms = match k % 3 {
            0 => next() >> 16,
            1 => (next() >> 54) + 1_763_251_200_000,
            _ => next() & 0xff,
        };
        let mut random = [0u8; 10];
        let (a, b) = (next(), next());
        random[..8].copy_from_slice(&a.to_be_bytes());
        random[8..].copy_from_slice(&b.to_be_bytes()[..2]);
        if k % 5 == 0 {
            random[..6].fill(0);
        }
        out.push((ms, random));
    }
    out
}

#[test]
fn ulid_order_and_round_trip() {
    let pairs = rule_pairs();
    let minted: Vec<String> = pairs.iter().map(|(ms, r)| mint::ulid(*ms, r)).collect();
    let mut checked = 0usize;
    for (i, (ms, _)) in pairs.iter().enumerate() {
        let text = cps(&minted[i]);
        assert!(mint::is_valid_ulid(&text), "{}", minted[i]);
        assert_eq!(mint::ulid_ms(&text), Some(*ms), "{}", minted[i]);
        checked += 1;
    }
    for i in 0..pairs.len() {
        for j in [(i + 1) % pairs.len(), (i * 7 + 3) % pairs.len()] {
            assert_eq!(
                minted[i].cmp(&minted[j]),
                pairs[i].cmp(&pairs[j]),
                "{} {}",
                minted[i],
                minted[j]
            );
            checked += 1;
        }
    }
    // Every 48-bit value read back from its clock float m / 1000.
    for (ms, _) in &pairs {
        let seconds = *ms as f64 / 1000.0;
        let got = mint::millis(seconds).expect("finite");
        assert!(got.abs_diff(*ms) <= 1, "{ms} -> {got}");
        checked += 1;
    }
    assert!(checked > 12_000, "{checked}");
}
