//! Cases a plausible wrong port of the standing guard fails: the order of
//! its questions, the de-duplication of `rule_s`, the hints and the key
//! order of the refusal.

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::standing_guard::{check, guard, GuardDeps, GuardError, HINT, RULE_S_HINT};
use bettermemory_core::unicode::cps;

#[derive(Default)]
struct D {
    own: Vec<PyStr>,
    asking: bool,
    fail: bool,
    holds: bool,
    calls: Vec<String>,
    rows: Vec<Vec<PyStr>>,
}

impl GuardDeps for D {
    type Caller = u8;
    type Error = &'static str;
    fn guarded_ids(&mut self, _: &[PyStr]) -> Vec<PyStr> {
        self.calls.push("ids".into());
        self.own.clone()
    }
    fn asking(&mut self) -> bool {
        self.calls.push("asking".into());
        self.asking
    }
    fn caller(&mut self) -> Result<(Option<PyStr>, u8), &'static str> {
        self.calls.push("caller".into());
        if self.fail {
            Err("raised")
        } else {
            Ok((Some(cps("/c")), 7))
        }
    }
    fn refusal_rows(&mut self, ids: &[PyStr], caller: &u8) -> PyValue {
        assert_eq!(*caller, 7);
        self.rows.push(ids.to_vec());
        PyValue::List(vec![])
    }
    fn words_seen(&mut self, _: &[u32]) -> bool {
        self.calls.push("seen".into());
        true
    }
    fn words_hold(&mut self, _: &[u32], _: &[u32]) -> bool {
        self.calls.push("holds".into());
        self.holds
    }
    fn record_row(&mut self, kind: &str, _: Vec<(&'static str, PyValue)>) {
        self.calls.push(kind.into());
    }
}

fn ids(v: &[&str]) -> Vec<PyStr> {
    v.iter().map(|s| cps(s)).collect()
}

#[test]
fn nothing_guarded_asks_nothing_more() {
    let mut d = D {
        asking: true,
        ..D::default()
    };
    assert_eq!(
        check(&mut d, &cps("x"), &[], None, None).unwrap(),
        (None, None)
    );
    assert!(d.calls.is_empty(), "{:?}", d.calls);
}

#[test]
fn the_caller_is_resolved_only_when_asking() {
    let mut d = D {
        own: ids(&["r"]),
        fail: true,
        ..D::default()
    };
    assert!(check(&mut d, &cps("x"), &ids(&["r"]), None, None)
        .unwrap()
        .0
        .is_none());
    assert_eq!(d.calls, ["ids", "asking"]);
    d.asking = true;
    assert_eq!(
        check(&mut d, &cps("x"), &ids(&["r"]), None, None),
        Err(GuardError::Caller("raised"))
    );
}

#[test]
fn rule_s_is_deduplicated_in_first_order_and_follows_the_own() {
    let mut d = D {
        own: ids(&["r2"]),
        asking: true,
        ..D::default()
    };
    let rule_s = ids(&["s1", "r2", "s1", "s0"]);
    let (resp, _) = check(&mut d, &cps("x"), &ids(&["r1", "r2"]), None, Some(&rule_s)).unwrap();
    assert_eq!(d.rows, [ids(&["r2", "s1", "s0"])]);
    let resp = resp.unwrap();
    let PyValue::Dict(items) = &resp else {
        panic!()
    };
    let keys: Vec<_> = items.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(
        keys,
        ["status", "standing", "hint", "standing_write"].map(PyValue::str)
    );
    assert_eq!(items[2].1, PyValue::str(&format!("{HINT} {RULE_S_HINT}")));
}

#[test]
fn guard_writes_the_row_only_on_held_words() {
    let mut d = D {
        own: ids(&["r"]),
        asking: true,
        holds: true,
        ..D::default()
    };
    let said = cps("  the\tuser  said ");
    assert_eq!(
        guard(&mut d, &cps("x"), &ids(&["r"]), Some(&said)),
        Ok(None)
    );
    assert_eq!(d.calls.last().unwrap(), "standing_guard");
    d.holds = false;
    assert!(matches!(
        guard(&mut d, &cps("x"), &ids(&["r"]), Some(&said)),
        Err(GuardError::Value(_))
    ));
}
