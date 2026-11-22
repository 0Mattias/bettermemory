//! Every row of `pyre_api.jsonl` and the `api.` rows of
//! `pyre_unicode.jsonl` and `pyre_python.jsonl` against `pyre::regex`: each
//! pattern built from its text as `re.compile` builds it, its five methods
//! over every window of the texts `gen_pyre_api` names (regenerated here by
//! the same rule and compared by count and digest), the fuzz patterns, the
//! refusals by name, and the calls written out.

mod common;
mod pyre_api_support;

use bettermemory_core::pyre::replace::ReplaceError;
use common::parity::Ledger;
use pyre_api_support::{build, codes, line, refusal_name, replaced, reversed, windows, Built};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn text(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("a text: {v}"))
}

fn int(row: &Value, key: &str) -> isize {
    row[key].as_i64().unwrap_or_else(|| panic!("{key}: {row}")) as isize
}

fn built(row: &Value) -> Built {
    let b = build(row).unwrap_or_else(|e| panic!("{e:?}: {row}"));
    assert_eq!(
        u64::from(b.re.flags()),
        row["sre_flags"].as_u64().expect("sre_flags")
    );
    assert_eq!(
        b.re.groups() as u64,
        row["groups"].as_u64().expect("groups")
    );
    b
}

/// Every text over `alphabet` of length `n`, in `itertools.product` order.
fn texts(alphabet: &[u32], n: usize) -> impl Iterator<Item = Vec<u32>> + '_ {
    let k = alphabet.len();
    (0..k.pow(n as u32)).map(move |index| {
        let mut t = vec![0u32; n];
        let mut rest = index;
        for slot in t.iter_mut().rev() {
            *slot = alphabet[rest % k];
            rest /= k;
        }
        t
    })
}

const METHODS: [&str; 5] = ["match", "fullmatch", "search", "finditer", "findall"];

fn five(b: &Built, t: &[u32], pos: isize, endpos: isize) -> String {
    METHODS.map(|m| line(b, t, pos, endpos, m, false)).join(" ")
}

