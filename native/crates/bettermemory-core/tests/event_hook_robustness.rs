//! Hostile input for `event_hook`: code boundaries, clocks at the float
//! edges, scopes nested thousands deep (time at ten times the depth within
//! ten times), no panic in debug or release.

mod event_hook_tree;
mod timing;

use bettermemory_core::event_hook::{is_busy, primary_code, HookScope};
use event_hook_tree::TErr;

#[test]
fn integer_boundaries() {
    assert_eq!(primary_code(i64::MIN), 0);
    assert_eq!(primary_code(i64::MAX), 255);
    assert_eq!(primary_code(i64::MIN + 6), 6);
    assert!(is_busy("OperationalError", Some(i64::MIN + 5)));
    assert!(!is_busy("OperationalError", Some(i64::MAX)));
}

#[test]
fn clocks_at_the_edges() {
    for now in [f64::MAX, f64::INFINITY, f64::NEG_INFINITY, -0.0, f64::MIN] {
        let mut scope = HookScope::new();
        let mut clock = || now;
        let r =
            scope.within_hook_budget::<_, TErr>("stop", None, &mut clock, |s, _| Ok(s.deadline()));
        assert_eq!(r.unwrap().map(f64::to_bits), Some((now + 13.0).to_bits()));
        assert_eq!(scope, HookScope::new());
    }
}

fn nest(
    scope: &mut HookScope,
    depth: usize,
    clock: &mut dyn FnMut() -> f64,
) -> Result<usize, TErr> {
    if depth == 0 {
        return Ok(0);
    }
    scope
        .within_hook_budget("prompt", None, clock, |s, _| {
            s.suppressed(|s| nest(s, depth - 1, &mut || 0.0))
                .map(|n| n + 1)
        })
        .map_err(|_| unreachable!())
}

#[test]
fn deep_nesting_grows_linearly() {
    let run = |depth: usize| {
        let mut scope = HookScope::new();
        let mut t = 0.0;
        let mut clock = || {
            t += 1.0;
            t
        };
        assert_eq!(nest(&mut scope, depth, &mut clock).unwrap(), depth);
        assert_eq!(scope, HookScope::new());
    };
    timing::grows_within(
        "nested scopes: 100 and 1,000 deep",
        timing::TENFOLD,
        &mut || timing::once(|| run(100)),
        &mut || timing::once(|| run(1_000)),
    );
}
