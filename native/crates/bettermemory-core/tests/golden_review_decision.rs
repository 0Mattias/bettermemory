//! Exact replacement lists and event JSON for direct cases and a seeded sweep.
#[path = "common/review_decision.rs"]
mod cases;
mod common;

use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::review::*;
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

struct Tables {
    ids: Vec<PyStr>,
    labels: Vec<PyStr>,
    scores: Vec<f64>,
}

fn array(v: &Value) -> &[Value] {
    v.as_array().expect("array")
}

fn index(v: &Value) -> usize {
    v.as_u64().expect("non-negative index") as usize
}

fn spelling(value: PyValue) -> String {
    unicode::to_string(&json::dumps(&value, DumpOptions::DEFAULT).unwrap()).expect("ASCII JSON")
}

fn output(t: &Tables, plan: &Value) -> String {
    let id = |x: &Value| t.ids[index(x)].clone();
    let ids = |x: &Value| array(x).iter().map(id).collect::<Vec<_>>();
    let rel = array(&plan["relation"])
        .iter()
        .map(|p| (id(&p[0]), t.labels[index(&p[1])].clone()))
        .collect();
    let scores: Vec<_> = array(&plan["scores"])
        .iter()
        .map(|x| t.scores[index(x)])
        .collect();
    let answers = cases::answers(rel, scores[0], scores[1]);
    let entries = array(&plan["entries"])
        .iter()
        .map(|p| cases::entry(id(&p[0]), p[1].as_bool().unwrap()))
        .collect();
    let related = RelatedSet {
        entries,
        report: plan["cue"].as_array().map(|cue| CueReport {
            supersedes: cue.iter().map(id).collect(),
            conflicts: vec![],
        }),
    };
    let declared = ids(&plan["declared"]);
    let standing = ids(&plan["standing"]);
    let flags = index(&plan["flags"]);
    let options = ReviewOptions {
        tau: Tau {
            r#unsafe: scores[2],
            secret: scores[3],
        },
        write_claim_sized: flags & 1 != 0,
        write_supersession: flags & 2 != 0,
        said: flags & 4 != 0,
        restates_refuses: flags & 8 != 0,
        write_standing: flags & 16 != 0,
        declared_ids: &declared,
        standing_ids: &standing,
    };
    let replaced = replaced_ids(&answers, &related, &declared)
        .into_iter()
        .map(PyValue::Str)
        .collect();
    let decision = apply_review(&answers, &related, &options);
    spelling(PyValue::List(vec![
        PyValue::List(replaced),
        decision.to_event(),
    ]))
}

fn sweep_plan(a: u64, b: u64) -> Value {
    let bit = |word: u64, n: u32| word & (1_u64 << n) != 0;
    let members = |shift: u32| (0..6).filter(|i| bit(b, shift + i)).collect::<Vec<_>>();
    let mut entries: Vec<_> = (0..5)
        .filter(|i| bit(b, *i))
        .map(|i| (i, bit(b, 8 + i)))
        .collect();
    if !entries.is_empty() && bit(a, 52) {
        entries.push(entries[0]);
    }
    let relation: Vec<_> = (0..6)
        .map(|i| (i, (a >> (3 * i)) & 7))
        .filter(|(_, lab)| *lab < 6)
        .collect();
    let mut cue = members(16);
    if !cue.is_empty() && bit(a, 49) {
        cue.push(cue[0]);
    }
    json!({"entries":entries,"relation":relation,"cue":bit(a,51).then_some(cue),
       "declared":members(24),"standing":members(32),"flags":(a >> 18) & 31,
       "scores":[(a >> 24) & 7,(a >> 27) & 7,(a >> 30) & 7,(a >> 33) & 7]})
}

fn sweep(t: &Tables, row: &Value) -> String {
    let mut n = row["seed"].as_u64().unwrap();
    let next = |w: u64| {
        w.wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407)
    };
    let mut digest = Sha256::new();
    assert_eq!(index(&row["count"]), 8192);
    for _ in 0..index(&row["count"]) {
        let a = next(n);
        n = next(a);
        digest.update(output(t, &sweep_plan(a, n)).as_bytes());
        digest.update(b"\n");
    }
    common::hex(&digest.finalize())
}

#[test]
fn review_decision_every_case() {
    let name = "review_decision.jsonl";
    let rows = common::jsonl(name);
    let head = &rows[0];
    let t = Tables {
        ids: array(&head["ids"]).iter().map(common::cps_of).collect(),
        labels: array(&head["labels"])
            .iter()
            .map(|x| unicode::cps(x.as_str().unwrap()))
            .collect(),
        scores: array(&head["scores"])
            .iter()
            .map(|x| x.as_str().unwrap().parse().unwrap())
            .collect(),
    };
    let mut ledger = Ledger::new("review_decision_every_case", name);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("row {i} ({})", row["k"]), || {
            match row["k"].as_str().unwrap() {
                "tables" => assert_eq!((t.ids.len(), t.labels.len(), t.scores.len()), (11, 6, 8)),
                "case" => assert_eq!(output(&t, &row["plan"]), row["answer"].as_str().unwrap()),
                "event" => {
                    let id = |i: usize| t.ids[i].clone();
                    let decision = ReviewDecision {
                        action: id(8),
                        restated: vec![id(0)],
                        links: vec![id(1)],
                        update_suggested: vec![id(2)],
                        filings: vec![(id(3), id(8))],
                        cue_dropped: vec![id(4)],
                        extends: vec![id(5)],
                        standing: vec![id(6)],
                        standing_write: true,
                    };
                    assert_eq!(
                        spelling(decision.to_event()),
                        row["answer"].as_str().unwrap()
                    );
                }
                "sweep" => assert_eq!(sweep(&t, row), row["sha256"].as_str().unwrap()),
                other => panic!("unknown row {other}"),
            }
        });
    }
    ledger.finish(rows.len());
}
