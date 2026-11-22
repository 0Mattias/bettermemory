//! Every case of `verify_weak.jsonl` (not gated) against `verify::weak`
//! and `verify::legs`, git scripted as the generator scripts it
//! (`tests/contract/gen_verify_weak.py`) and the claim index read through
//! the built [`LiteralReader`].

mod common;
mod verify_weak_script;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_history::ReachableWalk;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::legs::{claim_legs, resolve_commit_drift, Reads};
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::{
    weak_tier_evaluation, Place, WeakError, MAX_PATCH_STREAM_COMMITS,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use verify_weak_script::{claims, literal, outs, set, text, texts, unsqueeze, Chosen, Script};

const FILE: &str = "verify_weak.jsonl";

fn walk(w: &Value) -> Option<ReachableWalk> {
    if w.is_null() {
        return None;
    }
    Some(ReachableWalk {
        anchor: text(&w[0]),
        head: cps(&"f".repeat(40)),
        commits: texts(&w[1]),
        touched: w[2]
            .as_array()
            .expect("touched")
            .iter()
            .map(|p| (text(&p[0]), set(&p[1])))
            .collect(),
        listed: w[3].as_bool().expect("listed"),
        timed_out: false,
    })
}

/// The answer and the calls made for one case.
fn run(case: &Value) -> (Value, Value) {
    let script = Script::new(&case["s"]);
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let toplevel = case.get("top").and_then(Value::as_str).map(cps);
    let at = Place {
        cwd: &cwd,
        toplevel: toplevel.as_deref(),
    };
    let r = Reads {
        git: &script,
        at,
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let cl = claims(&case["c"]);
    let o = match case["k"].as_str().expect("k") {
        "legs" => match claim_legs(r, &texts(&case["a"]), &cl).expect("an answer") {
            None => Value::Null,
            Some((u, g)) => json!([outs(&u), outs(&g)]),
        },
        "weak" => {
            let chosen = Chosen::of_script(&case["s"]);
            let spell = case["sp"].as_bool().expect("sp").then_some(&chosen as _);
            let (drifted, implicated) = weak_tier_evaluation(
                &script,
                at,
                &texts(&case["shas"]),
                &texts(&case["specs"]),
                &cl,
                spell,
                &literal,
                &lit,
            )
            .expect("an answer");
            let implicated =
                implicated.map_or(Value::Null, |i| outs(&i.into_iter().collect::<Vec<_>>()));
            json!([outs(&drifted), implicated])
        }
        _ => {
            let since = DateTime::from_isoformat(&text(&case["since"])).expect("a stamp");
            let w = walk(&case["w"]);
            let mut probe = || {
                script
                    .calls
                    .borrow_mut()
                    .push(json!(["count", verify_weak_script::top(&at)]));
                case["s"].get("count").and_then(Value::as_i64)
            };
            match resolve_commit_drift(
                r,
                &since,
                case["u"].as_i64().expect("u"),
                &texts(&case["a"]),
                &cl,
                w.as_ref(),
                &mut probe,
            ) {
                Ok(None) => Value::Null,
                Ok(Some(d)) => json!([d.count, d.claims_checked, outs(&d.claims_drifted), d.basis]),
                Err(WeakError::NaiveAware) => json!("TypeError"),
                Err(WeakError::IntTooLong) => json!("ValueError"),
                Err(e) => panic!("no answer: {e:?}"),
            }
        }
    };
    assert_eq!(lit.gap(), None);
    let calls = script.calls.borrow().clone();
    (o, Value::Array(calls))
}

#[test]
fn golden_verify_weak() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    assert_eq!(header["max"], json!(MAX_PATCH_STREAM_COMMITS));
    let cases = &lines[1..];
    assert_eq!(header["count"], json!(cases.len()));
    let mut ledger = Ledger::new("golden_verify_weak", FILE);
    for (n, raw) in cases.iter().enumerate() {
        let case = unsqueeze(raw, header);
        let label = format!("case {n} ({})", case["k"]);
        let (o, calls) = run(&case);
        let same = o == case["o"] && calls == case["calls"];
        assert!(
            !ledger.differs_fatally(&label, same),
            "{label} differs: got {o} calls {calls}, want {case}"
        );
    }
    ledger.finish(cases.len());
}
