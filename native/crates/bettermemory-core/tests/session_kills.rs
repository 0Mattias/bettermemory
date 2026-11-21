//! Cases a plausible wrong `session` fails: the dict order of re-issued
//! and re-evicted tokens, the strict eviction bound, both readiness axes,
//! what `reset` keeps, the LRU's touch and its eviction at a cap of 0,
//! and one state for one key under concurrent first requests.

mod common;

use std::collections::BTreeSet;
use std::sync::{Arc, Barrier};

use bettermemory_core::session::{SessionRegistry, SessionState, DEFAULT_MAX_CLIENTS};
use common::session::{cps, text, Fx};

fn fx() -> Fx {
    Fx {
        now: 1000.0,
        n: 0,
        reads: 0,
        draws: 0,
    }
}

fn ids(s: &[&str]) -> Vec<Vec<u32>> {
    s.iter().map(|x| cps(x)).collect()
}

fn pending(st: &SessionState) -> Vec<String> {
    st.lock()
        .pending_use_tokens
        .keys()
        .map(|k| text(k))
        .collect()
}

#[test]
fn reissue_keeps_place_and_takes_new_token() {
    let mut f = fx();
    let st = SessionState::new(&mut f);
    let out = st.lock().issue_use_tokens(&ids(&["a", "b", "a"]), &mut f);
    let got: Vec<(String, String)> = out.iter().map(|(k, v)| (text(k), v.clone())).collect();
    assert_eq!(
        got,
        vec![
            ("a".into(), "use_0000000000000004".into()),
            ("b".into(), "use_0000000000000003".into())
        ]
    );
    assert_eq!(f.reads, 1);
    st.lock().issue_use_tokens(&ids(&["c", "a"]), &mut f);
    assert_eq!(pending(&st), ["a", "b", "c"]);
    st.lock().purge_use_token(&cps("a"));
    st.lock().issue_use_tokens(&ids(&["a"]), &mut f);
    assert_eq!(pending(&st), ["b", "c", "a"]);
}

#[test]
fn eviction_is_strict_at_the_ttl() {
    let mut f = fx();
    let st = SessionState::new(&mut f);
    st.lock().issue_use_tokens(&ids(&["a"]), &mut f);
    f.now = 2800.0;
    st.lock().advance_turn(&mut f);
    assert_eq!(pending(&st), ["a"]);
    f.now = 2800.5;
    st.lock().advance_turn(&mut f);
    assert!(pending(&st).is_empty());
    let drained = st.lock().pop_expired_use_tokens();
    assert_eq!(drained.len(), 1);
    assert!(st.lock().pop_expired_use_tokens().is_empty());
}

#[test]
fn consume_needs_both_axes_inclusive() {
    let mut f = fx();
    let st = SessionState::new(&mut f);
    st.lock().issue_use_tokens(&ids(&["a", "b"]), &mut f);
    let mut g = st.lock();
    g.advance_turn(&mut f);
    g.advance_turn(&mut f);
    f.now = 1599.0;
    assert!(g.consume_old_tokens(2, None, None, &mut f).is_empty());
    f.now = 1600.0;
    assert!(g.consume_old_tokens(3, None, None, &mut f).is_empty());
    let ovr: BTreeSet<Vec<u32>> = [cps("a")].into();
    assert_eq!(
        g.consume_old_tokens(2, None, Some(&ovr), &mut f),
        [cps("b")]
    );
    assert_eq!(
        g.consume_old_tokens(2, Some(f64::NAN), None, &mut f),
        Vec::<Vec<u32>>::new()
    );
    assert_eq!(
        g.consume_old_tokens(i64::MIN, Some(-1e308), None, &mut f),
        [cps("a")]
    );
}

#[test]
fn reset_keeps_id_and_turn() {
    let mut f = fx();
    let st = SessionState::new(&mut f);
    let mut g = st.lock();
    let sid = g.session_id.clone();
    g.advance_turn(&mut f);
    g.disable(cps("x"));
    g.issue_use_tokens(&ids(&["a"]), &mut f);
    g.reset();
    assert_eq!(g.session_id, sid);
    assert_eq!(g.turn_counter, 1);
    assert!(g.disabled_scopes.is_empty() && g.pending_use_tokens.is_empty());
}

#[test]
fn lru_touches_on_hit_and_cap_zero_evicts_itself() {
    let mut f = fx();
    let reg = SessionRegistry::new(2);
    let a = reg.for_request(Some(cps("a")), &mut f);
    reg.for_request(Some(cps("b")), &mut f);
    assert!(Arc::ptr_eq(&a, &reg.for_request(Some(cps("a")), &mut f)));
    reg.for_request(Some(cps("c")), &mut f);
    let keys: Vec<String> = reg.keys_in_order().iter().map(|k| text(k)).collect();
    assert_eq!(keys, ["a", "c"]);
    assert_eq!(reg.stats().evicted, 1);
    let zero = SessionRegistry::new(0);
    let x = zero.for_request(Some(cps("a")), &mut f);
    assert!(zero.known_keys().is_empty());
    assert!(!Arc::ptr_eq(&x, &zero.for_request(Some(cps("a")), &mut f)));
    assert_eq!(zero.stats().evicted, 2);
    let d = SessionRegistry::default();
    assert_eq!(d.max_clients, DEFAULT_MAX_CLIENTS);
    let none = d.for_request(None, &mut f);
    assert!(Arc::ptr_eq(
        &none,
        &d.for_request(Some(cps("__default__")), &mut f)
    ));
    assert_eq!(text(&none.lock().client_key), "__default__");
}

#[test]
fn concurrent_first_requests_make_one_state() {
    for _ in 0..20 {
        let reg = Arc::new(SessionRegistry::new(256));
        let n = 8;
        let barrier = Arc::new(Barrier::new(n));
        let handles: Vec<_> = (0..n)
            .map(|i| {
                let reg = Arc::clone(&reg);
                let barrier = Arc::clone(&barrier);
                std::thread::spawn(move || {
                    let mut f = Fx {
                        now: 0.0,
                        n: (i as u64) << 32,
                        reads: 0,
                        draws: 0,
                    };
                    barrier.wait();
                    let s = reg.for_request(Some(cps("k")), &mut f);
                    s.lock().advance_turn(&mut f);
                    s
                })
            })
            .collect();
        let states: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        assert!(states.iter().all(|s| Arc::ptr_eq(s, &states[0])));
        assert_eq!(states[0].lock().turn_counter, n as i64);
        assert_eq!(reg.stats().size, 1);
    }
}

#[test]
fn system_effects_spell_as_the_reference() {
    let st = bettermemory_core::session::get_state();
    let sid = st.lock().session_id.clone();
    assert_eq!(sid.len(), 21);
    assert!(sid.starts_with("sess_"));
    assert!(sid[5..]
        .chars()
        .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    assert!(Arc::ptr_eq(&st, &bettermemory_core::session::get_state()));
    use bettermemory_core::session::Effects;
    let t = bettermemory_core::session::System.time();
    assert!(t > 1.7e9);
}
