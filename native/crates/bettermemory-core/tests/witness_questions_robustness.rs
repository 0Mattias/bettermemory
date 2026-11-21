//! Hostile input to every reader of `witness_questions`: arbitrary decoded
//! JSON (seeded), very deep and very long values, integer boundaries. No
//! reader panics, and reading many views grows in proportion.

mod timing;
#[allow(dead_code)]
mod witness;

use bettermemory_core::json::loads;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::witness_questions as wq;
use serde_json::json;
use witness::{capture, checkout, cps, location, view};

/// A small linear congruential source, so the cases are the same on
/// every run.
struct Seeded(u64);

impl Seeded {
    fn next(&mut self, n: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 33) % n
    }

    fn value(&mut self, depth: u32) -> PyValue {
        let words = [
            "views",
            "path",
            "remint",
            "status",
            "token",
            "exists",
            "written",
            "object",
            "stamps",
            "kind",
            "last_head",
            "superseded",
            "replacing",
            "remint_ok",
            "common_object",
            "write_token",
            "/r",
        ];
        match if depth > 4 {
            self.next(6)
        } else {
            self.next(9)
        } {
            0 => PyValue::None,
            1 => PyValue::Bool(self.next(2) == 1),
            2 => PyValue::int(i128::from(self.next(5) as i64 - 2) * i128::from(i64::MAX)),
            3 => PyValue::Float([f64::NAN, f64::INFINITY, -0.0, 1.5, 1e300][self.next(5) as usize]),
            4 => PyValue::str(words[self.next(words.len() as u64) as usize]),
            5 => PyValue::str("01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            6 | 7 => PyValue::List((0..self.next(7)).map(|_| self.value(depth + 1)).collect()),
            _ => PyValue::Dict(
                (0..self.next(6))
                    .map(|_| {
                        let k = PyValue::str(words[self.next(words.len() as u64) as usize]);
                        (k, self.value(depth + 1))
                    })
                    .collect(),
            ),
        }
    }
}

fn every_reader(v: &PyValue) {
    let k = checkout(&json!({"token": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "key": ["1", "2", null]}));
    let _ = wq::AnsweredToken::new(v, true).call(&k);
    let _ = wq::AnsweredToken::new(v, false).call(&k);
    let loc = location(&json!({"machine": "m1", "token": null, "written": null,
        "key": ["1", "2", null]}));
    let mut model = |_: &[u32]| vec![(cps("m1"), witness::key(&json!(["1", "2", "3"])))];
    let _ = wq::AnsweredToken::new(v, true).located(&loc, &mut model);
    let cap = capture(
        &json!({"git_dir": "/r/.git", "token": null, "written": null,
        "key": null, "kind": "primary"}),
    );
    let mut stub = |_: &[u32], _: bool, _: bool, _: Option<_>| (None, "skipped:policy".into());
    let _ = wq::remint_answer(&cap, v, &mut stub);
    let _ = wq::remint_refused(v, v);
    if let Ok(live) = wq::AnsweredLiveness::new(v) {
        let _ = live.call(&k, &view("/r"));
    }
    let _ = wq::asked_views(v);
    let _ = wq::asked_head(Some(v));
    let _ = wq::asked_stamps(Some(v));
}

#[test]
fn seeded_arbitrary_values_reach_every_reader_without_panic() {
    let mut rng = Seeded(0x5eed);
    for _ in 0..20_000 {
        let v = rng.value(0);
        every_reader(&v);
        every_reader(&PyValue::Dict(vec![(PyValue::str("views"), v.clone())]));
        every_reader(&PyValue::Dict(vec![(
            PyValue::str("views"),
            PyValue::List(vec![v]),
        )]));
    }
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::int(1);
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn deep_values_raise_recursion_error_or_read() {
    let deep = nested(20_000);
    let q = PyValue::Dict(vec![(
        PyValue::str("views"),
        PyValue::List(vec![PyValue::Dict(vec![
            (PyValue::str("path"), PyValue::str("/r")),
            (PyValue::str("kind"), deep.clone()),
        ])]),
    )]);
    assert_eq!(wq::asked_views(&q), Err(PyError::RecursionError));
    let shallow = nested(50);
    let q = PyValue::Dict(vec![(
        PyValue::str("views"),
        PyValue::List(vec![PyValue::Dict(vec![
            (PyValue::str("path"), PyValue::str("/r")),
            (
                PyValue::str("object"),
                PyValue::List(vec![shallow.clone(), shallow.clone(), shallow]),
            ),
        ])]),
    )]);
    assert!(wq::asked_views(&q).is_ok());
    let a = PyValue::Dict(vec![(
        PyValue::str("views"),
        PyValue::Dict(vec![(PyValue::str("/r"), deep)]),
    )]);
    assert!(matches!(
        wq::AnsweredLiveness::new(&a),
        Err(PyError::RecursionError)
    ));
    every_reader(&nested(100_000));
}

#[test]
fn long_and_boundary_values() {
    let long = "a".repeat(1 << 20);
    let text = format!(
        r#"{{"views": [{{"path": "{long}", "last_head": "{long}", "token": "{long}",
        "stamps": [["n", "{d}", -9223372036854775809, 18446744073709551616, "0"]]}}]}}"#,
        d = "9".repeat(4300)
    );
    let q = loads(&cps(&text)).unwrap();
    let got = wq::asked_views(&q).unwrap();
    assert_eq!(got[0].1.stamps[0].numbers[0], "9".repeat(4300));
    assert_eq!(got[0].1.last_head, None);
    // One digit more than `int()` reads: the stamp is left out.
    let text = text.replace(&"9".repeat(4300), &"9".repeat(4301));
    let got = wq::asked_views(&loads(&cps(&text)).unwrap()).unwrap();
    assert!(got[0].1.stamps.is_empty());
}

#[test]
fn many_views_grow_in_proportion() {
    let make = |n: usize| {
        let one = r#"{"path": "/r", "kind": "linked", "object": [1, 2, null],
            "last_head": "0123456789ABCDEF0123456789abcdef01234567",
            "stamps": [["d", 1, 2, 3, 4], ["e", "5", 6, 7, 8]]}"#;
        let all = vec![one; n].join(", ");
        loads(&cps(&format!(r#"{{"views": [{all}]}}"#))).unwrap()
    };
    let (small, large) = (make(500), make(5_000));
    timing::grows_within(
        "asked_views: 500 and 5,000 views",
        timing::TENFOLD,
        &mut || timing::once(|| wq::asked_views(&small).unwrap().len()),
        &mut || timing::once(|| wq::asked_views(&large).unwrap().len()),
    );
}
