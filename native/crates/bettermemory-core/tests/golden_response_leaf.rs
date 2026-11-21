//! Every case of `response_leaf.jsonl` (not gated) against the native
//! `response_links`, `response_memo` and `verdict_signals`. The spelling
//! is the generator's (`tests/contract/gen_response_leaf.py`).

mod common;

use bettermemory_core::response_memo::{DriftMemo, DRIFT_MEMO_CAP, TIMED_OUT_SECONDS};
use bettermemory_core::verdict_signals::{
    commit_leg_escalates, compute_staleness_verdict, verdict_from_signals, COMMIT_DRIFT_ESCALATES,
    VERDICT_FRESH, VERDICT_RAISE_STATUSES, VERDICT_RECOMMENDED, VERDICT_REQUIRED,
};
use bettermemory_core::verify_status::{Status, VerificationStatus};
use common::parity::Ledger;
use common::response_leaf::{answer, spell, sweep_case, Snapshot};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "response_leaf.jsonl";

fn sha(text: &str) -> String {
    common::hex(&Sha256::digest(text.as_bytes()))
}

fn snapshot(v: &Value) -> Snapshot {
    v.as_array()
        .expect("a snapshot")
        .iter()
        .map(|m| {
            let links = m[1]
                .as_array()
                .expect("links")
                .iter()
                .map(|l| {
                    (
                        l[0].as_str().expect("a type").to_string(),
                        l[1].as_u64().expect("a target"),
                        l[2].as_str().map(str::to_string),
                    )
                })
                .collect();
            (m[0].as_u64().expect("an id"), links)
        })
        .collect()
}

fn ids(v: &Value) -> Vec<u64> {
    v.as_array()
        .expect("ids")
        .iter()
        .map(|i| i.as_u64().expect("an id"))
        .collect()
}

fn answer_json(snap: &Snapshot, wanted: &[u64]) -> Value {
    let edges = |rows: &Vec<(String, u64, Option<String>)>| {
        Value::Array(rows.iter().map(|(k, o, n)| json!([k, o, n])).collect())
    };
    Value::Array(
        answer(snap, wanted)
            .iter()
            .map(|(id, o, i)| json!([id, edges(o), edges(i)]))
            .collect(),
    )
}

fn now(v: &Value) -> f64 {
    match v {
        // The generator spells each time as Python's repr; str::parse is
        // correctly rounded where serde_json's default float parse is not.
        Value::String(s) => s.parse::<f64>().expect("a float repr"),
        other => panic!("a time is spelled as a string: {other}"),
    }
}

fn keys_json(keys: Vec<String>) -> Value {
    if keys.len() > 10 {
        return json!({"n": keys.len(), "first": keys[0], "last": keys[keys.len() - 1]});
    }
    json!(keys)
}

type Pairs = Vec<(String, Option<String>)>;

fn pairs(v: &Value) -> Pairs {
    v.as_array()
        .expect("pairs")
        .iter()
        .map(|p| {
            (
                p[0].as_str().expect("a key").to_string(),
                p[1].as_str().map(str::to_string),
            )
        })
        .collect()
}

fn run_memo(cap: usize, steps: &[Value]) -> Value {
    let memo: DriftMemo<String, Option<String>> = DriftMemo::with_cap(cap);
    let mut out = Vec::new();
    for step in steps {
        let mut answer = serde_json::Map::new();
        match step[0].as_str().expect("an op") {
            "r" => {
                let key = step[1].as_str().expect("a key").to_string();
                let got = memo.recall(&key, now(&step[2]));
                answer.insert(
                    "r".into(),
                    match got {
                        Some(v) => json!([true, v]),
                        None => json!([false, null]),
                    },
                );
            }
            "m" => memo.remember(&pairs(&step[1]), &pairs(&step[2]), now(&step[3])),
            "mr" => {
                let n = step[1].as_u64().expect("a count");
                let resolved: Pairs = (0..n)
                    .map(|i| (format!("k{i}"), Some(format!("k{i}"))))
                    .collect();
                memo.remember(&resolved, &[], now(&step[2]));
            }
            "c" => memo.clear(),
            other => panic!("unknown op {other}"),
        }
        answer.insert("mk".into(), keys_json(memo.memo_keys()));
        answer.insert("tk".into(), keys_json(memo.timed_out_keys()));
        out.push(Value::Object(answer));
    }
    Value::Array(out)
}

