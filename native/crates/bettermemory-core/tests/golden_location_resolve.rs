//! Every case of `location_resolve.jsonl` (class i): `_caller`,
//! `_hooked`, `resolve_caller`, `resolve_here`, `capture_location` and
//! `Caller.home` with `add_head_witness`, each with the generator's
//! recording doubles, against `location_resolve`.

mod common;
mod location_resolve_support;

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::capture::location::to_location;
use bettermemory_core::capture::{Capture, State};
use bettermemory_core::location_resolve::{
    caller, capture_location, hooked, resolve_caller, resolve_here, Caller,
};
use common::jsonl;
use common::parity::Ledger;
use location_resolve_support::*;
use serde_json::{json, Value};

fn run(c: &Value) -> Value {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let process = new_process();
    let mut cs = caps(c, &log);
    let req = request(c);
    let out = match c["k"].as_str().unwrap() {
        "caller" => {
            let stand = c["stand"].as_array().map(|a| {
                let mut r = resolution(&json!({"o": "silent", "p": "p", "c": a[1]}));
                r.rule = text("r");
                bettermemory_core::location_caller::Stand {
                    machine: text("m1"),
                    path: opt(&a[0]).unwrap(),
                    resolution: r,
                }
            });
            match with_hands(c, &log, &mut cs, &req, |h| {
                caller(&process, &req, stand.as_ref(), h)
            }) {
                Ok(got) => caller_json(&got),
                Err(()) => json!({"r": "raised"}),
            }
        }
        "hooked" => {
            let session = req.session.clone();
            let res = with_hands(c, &log, &mut cs, &req, |h| {
                hooked(
                    &process,
                    &req.store_path,
                    req.policy,
                    req.cwd.as_deref(),
                    session.as_deref(),
                    h,
                )
            })
            .unwrap();
            let stands: Vec<Value> = process
                .stands
                .keys()
                .iter()
                .map(|(store, sess)| {
                    let st = process.stands.stand_of(store, Some(sess)).unwrap();
                    json!([
                        s(sess),
                        s(&st.machine),
                        s(&st.path),
                        st.resolution.checkout.as_deref().map(s)
                    ])
                })
                .collect();
            let mut out = json!({"res": res_json(&res), "stands": stands});
            if c["again"].as_bool() == Some(true) {
                let mut again = req.clone();
                again.identity = Some(identity("m1"));
                again.budget = 0.3;
                let then = with_hands(c, &log, &mut cs, &again, |h| {
                    resolve_caller(&process, &again, h)
                })
                .unwrap();
                out["again"] = res_json(&then.resolution);
            }
            out
        }
        "here" => {
            let (g, l) = (opt(&c["g"]), log.clone());
            let getcwd = move || {
                l.borrow_mut().push(json!(["getcwd"]));
                g
            };
            let res = with_hands(c, &log, &mut cs, &req, |h| {
                resolve_here(&process, &req, getcwd, h)
            })
            .unwrap();
            json!({"res": res_json(&res)})
        }
        "capture_location" => {
            let (loc, history) = capture_location(
                &mut cs,
                req.cwd.as_deref().unwrap(),
                &text("m1"),
                req.session.as_deref(),
                req.policy.write_token,
                req.budget,
            );
            json!({"loc": loc_json(&loc), "history": history.as_deref().map(s)})
        }
        "home" => {
            let mut cap = Capture::new(text("/w"), State::Repo);
            cap.head = opt(&c["head"]);
            let mut who = Caller {
                location: to_location(&cap, &text("m1"), opt(&c["session"]).as_deref()),
                history: opt(&c["hist"]),
                resolution: resolution(&c["res"]),
                model: Fake {
                    case: c.clone(),
                    log: log.clone(),
                    writer: None,
                },
            };
            let h = who.home().unwrap();
            let source = opt(&c["source"]).unwrap_or_else(|| text("write"));
            who.add_head_witness(&source).unwrap();
            json!({"home": [s(&h.project), h.checkout.as_deref().map(s), s(&h.rule)]})
        }
        other => panic!("unknown kind {other}"),
    };
    let mut r = json!({"calls": log.borrow().clone()});
    for (k, v) in out.as_object().unwrap() {
        r[k] = v.clone();
    }
    r
}

#[test]
fn location_resolve_every_case() {
    let file = "location_resolve.jsonl";
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let mut ledger = Ledger::new("location_resolve_every_case", file);
    for (i, c) in cases.iter().enumerate() {
        ledger.case(format!("#{i}"), || assert_eq!(run(c), c["r"], "#{i}"));
    }
    ledger.finish(cases.len());
}
