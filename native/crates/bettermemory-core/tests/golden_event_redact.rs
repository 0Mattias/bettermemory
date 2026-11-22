//! Every case of `event_redact.jsonl` (not gated) against the native
//! `event_redact`, `store::rows::event_import_payload` and `time_utils`.
//! The spelling is the generator's (`tests/contract/gen_event_redact.py`):
//! each row's `sp` holds the reference's calls to
//! `credentials.secret_spans`, which the test answers through the `spans`
//! argument.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::event_redact as er;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::rows;
use bettermemory_core::time_utils;
use bettermemory_core::unicode::cps;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

type Call = (PyStr, Vec<(usize, usize, PyStr)>);

fn calls(r: &Value) -> Vec<Call> {
    r.as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let spans = c[1]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| {
                    let n = |i: usize| s[i].as_u64().unwrap() as usize;
                    (n(0), n(1), cps(s[2].as_str().unwrap()))
                })
                .collect();
            (common::text_field(&c[0], "t"), spans)
        })
        .collect()
}

/// A `secret_spans` that answers the recorded calls by their text and
/// counts them.
fn recorded<'a>(
    want: &'a [Call],
    made: &'a mut usize,
) -> impl FnMut(&[u32]) -> Vec<(usize, usize, PyStr)> + 'a {
    move |text: &[u32]| {
        *made += 1;
        let (_, spans) = want
            .iter()
            .find(|(t, _)| t == text)
            .unwrap_or_else(|| panic!("an unrecorded call: {text:?}"));
        spans.clone()
    }
}

fn items(value: PyValue) -> Vec<(PyValue, PyValue)> {
    match value {
        PyValue::Dict(ref items) => items.clone(),
        other => panic!("not a dict: {other:?}"),
    }
}

fn dt(r: &Value) -> Option<DateTime> {
    r["d"]
        .as_str()
        .map(|s| DateTime::from_isoformat(&cps(s)).unwrap())
}

const PIECES: [&str; 18] = [
    "sk-",
    "ant-",
    "ghp_",
    "github_pat_",
    "AKIA",
    "ABCD1234",
    "abcdefghij",
    "0123456789",
    "_",
    "-",
    " ",
    ".",
    "SURROGATE",
    "\u{e9}",
    "\u{660}",
    "<|im_start|>",
    "x",
    "Z",
];

fn piece(i: usize) -> PyStr {
    match PIECES[i] {
        "SURROGATE" => vec![0xD800],
        p => cps(p),
    }
}

fn lcg(state: &mut u64) -> u64 {
    *state = state
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407);
    *state >> 33
}

fn sweep(r: &Value) {
    let mut state = r["seed"].as_u64().unwrap();
    let n = r["n"].as_u64().unwrap() as usize;
    let recorded: Vec<(usize, Call)> = r["sp"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let i = c[0].as_u64().unwrap() as usize;
            let rest = json!([[c[1], c[2]]]);
            (i, calls(&rest).remove(0))
        })
        .collect();
    let mut lines = String::new();
    for i in 0..n {
        let count = lcg(&mut state) % 12;
        let text: PyStr = (0..count)
            .flat_map(|_| piece((lcg(&mut state) % PIECES.len() as u64) as usize))
            .collect();
        let want = recorded.iter().find(|(j, _)| *j == i).map(|(_, c)| c);
        let out = er::redact_query(&text, &mut |t: &[u32]| match want {
            Some((wt, spans)) => {
                assert_eq!(wt, t, "sweep case {i}");
                spans.clone()
            }
            None => Vec::new(),
        });
        let out = items(out);
        let PyValue::Str(hash) = &out[0].1 else {
            panic!()
        };
        let PyValue::Str(preview) = &out[1].1 else {
            panic!()
        };
        let PyValue::Int(len) = &out[2].1 else {
            panic!()
        };
        let preview: Vec<String> = preview.iter().map(u32::to_string).collect();
        lines += &format!(
            "{} {} {}\n",
            String::from_utf8(hash.iter().map(|&c| c as u8).collect()).unwrap(),
            preview.join(","),
            len
        );
    }
    let sha: String = Sha256::digest(lines.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(json!(sha), r["sha"], "sweep digest over {n} cases");
}

