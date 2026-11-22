//! Every case of `declare.jsonl` (not gated) against
//! `declare::declaring_model`: what the model is opened with and what its
//! token writer answers and re-mints over each checkout. The re-mint's
//! rule is the generator's (`tests/contract/gen_declare.py`): with the
//! policy on, the token `tok:` and the git directory, status `written`;
//! with it off, no token, status `skipped:policy`.

mod common;

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::rc::Rc;

use bettermemory_core::capture::location::ObjectKey;
use bettermemory_core::declare::{declaring_model, RemintCall};
use bettermemory_core::machine::{MachineFacts, MachineIdentity};
use bettermemory_core::membership::LivenessSeam;
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use common::membership_core::Fake;
use serde_json::{json, Value};

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn checkout(v: &Value) -> Checkout {
    let key = v["object_key"].as_array().map(|k| ObjectKey {
        device: cps(k[0].as_str().unwrap()),
        inode: cps(k[1].as_str().unwrap()),
        birth: opt(&k[2]),
    });
    Checkout {
        id: cps("k1"),
        project: cps("p1"),
        machine: cps(v["machine"].as_str().unwrap()),
        kind: cps(v["kind"].as_str().unwrap()),
        object_key: key,
        common_object_key: None,
        git_dir: opt(&v["git_dir"]),
        token: opt(&v["token"]),
        token_status: cps("own"),
        stamps: Vec::new(),
        last_head: None,
        branch: None,
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: cps("t0"),
        views: Vec::new(),
        sessions: Vec::new(),
        replaced_by: None,
        ticks: None,
    }
}

fn text(s: &[u32]) -> Value {
    json!(s
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect::<String>())
}

fn call_json(c: &RemintCall) -> Value {
    let rep = c.replacing.as_ref().map_or(Value::Null, |l| {
        json!([text(&l.token), text(&l.device), text(&l.inode)])
    });
    json!([text(&c.git_dir), c.policy, c.bare, rep, c.only_siblings])
}

#[test]
fn every_case() {
    let rows = common::jsonl("declare.jsonl");
    let mut checked = 0;
    let mut i = 0;
    while i < rows.len() {
        let head = &rows[i];
        assert_eq!(head["row"], "open", "row {i}");
        let policy = head["policy"].as_bool().unwrap();
        let identity = MachineIdentity {
            id: cps(head["opened"]["identity"].as_str().unwrap()),
            facts: MachineFacts {
                hardware: None,
                hostname: None,
                id_file: None,
            },
            predecessor: None,
            minted: false,
            cloned: false,
            facts_updated: false,
            recorded: None,
        };
        let calls: Rc<RefCell<Vec<Value>>> = Rc::default();
        let seen = calls.clone();
        let recorded = RefCell::new(None);
        let model = declaring_model(
            Fake::new(BTreeMap::new(), None, false),
            &identity,
            policy,
            opt(&head["session"]),
            LivenessSeam::Given(Box::new(|_: &Checkout, _: &View| cps("live"))),
            Box::new(|| unreachable!("no clock is read")),
            move |c: &RemintCall| {
                seen.borrow_mut().push(call_json(c));
                if c.policy {
                    let mut t = cps("tok:");
                    t.extend(&c.git_dir);
                    Ok((Some(t), cps("written")))
                } else {
                    Ok((None, cps("skipped:policy")))
                }
            },
            |_, id| {
                *recorded.borrow_mut() = Some(id.id.clone());
                Ok(())
            },
        )
        .unwrap_or_else(|_| panic!("row {i}: open raised"));
        assert_eq!(model.machine, identity.id, "row {i}");
        assert_eq!(*recorded.borrow(), Some(identity.id.clone()), "row {i}");
        assert_eq!(
            model.session().cloned(),
            opt(&head["opened"]["session"]),
            "row {i}"
        );
        let writer = model.token_writer().expect("a token writer");
        checked += 1;
        i += 1;
        while i < rows.len() && rows[i]["row"] == "write" {
            let row = &rows[i];
            assert_eq!(row["policy"], head["policy"], "row {i}");
            calls.borrow_mut().clear();
            let (token, status) = writer(&checkout(&row["checkout"]))
                .expect("no raise")
                .expect("an answer");
            let got = json!([token.as_deref().map_or(Value::Null, text), text(&status)]);
            assert_eq!(got, row["answer"], "row {i}");
            assert_eq!(
                Value::Array(calls.borrow().clone()),
                row["remint"],
                "row {i}"
            );
            checked += 1;
            i += 1;
        }
    }
    assert!(checked > 0);
    assert_eq!(checked, rows.len());
}
