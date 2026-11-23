//! Every reference script in response_history.jsonl, including the effect
//! order, budget scope, original errors and subsequent calls on the pass.
mod common;

use bettermemory_core::git_read_budget::{Raise, ReadDeadline, Scope};
use bettermemory_core::origin_dates::memo::Timestamps;
use bettermemory_core::origin_history::{author_instant, HistoryUnread, Key};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::response_history::{HistoryPass, HistoryReads, Read};
use common::parity::Ledger;
use serde_json::{json, Value};
use std::sync::Arc;

const FILE: &str = "response_history.jsonl";

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}
fn codes(v: &Value) -> PyStr {
    v.as_array()
        .unwrap()
        .iter()
        .map(|n| n.as_u64().unwrap().try_into().unwrap())
        .collect()
}
fn pair_value(pair: Option<&Key>) -> Value {
    pair.map_or(Value::Null, |(root, head)| {
        let head: String = head.iter().map(|&n| char::from_u32(n).unwrap()).collect();
        json!([root, head])
    })
}
fn dates_value(value: Timestamps) -> Value {
    match value {
        Timestamps::Absent => Value::Null,
        Timestamps::Dates(dates) => json!(dates.iter().map(|d| d.day).collect::<Vec<_>>()),
        Timestamps::Unread(why) => json!(["unread", why.value()]),
    }
}

struct Script<'a> {
    case: &'a Value,
    calls: Vec<Value>,
    raised: bool,
    n_dates: usize,
}
impl Script<'_> {
    fn effect(&mut self, scope: &Scope, name: &str, args: Vec<Value>) -> Read<(), &'static str> {
        let mut parts = vec![
            json!(name),
            json!(scope.root),
            json!(scope.active.is_some()),
        ];
        parts.extend(args);
        self.calls.push(Value::Array(parts));
        if self.case["stop"] == name && !self.raised {
            self.raised = true;
            return Err(match self.case["error"].as_str().unwrap() {
                "ReadDeadline" => Raise::Deadline(ReadDeadline),
                "RuntimeError" => Raise::Other("RuntimeError"),
                "unwind" => std::panic::panic_any(name.to_owned()),
                other => panic!("unknown error {other}"),
            });
        }
        Ok(())
    }
    fn pair(&self, name: &str) -> Option<Key> {
        self.case[name]
            .as_str()
            .map(|head| (codes(&self.case["root"]), cps(head)))
    }
}
impl HistoryReads for Script<'_> {
    type Error = &'static str;
    fn files_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, Self::Error> {
        self.effect(scope, "files", vec![json!(cwd)])?;
        Ok(self.pair("files"))
    }
    fn git_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, Self::Error> {
        self.effect(scope, "git", vec![json!(cwd)])?;
        Ok(self.pair("git"))
    }
    fn discovery_moved(&mut self, scope: &mut Scope) -> Read<bool, Self::Error> {
        self.effect(scope, "moved", vec![])?;
        Ok(self.case["moved"].as_bool().unwrap())
    }
    fn located_head(
        &mut self,
        scope: &mut Scope,
        root: &[u32],
    ) -> Read<Option<PyStr>, Self::Error> {
        self.effect(scope, "head", vec![json!(root)])?;
        Ok(self.case["head"].as_str().map(cps))
    }
    fn dates(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        located: Option<&Key>,
    ) -> Read<Timestamps, Self::Error> {
        self.effect(scope, "dates", vec![json!(cwd), pair_value(located)])?;
        let series = self.case["dates"].as_array().unwrap();
        let value = &series[self.n_dates.min(series.len() - 1)];
        self.n_dates += 1;
        if value.is_null() {
            return Ok(Timestamps::Absent);
        }
        if let Some(why) = value.as_str() {
            return Ok(Timestamps::Unread(HistoryUnread::from_value(why).unwrap()));
        }
        Ok(Timestamps::Dates(Arc::new(
            value
                .as_array()
                .unwrap()
                .iter()
                .map(|n| author_instant(1_577_836_800 + (n.as_i64().unwrap() as i128 - 1) * 86_400))
                .collect(),
        )))
    }
    fn note_unread(&mut self, scope: &mut Scope, why: HistoryUnread) -> Read<(), Self::Error> {
        self.effect(scope, "note", vec![json!(why.value())])
    }
}

