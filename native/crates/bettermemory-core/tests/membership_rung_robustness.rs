//! Hostile input for rung 2: many checkouts recorded at one object, a
//! sighting whose texts are very long, and a token file far larger than
//! a token. No panic, and `_rung_two` grows linearly in the checkouts.

mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;
#[path = "common/membership_rung.rs"]
mod rung;
mod timing;

use bettermemory_core::pyvalue::PyValue;
use common::membership_rows::{field, lines, name};

fn base() -> PyValue {
    lines("membership_rung.jsonl")
        .into_iter()
        .find(|c| {
            name(field(c, "f")) == "rung_two" && c.get("remint") == Some(&PyValue::Bool(true))
        })
        .expect("a re-mint case")
}

fn put(v: &PyValue, key: &str, value: PyValue) -> PyValue {
    let PyValue::Dict(items) = v else {
        panic!("a dict")
    };
    let mut items: Vec<_> = items
        .iter()
        .filter(|(k, _)| k != &PyValue::str(key))
        .cloned()
        .collect();
    items.push((PyValue::str(key), value));
    PyValue::Dict(items)
}

/// The case with `n` copies of its first checkout, each its own id.
fn many(n: usize) -> PyValue {
    let c = base();
    let first = match field(field(&c, "tables"), "checkouts") {
        PyValue::List(v) => v[0].clone(),
        other => panic!("checkouts: {other:?}"),
    };
    let pairs = match field(&first, "$d") {
        PyValue::List(p) => p.clone(),
        other => panic!("a row: {other:?}"),
    };
    let rows = (0..n)
        .map(|i| {
            let p = pairs
                .iter()
                .map(|x| match x {
                    PyValue::List(kv) if kv[0] == PyValue::str("id") => {
                        PyValue::List(vec![kv[0].clone(), PyValue::str(&format!("K{i}"))])
                    }
                    other => other.clone(),
                })
                .collect();
            PyValue::Dict(vec![(PyValue::str("$d"), PyValue::List(p))])
        })
        .collect();
    let tables = PyValue::Dict(vec![(PyValue::str("checkouts"), PyValue::List(rows))]);
    put(&put(&c, "tables", tables), "remint", PyValue::Bool(false))
}

#[test]
fn rung_two_grows_linearly_in_the_checkouts() {
    let (small, large) = (many(300), many(3000));
    let (o, _) = rung::run(&large);
    assert!(matches!(o, PyValue::List(_)), "{o:?}");
    timing::grows_within(
        "rung_two",
        30,
        &mut || timing::once(|| rung::run(&small)),
        &mut || timing::once(|| rung::run(&large)),
    );
}

#[test]
fn a_long_sighting_does_not_panic() {
    let c = base();
    let long = "Q".repeat(1 << 20);
    let loc = put(field(&c, "loc"), "t", PyValue::str(&long));
    let loc = put(&loc, "g", PyValue::str(&long));
    let (o, _) = rung::run(&put(&c, "loc", loc.clone()));
    assert!(matches!(o, PyValue::List(_)));
    let now = put(&put(&c, "f", PyValue::str("token_now")), "loc", loc);
    let (o, _) = rung::run(&now);
    assert!(matches!(o, PyValue::List(_)));
}

#[test]
fn a_huge_token_file_reads_none() {
    let dir = std::env::temp_dir().join(format!("bm-rung-big-{}", std::process::id()));
    let info = dir.join(".git").join("info");
    std::fs::create_dir_all(&info).expect("a directory");
    let mut text = String::from("bettermemory checkout token v1\n");
    text.push_str(&"A".repeat(8 << 20));
    std::fs::write(info.join("bettermemory"), text).expect("a file");
    let git_dir = dir.join(".git");
    let got = rung::read_token(git_dir.to_str().expect("UTF-8"), false);
    assert_eq!(got, Ok(None));
    let _ = std::fs::remove_dir_all(&dir);
}
