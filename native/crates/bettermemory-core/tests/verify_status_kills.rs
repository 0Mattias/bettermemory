//! Cases a plausible wrong `verify_status` fails, each answer the
//! reference's (checked against `verify_status.py` at 465bc600).

use bettermemory_core::datetime::DateTime;
use bettermemory_core::verify_status::{
    compute_verification_status, float_floor_div, micros_to_seconds, remote_verification_status,
    NaiveAwareMix, Status,
};

const DAY: i128 = 86_400_000_000;

fn at(y: i64, mo: u32, d: u32, us: u32, off: Option<i64>) -> DateTime {
    DateTime::new(y, mo, d, 0, 0, 0, us, off).expect("valid")
}

fn now() -> DateTime {
    at(2026, 3, 1, 0, Some(0))
}

fn back(d: DateTime, us: i128) -> DateTime {
    d.minus_micros(us).expect("in range")
}

#[test]
fn the_bound_is_strict_on_seconds_not_days() {
    let exactly = compute_verification_status(Some(back(now(), 30 * DAY)), now(), 30).unwrap();
    assert_eq!(
        (exactly.status, exactly.age_days),
        (Status::Fresh, Some(30))
    );
    let past = compute_verification_status(Some(back(now(), 30 * DAY + 1)), now(), 30).unwrap();
    assert_eq!((past.status, past.age_days), (Status::Stale, Some(30)));
}

#[test]
fn zero_and_negative_thresholds_flip_at_once_and_clamp() {
    let one_us = compute_verification_status(Some(back(now(), 1)), now(), -7).unwrap();
    assert_eq!(one_us.status, Status::Stale);
    assert_eq!(one_us.stale_after_days, 0);
    assert_eq!(one_us.age_days, Some(0));
    // No elapsed time is not more than zero.
    let same = compute_verification_status(Some(now()), now(), 0).unwrap();
    assert_eq!(same.status, Status::Fresh);
    // A stamp in the future clamps to zero elapsed.
    let ahead = compute_verification_status(Some(back(now(), -5 * DAY)), now(), 0).unwrap();
    assert_eq!((ahead.status, ahead.age_days), (Status::Fresh, Some(0)));
}

#[test]
fn the_day_count_follows_the_rounded_seconds() {
    // 3,000,000 days less a microsecond: total_seconds rounds up to the
    // whole day count; integer arithmetic would give one day fewer.
    let end = DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(0)).unwrap();
    let lv = back(end, 3_000_000 * DAY - 1);
    let got = compute_verification_status(Some(lv), end, 3_000_000).unwrap();
    assert_eq!(got.age_days, Some(3_000_000));
    // The rounded seconds equal the threshold, so it is still fresh.
    assert_eq!(got.status, Status::Fresh);
}

#[test]
fn naive_values() {
    let naive = at(2026, 1, 1, 0, None);
    let got = compute_verification_status(Some(naive), now(), 30).unwrap();
    assert_eq!(got.last_verified_at.unwrap().offset_us, Some(0));
    assert_eq!(
        compute_verification_status(Some(naive), at(2026, 3, 1, 0, None), 30),
        Err(NaiveAwareMix)
    );
    // Never verified asks nothing of the clock.
    let never = compute_verification_status(None, at(2026, 3, 1, 0, None), -1).unwrap();
    assert_eq!((never.status, never.stale_after_days), (Status::Never, 0));
}

#[test]
fn offsets_are_kept_and_only_plus_zero_becomes_z() {
    let two = at(2026, 1, 1, 0, Some(7_200_000_000));
    let got = compute_verification_status(Some(two), now(), 30).unwrap();
    assert!(got
        .last_verified_at
        .unwrap()
        .isoformat_utc()
        .ends_with("+02:00"));
    assert_eq!((got.status, got.age_days), (Status::Stale, Some(59)));
    let odd = at(2026, 1, 1, 0, Some(30_000_000));
    let remote = remote_verification_status(odd, 30);
    assert_eq!(
        remote.last_verified_at.unwrap().isoformat_utc(),
        "2026-01-01T00:00:00Z:30"
    );
    assert!(remote
        .recommendation
        .unwrap()
        .contains("(2026-01-01T00:00:00Z:30)"));
    assert_eq!(remote.age_days, None);
}

#[test]
fn total_seconds_is_one_rounding() {
    // 2^53 + 1 microseconds: a float conversion first, then the division,
    // rounds twice.
    let n: i128 = (1 << 53) + 1;
    assert_eq!(micros_to_seconds(n), 9_007_199_254.740_993);
    assert_eq!(micros_to_seconds(1), 1e-6);
    assert_eq!(micros_to_seconds(-1), -1e-6);
    assert_eq!(micros_to_seconds(3_000_000 * DAY - 1), 259_200_000_000.0);
    assert_eq!(float_floor_div(86_399.999_999_999_99, 86_400.0), 0.0);
    assert_eq!(float_floor_div(86_400.0, 86_400.0), 1.0);
}
