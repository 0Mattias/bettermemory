//! Every case of `claims.jsonl` (not gated), `claims_unicode.jsonl` (gated
//! on the Unicode version) and `claims_python.jsonl` (gated on the Python
//! release) against `claims`. The spelling of texts and outputs is the
//! generator's (`tests/contract/gen_claims.py`).

mod common;

use bettermemory_core::claims::{self, Binding, Claim, Kind};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A text as the generator spells it: a string, a code point list, or a
/// recipe `{"r": [[text, n], ...]}`.
fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => s.chars().map(u32::from).collect(),
        Value::Array(items) => items
            .iter()
            .map(|c| c.as_u64().expect("cp") as u32)
            .collect(),
        Value::Object(_) => {
            let mut out = Vec::new();
            for pair in v["r"].as_array().expect("a recipe") {
                let piece = text(&pair[0]);
                for _ in 0..pair[1].as_u64().expect("a count") {
                    out.extend_from_slice(&piece);
                }
            }
            out
        }
        other => panic!("not a text: {other:?}"),
    }
}

/// The generator's `_t`.
fn t(text: &[u32]) -> Value {
    if text
        .iter()
        .any(|&c| (0xD800..=0xDFFF).contains(&c) || c == 0x5C)
    {
        return json!(text);
    }
    json!(text
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect::<String>())
}

fn texts(v: &[Vec<u32>]) -> Value {
    Value::Array(v.iter().map(|x| t(x)).collect())
}

fn kind(name: &str) -> Kind {
    match name {
        "path" => Kind::Path,
        "symbol" => Kind::Symbol,
        "literal" => Kind::Literal,
        "absent" => Kind::Absent,
        other => panic!("no kind {other}"),
    }
}

fn token(b: Option<(Binding, Vec<u32>)>) -> Value {
    match b {
        None => Value::Null,
        Some((Binding::Def, n)) => json!(["def", t(&n)]),
        Some((Binding::Assign, n)) => json!(["assign", t(&n)]),
    }
}

fn hunk(line: &[u32]) -> Value {
    match claims::hunk_lines(line) {
        Err(claims::IntTooLong) => json!({"raised": "ValueError"}),
        Ok(None) => Value::Null,
        Ok(Some(n)) if n.len() > 64 => {
            json!({"n": n.len(), "h": common::hex(&Sha256::digest(n.as_bytes()))})
        }
        Ok(Some(n)) => json!(n),
    }
}

fn sorted(set: claims::PathSet) -> Vec<Vec<u32>> {
    let mut v: Vec<Vec<u32>> = set.into_iter().collect();
    v.sort();
    v
}

/// The port's answer to one row.
fn answer(f: &str, a: &[Value]) -> Value {
    let a0 = || text(&a[0]);
    match f {
        "render" => {
            let c = Claim {
                kind: kind(a[0].as_str().unwrap()),
                rel_path: text(&a[1]),
                name: text(&a[2]),
                value: text(&a[3]),
            };
            t(&c.render())
        }
        "control_character" => json!(claims::control_character(&a0())),
        "lone_surrogate" => json!(claims::lone_surrogate(&a0())),
        "is_identifier" => json!(claims::is_identifier(&a0())),
        "claim_reason_is_indeterminate" => json!(claims::claim_reason_is_indeterminate(&a0())),
        "path_key" => t(&claims::path_key(&a0())),
        "spellings_capped" => json!(claims::spellings_capped(&a0())),
        "stored_spellings" => texts(&claims::stored_spellings(&a0())),
        "claim_paths" => {
            let cs: Vec<Claim> = a[0]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| Claim {
                    kind: kind(p[0].as_str().unwrap()),
                    rel_path: text(&p[1]),
                    name: claims_cps("n"),
                    value: claims_cps("1"),
                })
                .collect();
            texts(&claims::claim_paths(&cs))
        }
        "binding_token" => token(claims::binding_token(&a0())),
        "hunk_lines" => hunk(&a0()),
        "is_mark_hash" => json!(claims::is_mark_hash(&a0())),
        "header_path" => t(&claims::header_path(&a0())),
        "patch_header_path" => {
            let list = |v: &Value| {
                v.as_array()
                    .unwrap()
                    .iter()
                    .map(text)
                    .collect::<claims::PathSet>()
            };
            let mut files = list(&a[2]);
            let mut deleted = list(&a[3]);
            let r = claims::patch_header_path(&a0(), &text(&a[1]), &mut files, &mut deleted);
            json!({
                "r": r.map_or(Value::Null, |p| t(&p)),
                "files": texts(&sorted(files)),
                "deleted": texts(&sorted(deleted)),
            })
        }
        "constant" => match a[0].as_str().unwrap() {
            "MAX_CLAIMS" => json!(claims::MAX_CLAIMS),
            "MAX_CLAIM_LEN" => json!(claims::MAX_CLAIM_LEN),
            "MAX_SPELLINGS" => json!(claims::MAX_SPELLINGS),
            "COMMIT_MARK" => t(&[claims::COMMIT_MARK]),
            "INDETERMINATE_REASON_MARKER" => t(&claims_cps(claims::INDETERMINATE_REASON_MARKER)),
            other => panic!("no constant {other}"),
        },
        other => panic!("no function {other}"),
    }
}

