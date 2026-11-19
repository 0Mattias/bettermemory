//! Every case of `checkout_token.jsonl`
//! against `checkout_token`: the random text from given bytes, `valid`,
//! the lineage readers, the link, `successor` and `succeeds`, `render`,
//! `parse`, `_cut_short`, `_malformed_detail` and `token_path`.

mod common;

use bettermemory_core::checkout_token::{self as ct, Lineage, Raised};
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use common::parity::Ledger;
use common::{hex, jsonl, neutral, str_output, text_field, unhex};
use serde_json::{json, Value};

fn lin(v: &Value) -> Option<Lineage> {
    let a = v.as_array()?;
    let s = |x: &Value| match neutral(x) {
        PyValue::Str(ref s) => s.clone(),
        other => panic!("a lineage field {other:?}"),
    };
    Some(Lineage {
        token: s(&a[0]),
        device: s(&a[1]),
        inode: s(&a[2]),
    })
}

fn lin_json(l: &Option<Lineage>) -> Value {
    match l {
        None => Value::Null,
        Some(l) => json!([
            {"t": "str", "v": text(&l.token)},
            {"t": "str", "v": text(&l.device)},
            {"t": "str", "v": text(&l.inode)}
        ]),
    }
}

fn text(s: &PyStr) -> String {
    s.iter()
        .map(|&c| char::from_u32(c).expect("no surrogate"))
        .collect()
}

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn answer<T>(r: Result<T, Raised>, f: impl FnOnce(T) -> Value) -> Value {
    match r {
        Ok(v) => json!({ "r": f(v) }),
        Err(e) => json!({ "x": e.class() }),
    }
}

fn expected(row: &Value) -> Value {
    match row.get("x") {
        Some(x) => json!({ "x": x }),
        None => json!({ "r": row["r"] }),
    }
}

fn run(row: &Value) -> Value {
    let f = row["f"].as_str().expect("f");
    match f {
        "rc" => answer(
            ct::random_crockford(
                row["n"].as_i64().expect("n"),
                &unhex(row["b"].as_str().expect("b")),
            ),
            |s| str_output(&cps(&s)),
        ),
        "mint" => answer(ct::mint(&unhex(row["b"].as_str().expect("b"))), |s| {
            str_output(&cps(&s))
        }),
        "valid" => json!({ "r": ct::valid(&neutral(&row["a"])) }),
        "lineage" => {
            let a = row["a"].as_array().expect("a");
            json!({ "r": lin_json(&ct::lineage(&neutral(&a[0]), &neutral(&a[1]))) })
        }
        "lread" => json!({ "r": lin_json(&Lineage::read(&neutral(&row["a"]))) }),
        "link" => {
            let l = lin(&row["l"]).expect("a lineage");
            answer(ct::link(&l, &text_field(row, "n")), |s| {
                str_output(&cps(&s))
            })
        }
        "succ" => {
            let l = lin(&row["l"]).expect("a lineage");
            answer(
                ct::successor(&l, &unhex(row["b"].as_str().expect("b"))),
                |s| str_output(&cps(&s)),
            )
        }
        "sc" => {
            let l = lin(&row["l"]);
            answer(ct::succeeds(&neutral(&row["a"]), l.as_ref()), |b| json!(b))
        }
        "render" => answer(ct::render(&text_field(row, "a")), |b| json!(hex(&b))),
        "parse" => {
            json!({ "r": ct::parse(&text_field(row, "a")).map(|t| str_output(&t)) })
        }
        "detail" => {
            let a = text_field(row, "a");
            json!({
                "cs": ct::cut_short(&a),
                "r": str_output(&cps(&ct::malformed_detail(&a))),
            })
        }
        "path" => {
            let flavour = match row["fl"].as_str() {
                Some("p") => Flavour::Posix,
                _ => Flavour::Windows,
            };
            json!({ "r": ct::token_path(row["a"].as_str().expect("a"), flavour) })
        }
        other => panic!("unknown row {other}"),
    }
}

fn check_file(test: &str, name: &str) {
    let lines = jsonl(name);
    let header = &lines[0];
    let count = header["count"].as_u64().expect("count") as usize;
    assert_eq!(count, lines.len() - 1, "{name}: header count");
    let ledger = Ledger::new(test, name);
    let mut failures = Vec::new();
    for (i, row) in lines[1..].iter().enumerate() {
        let got = run(row);
        let mut want = expected(row);
        if row["f"] == "detail" {
            want = json!({ "cs": row["cs"], "r": row["r"] });
        }
        if got != want {
            failures.push(format!("row {i} {row}: native {got}"));
        }
    }
    assert!(count > 0, "{name}: no case");
    ledger.settle(count, &failures);
}

#[test]
fn checkout_token_every_case() {
    check_file("golden_checkout_token", "checkout_token.jsonl");
}
