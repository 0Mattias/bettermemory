//! Every case of `verify_status.jsonl` (not gated) against the native
//! `verify_status`. The spelling is the generator's
//! (`tests/contract/gen_verify_status.py`).

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use bettermemory_core::verify_status::{
    self as vs, compute_verification_status, remote_verification_status, VerificationStatus,
};
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "verify_status.jsonl";

fn dt(v: &Value) -> DateTime {
    let a = v.as_array().expect("a datetime");
    let n = |i: usize| a[i].as_i64().expect("an int");
    DateTime::new(
        n(0),
        n(1) as u32,
        n(2) as u32,
        n(3) as u32,
        n(4) as u32,
        n(5) as u32,
        n(6) as u32,
        a[7].as_i64(),
    )
    .expect("a valid datetime")
}

fn to_json(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Int(t) => json!(t.parse::<i64>().expect("an int")),
        PyValue::Str(t) => json!(unicode::to_string(t).expect("text")),
        PyValue::Dict(items) => {
            let mut m = serde_json::Map::new();
            for (k, v) in items {
                let PyValue::Str(k) = k else { panic!("a key") };
                m.insert(unicode::to_string(k).expect("a key"), to_json(v));
            }
            Value::Object(m)
        }
        other => panic!("unexpected {other:?}"),
    }
}

/// `to_dict()` spelled as the generator spells it.
fn spelled(got: &VerificationStatus) -> Value {
    let mut out = to_json(&got.to_dict());
    let rec = match &got.recommendation {
        None => Value::Null,
        Some(r) if r == vs::NEVER_RECOMMENDATION => json!({"never": true}),
        Some(r)
            if got
                .age_days
                .is_some_and(|n| *r == vs::stale_recommendation(n)) =>
        {
            json!({"stale": got.age_days})
        }
        Some(r) => {
            let stamp = got.last_verified_at.expect("a stamp");
            assert_eq!(*r, vs::remote_recommendation(&stamp));
            json!({"remote": stamp.isoformat_utc()})
        }
    };
    out["recommendation"] = rec;
    out
}

#[test]
fn golden_verify_status() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    assert_eq!(
        header["default"],
        json!(vs::DEFAULT_VERIFICATION_STALE_DAYS)
    );
    assert_eq!(header["never"], json!(vs::NEVER_RECOMMENDATION));
    for pair in header["stale"].as_array().expect("stale texts") {
        let n = pair[0].as_i64().expect("an age");
        assert_eq!(pair[1], json!(vs::stale_recommendation(n)), "stale {n}");
    }
    for pair in header["remote"].as_array().expect("remote texts") {
        let stamp = dt(&pair[0]).assume_utc();
        assert_eq!(pair[1], json!(vs::remote_recommendation(&stamp)));
    }
    let count = header["count"].as_u64().expect("a count") as usize;
    let mut ledger = Ledger::new("golden_verify_status", FILE);
    for (i, case) in lines[1..].iter().enumerate() {
        let s = case
            .get("s")
            .map_or(vs::DEFAULT_VERIFICATION_STALE_DAYS, |v| {
                v.as_i64().expect("an i64")
            });
        let lv = (!case["lv"].is_null()).then(|| dt(&case["lv"]));
        let got = match case["f"].as_str().expect("a function") {
            "c" => compute_verification_status(lv, dt(&case["now"]), s)
                .map_err(|_| json!({"raised": "TypeError"})),
            "r" => Ok(remote_verification_status(lv.expect("a stamp"), s)),
            other => panic!("unknown function {other}"),
        };
        let got = got.map_or_else(|e| e, |v| spelled(&v));
        let label = format!("line {} {}", i + 2, case);
        assert!(
            !ledger.differs_fatally(&label, got == case["o"]),
            "{label}: got {got}"
        );
    }
    assert_eq!(lines.len() - 1, count);
    ledger.finish(count);
}
