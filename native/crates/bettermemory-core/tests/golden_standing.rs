//! Every case of `standing.jsonl` against `standing`: the constants in the
//! header and every branch of `asking`, `is_standing`,
//! `check_probability`, `answered_by`, `answer_of`, `record_text`,
//! `backlog_line`, `held_lines`, `held_line`, `question` and `batch_hint`.

mod common;

use bettermemory_core::float_repr;
use bettermemory_core::identity::Actor;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::standing::*;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn cps(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("text"))
}

fn text(t: &[u32]) -> Value {
    json!(unicode::to_string(t).expect("text"))
}

fn float(f: f64) -> Value {
    json!({"t": "float", "bits": format!("{:016x}", f.to_bits()), "repr": float_repr::repr(f)})
}

fn err(e: StandingError) -> Value {
    json!({"e": e.class.name(), "m": e.text})
}

fn actor(v: &Value) -> Option<Actor> {
    if v.is_null() {
        return None;
    }
    let part = |x: &Value| x.as_str().map(unicode::cps);
    Some(Actor {
        client: part(&v[0]),
        client_version: None,
        model: part(&v[1]),
        principal: None,
        session: None,
        sources: Vec::new(),
    })
}

fn ids(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("ids").iter().map(cps).collect()
}

fn answer(case: &Value) -> Value {
    match case["k"].as_str().expect("k") {
        "asking" => json!(asking(case["tau"].as_bool().expect("tau"))),
        "is" => {
            let c = case["c"].as_str().map(unicode::cps);
            let p = match &case["p"] {
                Value::Null => None,
                v => match common::neutral(v) {
                    PyValue::Float(f) => Some(f),
                    other => panic!("p {other:?}"),
                },
            };
            json!(is_standing(c.as_deref(), p))
        }
        "cp" => {
            let name = case["n"].as_str().expect("n");
            match check_probability(&common::neutral(&case["v"]), name) {
                Ok(f) => float(f),
                Err(e) => err(e),
            }
        }
        "by" => {
            let a = actor(&case["a"]);
            text(&answered_by(a.as_ref()))
        }
        "ans" => {
            let a = actor(&case["a"]);
            match answer_of(&common::neutral(&case["v"]), &cps(&case["s"]), a.as_ref()) {
                Ok(ans) => {
                    let PyValue::Float(p) = &ans.probability else {
                        panic!("probability not a float")
                    };
                    let (PyValue::Str(s), PyValue::Str(b)) = (&ans.source, &ans.answered_by) else {
                        panic!("source or giver not a str")
                    };
                    json!({"p": float(*p), "s": text(s), "b": text(b)})
                }
                Err(e) => err(e),
            }
        }
        "rt" => text(&record_text(&cps(&case["b"]))),
        "bl" => json!(backlog_line(
            case["n"].as_str().expect("n").parse().expect("count")
        )),
        "hls" => json!(held_lines(&ids(&case["ids"]))
            .iter()
            .map(|l| text(l))
            .collect::<Vec<_>>()),
        "hl" => text(&held_line(&ids(&case["ids"]))),
        "q" => {
            let q = question(&cps(&case["id"]));
            let PyValue::Dict(items) = &q else {
                panic!("question not a dict")
            };
            json!(items
                .iter()
                .map(|(k, v)| {
                    let (PyValue::Str(k), PyValue::Str(v)) = (k, v) else {
                        panic!("not str")
                    };
                    json!([text(k), text(v)])
                })
                .collect::<Vec<_>>())
        }
        "bh" => json!(batch_hint(i128::from(case["n"].as_i64().expect("n")))),
        other => panic!("unknown kind {other}"),
    }
}

#[test]
fn standing_every_case() {
    let name = "standing.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    assert_eq!(header["tau"], json!(TAU_STANDING));
    assert_eq!(header["sources"], json!(SOURCES));
    assert_eq!(header["batch"], json!(BATCH));
    assert_eq!(header["call"], json!(CALL));
    assert_eq!(header["format"], json!(ANSWER_FORMAT));
    assert_eq!(header["related_cap"], json!(RELATED_CAP));
    assert_eq!(header["related_text_chars"], json!(RELATED_TEXT_CHARS));
    assert_eq!(header["statement"], json!(STANDING_STATEMENT));
    let in_file = header["cases"].as_u64().expect("cases") as usize;
    assert_eq!(in_file, lines.len() - 1);
    let mut ledger = Ledger::new("standing_every_case", name);
    for (i, case) in lines[1..].iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            let mut got = case.clone();
            got["r"] = answer(case);
            assert_eq!(got, *case, "{name} case {i}");
        });
    }
    ledger.finish(in_file);
}
