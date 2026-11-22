//! Hostile input to the standing guard: a very long quote, a very long
//! `rule_s` full of repeats; no panic, and time linear in the input.

mod timing;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::standing_guard::{check, read_words, GuardDeps};
use bettermemory_core::unicode::cps;

struct D;

impl GuardDeps for D {
    type Caller = ();
    type Error = ();
    fn guarded_ids(&mut self, ids: &[PyStr]) -> Vec<PyStr> {
        ids.to_vec()
    }
    fn asking(&mut self) -> bool {
        true
    }
    fn caller(&mut self) -> Result<(Option<PyStr>, ()), ()> {
        Ok((Some(cps("/c")), ()))
    }
    fn refusal_rows(&mut self, ids: &[PyStr], _: &()) -> PyValue {
        PyValue::int(ids.len() as i128)
    }
    fn words_seen(&mut self, _: &[u32]) -> bool {
        true
    }
    fn words_hold(&mut self, _: &[u32], _: &[u32]) -> bool {
        true
    }
    fn record_row(&mut self, _: &str, _: Vec<(&'static str, PyValue)>) {}
}

#[test]
fn a_long_quote_is_folded_in_linear_time() {
    let run = |n: usize| {
        let said: Vec<u32> = (0..n)
            .map(|i| if i % 3 == 0 { 0x3000 } else { 0x61 })
            .collect();
        let w = read_words(&mut D, Some(&cps("/c")), Some(&said), &cps("said")).unwrap();
        assert!(w.unwrap().checked);
    };
    timing::grows_within(
        "standing_guard quote",
        timing::TENFOLD,
        &mut || timing::once(|| run(100_000)),
        &mut || timing::once(|| run(1_000_000)),
    );
}

#[test]
fn a_long_rule_s_is_deduplicated_in_linear_time() {
    let run = |n: usize| {
        let rule_s: Vec<PyStr> = (0..n).map(|i| cps(&format!("r{}", i % (n / 2)))).collect();
        let records: Vec<PyStr> = (0..n / 4).map(|i| cps(&format!("r{i}"))).collect();
        let (resp, _) = check(&mut D, &cps("d"), &records, None, Some(&rule_s)).unwrap();
        assert!(resp.is_some());
    };
    timing::grows_within(
        "standing_guard rule_s",
        timing::TENFOLD,
        &mut || timing::once(|| run(10_000)),
        &mut || timing::once(|| run(100_000)),
    );
}

#[test]
fn an_empty_quote_is_refused_whatever_its_whitespace() {
    let said: Vec<u32> = [0x20, 0x3000, 0x85, 0x2028, 0x1c].repeat(10_000);
    assert!(read_words(&mut D, None, Some(&said), &cps("said")).is_err());
}
