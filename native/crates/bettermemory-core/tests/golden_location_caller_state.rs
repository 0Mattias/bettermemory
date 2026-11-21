//! Every case of `location_caller_state.jsonl` (class i): the lock names,
//! the lock and stand tables after each sequence, the machine opened
//! once and recorded once per store, `cwd_of`, `same_namespace` and
//! `with_file_errors` against `location_caller`.

mod common;
mod location_caller_support;

use bettermemory_core::location_caller::{
    cwd_of, lock_name, same_namespace, with_file_errors, Locks, ProcessMachine, Stands,
};
use bettermemory_core::pyvalue::PyStr;
use common::jsonl;
use common::parity::Ledger;
use location_caller_support::{identity, loc, res, s, table, text};
use serde_json::{json, Value};

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(text)
}

fn key(v: &Value) -> Option<(PyStr, PyStr, Option<PyStr>)> {
    let a = v.as_array()?;
    Some((
        text(a[0].as_str().unwrap()),
        text(a[1].as_str().unwrap()),
        opt(&a[2]),
    ))
}

fn name_case(c: &Value) -> Value {
    let mut l = loc(&opt(&c["cwd"]).unwrap(), opt(&c["root"]), key(&c["key"]));
    l.machine = opt(&c["m"]).unwrap();
    json!(s(&lock_name(&l)))
}

fn locks_case(c: &Value) -> Value {
    let locks = Locks::new();
    let mut open: Vec<(String, bettermemory_core::location_caller::Taken)> = Vec::new();
    let name = |cwd: &str| lock_name(&loc(&text(cwd), None, None));
    for op in c["ops"].as_array().unwrap() {
        match op[0].as_str().unwrap() {
            "e" => {
                let cwd = op[1].as_str().unwrap();
                open.push((cwd.into(), locks.take(&name(cwd))));
            }
            "x" => {
                let cwd = op[1].as_str().unwrap();
                let at = open.iter().rposition(|(k, _)| k == cwd).unwrap();
                locks.give_back(open.remove(at).1);
            }
            _ => {
                for i in 0..op[1].as_u64().unwrap() {
                    let l = loc(
                        &text(&format!("{}{i}", op[2].as_str().unwrap())),
                        None,
                        None,
                    );
                    locks.with_location_lock(&l, || ());
                }
            }
        }
    }
    let entries = locks
        .snapshot()
        .iter()
        .map(|(k, u)| format!("{}\t{u}", s(k)))
        .collect();
    table(entries)
}

fn stands_case(c: &Value) -> (Value, Value) {
    let stands = Stands::new();
    let mut out = Vec::new();
    for op in c["ops"].as_array().unwrap() {
        let st = |i: usize| text(op[i].as_str().unwrap());
        match op[0].as_str().unwrap() {
            "k" => stands.keep_stand(
                &st(1),
                opt(&op[2]).as_deref(),
                &loc(&st(3), None, None),
                &res(opt(&op[4]), vec![]),
            ),
            "f" => {
                for i in 0..op[1].as_u64().unwrap() {
                    let session = text(&format!("{}{i}", op[2].as_str().unwrap()));
                    let w = loc(&text("w"), None, None);
                    stands.keep_stand(
                        &text("S"),
                        Some(&session),
                        &w,
                        &res(Some(text("c")), vec![]),
                    );
                }
            }
            "g" => stands.forget_session(&st(1)),
            "c" => stands.clear(),
            _ => out.push(match stands.stand_of(&st(1), opt(&op[2]).as_deref()) {
                None => Value::Null,
                Some(stand) => json!([
                    s(&stand.machine),
                    s(&stand.path),
                    stand.resolution.checkout.as_deref().map(s)
                ]),
            }),
        }
    }
    let entries = stands
        .keys()
        .iter()
        .map(|(store, session)| {
            let stand = stands.stand_of(store, Some(session)).unwrap();
            let checkout = stand
                .resolution
                .checkout
                .as_deref()
                .map_or("None".into(), s);
            format!(
                "{}\t{}\t{}\t{}\t{checkout}",
                s(store),
                s(session),
                s(&stand.machine),
                s(&stand.path)
            )
        })
        .collect();
    (Value::Array(out), table(entries))
}

