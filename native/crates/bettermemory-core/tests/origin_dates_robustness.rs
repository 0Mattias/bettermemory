//! Hostile input for `origin_dates`: very long logs, very long lines,
//! integer bounds. No panic, and time linear in the log's length.

mod common;
mod timing;

use std::collections::BTreeSet;

use bettermemory_core::origin_dates as od;
use bettermemory_core::origin_history::HistoryUnread;
use bettermemory_core::pyvalue::PyStr;
use common::origin_dates::Script;
use serde_json::json;

fn t(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn log(n: usize) -> String {
    (0..n).map(|i| format!("{}\n", i % 1000)).collect()
}

#[test]
fn bounds_saturate() {
    let raw =
        "0\n253402300799\n253402300800\n18446744073709551616\n".to_string() + &"9".repeat(100_000);
    let s = Script::new(&json!({"g": [raw], "l": {}}));
    let r = od::read_author_timestamps(
        &mut s.clone(),
        &mut s.clone(),
        b"R",
        &t("HEAD"),
        &BTreeSet::new(),
    );
    assert_eq!(r.map(|d| d.len()), Ok(5));
    let long = format!("-{} 1", "a".repeat(1_000_000));
    assert_eq!(od::marked_seconds(&t(&long)), None);
    assert_eq!(od::hashed_seconds(&t(&"\u{10FFFF}".repeat(100_000))), None);
}

#[test]
fn garbage_declines() {
    let raw: String = (0..50_000)
        .map(|i| char::from_u32(0x20 + (i * 7919) % 0x7000).unwrap_or('x'))
        .collect();
    let s = Script::new(&json!({"g": [raw], "l": {}}));
    let r = od::read_author_timestamps(
        &mut s.clone(),
        &mut s.clone(),
        b"R",
        &t("HEAD"),
        &BTreeSet::new(),
    );
    assert_eq!(r, Err(Some(HistoryUnread::Error)));
}

#[test]
fn time_grows_linearly() {
    let time = |raw: &String| {
        let s = Script::new(&json!({"g": [raw], "l": {}}));
        let (mut git, mut files) = (s.clone(), s.clone());
        timing::once(|| {
            let r = od::read_author_timestamps(
                &mut git,
                &mut files,
                b"R",
                &t("HEAD"),
                &BTreeSet::new(),
            );
            assert!(r.is_ok());
            r
        })
    };
    let (small, large) = (log(20_000), log(200_000));
    timing::grows_within(
        "20,000 and 200,000 lines",
        timing::TENFOLD,
        &mut || time(&small),
        &mut || time(&large),
    );
}
