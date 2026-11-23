//! Run the same hit sequences against the native resolver. Each pass
//! owns one walked map, history-failure counter and ordered read trace.

mod common;
mod verify_count_script;
mod verify_weak_script;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_dates::memo::Timestamps;
use bettermemory_core::origin_history::{HistoryUnread, ReachableWalk, Unwalked};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::count::CountReads;
use bettermemory_core::verify::hit::{resolve_hit_drift, Hit};
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::{DriftGit, Place, WeakError};
use common::parity::Ledger;
use serde_json::{json, Value};
use std::sync::Arc;
use verify_count_script::Counted;
use verify_weak_script::{claims, literal, out, outs, set, text, texts};

const FIXTURE: &str = "verify_hit.jsonl";

#[derive(Clone)]
enum Saved {
    Walk(Arc<ReachableWalk>),
    Unread(HistoryUnread),
    Absent,
}

type WalkKey = (PyStr, PyStr, PyStr);
type Walks = Vec<(WalkKey, Saved)>;

fn why(reason: &str) -> HistoryUnread {
    HistoryUnread::from_value(reason).expect("known unread reason")
}

fn cut(git: &Counted) -> Result<(), WeakError> {
    if git.deadline() {
        Err(WeakError::ReadDeadline)
    } else {
        Ok(())
    }
}

fn observed(step: &Value, saved: &mut Walks, unwalked: &mut Unwalked) -> Value {
    let mut script = step.get("git").cloned().unwrap_or(json!({}));
    script["trace_cwd"] = json!(true);
    script["deadline_at"] = step["cut"].clone();
    let git = Counted::new(&script);
    let cwd = cps(step["cwd"].as_str().unwrap_or("subdir"));
    let top = step["top"].as_str().map(cps);
    let at = Place {
        cwd: &cwd,
        toplevel: top.as_deref(),
    };
    let head = step["head"].as_str().map(cps);
    let anchor = step["anchor_head"].as_str().map(cps);
    let anchors = step.get("anchors").map_or_else(|| vec![cps("m.py")], texts);
    let declared = step.get("claims").map_or_else(Vec::new, claims);
    let since = DateTime::from_isoformat(&cps(step["since"]
        .as_str()
        .unwrap_or("2024-02-03T11:12:13+00:00")))
    .expect("valid since");
    let hit = Hit {
        since: &since,
        head: head.as_deref(),
        verified_head: anchor.as_deref(),
        anchors: &anchors,
        claims: &declared,
    };
    let lit = LiteralReader::default();
    let reads = CountReads {
        git: &git,
        at,
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let result = resolve_hit_drift(
        reads,
        hit,
        unwalked,
        |at, anchor, head, unwalked| {
            let key = (at.cwd.to_vec(), head.to_vec(), anchor.to_vec());
            let index = saved.iter().position(|(k, _)| *k == key);
            git.log(json!([
                "walk",
                out(at.cwd),
                out(anchor),
                at.toplevel.map(out),
                out(head),
                saved.len(),
                index.is_some()
            ]));
            cut(&git)?;
            let known = index.filter(|_| step["reuse"].as_bool() == Some(true));
            let value = if let Some(i) = known {
                saved[i].1.clone()
            } else if let Some(w) = step["walk"].as_array() {
                Saved::Walk(Arc::new(ReachableWalk {
                    anchor: anchor.to_vec(),
                    head: head.to_vec(),
                    commits: texts(&w[0]),
                    touched: w[1]
                        .as_array()
                        .expect("touched")
                        .iter()
                        .map(|row| (text(&row[0]), set(&row[1])))
                        .collect(),
                    listed: w[2].as_bool().expect("listed"),
                    timed_out: w[3].as_bool().expect("timed out"),
                }))
            } else if let Some(reason) = step["why"].as_str() {
                Saved::Unread(why(reason))
            } else {
                Saved::Absent
            };
            if let Some(i) = index {
                saved[i].1 = value.clone();
            } else {
                saved.push((key, value.clone()));
            }
            if let Some(reason) = step["nested_note"].as_str() {
                unwalked.note(why(reason));
            }
            Ok(match value {
                Saved::Walk(w) => Some(w),
                Saved::Absent => None,
                Saved::Unread(reason) => {
                    unwalked.note(reason);
                    None
                }
            })
        },
        || {
            git.log(json!(["dates"]));
            cut(&git)?;
            let dates = &step["dates"];
            Ok(if let Some(values) = dates.as_array() {
                Timestamps::Dates(Arc::new(
                    values
                        .iter()
                        .map(|v| DateTime::from_isoformat(&text(v)).expect("date"))
                        .collect(),
                ))
            } else if let Some(reason) = dates.as_str() {
                Timestamps::Unread(why(reason))
            } else {
                Timestamps::Absent
            })
        },
    );
    let value = match result {
        Err(WeakError::NaiveAware) => json!(["raise", "TypeError"]),
        Err(WeakError::ReadDeadline) => json!(["raise", "ReadDeadline"]),
        Err(e) => panic!("unexpected error {e:?}"),
        Ok(None) => Value::Null,
        Ok(Some(Err(reason))) => json!(["unread", reason.value()]),
        Ok(Some(Ok(r))) => json!([r.count, r.basis, r.claims_checked, outs(&r.claims_drifted)]),
    };
    assert_eq!(lit.gap(), None);
    let keys: Vec<_> = saved
        .iter()
        .map(|((cwd, head, anchor), _)| json!([out(cwd), out(head), out(anchor)]))
        .collect();
    json!({"result":value,"calls":*git.script.calls.borrow(),"walked":keys,"unwalked":unwalked.count()})
}

#[test]
fn every_hit_and_read_matches_the_reference() {
    let cases = common::jsonl(FIXTURE);
    let mut ledger = Ledger::new("golden_verify_hit", FIXTURE);
    assert_eq!(cases[0]["passes"], json!(cases.len() - 1));
    let mut compared = 0;
    for (pass, case) in cases[1..].iter().enumerate() {
        let steps = case["steps"].as_array().expect("steps");
        let expected = case["expected"].as_array().expect("expected");
        assert_eq!(steps.len(), expected.len());
        let mut saved = Vec::new();
        let mut unwalked = Unwalked::new();
        for (i, (step, want)) in steps.iter().zip(expected).enumerate() {
            let actual = observed(step, &mut saved, &mut unwalked);
            assert!(
                !ledger.differs_fatally(format!("pass {pass}, hit {i}"), actual == *want),
                "pass {pass}, hit {i}: got {actual}, expected {want}"
            );
            compared += 1;
        }
    }
    ledger.finish(compared);
}
