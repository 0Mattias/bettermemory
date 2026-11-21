//! Every row of `pyre_lower.jsonl` and the `lower.*` rows of
//! `pyre_unicode.jsonl` against `pyre::lower::lower`: written cases, the
//! reference's own patterns under no flag, and a sweep of random trees
//! regenerated here by the generator's rule and compared by count and
//! digest. An answer is `=TRACE #N` (the opcodes `re` emits for each
//! repeat, anchor, ANY, lookaround, FAILURE and literal under IGNORECASE,
//! and the arena's size), `!Error` for an error of `_compile`, or
//! `?Feature` for a refusal (`gen_pyre_lower`).

mod common;
mod pyre_lower_support;

use common::parity::Ledger;
use pyre_lower_support::{answer, folds, random_sub, sub, text, Lcg, FLAGS};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn check_case(row: &Value) {
    let tree = sub(&row["tree"]);
    let flags = row["flags"].as_u64().expect("flags") as u32;
    let bytes = row["bytes"].as_bool().expect("bytes");
    assert_eq!(
        answer(&tree, flags, bytes),
        text(&row["answer"]),
        "{} {}",
        row.get("pattern").unwrap_or(&Value::Null),
        row["tree"]
    );
}

fn check_sweep(row: &Value, with_ignorecase: bool) {
    let seed = row["seed"].as_u64().expect("seed");
    let draws = row["draws"].as_u64().expect("draws");
    let mut digest = Sha256::new();
    let mut count = 0;
    for i in 0..draws {
        let tree = random_sub(&mut Lcg(seed + i), 0);
        let flags = FLAGS[(i % 4) as usize];
        if folds(&tree, flags) != with_ignorecase {
            continue;
        }
        digest.update(answer(&tree, flags, false).as_bytes());
        digest.update(b"\n");
        count += 1;
    }
    assert!(count > 0, "a sweep of no tree");
    assert_eq!(count, row["count"].as_u64().expect("count"), "sweep count");
    assert_eq!(
        common::hex(&digest.finalize()),
        text(&row["sha256"]),
        "sweep digest"
    );
}

#[test]
fn pyre_lower_every_row() {
    let mut file = common::jsonl("pyre_lower.jsonl");
    let header = file.remove(0);
    assert_eq!(header["part"], "header");
    assert_eq!(header["max_nodes"], 4096);
    assert_eq!(header["max_depth"], 32);
    let mut parts = std::collections::BTreeSet::new();
    let mut checked = 0;
    for row in &file {
        let part = text(&row["part"]);
        match part {
            "lower.case" | "lower.inventory" => check_case(row),
            "lower.sweep" => check_sweep(row, false),
            other => panic!("a part this test does not know: {other}"),
        }
        parts.insert(part);
        checked += 1;
    }
    assert_eq!(parts.len(), 3, "a part with no row: {parts:?}");
    common::assert_count("pyre_lower.jsonl", checked, file.len());
    common::assert_count(
        "pyre_lower.jsonl header",
        checked,
        header["rows"].as_u64().expect("rows") as usize,
    );
}

/// The rows under IGNORECASE, whose trace names what the fold tables say
/// of each literal.
#[test]
fn pyre_lower_unicode_every_row() {
    let file = common::jsonl("pyre_unicode.jsonl");
    let rows: Vec<&Value> = file
        .iter()
        .filter(|row| text(&row["part"]).starts_with("lower."))
        .collect();
    assert_eq!(
        file[0]["rows"]["pyre-4b"]
            .as_u64()
            .expect("the header counts pyre-4b") as usize,
        rows.len()
    );
    let mut ledger = Ledger::new("pyre_lower_unicode_every_row", "pyre_unicode.jsonl");
    for row in &rows {
        let part = text(&row["part"]);
        match part {
            "lower.case" => ledger.case(format!("{part} {}", row["tree"]), || check_case(row)),
            "lower.sweep" => ledger.case(part, || check_sweep(row, true)),
            other => panic!("a part this test does not know: {other}"),
        }
    }
    ledger.finish(rows.len());
}