fn machine_case(c: &Value) -> Value {
    let pm = ProcessMachine::new();
    let mut out = Vec::new();
    for op in c["ops"].as_array().unwrap() {
        match op[0].as_str().unwrap() {
            "p" => {
                let mut opened = 0;
                let got = pm.process_machine(|| {
                    opened += 1;
                    opt(&op[1]).map(|id| identity(&id)).ok_or(())
                });
                out.push(match got {
                    Ok(m) => json!([s(&m.id), opened]),
                    Err(()) => json!(["raised", opened]),
                });
            }
            "r" => {
                let mut calls = 0;
                let fails = op[3].as_bool().unwrap();
                let got = pm.recorded(
                    &text(op[1].as_str().unwrap()),
                    &identity(&text(op[2].as_str().unwrap())),
                    |_| {
                        calls += 1;
                        if fails {
                            Err(())
                        } else {
                            Ok(())
                        }
                    },
                );
                out.push(json!([calls, got.is_err()]));
            }
            _ => {
                pm.clear();
                out.push(Value::Null);
            }
        }
    }
    Value::Array(out)
}

fn ns_case(c: &Value) -> Value {
    let mut asked = Vec::new();
    let here = key(&c["here"]);
    let local = opt(&c["local"]).map(|t| s(&t).into_bytes());
    let host = opt(&c["host"]).map(|t| s(&t).into_bytes());
    let asked_cell = std::cell::RefCell::new(&mut asked);
    let r = same_namespace(
        key(&c["key"]).as_ref(),
        || {
            asked_cell.borrow_mut().push("key");
            here
        },
        host.as_deref(),
        || {
            asked_cell.borrow_mut().push("host");
            local
        },
    );
    json!([r, asked])
}

fn errs_case(c: &Value) -> (Value, bool) {
    let lines: Vec<PyStr> = c["lines"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| text(v.as_str().unwrap()))
        .collect();
    let errors: Vec<PyStr> = c["errors"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| text(v.as_str().unwrap()))
        .collect();
    let r = res(Some(text("c")), lines);
    let got = with_file_errors(&r, &errors);
    let shown: Vec<String> = got.lines.iter().map(|l| s(l)).collect();
    (json!(shown), errors.is_empty() && got == r)
}

#[test]
fn location_caller_state_every_case() {
    let file = "location_caller_state.jsonl";
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let mut ledger = Ledger::new("location_caller_state_every_case", file);
    for (i, c) in cases.iter().enumerate() {
        ledger.case(format!("#{i}"), || match c["k"].as_str().unwrap() {
            "name" => assert_eq!(name_case(c), c["r"], "#{i}"),
            "locks" => assert_eq!(locks_case(c), c["r"], "#{i}"),
            "stands" => {
                let (o, r) = stands_case(c);
                assert_eq!(o, c["o"], "#{i} stand_of");
                assert_eq!(r, c["r"], "#{i}");
            }
            "machine" => assert_eq!(machine_case(c), c["r"], "#{i}"),
            "cwd" => {
                let g = opt(&c["g"]);
                let got = cwd_of(opt(&c["cwd"]), || g).map(|t| s(&t));
                assert_eq!(json!(got), c["r"], "#{i}");
            }
            "ns" => assert_eq!(ns_case(c), c["r"], "#{i}"),
            "errs" => {
                let (r, same) = errs_case(c);
                assert_eq!(r, c["r"], "#{i}");
                assert_eq!(same, c["same"].as_bool().unwrap(), "#{i} same");
            }
            "errs_cp" => {
                let cps = |v: &Value| -> Vec<PyStr> {
                    v.as_array()
                        .unwrap()
                        .iter()
                        .map(|t| {
                            t.as_array()
                                .unwrap()
                                .iter()
                                .map(|n| u32::try_from(n.as_u64().unwrap()).unwrap())
                                .collect()
                        })
                        .collect()
                };
                let r = res(Some(text("c")), cps(&c["lines"]));
                let errors = cps(&c["errors"]);
                let got = with_file_errors(&r, &errors);
                assert_eq!(got.lines, cps(&c["r"]), "#{i}");
                let same = errors.is_empty() && got == r;
                assert_eq!(same, c["same"].as_bool().unwrap(), "#{i} same");
            }
            other => panic!("unknown kind {other}"),
        });
    }
    ledger.finish(cases.len());
}
