//! Cases a plausible wrong `remint` fails, each over a row of
//! `checkout_token_remint.jsonl` or one built here: the mark at the stale
//! bound kept, the Windows-only branches, the close error that replaces
//! the write's, the deadline read once before the wait, the claim
//! released after the read, `replace_atomic`'s budget.

#![cfg(unix)]

mod common;
mod ct_remint_script;

use bettermemory_core::checkout_token::write::WriteStatus;
use common::jsonl;
use ct_remint_script::{run, RScript};
use serde_json::{json, Value};

fn row(name: &str) -> Value {
    jsonl("checkout_token_remint.jsonl")
        .into_iter()
        .find(|r| r["n"] == name)
        .unwrap_or_else(|| panic!("a row named {name}"))
}

fn logged(row: &Value) -> (WriteStatus, Vec<String>) {
    let script = RScript::new(row);
    let answer = run(row, &script).expect("an answer");
    let log = script.w.logged();
    let log = log.as_array().expect("log").iter();
    (
        answer.status,
        log.map(|v| v.as_str().expect("text").to_owned()).collect(),
    )
}

fn count(log: &[String], prefix: &str) -> usize {
    log.iter().filter(|l| l.starts_with(prefix)).count()
}

#[test]
fn a_mark_exactly_at_the_bound_is_live_and_one_past_it_is_stale() {
    let (_, at) = logged(&row("held through the wait, a mark at the bound"));
    assert_eq!(count(&at, "create r/.git/info/bettermemory.tmp-break-"), 1);
    let (_, past) = logged(&row("held through the wait, a stale mark"));
    assert_eq!(
        count(&past, "create r/.git/info/bettermemory.tmp-break-"),
        2
    );
    assert!(past.iter().any(|l| l.ends_with("-1 644")));
}

#[test]
fn a_permission_error_at_the_claim_create_raises_off_windows_only() {
    let (posix, _) = logged(&row("link refused, create refused"));
    assert_eq!(posix, WriteStatus::FailedReadonly);
    let (win, _) = logged(&row("link refused, create refused, win32"));
    assert_eq!(win, WriteStatus::Written);
    let (mark, _) = logged(&row("held through the wait, mark create refused"));
    assert_eq!(mark, WriteStatus::FailedReadonly);
    let (mark, _) = logged(&row("held through the wait, win32 mark create refused"));
    assert_eq!(mark, WriteStatus::SkippedContended);
}

#[test]
fn the_close_error_replaces_the_write_error_of_the_own_file() {
    let r = row("claim taken, own write and close fail");
    let script = RScript::new(&r);
    let answer = run(&r, &script).expect("an answer");
    assert_eq!(answer.detail, "text of EIO (EIO)");
}

#[test]
fn the_wait_reads_its_deadline_once_before_the_loop() {
    let (status, log) = logged(&row("held through the wait, not taken after the break"));
    assert_eq!(status, WriteStatus::SkippedContended);
    let pauses: Vec<&String> = log.iter().filter(|l| l.starts_with("pause")).collect();
    assert_eq!(pauses.first().map(|s| s.as_str()), Some("pause 0.01"));
    assert!(pauses.iter().any(|p| p.as_str() == "pause 0.2"));
    // One read of the clock opens the wait, one follows each look.
    let looks = count(&log, "lstat ");
    assert_eq!(
        count(
            &log[..log
                .iter()
                .position(|l| l.starts_with("utime r/.git/info/bettermemory.tmp-58"))
                .unwrap_or(log.len())],
            "now"
        ),
        0
    );
    assert!(count(&log, "now") > looks);
}

#[test]
fn the_claim_is_released_after_the_token_is_read_and_replaced() {
    let (_, log) = logged(&row("claim release finds it held"));
    let replace = log
        .iter()
        .position(|l| l.starts_with("replace "))
        .expect("a replace");
    let unlink = log
        .iter()
        .position(|l| l == "unlink r/.git/info/bettermemory.tmp-claim")
        .expect("the claim removed");
    assert!(replace < unlink);
}

#[test]
fn replace_atomic_tries_five_times_on_windows_and_once_elsewhere() {
    let (status, log) = logged(&row("no lineage, win32 replace gives up"));
    assert_eq!(status, WriteStatus::FailedReadonly);
    assert_eq!(count(&log, "replace "), 5);
    assert_eq!(
        log.iter()
            .filter(|l| l.starts_with("pause"))
            .cloned()
            .collect::<Vec<_>>(),
        ["pause 0.01", "pause 0.02", "pause 0.04", "pause 0.08"]
    );
    let posix = json!({"n": "x", "rep": null, "replaces": ["EACCES", "ok"]});
    let script = RScript::new(&posix);
    let answer = run(&posix, &script).expect("an answer");
    assert_eq!(answer.status, WriteStatus::FailedReadonly);
    let (_, other) = logged(&row("no lineage, win32 replace other error"));
    assert_eq!(count(&other, "replace "), 1);
}

#[test]
fn only_siblings_leaves_a_sibling_and_replaces_another_token() {
    assert_eq!(
        logged(&row("claim taken, sibling, only siblings")).0,
        WriteStatus::Exists
    );
    assert_eq!(
        logged(&row("claim taken, other token, only siblings")).0,
        WriteStatus::Written
    );
    assert_eq!(logged(&row("claim taken, sibling")).0, WriteStatus::Exists);
}