fn claims_cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn rows_file(test: &str, name: &str) {
    let rows = jsonl(name);
    let mut ledger = Ledger::new(test, name);
    for (i, row) in rows.iter().enumerate() {
        let f = row["f"].as_str().expect("f");
        let got = answer(f, row["a"].as_array().expect("args"));
        ledger.case(format!("{name}:{} {f}", i + 1), || {
            assert_eq!(got, row["o"], "{f} {:?}", row["a"]);
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn claims_every_case() {
    rows_file("claims_every_case", "claims.jsonl");
}

#[test]
fn claims_python_every_case() {
    rows_file("claims_python_every_case", "claims_python.jsonl");
}

fn hexes(t: &[u32]) -> String {
    t.iter()
        .map(|c| format!("{c:x}"))
        .collect::<Vec<_>>()
        .join("-")
}

fn token_answer(line: &[u32]) -> String {
    match claims::binding_token(line) {
        None => "-".into(),
        Some((Binding::Def, n)) => format!("def:{}", hexes(&n)),
        Some((Binding::Assign, n)) => format!("assign:{}", hexes(&n)),
    }
}

fn wrap(pre: &str, cp: u32, post: &str) -> Vec<u32> {
    let mut v = claims_cps(pre);
    v.push(cp);
    v.extend(claims_cps(post));
    v
}

/// The generator's `_PROBES`, one code point at a time.
fn probe(f: &str, cp: u32) -> String {
    match f {
        "is_identifier" => {
            let ok = claims::is_identifier(&wrap("a", cp, ""));
            (ok as u8).to_string()
        }
        "def" => token_answer(&wrap("def a", cp, "(")),
        "assign" => token_answer(&wrap("a", cp, " = 1")),
        "hunk" => match claims::hunk_lines(&wrap("@@ -1,", cp, " +1 @@")) {
            Ok(Some(n)) => n,
            Ok(None) => "None".into(),
            Err(_) => panic!("no digit run here is long"),
        },
        "patch" => {
            let mut line = wrap("+++ b/", cp, "x");
            line.push(cp);
            let (mut fs, mut ds) = (claims::PathSet::new(), claims::PathSet::new());
            let out = claims::patch_header_path(&line, &[], &mut fs, &mut ds).expect("a header");
            hexes(&out)
        }
        "spellings" => {
            let mut path = vec![cp];
            path.extend(claims_cps("/e\u{301}"));
            claims::stored_spellings(&path)
                .iter()
                .map(|s| hexes(s))
                .collect::<Vec<_>>()
                .join(",")
        }
        other => panic!("no probe {other}"),
    }
}

#[test]
fn claims_unicode_every_case() {
    let name = "claims_unicode.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("claims_unicode_every_case", name);
    for row in &rows {
        let f = row["f"].as_str().expect("f");
        let lo = row["lo"].as_u64().expect("lo") as u32;
        let hi = row["hi"].as_u64().expect("hi") as u32;
        let mut h = Sha256::new();
        for cp in lo..hi {
            h.update(format!("{cp:x}:{}\n", probe(f, cp)).as_bytes());
        }
        let got = common::hex(&h.finalize());
        ledger.case(format!("{f} {lo:x}..{hi:x}"), || {
            assert_eq!(got, row["h"].as_str().unwrap(), "{f} {lo:x}..{hi:x}");
        });
    }
    ledger.finish(rows.len());
}