fn error(e: Raise<&'static str>) -> &'static str {
    match e {
        Raise::Deadline(_) => "ReadDeadline",
        Raise::Other(s) => s,
    }
}
fn replay(case: &Value) -> Value {
    let mut s = Script {
        case,
        calls: vec![],
        raised: false,
        n_dates: 0,
    };
    let outside = Some((cps("outside"), true));
    let mut scope = Scope {
        root: outside.clone(),
        ..Scope::default()
    };
    let opened = HistoryPass::open(
        &mut s,
        &mut scope,
        codes(&case["cwd"]),
        case["at_home"].as_bool().unwrap(),
    );
    let mut results = Vec::new();
    let mut open_error = "";
    match opened {
        Err(e) => open_error = error(e),
        Ok(mut p) => {
            for step in case["steps"].as_array().unwrap() {
                let got = match step.as_str().unwrap() {
                    "owned" => Ok(json!(p.owned())),
                    "key-on" | "key-off" => p
                        .keyed_pair(&mut s, &mut scope, step == "key-on")
                        .map(pair_value),
                    "located" => p.located(&mut s, &mut scope).map(pair_value),
                    "timestamps" => p
                        .timestamps(&mut s, &mut scope)
                        .map(dates_value)
                        .map_err(Raise::Other),
                    "applies" => p.history_applies(&mut s, &mut scope).map(Value::Bool),
                    "history-a" | "history-b" => {
                        Ok(json!(p.history_at(&[if step == "history-a" {
                            65
                        } else {
                            66
                        }])))
                    }
                    other => panic!("unknown step {other}"),
                };
                let observation = match got {
                    Ok(v) => json!(["return", v]),
                    Err(e) => json!(["raise", error(e)]),
                };
                results.push(json!([observation, s.calls.len()]));
                assert_eq!(scope.root, outside);
                assert!(scope.active.is_none());
            }
        }
    }
    json!([open_error, s.calls, results])
}

#[test]
fn golden_response_history() {
    let rows = common::jsonl(FILE);
    let mut ledger = Ledger::new("golden_response_history", FILE);
    assert_eq!(rows[0]["count"], rows.len() - 1);
    for case in &rows[1..] {
        let got = replay(case);
        let expected = json!([case["opened"], case["calls"], case["results"]]);
        let label = case["label"].as_str().unwrap();
        assert!(
            !ledger.differs_fatally(label, got == expected),
            "{label}\n{got}\n{expected}"
        );
    }
    ledger.finish(rows.len() - 1);
}

#[test]
fn warm_large_histories_share_the_original_dates() {
    let mut case = common::jsonl(FILE)
        .into_iter()
        .find(|v| v["label"] == "settled")
        .unwrap();
    case["dates"] = json!([vec![2; 150_000]]);
    let mut s = Script {
        case: &case,
        calls: Vec::new(),
        raised: false,
        n_dates: 0,
    };
    let mut scope = Scope {
        root: Some((cps("prior"), false)),
        ..Scope::default()
    };
    let mut p = HistoryPass::open(&mut s, &mut scope, cps("cwd"), true).unwrap();
    let Timestamps::Dates(first) = p.timestamps(&mut s, &mut scope).unwrap() else {
        panic!("dates")
    };
    for _ in 0..500 {
        let Timestamps::Dates(hit) = p.timestamps(&mut s, &mut scope).unwrap() else {
            panic!("dates")
        };
        assert!(Arc::ptr_eq(&first, &hit));
    }
    assert_eq!(s.calls.len(), 2);
}

#[test]
fn original_unwind_leaves_the_pass_retryable_at_each_read_boundary() {
    for effect in ["git", "dates", "note"] {
        let mut case = common::jsonl(FILE)
            .into_iter()
            .find(|r| r["label"] == "timeout")
            .unwrap();
        case["stop"] = json!(effect);
        case["error"] = json!("unwind");
        let mut reads = Script {
            case: &case,
            calls: Vec::new(),
            raised: false,
            n_dates: 0,
        };
        let outside = Some((cps("caller"), true));
        let mut scope = Scope {
            root: outside.clone(),
            ..Scope::default()
        };
        let mut pass = HistoryPass::open(&mut reads, &mut scope, cps("sub"), false).unwrap();
        let timeout = Timestamps::Unread(HistoryUnread::Timeout);
        if effect == "note" {
            assert_eq!(pass.timestamps(&mut reads, &mut scope), Ok(timeout.clone()));
        }
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            pass.timestamps(&mut reads, &mut scope)
        }))
        .unwrap_err();
        assert_eq!(*panic.downcast::<String>().unwrap(), effect);
        assert_eq!(scope.root, outside);
        assert!(scope.active.is_none());
        assert_eq!(pass.history_at(&cps("another head")), effect == "git");
        if effect == "dates" {
            assert!(!pass.history_applies(&mut reads, &mut scope).unwrap());
        }
        assert_eq!(pass.timestamps(&mut reads, &mut scope), Ok(timeout));
    }
}
