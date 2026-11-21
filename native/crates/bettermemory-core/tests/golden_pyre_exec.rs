//! Every row of `pyre_exec.jsonl` and the `exec.` rows of
//! `pyre_unicode.jsonl` and `pyre_python.jsonl` against `pyre::exec::exec`:
//! for each pattern, `match`, `fullmatch` and `search` over every window of
//! the texts `gen_pyre_exec._windows` names, regenerated here by the same
//! rule and compared by count and digest; and the calls written out.

mod common;
mod pyre_exec_support;
mod pyre_lower_support;

use bettermemory_core::pyre::exec::Mode;
use common::parity::Ledger;
use pyre_exec_support::{answer, codes, mode_named, program, reversed, text, windows};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn groups(row: &Value) -> usize {
    row["groups"].as_u64().expect("groups") as usize
}

fn check_sweep(row: &Value) {
    let prog = program(row);
    let bytes = row["bytes"].as_bool().expect("bytes");
    let alphabet = codes(&row["alphabet"]);
    let short = row["short"].as_i64().expect("short") as isize;
    let long = row["long"].as_u64().expect("long") as u32;
    let want_reversed = row["reversed"].as_bool().expect("reversed");
    let groups = groups(row);
    let mut digest = Sha256::new();
    let mut count = 0u64;
    let k = alphabet.len();
    for n in 0..=long {
        // `itertools.product` order: the last position varies fastest.
        for index in 0..k.pow(n) {
            let mut t = vec![0u32; n as usize];
            let mut rest = index;
            for slot in t.iter_mut().rev() {
                *slot = alphabet[rest % k];
                rest /= k;
            }
            let len = n as isize;
            for (pos, endpos) in windows(len, short) {
                if reversed(len, pos, endpos) != want_reversed {
                    continue;
                }
                let line = [Mode::Match, Mode::Fullmatch, Mode::Search]
                    .map(|mode| answer(&prog, bytes, &t, pos, endpos, mode, groups))
                    .join(" ");
                digest.update(line.as_bytes());
                digest.update(b"\n");
                count += 1;
            }
        }
    }
    assert!(count > 0, "a sweep of no window");
    assert_eq!(
        count,
        row["count"].as_u64().expect("count"),
        "{}",
        row["pattern"]
    );
    assert_eq!(
        common::hex(&digest.finalize()),
        text(&row["sha256"]),
        "{} reversed={want_reversed}",
        row["pattern"]
    );
}

fn check_kill(row: &Value) {
    let prog = program(row);
    let got = answer(
        &prog,
        row["bytes"].as_bool().expect("bytes"),
        &codes(&row["text"]),
        row["pos"].as_i64().expect("pos") as isize,
        row["endpos"].as_i64().expect("endpos") as isize,
        mode_named(text(&row["mode"])),
        groups(row),
    );
    assert_eq!(got, text(&row["answer"]), "{row}");
}

fn check(ledger: &mut Ledger, row: &Value) {
    let part = text(&row["part"]);
    let name = format!(
        "{part} {} {}",
        row["pattern"],
        row.get("text").unwrap_or(&Value::Null)
    );
    match part {
        "exec.sweep" => ledger.case(name, || check_sweep(row)),
        "exec.kill" => ledger.case(name, || check_kill(row)),
        other => panic!("a part this test does not know: {other}"),
    }
}

#[test]
fn pyre_exec_every_row() {
    let mut file = common::jsonl("pyre_exec.jsonl");
    let header = file.remove(0);
    assert_eq!(header["part"], "header");
    let mut ledger = Ledger::new("pyre_exec_every_row", "pyre_exec.jsonl");
    for row in &file {
        check(&mut ledger, row);
    }
    common::assert_count(
        "pyre_exec.jsonl header",
        file.len(),
        header["rows"].as_u64().expect("rows") as usize,
    );
    ledger.finish(file.len());
}

fn shared(name: &str, test: &str) {
    let file = common::jsonl(name);
    let rows: Vec<&Value> = file
        .iter()
        .filter(|row| text(&row["part"]).starts_with("exec."))
        .collect();
    assert_eq!(
        file[0]["rows"]["pyre-5a"]
            .as_u64()
            .expect("the header counts pyre-5a") as usize,
        rows.len()
    );
    let mut ledger = Ledger::new(test, name);
    for row in &rows {
        check(&mut ledger, row);
    }
    ledger.finish(rows.len());
}

/// The patterns under IGNORECASE, which read the fold tables.
#[test]
fn pyre_exec_unicode_every_row() {
    shared("pyre_unicode.jsonl", "pyre_exec_unicode_every_row");
}

/// The reversed windows, whose answers are 3.13.13's.
#[test]
fn pyre_exec_python_every_row() {
    shared("pyre_python.jsonl", "pyre_exec_python_every_row");
}
