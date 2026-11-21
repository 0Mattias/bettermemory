//! Inputs on which a plausible wrong port of `entry` and `cli::common`
//! answers otherwise than the reference.

use bettermemory_core::cli::common::{
    exit_if_bundle_file, isoformat_fault, migration_line, parse_iso_cutoff, CutoffFault,
    MigrationTotals,
};
use bettermemory_core::datetime::{DateError, DateTime};
use bettermemory_core::entry::{is_hook_word, route_hook_word};
use bettermemory_core::pyvalue::PyStr;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn text(t: &[u32]) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn now() -> DateTime {
    DateTime::from_isoformat(&cps("2026-05-25T12:00:00+00:00")).unwrap()
}

fn err_of(value: &str, far: &str) -> String {
    match parse_iso_cutoff(&cps(value), &cps("--since"), far, &now()) {
        Err(CutoffFault::Exit(e)) => text(&e.stderr),
        other => panic!("{value}: {other:?}"),
    }
}

/// Only the first word routes, matched exactly (no case folding, no strip).
#[test]
fn hook_words_exact_and_first() {
    for w in [
        "HOOK",
        "hook ",
        " hook",
        "hooks",
        "prompt_recall",
        "stop",
        "prompt",
    ] {
        assert!(!is_hook_word(&cps(w)), "{w}");
    }
    let argv = vec![cps("serve"), cps("hook")];
    assert_eq!(route_hook_word(&argv, |_| 9), None);
    let argv = vec![cps("prompt-recall"), cps("x")];
    assert_eq!(route_hook_word(&argv, |a| a.len() as i32), Some(2));
}

/// `\d` is Unicode's decimal digits; the shape is exactly ten characters.
#[test]
fn bare_date_hint_shape() {
    let hint = "if you meant midnight UTC";
    assert!(err_of(
        "\u{0662}\u{0660}\u{0662}\u{0666}-\u{0660}\u{0665}-\u{0662}\u{0665}",
        "refuse"
    )
    .contains(hint));
    assert!(err_of("2026-13-01", "refuse").contains(hint));
    for v in [
        "2026-13-011",
        "2026/13/01",
        "2026-1-001",
        "\u{00b2}026-13-01",
        "202x-13-01",
        "2026-13-0",
    ] {
        assert!(!err_of(v, "refuse").contains(hint), "{v}");
    }
}

/// The far-future bound is strict, compares instants, and any word but
/// "refuse" warns.
#[test]
fn far_future_rules() {
    let at = |v: &str, far: &str| parse_iso_cutoff(&cps(v), &cps("--f"), far, &now());
    assert!(matches!(at("2026-05-26T12:00:00Z", "refuse"), Ok(c) if c.warning.is_empty()));
    assert!(matches!(
        at("2026-05-26T12:00:00.000001Z", "refuse"),
        Err(CutoffFault::Exit(_))
    ));
    assert!(at("2026-05-26T13:00:00+01:00", "refuse").is_ok());
    assert!(matches!(
        at("2026-05-26T11:00:01-01:00", "refuse"),
        Err(CutoffFault::Exit(_))
    ));
    for far in ["warn", "Refuse", ""] {
        let c = at("2126-01-01T00:00:00Z", far).unwrap();
        assert!(
            text(&c.warning).starts_with("--f: warning \u{2014} '2126"),
            "{far}"
        );
    }
}

/// Every `Z` is replaced, and the canonical form is UTC with `Z`.
#[test]
fn z_and_canonical() {
    let c = parse_iso_cutoff(
        &cps("2026-05-25T05:25:35+05:30"),
        &cps("x"),
        "refuse",
        &now(),
    )
    .unwrap();
    assert_eq!(text(&c.canonical), "2026-05-24T23:55:35Z");
    assert_eq!(c.parsed.isoformat(), "2026-05-25T05:25:35+05:30");
    assert!(err_of("2026-05-25T05:25:35ZZ", "refuse").contains("invalid ISO"));
    let naive = err_of("2026-05-25T10:00:00", "refuse");
    assert!(naive.contains("'2026-05-25T10:00:00T00:00:00Z'"), "{naive}");
}

#[test]
fn bundle_and_migration() {
    assert_eq!(exit_if_bundle_file(None, 1, &cps("p")), Ok(()));
    let e = exit_if_bundle_file(Some(cps("why")), 0, &cps("p")).unwrap_err();
    assert_eq!((e.code, text(&e.stderr)), (0, "p: why\n".to_owned()));
    let t = MigrationTotals {
        memory_homes: i64::MAX,
        tombstone_homes: i64::MAX,
        episode_homes: 1,
        ..MigrationTotals::default()
    };
    assert!(text(&migration_line(&t))
        .starts_with("membership migrated: 18446744073709551615 records homed"));
}

/// An exception from `fromisoformat` other than `ValueError` propagates
/// with nothing written; only `ValueError` becomes the exit-1 message.
#[test]
fn non_value_error_from_isoformat_propagates() {
    let value = cps("2026-05-25");
    match isoformat_fault(
        &value,
        &cps("--since"),
        &cps("'2026-05-25'"),
        DateError::OverflowError,
    ) {
        CutoffFault::Raised { error, stderr } => {
            assert_eq!(error, DateError::OverflowError);
            assert!(stderr.is_empty());
        }
        other => panic!("{other:?}"),
    }
    match isoformat_fault(
        &value,
        &cps("--since"),
        &cps("'2026-05-25'"),
        DateError::ValueError,
    ) {
        CutoffFault::Exit(e) => {
            assert_eq!(e.code, 1);
            assert_eq!(
                text(&e.stderr),
                "--since: invalid ISO timestamp '2026-05-25'. Expected e.g. \
                 '2026-05-25T05:25:35Z' (or '2026-05-25T00:00:00Z' if you meant midnight UTC).\n"
            );
        }
        other => panic!("{other:?}"),
    }
}
