//! Every case of `origin_gitfiles_leaves.jsonl` and
//! `origin_gitfiles_leaves_python.jsonl` against `origin_gitfiles`: the C
//! path decoder, the header rewriters, `hunk_lines`, the varint, the
//! index's `.gitmodules` entries and the shortening refs.

mod common;

use bettermemory_core::origin_gitfiles::{
    decode_varint, dequote_named_header, dequote_patch_headers, gitmodules_entries, hunk_lines,
    quoted_header_path, shortening_refs, unquote_c_path, GitmodulesEntry, IndexFault, Raised,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn text(value: &Value) -> PyStr {
    match value.as_str() {
        Some(s) => unicode::cps(s),
        None => common::cps_of(value),
    }
}

fn spell(text: &[u32]) -> Value {
    match unicode::to_string(text) {
        Some(s) => json!(s),
        None => json!(text),
    }
}

fn runs(value: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for run in value.as_array().expect("runs") {
        match run.as_str() {
            Some(hex) => out.extend(common::unhex(hex)),
            None => {
                let byte = run[0].as_u64().expect("a byte") as u8;
                let n = run[1].as_u64().expect("a count") as usize;
                out.extend(std::iter::repeat_n(byte, n));
            }
        }
    }
    out
}

fn answer<T>(got: Result<T, Raised>, ok: impl Fn(T) -> Value) -> Value {
    match got {
        Ok(v) => json!(["ok", ok(v)]),
        Err(e) => json!(["raise", e.name()]),
    }
}

fn index_answer<T>(got: Result<T, IndexFault>, ok: impl Fn(T) -> Value) -> Value {
    match got {
        Ok(v) => json!(["ok", ok(v)]),
        Err(e) => json!(["raise", e.name()]),
    }
}

fn opt(v: Option<PyStr>) -> Value {
    v.map_or(Value::Null, |t| spell(&t))
}

fn entries(v: Vec<GitmodulesEntry>) -> Value {
    json!(v
        .into_iter()
        .map(|(s, m, o)| json!([
            s,
            m,
            o.iter().map(|b| format!("{b:02x}")).collect::<String>()
        ]))
        .collect::<Vec<_>>())
}

fn check(file: &str, test: &str) {
    let lines = jsonl(file);
    let header = &lines[0];
    let bases: Vec<Vec<u8>> = header
        .get("bases")
        .and_then(Value::as_array)
        .map(|b| b.iter().map(runs).collect())
        .unwrap_or_default();
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut counts = std::collections::BTreeMap::<String, u64>::new();
    let mut raised = 0u64;
    for (i, row) in lines[1..].iter().enumerate() {
        let row = row.as_array().expect("a row");
        let kind = row[0].as_str().expect("a kind");
        *counts.entry(kind.into()).or_default() += 1;
        let got = match kind {
            "unq" => answer(unquote_c_path(&text(&row[1])), opt),
            "qhp" => answer(quoted_header_path(&text(&row[1])), opt),
            "dnh" => answer(dequote_named_header(&text(&row[1])), opt),
            "dph" => answer(dequote_patch_headers(&text(&row[1])), |t| spell(&t)),
            "hunk" => answer(hunk_lines(&text(&row[1])), |v| json!(v)),
            "short" => answer(Ok(shortening_refs(&text(&row[1]))), |v| {
                json!(v.iter().map(|t| spell(t)).collect::<Vec<_>>())
            }),
            "var" => {
                let data = common::unhex(row[1].as_str().expect("hex"));
                let pos = row[2].as_u64().expect("pos") as usize;
                index_answer(decode_varint(&data, pos, row[3] == 1), |(v, p)| {
                    json!([v.to_string(), p])
                })
            }
            "gm" => index_answer(gitmodules_entries(&runs(&row[1]), row[2] == 1), entries),
            "gmcut" => {
                let base = &bases[row[1].as_u64().expect("base") as usize];
                let cut = row[2].as_u64().expect("cut") as usize;
                index_answer(gitmodules_entries(&base[..cut], row[3] == 1), entries)
            }
            other => panic!("no kind {other}"),
        };
        let want = row.last().expect("an answer");
        raised += u64::from(want[0] == "raise");
        if &got != want {
            failures.push(format!("#{i} {kind}: native {got} reference {want}"));
        }
    }
    let mut total = 0u64;
    for kind in [
        "unq", "qhp", "dnh", "dph", "hunk", "var", "gm", "gmcut", "short",
    ] {
        let n = counts.get(kind).copied().unwrap_or(0);
        assert_eq!(header[kind].as_u64(), Some(n), "{kind} count");
        total += n;
    }
    assert_eq!(total, (lines.len() - 1) as u64);
    assert!(total > 0, "no case checked");
    assert_eq!(header["raised"].as_u64(), Some(raised));
    ledger.settle(total as usize, &failures);
}

#[test]
fn origin_gitfiles_leaves_every_case() {
    check("origin_gitfiles_leaves.jsonl", "golden_origin_gitfiles");
}

#[test]
fn origin_gitfiles_leaves_python_every_case() {
    check(
        "origin_gitfiles_leaves_python.jsonl",
        "golden_origin_gitfiles",
    );
}