fn check_header(h: &Value) {
    let named: Vec<Value> = er::NAMED_EVENT_FIELDS
        .iter()
        .map(|(n, keys)| json!([n, keys]))
        .collect();
    assert_eq!(h["named_event_fields"], json!(named));
    let pats: Vec<Value> = er::SECRET_PATTERNS
        .iter()
        .map(|(p, m)| json!([p, 32, m]))
        .collect();
    assert_eq!(h["secret_patterns"], json!(pats));
    assert_eq!(h["imported_from_v8"], json!(rows::IMPORTED_FROM_V8));
    assert_eq!(h["row_columns"], json!(rows::ROW_COLUMNS));
    let pieces: Vec<PyStr> = (0..PIECES.len()).map(piece).collect();
    let want: Vec<PyStr> = h["pieces"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| common::text_field(p, "t"))
        .collect();
    assert_eq!(pieces, want);
}

fn check_row(r: &Value) {
    match r["k"].as_str().unwrap() {
        "header" => return check_header(r),
        "sweep" => return sweep(r),
        _ => {}
    }
    let want = if r.get("sp").is_some() {
        calls(&r["sp"])
    } else {
        Vec::new()
    };
    let mut made = 0;
    match r["k"].as_str().unwrap() {
        "rq" => {
            let text = common::text_field(r, "t");
            let out = er::redact_query(&text, &mut recorded(&want, &mut made));
            assert!(out == common::neutral(&r["o"]), "redact_query {r}");
        }
        "nf" => {
            let out = er::named_event_fields(&items(common::neutral(&r["f"])));
            assert!(PyValue::Dict(out) == common::neutral(&r["o"]), "named {r}");
        }
        "rf" => {
            let f = items(common::neutral(&r["f"]));
            let out = er::redact_event_fields(&f, &mut recorded(&want, &mut made));
            assert!(
                PyValue::Dict(out) == common::neutral(&r["o"]),
                "redact fields {r}"
            );
        }
        "imp" => {
            let f = items(common::neutral(&r["f"]));
            let from = cps(r["from"].as_str().unwrap_or(rows::IMPORTED_FROM_V8));
            let got = rows::event_import_payload(&f, &from, &mut recorded(&want, &mut made));
            match got {
                Ok((ts, session, kind, payload)) => {
                    let o = r["o"].as_array().expect("a result");
                    let opt = |v: Option<PyStr>| v.map_or(PyValue::None, PyValue::Str);
                    assert!(opt(ts) == common::neutral(&o[0]), "ts {r}");
                    assert!(opt(session) == common::neutral(&o[1]), "session {r}");
                    assert!(PyValue::Str(kind) == common::neutral(&o[2]), "kind {r}");
                    assert!(
                        PyValue::Dict(payload) == common::neutral(&o[3]),
                        "payload {r}"
                    );
                }
                Err(text) => assert_eq!(common::str_output(&text), {
                    let e = r["o"]["err"].as_str().expect("an error");
                    common::str_output(&cps(e))
                }),
            }
        }
        "iso" => {
            let got = time_utils::isoformat_utc_optional(dt(r).as_ref());
            assert_eq!(json!(got), r["o"], "{r}");
        }
        "eu" => {
            let got = time_utils::ensure_utc(dt(r)).map(|d| d.isoformat());
            assert_eq!(json!(got), r["o"], "{r}");
        }
        other => panic!("unknown row kind {other}"),
    }
    assert_eq!(made, want.len(), "secret_spans calls {r}");
}

#[test]
fn event_redact_every_case() {
    let lines = common::jsonl("event_redact.jsonl");
    for r in &lines {
        check_row(r);
    }
    common::assert_count("event_redact.jsonl", lines.len(), lines.len());
    assert!(lines.len() > 100);
}

/// `state_fields` with `redact_query` passed as its redactor, the way the
/// recorder calls it. The answer is the reference's
/// (`events.state_fields("turn_audited", ...)` under 3.13.13).
#[test]
fn state_fields_takes_redact_query() {
    let mut spans = |_: &[u32]| Vec::new();
    let text = PyValue::str("a sk-ant-aaaaaaaaaaaaaaaaaaaa");
    let fields = vec![(PyValue::str("probe_query"), text)];
    let kept = bettermemory_core::events::state_fields(&cps("turn_audited"), &fields, &mut |t| {
        er::redact_query(t, &mut spans)
    })
    .unwrap();
    let want = PyValue::Dict(vec![
        (PyValue::str("hash"), PyValue::str("a9f7d63a84396312")),
        (PyValue::str("len"), PyValue::int(29)),
    ]);
    assert_eq!(kept, vec![(PyValue::str("probe_query"), want)]);
}
