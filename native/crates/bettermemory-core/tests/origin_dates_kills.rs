//! Cases a plausible wrong `origin_dates` fails: each names the mistake.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::origin_dates as od;
use bettermemory_core::origin_history::{author_dates, HistoryUnread};
use bettermemory_core::pyvalue::PyStr;
use common::origin_dates::Script;
use serde_json::json;

fn t(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn script(g: serde_json::Value, l: serde_json::Value) -> Script {
    Script::new(&json!({"g": g, "l": l}))
}

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const C: &str = "cccccccccccccccccccccccccccccccccccccccc";

/// A shallow check before the whole read declines: a malformed line after
/// a cut line is an error, not `SHALLOW`.
#[test]
fn malformed_line_beats_cut() {
    let s = script(json!([format!("{C} 5\n{A} x\n")]), json!({}));
    let cut: BTreeSet<PyStr> = [t(C)].into();
    let r = od::read_author_timestamps(&mut s.clone(), &mut s.clone(), b"R", &t("HEAD"), &cut);
    assert_eq!(r, Err(Some(HistoryUnread::Error)));
    assert_eq!(s.0.borrow().failed, 1);
}

/// Splitting on any whitespace instead of the first space: two spaces
/// leave " 5", which `seconds` strips.
#[test]
fn partition_on_first_space() {
    assert_eq!(od::hashed_seconds(&t(&format!("{A}  5"))), Some((t(A), 5)));
    assert_eq!(od::hashed_seconds(&t(&format!("{A} 5 6"))), None);
}

/// A boundary's seconds read 0 whatever git printed, and the boundary
/// mark is the dash alone.
#[test]
fn boundary_reads_zero() {
    assert_eq!(
        od::marked_seconds(&t(&format!("-{A} 99"))),
        Some((false, t(A), 0))
    );
    assert_eq!(
        od::marked_seconds(&t(&format!("<{A} 99"))),
        Some((true, t(A), 99))
    );
    assert_eq!(od::marked_seconds(&t(&format!("+{A} 99"))), None);
    assert_eq!(od::marked_seconds(&[]), None);
    // An uppercase hash is no full commit hash.
    assert_eq!(
        od::hashed_seconds(&t(&format!("{} 1", A.to_uppercase()))),
        None
    );
}

/// A full hash's refusal is an error and runs no probe; HEAD's runs them.
#[test]
fn named_revision_skips_probes() {
    let s = script(json!([null]), json!({}));
    let r = od::read_author_timestamps(
        &mut s.clone(),
        &mut s.clone(),
        b"R",
        &t(A),
        &BTreeSet::new(),
    );
    assert_eq!(r, Err(Some(HistoryUnread::Error)));
    assert_eq!(s.0.borrow().calls.len(), 1);
}

/// A probe that timed out answers `TIMEOUT` before any leaf is read.
#[test]
fn probe_timeout_reads_no_leaf() {
    let s = script(json!(["T"]), json!({"refusal": false, "above": true}));
    assert_eq!(
        od::refused_head(&mut s.clone(), &mut s.clone(), b"R"),
        Some(HistoryUnread::Timeout)
    );
    assert_eq!(s.0.borrow().calls.len(), 1);
}

/// `show-ref` exit 2 with a packed line naming the ref is an error, and
/// `locate` is asked only where `gitdir_at` finds none.
#[test]
fn packed_line_is_error() {
    let s = script(
        json!(["refs/heads/m", [2, ""]]),
        json!({"gd": "at", "packed": true}),
    );
    assert_eq!(
        od::unborn_or_broken(&mut s.clone(), &mut s.clone(), b"R"),
        Some(HistoryUnread::Error)
    );
    assert!(!s.0.borrow().calls.contains(&json!("loc")));
}

/// The extension needs the boundary to be `base_head` itself, and sorts
/// the added dates among the base's.
#[test]
fn extension_sorts_and_needs_boundary() {
    let b = "b".repeat(40);
    let s = script(json!([[0, format!(">{b} 20\n-{A} 10\n")]]), json!({}));
    let base = author_dates(&[10, 30]);
    let got = od::extended_author_timestamps(
        &mut s.clone(),
        b"R",
        &t(A),
        &base,
        &t(&b),
        &BTreeSet::new(),
    );
    assert_eq!(got, Ok(Some(author_dates(&[10, 20, 30]))));
    let s = script(json!([[0, format!(">{b} 20\n-{C} 10\n")]]), json!({}));
    let got = od::extended_author_timestamps(
        &mut s.clone(),
        b"R",
        &t(A),
        &base,
        &t(&b),
        &BTreeSet::new(),
    );
    assert_eq!(got, Ok(None));
}

/// Hex ends at `f` and starts at `0`: the code points just outside each
/// range (`g`, `` ` ``, `/`, `:`) and the upper case letters are refused.
#[test]
fn full_sha_hex_bounds() {
    let of = |c: char, n: usize| -> Vec<u32> { std::iter::repeat_n(c as u32, n).collect() };
    for n in [40, 64] {
        for c in ['0', '9', 'a', 'f'] {
            assert!(od::is_full_commit_sha(&of(c, n)), "{c} x {n}");
        }
        for c in ['g', '`', '/', ':', 'F', 'G'] {
            assert!(!od::is_full_commit_sha(&of(c, n)), "{c} x {n}");
        }
    }
    assert!(!od::is_full_commit_sha(&of('a', 39)));
    assert!(!od::is_full_commit_sha(&of('a', 41)));
}
