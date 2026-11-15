//! Every part of `unicode.jsonl` (one record): each map's full-table
//! SHA-256, per-block SHA-256 and changed code points; the category and
//! combining-class runs; each predicate's ranges; each multi-code-point
//! sequence through each map. And every record of `unicode_sequences.jsonl`:
//! the NFKD table, and final sigma and multi-code-point case mappings on
//! sequences through each map.

mod common;

use bettermemory_core::unicode::{self, Form};
use common::parity::{self, Ledger};
use common::{cps_of, jsonl};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn scalars() -> impl Iterator<Item = u32> {
    (0..0x110000u32).filter(|cp| !(0xD800..=0xDFFF).contains(cp))
}

fn map(name: &str, text: &[u32]) -> Vec<u32> {
    match name {
        "nfc" => unicode::normalize(Form::Nfc, text),
        "nfd" => unicode::normalize(Form::Nfd, text),
        "nfkc" => unicode::normalize(Form::Nfkc, text),
        "nfkd" => unicode::normalize(Form::Nfkd, text),
        "casefold" => unicode::casefold(text),
        "lower" => unicode::lower(text),
        "upper" => unicode::upper(text),
        other => panic!("map {other} is not ported"),
    }
}

fn predicate(name: &str, cp: u32) -> bool {
    match name {
        "isalpha" => unicode::is_alpha(cp),
        "isalnum" => unicode::is_alnum(cp),
        "isdigit" => unicode::is_digit(cp),
        "isspace" => unicode::is_space(cp),
        "re_word" => unicode::is_word(cp),
        "re_digit" => unicode::is_re_digit(cp),
        other => panic!("predicate {other} is not ported"),
    }
}

