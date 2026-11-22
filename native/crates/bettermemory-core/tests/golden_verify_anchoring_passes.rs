//! Every case of `verify_anchoring_passes.jsonl` (not gated) and
//! `verify_anchoring_passes_unicode.jsonl` (the Unicode release) against
//! `verify_anchoring::passes`, over the scripted host and the closure
//! rules `gen_verify_anchoring_passes` describes.

mod common;

use bettermemory_core::attestation_paths::{path_mode, Paths};
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::attest::pure_text;
use bettermemory_core::verify_anchoring::passes::{
    attestation_read, is_checkable_citation, relative_attestation_keys,
};
use common::attestation_paths::{cps, spell, spell_bool, spell_list, spell_str, text, Scripted};
use common::parity::Ledger;
use common::verify_anchoring_attest::paths;
use common::verify_anchoring_passes::{buckets, log_json, run, Logged, Pass, Stub};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const VP: [&str; 26] = [
    "src/ok.py",
    "src/gone.py",
    "SRC/OK.py",
    "/r/ok/a.py",
    "/r/gone/b.py",
    "/hq/gone",
    "/un/x",
    "/ab/r/ok",
    "/ab/r/gone",
    "/ab/else/gone",
    "~/gone.py",
    "$HOME/ok",
    "C:\\r\\ok.py",
    "../up.py",
    "src/../../x.py",
    "//srv/x",
    "a//b.py",
    "",
    " ",
    "sp/gone.py",
    "nod/gone.py",
    "zz/x.py",
    "/path/to/x",
    "src/*.py",
    "~z/x",
    "/r/sp/q",
];
const AP: [&str; 10] = [
    "src/gone.py",
    "src/ok.py",
    "absent.py",
    "/r/ok/a.py",
    "/hq/ok",
    "sp/x",
    "SRC/GONE.PY",
    "/ab/r/absent",
    "/un/y",
    "",
];

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| cps(s.as_str().unwrap()))
        .collect()
}

fn consts(items: &[&str]) -> Vec<PyStr> {
    items.iter().map(|s| cps(s)).collect()
}

fn list_of(row: &Value, key: &str, default: &[&str]) -> Vec<PyStr> {
    if row.get(key).is_some() {
        texts(&row[key])
    } else {
        consts(default)
    }
}

fn spans(v: &Value) -> Vec<(usize, usize)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| {
            (
                s[0].as_u64().unwrap() as usize,
                s[1].as_u64().unwrap() as usize,
            )
        })
        .collect()
}

fn flavour(nt: bool) -> Flavour {
    if nt {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

fn check(row: &Value) {
    let nt = row["nt"].as_bool().unwrap_or(false);
    let host = Scripted { nt };
    let kind = row["kind"].as_str().unwrap_or("P");
    let vp: Paths = paths(kind, &list_of(row, "vp", &VP));
    let ap: Paths = paths(kind, &list_of(row, "ap", &AP));
    let pre = row.get("pre").map(texts).unwrap_or_default();
    let outcome = |r, out, reads: Logged| {
        assert_eq!(json!(text(&spell(r, |()| cps("N")))), row["x"]);
        assert_eq!(buckets(&out), row["o"]);
        assert_eq!(log_json(&reads.log), row["c"]);
    };
    match row["k"].as_str().unwrap() {
        "ic" => {
            let got: Vec<String> = texts(&row["s"])
                .iter()
                .map(|s| text(&spell_bool(is_checkable_citation(s))))
                .collect();
            assert_eq!(json!(got), row["o"]);
        }
        "rk" => {
            let root = cps(row["root"].as_str().unwrap());
            let mut reads = Logged::default();
            let got = relative_attestation_keys(&host, &mut reads, &root, &vp, &ap);
            let got = spell(got, |set| {
                let mut v: Vec<PyStr> = set.into_iter().collect();
                v.sort();
                spell_list(&v)
            });
            assert_eq!(json!(text(&got)), row["o"]);
            assert_eq!(log_json(&reads.log), row["c"]);
        }
        "at" => {
            let stub = Stub::from_json(&row["a"]);
            let root = stub
                .root
                .as_ref()
                .map(|r| pure_text(flavour(nt), r).unwrap());
            let mut reads = Logged::default();
            let got: Vec<String> = vp
                .items
                .iter()
                .map(|s| {
                    let r = attestation_read(
                        &host,
                        &mut reads,
                        &stub,
                        root.as_deref(),
                        s,
                        path_mode(&vp),
                    );
                    text(&spell(r, |f| match f {
                        None => cps("N"),
                        Some(f) => {
                            let parts = [
                                spell_str(&f.path),
                                spell_str(&cps(&f.rule)),
                                spell_bool(f.convicting),
                                spell_bool(f.readable),
                            ];
                            let mut out = cps("L");
                            out.extend(parts.join(&0x1d));
                            out
                        }
                    }))
                })
                .collect();
            assert_eq!(json!(got), row["o"]);
            assert_eq!(log_json(&reads.log), row["c"]);
        }
        "ra" => {
            let stub = Stub::from_json(&row["a"]);
            let (r, out, reads) = run(nt, Pass::ReadAttestations(&stub, &vp, &ap), &[], &pre);
            outcome(r, out, reads);
        }
        "ca" => {
            let root = row["root"].as_str().map(cps);
            let (r, out, reads) = run(
                nt,
                Pass::CheckAttestations(root.as_deref(), &vp, &ap),
                &[],
                &pre,
            );
            outcome(r, out, reads);
        }
        "rc" => {
            let stub = Stub::from_json(&row["a"]);
            let body = cps(row["body"].as_str().unwrap());
            let m = spans(&row["m"]);
            let (r, out, reads) = run(nt, Pass::ReadCitations(&stub, &body), &m, &pre);
            outcome(r, out, reads);
        }
        "cc" => {
            let root = pure_text(flavour(nt), &cps(row["root"].as_str().unwrap())).unwrap();
            let body = cps(row["body"].as_str().unwrap());
            let m = spans(&row["m"]);
            let (r, out, reads) = run(nt, Pass::CheckCitations(&root, &body), &m, &pre);
            outcome(r, out, reads);
        }
        k => panic!("unknown row kind {k}"),
    }
}

#[test]
fn verify_anchoring_passes_every_case() {
    let file = "verify_anchoring_passes.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new("verify_anchoring_passes_every_case", file);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check(row));
    }
    ledger.finish(rows.len());
}

#[test]
fn verify_anchoring_passes_unicode_sweep() {
    let file = "verify_anchoring_passes_unicode.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert_eq!(rows.len(), 2);
    let mut ledger = Ledger::new("verify_anchoring_passes_unicode_sweep", file);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("sweep {i}"), || {
            let head = cps(row["head"].as_str().unwrap());
            let tail = cps(row["tail"].as_str().unwrap());
            let mut h = Sha256::new();
            let mut n = 0u64;
            for c in 0..0x11_0000u32 {
                let mut s = head.clone();
                s.push(c);
                s.extend_from_slice(&tail);
                h.update(if is_checkable_citation(&s) {
                    b"T"
                } else {
                    b"F"
                });
                n += 1;
            }
            assert_eq!(n, row["n"].as_u64().unwrap());
            assert_eq!(common::hex(&h.finalize()), row["sha"].as_str().unwrap());
        });
    }
    ledger.finish(rows.len());
}
