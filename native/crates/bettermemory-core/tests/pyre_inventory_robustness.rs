//! Hostile input through the reference's own patterns: every inventory
//! pattern over a long text made of its own fragments, answered or cut by
//! the budget of `try_*` and never a panic; scans of two inventory
//! patterns linear in the text; a text of a million code points.

mod common;
mod pyre_api_support;
mod timing;

use bettermemory_core::pyre::regex::{CallError, Regex};
use pyre_api_support::build;
use serde_json::Value;

const BUDGET: u64 = 2_000_000;

fn spelled(v: &Value) -> Vec<u32> {
    match v.as_str() {
        Some(s) => s.chars().map(u32::from).collect(),
        None => pyre_api_support::codes(v),
    }
}

/// The row's fragments and alphabet, one after another, to `len` code
/// points.
fn long_text(row: &Value, len: usize) -> Vec<u32> {
    let mut pieces: Vec<Vec<u32>> = row["fragments"]
        .as_array()
        .expect("fragments")
        .iter()
        .map(spelled)
        .collect();
    pieces.extend(
        pyre_api_support::codes(&row["alphabet"])
            .into_iter()
            .map(|c| vec![c]),
    );
    let mut out = Vec::with_capacity(len + 64);
    let mut i = 0;
    while out.len() < len {
        out.extend(&pieces[i % pieces.len()]);
        i += 1;
    }
    out.truncate(len);
    out
}

/// Every pattern of the reference over 4,000 code points of its own
/// pieces: `finditer` answers or stops at the budget (the patterns the
/// reference itself runs in quadratic time over such text), with no panic.
#[test]
fn every_inventory_pattern_survives_a_long_text_of_its_own_pieces() {
    let mut rows = common::jsonl("pyre_inventory.jsonl");
    rows.extend(common::jsonl("pyre_unicode.jsonl"));
    let mut seen = 0;
    let mut cut = 0;
    for row in rows.iter().filter(|r| r["part"] == "inventory.pattern") {
        let b = build(row).unwrap_or_else(|e| panic!("{e:?}: {}", row["pattern"]));
        let t = long_text(row, 4_000);
        let answer = if b.bytes {
            let bytes: Vec<u8> = t.iter().map(|&c| c as u8).collect();
            b.re.try_finditer(bytes.as_slice(), 0, isize::MAX, Some(BUDGET))
                .map(|v| v.len())
        } else {
            b.re.try_finditer(t.as_slice(), 0, isize::MAX, Some(BUDGET))
                .map(|v| v.len())
        };
        match answer {
            Ok(_) => {}
            Err(CallError::Budget) => cut += 1,
            Err(e) => panic!("{e}: {}", row["pattern"]),
        }
        seen += 1;
    }
    assert!(seen > 100, "{seen} patterns");
    eprintln!("{seen} patterns, {cut} stopped at the budget");
}

fn cp(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn linear(name: &str, pattern: &str, flags: u32, unit: &str) {
    let r = Regex::new_str(pattern, flags).unwrap_or_else(|e| panic!("{e:?}"));
    let small = cp(&unit.repeat(1_000));
    let large = cp(&unit.repeat(10_000));
    let time =
        |t: &Vec<u32>| timing::once(|| r.finditer(t.as_slice(), 0, isize::MAX).map(|v| v.len()));
    timing::grows_within(name, 30, &mut || time(&small), &mut || time(&large));
}

/// The key file name of `credentials`, over hex runs that almost match.
#[test]
fn a_key_name_scan_is_linear() {
    linear(
        "key name",
        r"(?P<id>[0-9a-f]{32})(?:\.[0-9a-f]{16})?\.key",
        0,
        "ee5a05cbd2a51d13fada458c70bf2ba9.ke ",
    );
}

/// A lookbehind and a lookahead per position, over text of dots.
#[test]
fn a_sentence_end_scan_is_linear() {
    linear(
        "sentence end",
        r"(?:[!?;:\n]|(?<!\d)\.|\.(?!\d))+",
        0,
        "a1.2. !x",
    );
}

/// A million code points through one search: no stack grows with the text.
#[test]
fn a_million_code_points_are_read() {
    let r =
        Regex::new_str(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", 0).expect("built");
    let mut t = vec![u32::from('g'); 1_000_000];
    t.extend(cp(" e8b73a7"));
    let m = r
        .search(t.as_slice(), 0, isize::MAX)
        .expect("str")
        .expect("a match");
    assert_eq!(m.regs[0], (1_000_001, 1_000_008));
}
