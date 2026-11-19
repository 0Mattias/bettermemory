//! Every case of `origin_history.jsonl` against `origin_history`: the
//! constants, `unanswered`, `note_unread`, `read_lines`, `author_instant`,
//! `author_dates`, `log_stdin`, `ReachableWalk.shas_touching`, the memo's
//! sequences and the tally of walks.

mod common;

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use bettermemory_core::datetime::{git_instant, git_seconds};
use bettermemory_core::origin_history::{self as oh, HistoryUnread, ReachableWalk, RunnerNote};
use bettermemory_core::pyvalue::PyStr;
use common::{assert_count, cps_of, hex, jsonl};
use serde_json::{json, Value};

fn s(v: &Value) -> String {
    v.as_str().expect("a string").to_string()
}

fn text(v: &Value) -> PyStr {
    match v {
        Value::String(t) => t.chars().map(u32::from).collect(),
        other => cps_of(other),
    }
}

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("a list").iter().map(text).collect()
}

fn why(v: &Value) -> HistoryUnread {
    HistoryUnread::from_value(v.as_str().expect("a member")).expect("a member's value")
}

fn why_out(w: Option<HistoryUnread>) -> Value {
    json!(w.map(HistoryUnread::value))
}

fn key(v: &Value) -> oh::Key {
    (text(&v[0]), text(&v[1]))
}

fn cut(v: &Value) -> oh::Cut {
    texts(v).into_iter().collect()
}

fn sorted_out(set: &BTreeSet<PyStr>) -> Value {
    json!(set
        .iter()
        .map(|t| t
            .iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect::<String>())
        .collect::<Vec<_>>())
}

fn guard(v: &Value) -> Option<Value> {
    (!v.is_null()).then(|| v.clone())
}

fn dev(v: &Value) -> Value {
    match v {
        Value::String(t) => json!(t.replace("{devnull}", oh::DEVNULL)),
        Value::Array(a) => Value::Array(a.iter().map(dev).collect()),
        other => other.clone(),
    }
}

fn consts(case: &Value) {
    let env: Vec<Value> = oh::AS_STORED_ENV
        .iter()
        .map(|(k, v)| json!([k, v]))
        .collect();
    assert_eq!(json!(env), dev(&case["env"]));
    assert_eq!(json!(oh::AS_STORED_CONFIG), case["config"]);
    assert_eq!(json!(oh::AS_STORED_DIFF), dev(&case["diff"]));
    assert_eq!(json!(oh::AS_STORED_PATHS), case["paths"]);
    assert_eq!(json!(oh::MEMO_CAP), case["cap"]);
    assert_eq!(json!(oh::MEMO_SLOTS), case["slots"]);
    assert_eq!(oh::FAILURE_SECONDS, case["failure"].as_f64().unwrap());
    assert_eq!(json!(oh::CUTS_MAX), case["cuts_max"]);
    assert_eq!(json!(oh::HISTORY_ENV), case["history_env"]);
    for m in case["members"].as_array().unwrap() {
        assert_eq!(why(&m[1]).value(), m[1].as_str().unwrap());
    }
    assert_eq!(case["members"].as_array().unwrap().len(), 6);
}

