//! Every case of `recall_render.jsonl` (not gated) and
//! `recall_render_unicode.jsonl` (gated on the Unicode version) against
//! the native `recall` (`words` and `render`). The spelling of texts is
//! the generator's (`tests/contract/gen_recall_render.py`).

mod common;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::recall::render::{self, RecallPointer};
use bettermemory_core::recall::words::{self, WordsSeen};
use bettermemory_core::recall::{self as rc};
use bettermemory_core::unicode::{self, cps};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn text(r: &Value, name: &str) -> PyStr {
    if let Some(runs) = r.get(format!("{name}_runs")) {
        let mut out = Vec::new();
        for run in runs.as_array().unwrap() {
            let piece = match &run[0] {
                Value::String(s) => cps(s),
                other => common::cps_of(other),
            };
            for _ in 0..run[1].as_u64().unwrap() {
                out.extend_from_slice(&piece);
            }
        }
        return out;
    }
    common::text_field(r, name)
}

fn opt(r: &Value, name: &str) -> Option<PyStr> {
    match r.get(name) {
        Some(Value::Null) => None,
        _ => Some(text(r, name)),
    }
}

fn pointer(p: &Value) -> RecallPointer {
    RecallPointer {
        id: text(p, "id"),
        summary: text(p, "summary"),
        snippet: text(p, "snippet"),
        provenance: opt(p, "provenance"),
        remote_stamp: p["remote_stamp"].as_bool().unwrap(),
        trust_unavailable: p["trust_unavailable"].as_bool().unwrap(),
        disputed: opt(p, "disputed"),
    }
}

fn s(v: &Value) -> PyStr {
    cps(v.as_str().unwrap())
}

fn check_words(r: &Value) {
    let seen = WordsSeen::new();
    let mut out = Vec::new();
    for op in r["ops"].as_array().unwrap() {
        match op[0].as_str().unwrap() {
            "remember" => seen.remember(&s(&op[1]), &s(&op[2]), &s(&op[3])),
            "fill" => {
                for i in 0..op[3].as_u64().unwrap() {
                    let ck = cps(&format!("{}{i}", op[2].as_str().unwrap()));
                    seen.remember(&s(&op[1]), &ck, &s(&op[4]));
                }
            }
            "seen" => out.push(json!(seen.seen(&s(&op[1]), &s(&op[2])))),
            "holds" => out.push(json!(seen.holds(&s(&op[1]), &s(&op[2]), &s(&op[3])))),
            "clear" => seen.clear(),
            other => panic!("unknown op {other}"),
        }
    }
    assert_eq!(Value::Array(out), r["o"]);
}

fn check_line(r: &Value) {
    let p = pointer(&r["p"]);
    let snip = match &r["s"] {
        Value::Null => None,
        v => Some(text(v, "t")),
    };
    assert_eq!(render::annotation(&p), text(&r["ann"], "t"), "annotation");
    assert_eq!(
        render::recall_line(&p, snip.as_deref()),
        text(&r["line"], "t"),
        "line"
    );
    let (kept, ex) = render::one_excerpt(&p, snip.as_deref());
    assert_eq!(kept.summary, text(&r["ex"][0], "t"), "excerpt summary");
    let want = match &r["ex"][1] {
        Value::Null => None,
        v => Some(text(v, "t")),
    };
    assert_eq!(ex, want, "excerpt snippet");
    assert_eq!(kept.id, p.id);
}

fn sweep() -> (u64, String) {
    let mut digest = Sha256::new();
    let mut count = 0;
    for c in 0..0x110000u32 {
        let fold: Vec<String> = words::fold(&[c]).iter().map(|x| format!("{x:x}")).collect();
        let a = words::bounded(&cps("x"), &[c, u32::from(b'x')]).unwrap();
        let b = words::bounded(&cps("x"), &[u32::from(b'x'), c]).unwrap();
        digest.update(format!(
            "{c:x}:{}:{}:{}\n",
            fold.join(","),
            a as u8,
            b as u8
        ));
        count += 1;
    }
    (count, common::hex(&digest.finalize()))
}

fn check(r: &Value) {
    match r["k"].as_str().unwrap() {
        "fold" => assert_eq!(words::fold(&text(r, "t")), text(r, "o")),
        "bounded" => {
            let got = match words::bounded(&text(r, "w"), &text(r, "t")) {
                Some(b) => json!(b),
                None => json!("IndexError"),
            };
            assert_eq!(got, r["o"]);
        }
        "summarize" => assert_eq!(
            render::summarize(&text(r, "t"), r["n"].as_i64().unwrap()),
            text(r, "o")
        ),
        "marks" => {
            let pairs: Vec<(PyStr, PyStr, PyStr)> = r["pairs"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| (s(&p[0]), s(&p[1]), s(&p[2])))
                .collect();
            let got: Vec<Value> = render::disputed_marks(&pairs)
                .iter()
                .map(|(m, k)| json!([unicode::to_string(m), unicode::to_string(k)]))
                .collect();
            assert_eq!(Value::Array(got), r["o"]);
        }
        "core" => assert_eq!(render::core(&text(r, "t")), &text(r, "o")[..]),
        "line" => check_line(r),
        "render" => {
            let ps: Vec<RecallPointer> = r["ps"].as_array().unwrap().iter().map(pointer).collect();
            let (block, ids) = render::render_recall_block(&ps);
            assert_eq!(block, text(r, "o"), "block");
            let want: Vec<PyStr> = r["ids"].as_array().unwrap().iter().map(s).collect();
            assert_eq!(ids, want, "ids");
        }
        "words" => check_words(r),
        "sweep" => {
            let (count, sha) = sweep();
            assert_eq!(count, r["count"].as_u64().unwrap(), "sweep count");
            assert_eq!(sha, r["sha"].as_str().unwrap(), "sweep digest");
        }
        other => panic!("unknown row kind {other}"),
    }
}

fn run(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let header = &lines[0];
    let rows = &lines[1..];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, rows.len());
    if header["k"] == "consts" {
        assert_eq!(header["max_pointers"], rc::RECALL_MAX_POINTERS);
        assert_eq!(header["cap"], rc::RECALL_CAP_CHARS);
        assert_eq!(header["snippet"], rc::SNIPPET_CHARS);
        assert_eq!(header["min_snippet"], rc::MIN_SNIPPET_CHARS);
        assert_eq!(header["per_checkout"], rc::WORDS_PER_CHECKOUT);
        assert_eq!(header["checkouts"], rc::WORDS_CHECKOUTS);
        assert_eq!(header["min_quote"], rc::MIN_QUOTE_CHARS);
        assert_eq!(header["header"], rc::RECALL_HEADER);
        let mut kinds = rc::SEEN_KINDS.to_vec();
        kinds.sort_unstable();
        assert_eq!(header["seen_kinds"], json!(kinds));
    }
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, r), || check(r));
    }
    ledger.finish(rows.len());
}

#[test]
fn recall_render_every_case() {
    run("recall_render_every_case", "recall_render.jsonl");
}

#[test]
fn recall_render_unicode_every_case() {
    run(
        "recall_render_unicode_every_case",
        "recall_render_unicode.jsonl",
    );
}
