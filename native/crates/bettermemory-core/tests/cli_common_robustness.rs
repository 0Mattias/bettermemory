//! Hostile input to `entry` and `cli::common`: very long values, every
//! year bound, integer bounds. No panic, and time linear in the value.

mod timing;

use std::path::PathBuf;

use bettermemory_core::cli::common::{
    key_unreadable_line, migration_line, parse_iso_cutoff, CutoffFault, MigrationTotals,
};
use bettermemory_core::datetime::{DateError, DateTime};
use bettermemory_core::entry::route_hook_word;
use bettermemory_core::log::ring::KeyUnreadable;
use bettermemory_core::pyvalue::PyStr;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn at(value: &[u32], now: &str) -> Result<(), CutoffFault> {
    let now = DateTime::from_isoformat(&cps(now)).unwrap();
    parse_iso_cutoff(value, &cps("--since"), "warn", &now).map(|_| ())
}

#[test]
fn year_bounds_raise_not_panic() {
    for (v, now) in [
        ("0001-01-01T00:00:00+23:59", "2026-01-01T00:00:00+00:00"),
        (
            "9999-12-31T23:59:59.999999-23:59",
            "2026-01-01T00:00:00+00:00",
        ),
        ("2026-01-01T00:00:00Z", "9999-12-31T00:00:00.000001+00:00"),
        (
            "9999-12-31T23:59:59.999999Z",
            "9999-12-30T23:59:59.999999+00:00",
        ),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00"),
    ] {
        match at(&cps(v), now) {
            Ok(()) | Err(CutoffFault::Exit(_)) => {}
            Err(CutoffFault::Raised { error, .. }) => assert_eq!(error, DateError::OverflowError),
        }
    }
}

#[test]
fn hostile_values_answer() {
    let mut values: Vec<PyStr> = vec![
        vec![0x5A; 100_000],
        vec![0xDCFF; 10_000],
        vec![0x10FFFF; 10_000],
        vec![0; 1_000],
        cps(&"9".repeat(50_000)),
    ];
    values.push(cps(&format!("2026-05-25T05:25:35{}", "0".repeat(100_000))));
    for v in &values {
        let _ = at(v, "2026-05-25T12:00:00+00:00");
    }
    let argv: Vec<PyStr> = vec![vec![0xD800; 100_000]; 3];
    assert_eq!(route_hook_word(&argv, |_| 0), None);
    let exc = KeyUnreadable {
        path: PathBuf::from("/k".repeat(10_000)),
        errno: None,
        reason: "\n\u{1b}".repeat(10_000),
    };
    assert!(key_unreadable_line(&exc, &cps("p")).len() > 60_000);
    let t = MigrationTotals {
        projects: i64::MIN,
        checkouts: i64::MAX,
        memory_homes: i64::MIN,
        tombstone_homes: i64::MIN,
        episode_homes: i64::MIN,
        proposals: -1,
        log_rows: 0,
    };
    let line: String = migration_line(&t)
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect();
    assert!(
        line.contains("-27670116110564327424 records homed"),
        "{line}"
    );
}

#[test]
fn long_values_are_linear() {
    let run = |n: usize| {
        let mut v = cps("2026-05-25T05:25:35Z");
        v.extend(std::iter::repeat_n(0x5A, n));
        let _ = at(&v, "2026-05-25T12:00:00+00:00");
    };
    timing::grows_within(
        "parse_iso_cutoff: 20,000 and 200,000 trailing Z",
        timing::TENFOLD,
        &mut || timing::once(|| run(20_000)),
        &mut || timing::once(|| run(200_000)),
    );
}
