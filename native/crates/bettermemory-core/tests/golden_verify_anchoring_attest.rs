//! Every case of `verify_anchoring_attest.jsonl` (not gated) and
//! `verify_anchoring_attest_unicode.jsonl` (the Unicode release) against
//! `verify_anchoring::attest`, over the scripted host and the closure
//! rules `gen_verify_anchoring_attest` describes.

mod common;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::attest::{
    is_absolute_attestation, join_under, normalize_attestations,
};
use common::attestation_paths::{cps, spell, spell_bool, spell_list, spell_opt, text, Scripted};
use common::parity::Ledger;
use common::verify_anchoring_attest::{gate, keys, paths, Stub};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| cps(s.as_str().unwrap()))
        .collect()
}

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn log_json(log: &[PyStr]) -> Value {
    json!(log.iter().map(|e| text(e)).collect::<Vec<_>>())
}

fn check(row: &Value, items: &[PyStr], na_items: &[PyStr]) {
    let nt = row["nt"].as_bool().unwrap_or(false);
    match row["k"].as_str().unwrap() {
        "abs" => {
            let got: Vec<String> = texts(&row["s"])
                .iter()
                .map(|s| text(&spell_bool(is_absolute_attestation(s))))
                .collect();
            assert_eq!(json!(got), row["o"]);
        }
        "join" => {
            let flavour = if nt { Flavour::Windows } else { Flavour::Posix };
            let root = cps(row["root"].as_str().unwrap());
            let rels = texts(&json!([
                "a.py",
                "src/a.py",
                "./a.py",
                "a/../b.py",
                "../x.py",
                "a/../../x.py",
                "/abs.py",
                "\\abs.py",
                "C:x.py",
                "c:/x.py",
                "a//b.py",
                "",
                "x/",
                "*.py",
                "/path/to/x",
                "path/to/x",
                "a\\b.py",
                "~/x.py"
            ]));
            let got: Vec<String> = rels
                .iter()
                .map(|rel| text(&spell(join_under(flavour, &root, rel), |v| spell_opt(&v))))
                .collect();
            assert_eq!(json!(got), row["o"]);
        }
        "uv" => {
            let p = paths(row["kind"].as_str().unwrap(), items);
            let root = opt(&row["root"]);
            let (out, log) = gate(nt, &p, root.as_deref(), None, false);
            assert_eq!(json!(text(&out)), row["o"]);
            assert_eq!(log_json(&log), row["c"]);
        }
        "at" => {
            let p = paths(row["kind"].as_str().unwrap(), items);
            let a = &row["a"];
            let stub = Stub {
                root: opt(&a[0]),
                convicting: a[1].as_bool().unwrap(),
                declared: opt(&a[2]),
            };
            let stored = row["stored"].as_bool().unwrap();
            let (out, log) = gate(nt, &p, None, Some(&stub), stored);
            assert_eq!(json!(text(&out)), row["o"]);
            assert_eq!(log_json(&log), row["c"]);
        }
        "na" => {
            assert_eq!(texts(&row["s"]), na_items);
            let p = paths(row["kind"].as_str().unwrap(), na_items);
            let host = Scripted { nt };
            let out = spell(normalize_attestations(&host, &p, &keys), |s| {
                spell_list(&s.into_iter().collect::<Vec<_>>())
            });
            assert_eq!(json!(text(&out)), row["o"]);
        }
        k => panic!("unknown row kind {k}"),
    }
}

pub fn uv_items() -> Vec<PyStr> {
    texts(&json!([
        "/ok/a",
        "/gone/a",
        "ok.py",
        "gone.py",
        "/path/to/file",
        "src/../../x",
        "$HOME/ok",
        "${HOME}/gone",
        "~/gone",
        "~/ok",
        "C:\\ok\\a",
        "C:/gone/x",
        "  /gone/b  ",
        " ",
        "",
        "sp/gone",
        "/sp/gone",
        "/var/log/*.log",
        "/hq/gone",
        "/un/gone",
        "/hq/ok",
        "a\\gone",
        "/healthz"
    ]))
}

#[test]
fn verify_anchoring_attest_every_case() {
    let file = "verify_anchoring_attest.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let items = uv_items();
    let mut na_items = items.clone();
    na_items.extend(texts(&json!(["/zz/a", "/a/k", "/a/kk", "~/k", "rel/k"])));
    let mut ledger = Ledger::new("verify_anchoring_attest_every_case", file);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check(row, &items, &na_items));
    }
    ledger.finish(rows.len());
}

#[test]
fn verify_anchoring_attest_unicode_sweep() {
    let file = "verify_anchoring_attest_unicode.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert_eq!(rows.len(), 1);
    let mut ledger = Ledger::new("verify_anchoring_attest_unicode_sweep", file);
    let row = &rows[0];
    ledger.case("sweep".to_owned(), || {
        let tail = cps(row["tail"].as_str().unwrap());
        let mut h = Sha256::new();
        let mut n = 0u64;
        for c in 0..0x11_0000u32 {
            let mut s = vec![c];
            s.extend_from_slice(&tail);
            h.update(if is_absolute_attestation(&s) {
                b"T"
            } else {
                b"F"
            });
            n += 1;
        }
        assert_eq!(n, row["n"].as_u64().unwrap());
        assert_eq!(common::hex(&h.finalize()), row["sha"].as_str().unwrap());
    });
    ledger.finish(1);
}