fn hex_digest(hasher: Sha256) -> String {
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// `_map_table`: the whole-table and per-block digests and the changes.
fn map_table(name: &str) -> Value {
    let mut whole = Sha256::new();
    let mut block = Sha256::new();
    let mut blocks = Vec::new();
    let mut changed = Vec::new();
    for cp in 0..0x110000u32 {
        if cp != 0 && cp % 0x1000 == 0 {
            blocks.push(hex_digest(std::mem::take(&mut block)));
        }
        if (0xD800..=0xDFFF).contains(&cp) {
            continue;
        }
        let result = map(name, &[cp]);
        let hexes: Vec<String> = result.iter().map(|c| format!("{c:X}")).collect();
        let line = format!("{cp:X}\t{}\n", hexes.join(" "));
        whole.update(line.as_bytes());
        block.update(line.as_bytes());
        if result != [cp] && !(matches!(name, "nfd" | "nfkd") && (0xAC00..=0xD7A3).contains(&cp)) {
            changed.push(json!([cp, result]));
        }
    }
    blocks.push(hex_digest(block));
    json!({ "sha256": hex_digest(whole), "block_sha256": blocks, "changed": changed })
}

fn runs<T: PartialEq + Into<Value> + Copy>(f: impl Fn(u32) -> T) -> Value {
    let mut out: Vec<(u32, T)> = Vec::new();
    for cp in scalars() {
        let v = f(cp);
        if out.last().is_none_or(|(_, last)| *last != v) {
            out.push((cp, v));
        }
    }
    Value::Array(
        out.into_iter()
            .map(|(cp, v)| json!([cp, v.into()]))
            .collect(),
    )
}

fn ranges(name: &str) -> Value {
    let mut out: Vec<[u32; 2]> = Vec::new();
    for cp in scalars() {
        if !predicate(name, cp) {
            continue;
        }
        match out.last_mut() {
            Some(last) if last[1] + 1 == cp => last[1] = cp,
            _ => out.push([cp, cp]),
        }
    }
    json!(out)
}

#[test]
fn unicode_every_part() {
    let lines = jsonl("unicode.jsonl");
    assert_eq!(lines.len(), 1, "unicode.jsonl is one record");
    let doc = &lines[0];
    assert_eq!(doc["fixture"], "unicode");
    // The Unicode that made the file; the port carries UNIDATA_VERSION, and
    // under another a differing part is set aside, named (class ii).
    assert_eq!(
        doc["unidata_version"],
        parity::provenance().unicode.as_str()
    );
    assert_eq!(doc["block_size"], 4096);
    let mut parts = 0;
    let mut in_file = 0;
    let mut failures = Vec::new();
    let mut check = |what: String, got: &Value, expected: &Value| {
        if got != expected {
            failures.push(what);
        }
    };

    let maps = doc["maps"].as_object().expect("maps");
    assert_eq!(maps.len(), 6);
    for (name, expected) in maps {
        let got = map_table(name);
        for key in ["sha256", "block_sha256", "changed"] {
            check(format!("maps.{name}.{key}"), &got[key], &expected[key]);
            parts += 1;
        }
        in_file += 3;
    }

    let category = runs(unicode::category);
    check("category_runs".into(), &category, &doc["category_runs"]);
    let combining = runs(unicode::combining);
    check("combining_runs".into(), &combining, &doc["combining_runs"]);
    parts += 2;
    in_file += 2;

    let predicates = doc["predicates"].as_object().expect("predicates");
    assert_eq!(predicates.len(), 6);
    for (name, expected) in predicates {
        check(format!("predicates.{name}"), &ranges(name), expected);
        parts += 1;
    }
    in_file += predicates.len();

    let sequences = doc["sequences"].as_array().expect("sequences");
    for seq in sequences {
        let input = cps_of(&seq["input"]);
        let names: Vec<&String> = seq
            .as_object()
            .unwrap()
            .keys()
            .filter(|k| *k != "input")
            .collect();
        assert_eq!(names.len(), 6);
        for name in names {
            check(
                format!("sequence {input:X?} / {name}"),
                &json!(map(name, &input)),
                &seq[name],
            );
            parts += 1;
            in_file += 1;
        }
    }
    eprintln!(
        "unicode: {} map tables, {} sequences, {} predicates, 2 run lists",
        maps.len(),
        sequences.len(),
        predicates.len()
    );
    assert_eq!(parts, in_file, "unicode.jsonl parts");
    Ledger::new("unicode_every_part", "unicode.jsonl").settle(parts, &failures);
}

#[test]
fn unicode_sequences_every_record() {
    let lines = jsonl("unicode_sequences.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "unicode_sequences");
    assert_eq!(
        header["unidata_version"],
        parity::provenance().unicode.as_str()
    );
    let maps: Vec<String> = header["maps"]
        .as_array()
        .expect("maps")
        .iter()
        .map(|m| m.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(maps.len(), 7);
    let mut failures = Vec::new();
    let (mut tables, mut sequences, mut cells) = (0, 0, 0);
    for record in &lines[1..] {
        if let Some(name) = record.get("map") {
            let name = name.as_str().unwrap();
            let got = map_table(name);
            for key in ["sha256", "block_sha256", "changed"] {
                if got[key] != record[key] {
                    failures.push(format!("table {name}.{key}"));
                }
                cells += 1;
            }
            tables += 1;
            continue;
        }
        let input = cps_of(&record["input"]);
        for name in &maps {
            let got = json!(map(name, &input));
            if got != record[name.as_str()] {
                failures.push(format!(
                    "{} {input:X?} / {name}: expected {}, got {got}",
                    record["kind"],
                    record[name.as_str()]
                ));
            }
            cells += 1;
        }
        sequences += 1;
    }
    eprintln!("unicode_sequences: {tables} tables, {sequences} sequences, {cells} cells");
    assert_eq!(
        tables + sequences,
        lines.len() - 1,
        "unicode_sequences.jsonl"
    );
    Ledger::new("unicode_sequences_every_record", "unicode_sequences.jsonl")
        .settle(cells, &failures);
}
