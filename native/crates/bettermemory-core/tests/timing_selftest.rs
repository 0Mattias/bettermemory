//! The time claims of `timing`, on work whose growth is known: ten times
//! the work passes a bound of thirty times, a hundred times the work (what
//! quadratic code costs on ten times the input) does not, however many
//! rounds are taken; forty times the work passes `TENFOLD` and a hundred
//! times fails it; a claim missed by far fails in three rounds. The amount
//! of work is sized on the machine first, so the small size takes about
//! ten milliseconds in any build.

mod timing;

use std::hint::black_box;
use std::time::Duration;

/// `steps` steps the compiler cannot fold away.
fn work(steps: u64) -> u64 {
    let mut acc = 0u64;
    for i in 0..steps {
        acc = black_box(acc.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(i));
    }
    acc
}

/// The steps that take about `ms` milliseconds here, from the least of
/// five timed probes.
fn steps_for(ms: u64) -> u64 {
    const PROBE: u64 = 2_000_000;
    let took = (0..5)
        .map(|_| timing::once(|| work(PROBE)))
        .min()
        .expect("a probe");
    let nanos = u64::try_from(took.as_nanos()).unwrap_or(u64::MAX).max(1);
    (PROBE.saturating_mul(ms).saturating_mul(1_000_000) / nanos).max(1)
}

#[test]
fn ten_times_the_work_is_within_thirty_times() {
    let small = steps_for(10);
    timing::grows_within(
        "ten times the work",
        30,
        &mut || timing::once(|| work(small)),
        &mut || timing::once(|| work(small * 10)),
    );
}

#[test]
#[should_panic(expected = "more than 30 times")]
fn a_hundred_times_the_work_is_not() {
    let small = steps_for(10);
    timing::grows_within(
        "a hundred times the work",
        30,
        &mut || timing::once(|| work(small)),
        &mut || timing::once(|| work(small * 100)),
    );
}

#[test]
fn forty_times_the_work_passes_the_tenfold_bound() {
    let small = steps_for(10);
    timing::grows_within(
        "forty times the work",
        timing::TENFOLD,
        &mut || timing::once(|| work(small)),
        &mut || timing::once(|| work(small * 40)),
    );
}

#[test]
#[should_panic(expected = "more than 60 times")]
fn a_hundred_times_the_work_fails_the_tenfold_bound() {
    let small = steps_for(10);
    timing::grows_within(
        "a hundred times the work",
        timing::TENFOLD,
        &mut || timing::once(|| work(small)),
        &mut || timing::once(|| work(small * 100)),
    );
}

#[test]
#[should_panic(expected = "the least of 3 rounds: more than 10 times")]
fn a_claim_missed_by_far_fails_in_three_rounds() {
    let small = steps_for(10);
    timing::grows_within(
        "a hundred times the work against ten",
        10,
        &mut || timing::once(|| work(small)),
        &mut || timing::once(|| work(small * 100)),
    );
}

#[test]
fn a_bound_is_held_by_the_least_run() {
    let small = steps_for(10);
    timing::ends_within(
        "ten milliseconds of work",
        Duration::from_secs(5),
        &mut || timing::once(|| work(small)),
    );
}

#[test]
#[should_panic(expected = "the bound is")]
fn work_past_its_bound_fails() {
    let small = steps_for(10);
    timing::ends_within(
        "ten milliseconds of work",
        Duration::from_micros(100),
        &mut || timing::once(|| work(small)),
    );
}

#[test]
fn the_claim_is_the_factor_plus_the_slack() {
    let ms = Duration::from_millis;
    assert!(timing::within(ms(10), ms(349), 30));
    assert!(!timing::within(ms(10), ms(350), 30));
    assert!(timing::within(Duration::ZERO, ms(49), 30));
    assert!(!timing::within(Duration::ZERO, ms(50), 30));
    assert!(timing::within(Duration::MAX, Duration::MAX - ms(1), 2));
}
