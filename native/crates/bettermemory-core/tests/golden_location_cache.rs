//! Every case of `location_cache.jsonl`: the capture cache over scripted
//! signatures, clocks, uncached captures, `config_unchanged` and the
//! tree's record (`gen_location_cache`), and `_evicted` over scripted
//! flags.

mod common;

use std::cell::RefCell;

use bettermemory_core::capture::{Capture, State};
use bettermemory_core::fsdata::{Kind, StatResult};
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::location_cache::{
    capture, capture_cached, capture_token, evicted, forget, Asked, Cache, CwdPart, Declined,
    Effects, RepoSignature, Signature, FETCH_BUDGET,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "location_cache.jsonl";

/// A label `X` or `X~n`: `("repo", ("/c", 1, 1), "X", ("token", n))`.
pub fn sig_of(label: &str) -> Signature {
    let (base, tok) = match label.split_once('~') {
        Some((b, n)) => (b, Some([n.parse::<i128>().unwrap(), 0, 0, 0, 0])),
        None => (label, None),
    };
    Signature::Repo(Box::new(RepoSignature {
        cwd: CwdPart {
            path: b"/c".to_vec(),
            id: Some((1, 1)),
        },
        dotgit: None,
        dotgit_stamp: None,
        git_dir: base.as_bytes().to_vec(),
        common_dir: Vec::new(),
        head: Vec::new(),
        head_stat: [0; 4],
        ref_part: None,
        packed: None,
        config: None,
        shallow: None,
        token: tok,
        inner: Vec::new(),
        refs: None,
    }))
}

fn label_of(sig: &Signature) -> String {
    let Signature::Repo(r) = sig else {
        panic!("a scripted signature is a repository's")
    };
    let base = String::from_utf8(r.git_dir.clone()).unwrap();
    match r.token {
        Some(t) => format!("{base}~{}", t[0]),
        None => base,
    }
}

fn text(t: &PyStr) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn opt(t: &Option<PyStr>) -> Value {
    t.as_ref().map_or(Value::Null, |t| json!(text(t)))
}

fn opt_str(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn made(cwd: &[u8], spec: &Value) -> Capture {
    let state = match spec["s"].as_str().unwrap_or("repo") {
        "repo" => State::Repo,
        "outside" => State::Outside,
        _ => State::Indeterminate,
    };
    let mut cap = Capture::new(cps(std::str::from_utf8(cwd).unwrap()), state);
    cap.detail = cps(spec["tag"].as_str().unwrap());
    cap.evicted = spec["ev"].as_bool().unwrap_or(false);
    cap.token_written = opt_str(&spec["tw"]);
    cap.token = opt_str(&spec["tok"]);
    cap.kept_apart = spec["ka"].as_bool().unwrap_or(false);
    cap.git_dir = opt_str(&spec["gd"]);
    cap.root = opt_str(&spec["root"]);
    cap
}

fn shown(cap: &Capture) -> Vec<Value> {
    vec![
        json!(text(&cap.detail)),
        opt(&cap.token),
        opt(&cap.token_written),
        json!(text(&cap.token_status)),
        json!(cap.tree_recorded),
    ]
}

fn cache_of(cache: &Cache) -> Value {
    let rows: Vec<Value> = cache
        .entries()
        .iter()
        .map(|((cwd, wt, mf), e)| {
            let mut row = vec![
                json!(String::from_utf8(cwd.clone()).unwrap()),
                json!(wt),
                json!(mf),
            ];
            row.extend(shown(&e.capture).into_iter().take(4));
            row.extend([
                json!(label_of(&e.signature)),
                json!(e.monotonic),
                json!(e.wall),
            ]);
            Value::Array(row)
        })
        .collect();
    if rows.len() > 6 {
        return json!({"n": rows.len(), "head": rows[..2], "tail": rows[rows.len() - 2..]});
    }
    Value::Array(rows)
}

#[derive(Default)]
struct Stubs {
    step: Value,
    n: [usize; 5],
    u: Vec<Value>,
}

fn num(v: &Value) -> f64 {
    v.as_f64().unwrap()
}

/// Runs `body` with the step's scripted effects.
fn with_effects<T>(stubs: &RefCell<Stubs>, body: impl FnOnce(&mut Effects<'_>) -> T) -> T {
    let mut signature = |_: &[u8]| -> Result<Option<Signature>, Declined> {
        let mut s = stubs.borrow_mut();
        let i = s.n[0];
        s.n[0] += 1;
        Ok(s.step["sig"][i].as_str().map(sig_of))
    };
    let mut clocks = || {
        let mut s = stubs.borrow_mut();
        let i = s.n[1];
        s.n[1] += 1;
        let pair = &s.step["clk"][i];
        (num(&pair[0]), num(&pair[1]))
    };
    let mut unchanged = |_: &Capture| -> Result<bool, Declined> {
        let mut s = stubs.borrow_mut();
        let i = s.n[2];
        s.n[2] += 1;
        Ok(s.step["cu"][i].as_bool().unwrap())
    };
    let mut uncached = |cwd: &[u8], wt: bool, budget: f64, mf: bool| {
        let mut s = stubs.borrow_mut();
        let i = s.u.len();
        let cwd_text = String::from_utf8(cwd.to_vec()).unwrap();
        s.u.push(json!([cwd_text, wt, budget, mf]));
        made(cwd, &s.step["un"][i])
    };
    let mut record = |_: &PyStr, _: &PyStr| {
        let mut s = stubs.borrow_mut();
        s.n[3] += 1;
        s.step["rec"].as_str().unwrap().to_owned()
    };
    let mut fx = Effects {
        signature: &mut signature,
        clocks: &mut clocks,
        config_unchanged: &mut unchanged,
        uncached: &mut uncached,
        record_working_tree: &mut record,
    };
    body(&mut fx)
}

fn flag(step: &Value, key: &str, default: bool) -> bool {
    step[key].as_bool().unwrap_or(default)
}

fn run(cache: &mut Cache, stubs: &RefCell<Stubs>, step: &Value) -> Value {
    {
        let mut s = stubs.borrow_mut();
        s.step = step.clone();
        s.n = [0; 5];
        s.u.clear();
    }
    let cwd = step["cwd"].as_str().unwrap_or("/w").as_bytes().to_vec();
    let asked = Asked {
        write_token: flag(step, "wt", false),
        budget: step["b"].as_f64().unwrap_or(FETCH_BUDGET),
        manifest: flag(step, "mf", true),
        use_cache: flag(step, "uc", true),
        record_tree: flag(step, "rt", true),
    };
    let mut out = serde_json::Map::new();
    match step["do"].as_str().unwrap() {
        "ev" => {
            let st = StatResult {
                kind: Kind::Regular,
                mode: None,
                ino: None,
                dev: None,
                size: 0,
                mtime_ns: 0,
                ctime_ns: None,
                flags: step["fl"][0].as_u64().unwrap() as u32,
                file_attributes: step["fl"][1].as_u64().unwrap() as u32,
                reparse_tag: None,
            };
            return json!({"a": evicted(&st)});
        }
        "fill" => {
            for i in 0..step["n"].as_u64().unwrap() {
                {
                    let mut s = stubs.borrow_mut();
                    s.step = json!({"sig": ["F", "F"], "clk": [[0, 0]], "cu": [false],
                                    "un": [{"tag": format!("f{i}")}]});
                    s.n = [0; 5];
                    s.u.clear();
                }
                let at = format!("/f{i}");
                with_effects(stubs, |fx| {
                    capture_cached(cache, fx, at.as_bytes(), Asked::new(false)).unwrap()
                });
            }
            return json!({"c": cache_of(cache)});
        }
        "forget" => {
            let mut cleared = 0;
            let cwd = step["cwd"].as_str().map(str::as_bytes);
            forget(cache, cwd, &mut || cleared += 1);
            stubs.borrow_mut().n[4] = cleared;
        }
        "tok" => {
            let tok = with_effects(stubs, |fx| {
                capture_token(
                    cache,
                    fx,
                    &cwd,
                    asked.write_token,
                    asked.budget,
                    asked.manifest,
                )
            })
            .unwrap();
            let dumped = dumps(&tok, DumpOptions::DEFAULT).unwrap();
            out.insert("a".into(), json!(text(&dumped)));
        }
        "cached" => {
            let got = with_effects(stubs, |fx| capture_cached(cache, fx, &cwd, asked)).unwrap();
            out.insert("a".into(), json!(shown(&got)));
        }
        _ => {
            let got = with_effects(stubs, |fx| capture(cache, fx, &cwd, asked)).unwrap();
            out.insert("a".into(), json!(shown(&got)));
        }
    }
    let s = stubs.borrow();
    out.insert("n".into(), json!(s.n));
    out.insert("u".into(), json!(s.u));
    out.insert("c".into(), cache_of(cache));
    Value::Object(out)
}

#[test]
fn location_cache_every_case() {
    let lines = common::jsonl(FILE);
    assert_eq!(lines[0]["cases"].as_u64(), Some(lines.len() as u64 - 1));
    let cases = &lines[1..];
    assert!(!cases.is_empty());
    let ledger = Ledger::new("golden_location_cache", FILE);
    let mut failures = Vec::new();
    let mut steps = 0;
    for case in cases {
        let mut cache = Cache::default();
        let stubs = RefCell::new(Stubs::default());
        for (j, step) in case["s"].as_array().unwrap().iter().enumerate() {
            steps += 1;
            let got = run(&mut cache, &stubs, step);
            for (key, value) in got.as_object().unwrap() {
                if step[key] != *value {
                    failures.push(format!(
                        "{} step {j} {key}: native {value} reference {}",
                        case["note"], step[key]
                    ));
                }
            }
        }
    }
    assert!(steps > 0);
    ledger.settle(cases.len(), &failures);
}
