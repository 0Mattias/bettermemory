//! Every case of `handlers_verify_gates.jsonl` (class i, POSIX only) and
//! `handlers_verify_gates_python.jsonl` (class ii, Python: a link loop met
//! by `Path.resolve`, which 3.11 and 3.12 raise RuntimeError for)
//! against the path gates of `handlers::verify`, over the header's tree in
//! memory (`MemFs`), the checks the gates call being the generator's
//! stubs (`tests/contract/gen_handlers_verify.py`), their calls compared
//! too.

#![cfg(unix)]

mod common;

use std::cell::RefCell;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::handlers::verify::{
    is_callers_tree, record_root, refuse_evidence_free_stamp, refuse_stale_stored_claims,
    refuse_unverifiable_stored_attestations, relative_to_root, within, AnchorFacts, DriftLists,
    Machine, ReadAt, Refused,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use serde_json::{json, Value};

const ROOT: &str = "/scratch-root";

fn at(v: &Value) -> Option<PyStr> {
    v.as_str().map(|s| cps(&s.replace('@', ROOT)))
}

fn back(p: &[u32]) -> String {
    to_string(p).expect("text").replace(ROOT, "@")
}

fn opt_back(p: Option<&[u32]>) -> Value {
    p.map_or(Value::Null, |p| json!(back(p)))
}

fn list(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|x| at(x).expect("a string"))
        .collect()
}

fn anchor(v: &Value) -> Option<AnchorFacts> {
    if v.is_null() {
        return None;
    }
    Some(AnchorFacts {
        root: at(&v["root"]),
        convicting: v["convicting"].as_bool().expect("a bool"),
    })
}

fn tree(header: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(ROOT.as_bytes(), Node::Dir);
    let path = |rel: &Value| format!("{ROOT}/{}", rel.as_str().unwrap()).into_bytes();
    for d in header["dirs"].as_array().unwrap() {
        fs.insert(&path(d), Node::Dir);
    }
    for f in header["files"].as_array().unwrap() {
        fs.insert(&path(f), Node::File(b"x\n".to_vec()));
    }
    for l in header["links"].as_array().unwrap() {
        let target = l[1].as_str().unwrap().replace('@', ROOT);
        fs.insert(&path(&l[0]), Node::Link(target.into_bytes()));
    }
    fs
}

fn spelled(r: Result<(), Refused>) -> Value {
    match r {
        Ok(()) => Value::Null,
        Err(Refused::ValueError(m)) => json!(back(&m)),
        Err(Refused::NotPorted) => json!("not ported"),
    }
}

fn read_at(calls: &RefCell<Vec<Value>>, how: ReadAt<'_>) {
    calls.borrow_mut().push(match how {
        ReadAt::Anchor(_) => json!(["anchor"]),
        ReadAt::WorktreeRoot(r) => json!(["root", opt_back(r)]),
    });
}

fn answer(fs: &MemFs, case: &Value) -> (Value, Value) {
    let cwd = format!("{ROOT}/tree");
    let m = Machine {
        fs,
        flavour: Flavour::Posix,
        cwd: (!case["nocwd"].as_bool().unwrap_or(false)).then_some(cwd.as_bytes()),
    };
    let calls = RefCell::new(Vec::new());
    let a = anchor(case.get("anchor").unwrap_or(&Value::Null));
    let origin = case.get("origin").and_then(at);
    let o = match case["f"].as_str().unwrap() {
        "stale" => {
            let stored: Vec<PyStr> = case["stored"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| cps(v.as_str().unwrap()))
                .collect();
            let fail = &case["fail"];
            spelled(refuse_stale_stored_claims(
                &m,
                &stored,
                origin.as_deref(),
                a.as_ref(),
                |s| s.to_vec(),
                |c: &PyStr, root| {
                    let name = to_string(c).unwrap();
                    calls.borrow_mut().push(json!([name, back(root)]));
                    fail.get(&name).map(|r| cps(r.as_str().unwrap()))
                },
                |c| {
                    let mut out = cps("claim ");
                    out.extend_from_slice(c);
                    out
                },
                |r| r.first() == Some(&u32::from(b'?')),
            ))
        }
        "att" => {
            let unseen = list(&case["unseen"]);
            spelled(refuse_unverifiable_stored_attestations(
                &m,
                &list(&case["stored"]),
                origin.as_deref(),
                a.as_ref(),
                |_, how| {
                    read_at(&calls, how);
                    unseen.clone()
                },
            ))
        }
        "evid" => {
            let flags = &case["flags"];
            let flag = |i: usize| flags[i].as_bool().unwrap();
            let d = &case["drift"];
            spelled(refuse_evidence_free_stamp(
                &m,
                &cps(case["id"].as_str().unwrap()),
                &cps(case["body"].as_str().unwrap()),
                origin.as_deref(),
                flag(0),
                flag(1),
                flag(2),
                a.as_ref(),
                |r| {
                    calls.borrow_mut().push(json!(["live", back(r)]));
                    case["live"].as_bool().unwrap()
                },
                |body, how| {
                    assert_eq!(to_string(body).unwrap(), case["body"].as_str().unwrap());
                    read_at(&calls, how);
                    DriftLists {
                        checked: list(&d[0]),
                        missing: list(&d[1]),
                        expected_absent: list(&d[2]),
                    }
                },
            ))
        }
        "rel" => match relative_to_root(&m, &at(&case["a"]).unwrap(), at(&case["b"]).as_deref()) {
            Ok(p) => json!(back(&p)),
            Err(e) => json!(format!("{e:?}")),
        },
        f @ ("within" | "tree") => {
            let (x, y) = (at(&case["a"]), at(&case["b"]));
            let got = if f == "within" {
                within(&m, x.as_deref(), y.as_deref())
            } else {
                is_callers_tree(&m, x.as_deref(), y.as_deref())
            };
            got.map_or_else(|e| json!(format!("{e:?}")), |b| json!(b))
        }
        "record" => {
            let wt = if case["origin"].as_bool().unwrap() {
                at(&case["wt"])
            } else {
                None
            };
            let spec = &case["anchoring"];
            let held = spec.get("anchor").map(anchor);
            let for_record = held.map(|h| {
                |rid: &[u32]| {
                    calls.borrow_mut().push(json!(["for_record", back(rid)]));
                    h
                }
            });
            json!(record_root(wt.as_deref(), &cps("m1"), for_record).map(|p| back(&p)))
        }
        other => panic!("unknown gate {other}"),
    };
    (o, Value::Array(calls.into_inner()))
}

#[test]
fn golden_handlers_verify_gates() {
    run(
        "handlers_verify_gates.jsonl",
        "golden_handlers_verify_gates",
    );
}

#[test]
fn golden_handlers_verify_gates_python() {
    run(
        "handlers_verify_gates_python.jsonl",
        "golden_handlers_verify_gates_python",
    );
}

fn run(file: &str, test: &str) {
    let lines = common::jsonl(file);
    let header = &lines[0];
    let count = header["count"].as_u64().expect("a count") as usize;
    let fs = tree(header);
    let mut ledger = Ledger::new(test, file);
    for (i, case) in lines[1..].iter().enumerate() {
        let (o, calls) = answer(&fs, case);
        let label = format!("line {}", i + 2);
        let same = o == case["o"] && calls == case["calls"];
        assert!(
            !ledger.differs_fatally(&label, same),
            "{label}: got {o} {calls}, reference {} {}",
            case["o"],
            case["calls"]
        );
    }
    assert_eq!(lines.len() - 1, count);
    assert!(count > 0);
    ledger.finish(count);
}