fn opt_i64(v: &Value) -> Option<i64> {
    if v.is_null() {
        None
    } else {
        Some(v.as_i64().expect("an int"))
    }
}

fn i(v: &Value) -> i64 {
    v.as_i64().expect("an int")
}

fn verdict(a: &Value) -> &'static str {
    verdict_from_signals(
        a[0].as_str().expect("a status"),
        i(&a[1]),
        opt_i64(&a[2]),
        i(&a[3]),
        i(&a[4]),
    )
}

#[test]
fn golden_response_leaf() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    assert_eq!(
        header["verdicts"],
        json!([VERDICT_FRESH, VERDICT_RECOMMENDED, VERDICT_REQUIRED])
    );
    assert_eq!(header["raise"], json!(VERDICT_RAISE_STATUSES));
    assert_eq!(header["escalates"], json!(COMMIT_DRIFT_ESCALATES));
    assert_eq!(header["cap"], json!(DRIFT_MEMO_CAP));
    assert_eq!(header["seconds"], json!(TIMED_OUT_SECONDS));
    let cases = &lines[1..];
    assert_eq!(header["count"], json!(cases.len()));
    assert!(!cases.is_empty());
    let mut ledger = Ledger::new("golden_response_leaf", FILE);
    let mut count = 0;
    for (n, case) in cases.iter().enumerate() {
        let label = format!("case {n} ({})", case["k"]);
        let same = match case["k"].as_str().expect("a kind") {
            "links" => answer_json(&snapshot(&case["m"]), &ids(&case["ids"])) == case["o"],
            "links_sweep" => {
                let mut state = case["seed"].as_u64().expect("a seed");
                let n = case["n"].as_u64().expect("a count");
                let mut lines = Vec::new();
                for _ in 0..n {
                    let (next, snap, wanted) = sweep_case(state);
                    state = next;
                    lines.push(spell(&answer(&snap, &wanted)));
                }
                n > 0 && sha(&lines.join("\n")) == case["sha"]
            }
            "memo" => {
                let cap = case["cap"].as_u64().expect("a cap") as usize;
                run_memo(cap, case["s"].as_array().expect("steps")) == case["o"]
            }
            "verdict" => {
                verdict(&case["a"]) == case["o"]
                    && json!(commit_leg_escalates(opt_i64(&case["a"][2]))) == case["e"]
            }
            "verdict_sweep" => {
                let list = |k: &str| case[k].as_array().expect("a list").clone();
                let mut answers = Vec::new();
                for st in list("statuses") {
                    for pdm in list("pdm") {
                        for cdc in list("cdc") {
                            for cm in list("cm") {
                                for aap in list("aap") {
                                    answers.push(verdict(&json!([st, pdm, cdc, cm, aap])));
                                }
                            }
                        }
                    }
                }
                json!(answers.len()) == case["n"] && sha(&answers.join("\n")) == case["sha"]
            }
            "compute" => {
                let status = match case["st"].as_str().expect("a status") {
                    "never" => Status::Never,
                    "stale" => Status::Stale,
                    "fresh" => Status::Fresh,
                    "remote" => Status::Remote,
                    other => panic!("status {other}"),
                };
                let vs = VerificationStatus {
                    status,
                    last_verified_at: None,
                    age_days: None,
                    recommendation: None,
                    stale_after_days: 30,
                };
                let a = &case["a"];
                compute_staleness_verdict(&vs, i(&a[0]), opt_i64(&a[1]), i(&a[2]), i(&a[3]))
                    == case["o"]
            }
            other => panic!("unknown kind {other}"),
        };
        assert!(!ledger.differs_fatally(&label, same), "{label} differs");
        count += 1;
    }
    ledger.finish(count);
}
