//! Every case of `verify_candidates_scan.jsonl` and
//! `verify_candidates_scan_unicode.jsonl` against
//! `verify_candidates::scan`: `_normalize_for_compare` and
//! `_scan_candidates` under a recorded home and user table, and digests of
//! every body joined from a few fragments. The fixtures follow the host's
//! `Path`, so they exist on POSIX only (`verify_candidates_scan_not_posix`).

#![cfg(unix)]

mod common;

use std::collections::HashMap;

use bettermemory_core::verify_candidates::scan::{
    getpwnam_refuses, normalize_for_compare, scan_candidates, Candidate, ScanError, UserHome,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => s.chars().map(u32::from).collect(),
        _ => v["cps"]
            .as_array()
            .expect("a text")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    }
}

fn spell(cps: &[u32]) -> Value {
    match cps
        .iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
    {
        Some(s) => json!(s),
        None => json!({ "cps": cps }),
    }
}

/// UTF-8 with surrogates passed (`"surrogatepass"`).
fn utf8_pass(cps: &[u32], out: &mut Vec<u8>) {
    for &c in cps {
        match char::from_u32(c) {
            Some(ch) => out.extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes()),
            None => out.extend_from_slice(&[
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]),
        }
    }
}

type Users = HashMap<Vec<u32>, Vec<u32>>;

fn users(header: &Value) -> Users {
    header["users"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (text(&json!(k)), text(v)))
        .collect()
}

/// The generator's `pwd`: a refused name first, as CPython's
/// `getpwnam` refuses it, then the header's table.
fn lookup(users: &Users, n: &[u32]) -> UserHome {
    if getpwnam_refuses(n) {
        return UserHome::Refused;
    }
    users
        .get(n)
        .cloned()
        .map_or(UserHome::Unknown, UserHome::Found)
}

fn scan(body: &[u32], home: Option<&[u32]>, users: &Users) -> Result<Vec<Candidate>, ScanError> {
    scan_candidates(body, home, &mut |n: &[u32]| lookup(users, n))
}

fn raised(e: ScanError) -> &'static str {
    match e {
        ScanError::Index(_) => "IndexError",
        ScanError::Home(_) => "RuntimeError",
        ScanError::NotPorted => unreachable!("this file runs on Unix only"),
    }
}

fn answer(got: Result<Vec<Candidate>, ScanError>) -> Value {
    match got {
        Ok(c) => json!(c
            .iter()
            .map(|(p, a, s)| json!([spell(p), a, s]))
            .collect::<Vec<_>>()),
        Err(e) => json!({ "raise": raised(e) }),
    }
}

fn sweep(row: &Value, home: Option<&[u32]>, users: &Users) {
    let frags: Vec<Vec<u32>> = row["frags"].as_array().unwrap().iter().map(text).collect();
    let depth = row["depth"].as_u64().unwrap() as usize;
    let mut digest = Sha256::new();
    let mut count = 0u64;
    for k in 0..=depth {
        let mut idx = vec![0usize; k];
        loop {
            let body: Vec<u32> = idx.iter().flat_map(|&i| frags[i].iter().copied()).collect();
            let mut out = Vec::new();
            match scan(&body, home, users) {
                Ok(c) => {
                    for (i, (p, a, s)) in c.iter().enumerate() {
                        if i > 0 {
                            out.push(b'\n');
                        }
                        utf8_pass(p, &mut out);
                        out.extend_from_slice(
                            format!("|{}{}", u8::from(*a), u8::from(*s)).as_bytes(),
                        );
                    }
                }
                Err(e) => out.extend_from_slice(format!("!{}", raised(e)).as_bytes()),
            }
            out.push(b'\n');
            digest.update(&out);
            count += 1;
            let mut j = k;
            while j > 0 {
                idx[j - 1] += 1;
                if idx[j - 1] < frags.len() {
                    break;
                }
                idx[j - 1] = 0;
                j -= 1;
            }
            if j == 0 {
                break;
            }
        }
    }
    assert_eq!(json!(count), row["count"], "sweep count");
    let hex: String = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(json!(hex), row["sha256"], "sweep {:?}", row["frags"]);
}

fn check(row: &Value, users: &Users) {
    let home = row["home"].as_str().map(|h| text(&json!(h)));
    let home = home.as_deref();
    if row.get("sha256").is_some() {
        return sweep(row, home, users);
    }
    let body = match row.get("runs") {
        Some(runs) => runs
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|r| text(&r[0]).repeat(r[1].as_u64().unwrap() as usize))
            .collect(),
        None => text(&row["a"]),
    };
    let got = match row["f"].as_str().unwrap() {
        "nfc" => match normalize_for_compare(&body, home, &mut |n: &[u32]| lookup(users, n)) {
            Ok(r) => spell(&r),
            Err(_) => json!({"raise": "RuntimeError"}),
        },
        "scan" => answer(scan(&body, home, users)),
        f => panic!("unknown function {f}"),
    };
    assert_eq!(
        got,
        row["r"],
        "{} {:?} home {:?}",
        row["f"],
        row.get("a"),
        row["home"]
    );
}

fn run(name: &str, test: &str) -> Vec<Value> {
    let all = jsonl(name);
    let users = users(&all[0]);
    let mut ledger = Ledger::new(test, name);
    for (i, row) in all[1..].iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check(row, &users));
    }
    assert_eq!(all[0]["cases"], json!(all.len() - 1), "{name} header");
    ledger.finish(all.len() - 1);
    all
}

#[test]
fn verify_candidates_scan_every_case() {
    let all = run(
        "verify_candidates_scan.jsonl",
        "verify_candidates_scan_every_case",
    );
    for f in ["nfc", "scan"] {
        assert!(all.iter().any(|r| r["f"] == f));
    }
    assert!(all.iter().any(|r| r.get("sha256").is_some()));
    assert!(all
        .iter()
        .any(|r| r["r"] == json!({"raise": "RuntimeError"})));
    assert!(all.iter().any(|r| r["r"] == json!({"raise": "IndexError"})));
}

#[test]
fn verify_candidates_scan_unicode_every_case() {
    let all = run(
        "verify_candidates_scan_unicode.jsonl",
        "verify_candidates_scan_unicode_every_case",
    );
    assert_eq!(all[0]["unicode"], "15.1.0");
}