fn memo(case: &Value) {
    let mut m: oh::TimestampsMemo<Value> = oh::TimestampsMemo::new();
    let mut lists: Vec<(oh::Dates, usize)> = Vec::new();
    let name = |lists: &Vec<(oh::Dates, usize)>, d: &oh::Dates| -> usize {
        lists
            .iter()
            .find(|(l, _)| Arc::ptr_eq(l, d))
            .expect("a named list")
            .1
    };
    for (i, pair) in case["s"].as_array().unwrap().iter().enumerate() {
        let (step, want) = (&pair[0], &pair[1]);
        let a = &step.as_array().unwrap()[1..];
        let got = match step[0].as_str().unwrap() {
            "recall" => {
                let c = (!a[2].is_null()).then(|| cut(&a[2]));
                let (d, w) = m.recall(
                    &key(&a[0]),
                    guard(&a[1]).as_ref(),
                    c.as_ref(),
                    a[3].as_f64().unwrap(),
                );
                json!([d.map(|d| name(&lists, &d)), why_out(w)])
            }
            "checked" => match m.checked_cut(&key(&a[0])) {
                None => Value::Null,
                Some(c) => sorted_out(&c),
            },
            "base" => match m.base_for(&text(&a[0]), guard(&a[1]).as_ref(), &cut(&a[2])) {
                None => Value::Null,
                Some((h, d, c)) => json!([
                    sorted_out(&BTreeSet::from([h]))[0],
                    name(&lists, &d),
                    sorted_out(&c)
                ]),
            },
            "remember" => {
                let n = a[1].as_u64().unwrap() as usize;
                let d: oh::Dates = Arc::new(vec![oh::author_instant(0); n]);
                let kept = m.remember(&key(&a[0]), Arc::clone(&d), guard(&a[2]), cut(&a[3]));
                if kept {
                    lists.push((d, i));
                }
                json!(kept)
            }
            "fail" => {
                m.remember_failure(
                    &key(&a[0]),
                    guard(&a[1]),
                    why(&a[2]),
                    a[3].as_f64().unwrap(),
                );
                Value::Null
            }
            other => panic!("unknown step {other}"),
        };
        assert_eq!(&got, want, "step {i} of {case}");
    }
    let entries: Vec<Value> = m
        .entries()
        .iter()
        .map(|((r, _), e)| {
            json!([
                sorted_out(&BTreeSet::from([r.clone()]))[0],
                name(&lists, &e.dates),
                e.guard,
                sorted_out(&e.cut)
            ])
        })
        .collect();
    let want: Vec<Value> = case["e"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| json!([e[0][0], e[1], e[2], e[3]]))
        .collect();
    assert_eq!(entries, want, "entries of {case}");
    let heads: Vec<Value> = m
        .entries()
        .iter()
        .map(|((_, h), _)| sorted_out(&BTreeSet::from([h.clone()]))[0].clone())
        .collect();
    let want_heads: Vec<Value> = case["e"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e[0][1].clone())
        .collect();
    assert_eq!(heads, want_heads);
    let failed: Vec<(Value, f64, Value, &str)> = m
        .failures()
        .iter()
        .map(|((r, h), f)| {
            (
                json!([
                    sorted_out(&BTreeSet::from([r.clone()]))[0],
                    sorted_out(&BTreeSet::from([h.clone()]))[0]
                ]),
                f.at,
                json!(f.guard),
                f.why.value(),
            )
        })
        .collect();
    let want: Vec<(Value, f64, Value, &str)> = case["x"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| {
            (
                x[0].clone(),
                x[1].as_f64().unwrap(),
                x[2].clone(),
                x[3].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(failed, want, "failures of {case}");
}

fn check(case: &Value) {
    match case["f"].as_str().unwrap() {
        "const" => consts(case),
        "unanswered" => {
            let p = |v: &Value| (v[0].as_i64().unwrap(), v[1].as_i64().unwrap());
            let got = oh::unanswered(
                p(&case["b"]),
                p(&case["n"]),
                case["named"].as_bool().unwrap(),
            );
            assert_eq!(why_out(got), case["r"], "{case}");
        }
        "note_unread" => {
            let got = match oh::note_unread(why(&case["why"])) {
                RunnerNote::TimedOut => "t",
                RunnerNote::Failed => "f",
            };
            assert_eq!(json!([got]), case["r"], "{case}");
        }
        "read_lines" => {
            let raw = text(&case["raw"]);
            let got = if s(&case["rd"]) == "seconds" {
                oh::read_lines(&raw, |l| git_seconds(l).map(|v| json!(v)))
            } else {
                oh::read_lines(&raw, |l| git_instant(l).map(|d| json!(d.isoformat())))
            };
            assert_eq!(json!(got), case["r"], "{case}");
        }
        "author_instant" => {
            let v: i128 = s(&case["v"]).parse().unwrap_or_else(|_| {
                if s(&case["v"]).starts_with('-') {
                    i128::MIN
                } else {
                    i128::MAX
                }
            });
            assert_eq!(
                json!(oh::author_instant(v).isoformat()),
                case["r"],
                "{case}"
            );
        }
        "author_dates" => {
            let vs: Vec<i128> = case["v"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| {
                    s(v).parse().unwrap_or(if s(v).starts_with('-') {
                        i128::MIN
                    } else {
                        i128::MAX
                    })
                })
                .collect();
            let got: Vec<String> = oh::author_dates(&vs)
                .iter()
                .map(|d| d.isoformat())
                .collect();
            assert_eq!(json!(got), case["r"], "{case}");
        }
        "log_stdin" => {
            let got = oh::log_stdin(&texts(&case["rev"]), &texts(&case["ps"]));
            assert_eq!(json!(got.map(|b| hex(&b))), case["r"], "{case}");
        }
        "shas" => {
            let touched: BTreeMap<PyStr, BTreeSet<PyStr>> = case["t"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| (text(&p[0]), texts(&p[1]).into_iter().collect()))
                .collect();
            let walk = ReachableWalk {
                anchor: text(&json!("an")),
                head: text(&json!("hd")),
                commits: texts(&case["c"]),
                touched,
                listed: case["listed"].as_bool().unwrap(),
                timed_out: false,
            };
            let got = walk.shas_touching(&texts(&case["ps"]));
            assert_eq!(sorted_out(&got), case["r"], "{case}");
        }
        "memo" => memo(case),
        "unwalked" => {
            let mut t = oh::Unwalked::new();
            for pair in case["s"].as_array().unwrap() {
                let step = &pair[0];
                let got = match step[0].as_str().unwrap() {
                    "note" => {
                        t.note(why(&step[1]));
                        Value::Null
                    }
                    "count" => json!(t.count()),
                    _ => why_out(t.given_up_since(step[1].as_u64().unwrap())),
                };
                assert_eq!(got, pair[1], "{case}");
            }
        }
        other => panic!("unknown case {other}"),
    }
}

#[test]
fn origin_history_every_case() {
    let lines = jsonl("origin_history.jsonl");
    let total = lines[0]["cases"].as_u64().unwrap() as usize;
    let mut checked = 0;
    for case in &lines[1..] {
        check(case);
        checked += 1;
    }
    assert_eq!(lines.len() - 1, total);
    assert_count("origin_history.jsonl", checked, total);
}
