//! Hostile input for `ntpath.realpath` on Windows: a path of thousands of
//! missing components, a readlink chain thousands long and a long cycle.
//! No panic, in debug or release; the deep path's time grows within the
//! bound the reference's own walk sets (each step joins the tail again,
//! so ten times the components may cost a hundred times as long).

mod ntfake;
mod timing;

use bettermemory_core::fsdata::realpath::nt_realpath;
use bettermemory_core::unicode::cps;
use ntfake::Table;
use serde_json::{json, Map, Value};

fn deep(n: usize) -> Vec<u32> {
    let mut q = String::from("C:");
    for _ in 0..n {
        q.push_str("\\d");
    }
    cps(&q)
}

#[test]
fn a_deep_missing_path_resolves_to_itself() {
    let nt = Table::of(&json!({}));
    let q = deep(3000);
    assert_eq!(nt_realpath(&nt, &q, Ok(&cps("C:\\w"))), Ok(q));
}

#[test]
fn a_deep_missing_path_grows_as_the_reference_walks() {
    let nt = Table::of(&json!({}));
    let (small, large) = (deep(100), deep(1000));
    let cwd = cps("C:\\w");
    timing::grows_within(
        "deep missing path",
        150,
        &mut || timing::once(|| nt_realpath(&nt, &small, Ok(&cwd))),
        &mut || timing::once(|| nt_realpath(&nt, &large, Ok(&cwd))),
    );
}

fn chain(n: usize, cycle: bool) -> Value {
    let mut r = Map::new();
    for i in 0..n {
        let next = if cycle && i + 1 == n { 0 } else { i + 1 };
        r.insert(format!("C:\\l{i}"), Value::String(format!("C:\\l{next}")));
    }
    json!({"f": {"C:\\l0": 1921}, "r": r})
}

#[test]
fn a_long_chain_and_a_long_cycle_end() {
    let cwd = cps("C:\\w");
    let nt = Table::of(&chain(5000, false));
    assert_eq!(
        nt_realpath(&nt, &cps("C:\\l0"), Ok(&cwd)),
        Ok(cps("C:\\l5000"))
    );
    let nt = Table::of(&chain(5000, true));
    assert_eq!(
        nt_realpath(&nt, &cps("C:\\l0"), Ok(&cwd)),
        Ok(cps("C:\\l0"))
    );
}
