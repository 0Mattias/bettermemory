//! Hostile input to `session`: many ids, long registries, integer and
//! float extremes. No panic but the documented one, and linear growth.

mod common;
mod timing;

use std::time::Duration;

use bettermemory_core::session::{SessionRegistry, SessionState};
use common::session::{cps, Fx};

fn fx() -> Fx {
    Fx {
        now: 0.0,
        n: 0,
        reads: 0,
        draws: 0,
    }
}

fn issue_consume(n: usize) -> Duration {
    let mut f = fx();
    let st = SessionState::new(&mut f);
    let ids: Vec<Vec<u32>> = (0..n).map(|i| cps(&format!("m{i}"))).collect();
    timing::once(|| {
        let mut g = st.lock();
        g.issue_use_tokens(&ids, &mut f);
        for id in ids.iter().step_by(2) {
            g.purge_use_token(id);
        }
        f.now = 1e9;
        g.advance_turn(&mut f);
        g.pop_expired_use_tokens().len()
    })
}

#[test]
fn many_tokens_grow_linearly() {
    timing::grows_within(
        "issue, purge, evict",
        30,
        &mut || issue_consume(2_000),
        &mut || issue_consume(20_000),
    );
}

fn registry(n: usize) -> Duration {
    registry_capped(n, 64)
}

fn registry_capped(n: usize, cap: i64) -> Duration {
    let mut f = fx();
    let reg = SessionRegistry::new(cap);
    let keys: Vec<Vec<u32>> = (0..n).map(|i| cps(&format!("k{i}"))).collect();
    timing::once(|| {
        for k in &keys {
            reg.for_request(Some(k.clone()), &mut f);
        }
        reg.stats()
    })
}

#[test]
fn registry_churn_grows_linearly() {
    timing::grows_within("registry churn", 30, &mut || registry(2_000), &mut || {
        registry(20_000)
    });
}

/// A cap that grows with the input: every insert at the cap pops the
/// least recent client, which stays constant time however many have gone.
#[test]
fn registry_churn_at_a_large_cap_grows_linearly() {
    timing::grows_within(
        "registry churn, cap n/3",
        30,
        &mut || registry_capped(6_000, 2_000),
        &mut || registry_capped(60_000, 20_000),
    );
}

#[test]
fn extremes_do_not_panic() {
    let mut f = fx();
    let st = SessionState::new(&mut f);
    let mut g = st.lock();
    for now in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, f64::MAX, -0.0] {
        f.now = now;
        g.issue_use_tokens(&[cps("a"), cps("")], &mut f);
        g.advance_turn(&mut f);
        for ttl in [i64::MIN, i64::MAX, 0] {
            for min in [
                None,
                Some(f64::NAN),
                Some(f64::INFINITY),
                Some(f64::NEG_INFINITY),
            ] {
                g.consume_old_tokens(ttl, min, None, &mut f);
            }
        }
        g.pop_expired_use_tokens();
    }
    g.turn_counter = i64::MIN;
    g.consume_old_tokens(i64::MAX, None, None, &mut f);
    for cap in [i64::MIN, -1, 0, i64::MAX] {
        let reg = SessionRegistry::new(cap);
        reg.for_request(Some(vec![0x10FFFF; 10_000]), &mut f);
        reg.for_request(None, &mut f);
    }
}

#[test]
#[should_panic(expected = "the turn counter past i64::MAX")]
fn turn_counter_bound_is_a_stated_panic() {
    let mut f = fx();
    let st = SessionState::new(&mut f);
    let mut g = st.lock();
    g.turn_counter = i64::MAX;
    g.advance_turn(&mut f);
}
