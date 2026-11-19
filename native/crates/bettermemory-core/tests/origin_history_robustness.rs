//! Hostile input for `origin_history`: very long text, many paths, deep
//! path prefixes, integer bounds and long memo sequences. No panic, and
//! each within a bound far above the reference's time for the same work.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bettermemory_core::datetime::git_seconds;
use bettermemory_core::origin_history::{
    author_dates, author_instant, log_stdin, read_lines, HistoryUnread, ReachableWalk,
    TimestampsMemo, Unwalked, MEMO_CAP, MEMO_SLOTS,
};
use bettermemory_core::pyvalue::PyStr;

const BOUND: Duration = Duration::from_secs(20);

fn t(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

#[test]
fn integer_bounds_clamp() {
    for v in [
        i128::MIN,
        i128::MAX,
        i128::from(i64::MIN),
        i128::from(i64::MAX),
        -1,
        0,
    ] {
        let d = author_instant(v);
        assert!(d.is_valid());
    }
    let many: Vec<i128> = (0..200_000)
        .map(|i| (i * 7919) % 100_003 - 50_000)
        .collect();
    let start = Instant::now();
    assert_eq!(author_dates(&many).len(), many.len());
    assert!(start.elapsed() < BOUND);
}

#[test]
fn a_million_lines_read() {
    let raw: PyStr = t(&"1700000000\n".repeat(1_000_000));
    let start = Instant::now();
    let out = read_lines(&raw, git_seconds).expect("all read");
    assert_eq!(out.len(), 1_000_000);
    let mut bad = raw.clone();
    bad.extend(t("x\n"));
    assert!(read_lines(&bad, git_seconds).is_none());
    let feeds: PyStr = vec![0x0A; 1_000_000];
    assert!(read_lines(&feeds, |l| (!l.is_empty()).then_some(())).is_none());
    assert!(start.elapsed() < BOUND);
}

#[test]
fn long_and_many_stdin_elements() {
    let long = vec![0x61; 4_000_000];
    let start = Instant::now();
    assert_eq!(
        log_stdin(std::slice::from_ref(&long), std::slice::from_ref(&long))
            .unwrap()
            .len(),
        8_000_005
    );
    let many: Vec<PyStr> = (0..100_000).map(|i| t(&format!("p{i}"))).collect();
    assert!(log_stdin(&many, &many).is_some());
    let mut late = many.clone();
    late.push(vec![0xD800]);
    assert!(log_stdin(&[], &late).is_none());
    assert!(start.elapsed() < BOUND);
}

#[test]
fn many_paths_and_deep_prefixes() {
    let mut touched: BTreeMap<PyStr, BTreeSet<PyStr>> = BTreeMap::new();
    let mut deep = String::new();
    for i in 0..2_000 {
        deep.push_str("d/");
        touched.insert(t(&format!("{deep}f")), BTreeSet::from([t(&i.to_string())]));
    }
    for i in 0..100_000 {
        touched.insert(t(&format!("w/{i}")), BTreeSet::from([t("w")]));
    }
    let walk = ReachableWalk {
        anchor: t("a"),
        head: t("h"),
        commits: vec![],
        touched,
        listed: true,
        timed_out: false,
    };
    let start = Instant::now();
    assert_eq!(walk.shas_touching(&[t("d")]).len(), 2_000);
    assert_eq!(walk.shas_touching(&[t("w")]).len(), 1);
    let specs: Vec<PyStr> = (0..20_000).map(|i| t(&format!("w/{i}"))).collect();
    assert_eq!(walk.shas_touching(&specs).len(), 1);
    let slashes = vec![0x2F; 1_000_000];
    walk.shas_touching(&[slashes]);
    assert!(start.elapsed() < BOUND);
}

#[test]
fn a_long_memo_sequence_stays_bounded() {
    let mut m: TimestampsMemo<Vec<u64>> = TimestampsMemo::new();
    let start = Instant::now();
    for i in 0..200_000u64 {
        let key = (t("r"), t(&(i % 97).to_string()));
        m.remember(&key, Arc::new(vec![]), Some(vec![i % 3]), BTreeSet::new());
        m.remember_failure(&key, None, HistoryUnread::Timeout, i as f64);
        m.recall(&key, None, Some(&BTreeSet::new()), f64::NAN);
        m.base_for(&t("r"), Some(&vec![0]), &BTreeSet::new());
        assert!(m.entries().len() <= MEMO_CAP && m.failures().len() <= MEMO_CAP);
    }
    assert!(!m.remember(
        &(t("r"), t("x")),
        Arc::new(vec![author_instant(0); MEMO_SLOTS + 1]),
        None,
        BTreeSet::new()
    ));
    for now in [f64::INFINITY, f64::NEG_INFINITY, f64::MAX, -0.0] {
        m.remember_failure(&(t("r"), t("y")), None, HistoryUnread::Error, now);
        m.recall(&(t("r"), t("y")), None, None, now);
    }
    let mut u = Unwalked::new();
    for _ in 0..1_000 {
        u.note(HistoryUnread::Shallow);
    }
    assert_eq!(u.given_up_since(u64::MAX), Some(HistoryUnread::Shallow));
    assert!(start.elapsed() < BOUND);
}