fn check_sweep(row: &Value) {
    let b = built(row);
    let alphabet = codes(&row["alphabet"]);
    let short = int(row, "short");
    let long = int(row, "long") as usize;
    let want_reversed = row["reversed"].as_bool().expect("reversed");
    let mut digest = Sha256::new();
    let mut count = 0u64;
    for n in 0..=long {
        for t in texts(&alphabet, n) {
            let len = n as isize;
            for (pos, endpos) in windows(len, short) {
                if reversed(len, pos, endpos) != want_reversed {
                    continue;
                }
                digest.update(five(&b, &t, pos, endpos).as_bytes());
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
        "{}",
        row["pattern"]
    );
}

fn check_fuzz(row: &Value) {
    if let Some(name) = row.get("refused") {
        assert_eq!(refusal_name(row), text(name), "{row}");
        return;
    }
    let b = built(row);
    let mut digest = Sha256::new();
    for n in 0..=4usize {
        for t in texts(&[97, 98], n) {
            let len = n as isize;
            for pos in 0..=len {
                for endpos in pos..=len {
                    digest.update(five(&b, &t, pos, endpos).as_bytes());
                    digest.update(b"\n");
                }
            }
        }
    }
    assert_eq!(
        &common::hex(&digest.finalize())[..16],
        text(&row["sha16"]),
        "{row}"
    );
}

fn check_kill(row: &Value) {
    let b = built(row);
    let got = line(
        &b,
        &codes(&row["text"]),
        int(row, "pos"),
        int(row, "endpos"),
        text(&row["method"]),
        true,
    );
    assert_eq!(got, text(&row["answer"]), "{row}");
}

fn check_replace(row: &Value) {
    let b = built(row);
    let repl = row.get("repl").map(codes).unwrap_or_default();
    let got = replaced(
        &b,
        &codes(&row["text"]),
        text(&row["method"]),
        &repl,
        int(row, "count"),
    );
    match row.get("refused") {
        Some(name) => {
            assert_eq!(text(name), "Template", "{row}");
            assert_eq!(got, Err(ReplaceError::Template), "{row}");
        }
        None => assert_eq!(got.as_deref(), Ok(text(&row["answer"])), "{row}"),
    }
}

/// `gen_pyre_api._replace_sweep` over the accepted `api.fuzz` rows of
/// the file, in order.
fn check_replace_sweep(row: &Value, file: &[Value]) {
    let counts: Vec<isize> = row["counts"]
        .as_array()
        .expect("counts")
        .iter()
        .map(|c| c.as_i64().expect("a count") as isize)
        .collect();
    let long = int(row, "long") as usize;
    let mut digest = Sha256::new();
    let mut calls = 0u64;
    for fuzz in file
        .iter()
        .filter(|r| r["part"] == "api.fuzz" && r.get("refused").is_none())
    {
        let b = built(fuzz);
        for n in 0..=long {
            for t in texts(&[97, 98], n) {
                for &c in &counts {
                    let answers = ["subn", "subn_fn", "split"].map(|m| {
                        replaced(&b, &t, m, &[60, 62], c).unwrap_or_else(|e| panic!("{e}"))
                    });
                    digest.update(answers.join(" ").as_bytes());
                    digest.update(b"\n");
                    calls += 1;
                }
            }
        }
    }
    assert!(calls > 0, "a sweep of no call");
    assert_eq!(calls, row["count"].as_u64().expect("count"));
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
}

fn check(ledger: &mut Ledger, row: &Value, file: &[Value]) {
    let part = text(&row["part"]);
    let name = format!(
        "{part} {} {}",
        row["pattern"],
        row.get("text").unwrap_or(&Value::Null)
    );
    match part {
        "api.sweep" => ledger.case(name, || check_sweep(row)),
        "api.fuzz" => ledger.case(name, || check_fuzz(row)),
        "api.kill" => ledger.case(name, || check_kill(row)),
        "api.replace" => ledger.case(name, || check_replace(row)),
        "api.replace_sweep" => ledger.case(name, || check_replace_sweep(row, file)),
        other => panic!("a part this test does not know: {other}"),
    }
}

#[test]
fn pyre_api_every_row() {
    let mut file = common::jsonl("pyre_api.jsonl");
    let header = file.remove(0);
    assert_eq!(header["part"], "header");
    let mut ledger = Ledger::new("pyre_api_every_row", "pyre_api.jsonl");
    for row in &file {
        check(&mut ledger, row, &file);
    }
    common::assert_count(
        "pyre_api.jsonl header",
        file.len(),
        header["rows"].as_u64().expect("rows") as usize,
    );
    ledger.finish(file.len());
}

fn shared(name: &str, test: &str) {
    let file = common::jsonl(name);
    let rows: Vec<&Value> = file
        .iter()
        .filter(|row| text(&row["part"]).starts_with("api."))
        .collect();
    assert!(!rows.is_empty(), "no api. row in {name}");
    assert_eq!(
        file[0]["rows"]["pyre-6a"]
            .as_u64()
            .expect("the header counts pyre-6a") as usize,
        rows.len()
    );
    let mut ledger = Ledger::new(test, name);
    for row in &rows {
        check(&mut ledger, row, &file);
    }
    ledger.finish(rows.len());
}

/// The patterns under IGNORECASE and the sweeps past ASCII, which read the
/// Unicode tables.
#[test]
fn pyre_api_unicode_every_row() {
    shared("pyre_unicode.jsonl", "pyre_api_unicode_every_row");
}

/// The reversed windows, whose `match` and `fullmatch` answers are
/// 3.13.13's.
#[test]
fn pyre_api_python_every_row() {
    shared("pyre_python.jsonl", "pyre_api_python_every_row");
}
