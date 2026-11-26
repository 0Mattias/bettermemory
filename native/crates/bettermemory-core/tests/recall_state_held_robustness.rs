//! `RecallState` under load and hostile input: a million calls (in a
//! release build) over ten thousand sessions with the cap at a thousand
//! (linear in the calls), eight threads at once on one state (each claim one a serial order
//! gives, a session made while another thread's rebuild reads kept),
//! a rebuild raising part way, bounds at the ends of `i64`, and claims of
//! a hundred thousand ids (linear).

mod common;
mod timing;

use std::collections::{BTreeMap, BTreeSet};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::thread;
use std::time::Duration;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::recall_settle::{SettleError, SettleState};
use bettermemory_core::recall_state::{RebuildError, RecallState, SessionEvents};
use bettermemory_core::unicode::cps;
use common::recall_state::{dt, events, Key, Rows, T0};

const ROWS: &str = r#"[
    {"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a","b"],"event_id":"E0"},
    {"kind":"prompt_recall","ts":"2026-01-01T12:02:00Z","shown_ids":["c"],"event_id":"E1"}]"#;

fn k(i: usize) -> Key {
    (cps("claude-code"), cps(&format!("S{i}")))
}

fn at(micros: i128) -> DateTime {
    dt(T0).minus_micros(-micros).unwrap()
}

fn ids(items: &[&str]) -> Vec<PyStr> {
    items.iter().map(|i| cps(i)).collect()
}

/// A store whose every session holds the same two rows.
struct Same(Vec<PyValue>);

impl SessionEvents for Same {
    type Error = ();
    fn session_events_of_kinds(&mut self, _: &[u32], _: &[&str]) -> (Vec<PyValue>, Option<()>) {
        (self.0.clone(), None)
    }
}

/// `calls` calls over `sessions` sessions, a millisecond apart, drawn by
/// xorshift: segment reads, shows, claims and SessionStarts.
fn drive(state: &RecallState, calls: usize, sessions: usize) -> usize {
    let keys: Vec<Key> = (0..sessions).map(k).collect();
    let mut store = Same(events(ROWS));
    let mut x = 0x2545_F491_4F6C_DD1Du64;
    let mut shown = 0;
    for i in 0..calls {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        let key = &keys[(x % sessions as u64) as usize];
        let now = at(i as i128 * 1000);
        let id = [cps(&format!("m{}", x % 97))];
        match (x >> 32) % 5 {
            0 => shown += state.segment(&mut store, key, now).unwrap().shown.len(),
            1 => state.note_shown(key, &id, x.is_multiple_of(2), now, None, None),
            2 => shown += state.claim_delivery(key, &id, now, None).0.len(),
            3 => {
                let source = cps(if x.is_multiple_of(3) {
                    "clear"
                } else {
                    "compact"
                });
                state
                    .open_segment(&mut store, key, &source, now, None)
                    .unwrap();
                state.take_closed(key);
            }
            _ => shown += state.claim_brief(key, &id, &id, now, None).0.len(),
        }
    }
    shown
}

#[test]
fn a_million_calls_over_ten_thousand_sessions_are_linear() {
    // A million calls in a release build; a debug build times a tenth of
    // each size (its calls cost about twenty-five microseconds each).
    let (small, large) = if cfg!(debug_assertions) {
        (10_000, 100_000)
    } else {
        (100_000, 1_000_000)
    };
    let state = RecallState::new(1_000, 3_600, None);
    drive(&state, small, 10_000);
    assert_eq!(state.len(), 1_000);
    timing::grows_within(
        "RecallState: ten times the calls over 10,000 sessions",
        timing::TENFOLD,
        &mut || {
            let state = RecallState::new(1_000, 3_600, None);
            timing::once(|| drive(&state, small, 10_000))
        },
        &mut || {
            let state = RecallState::new(1_000, 3_600, None);
            timing::once(|| drive(&state, large, 10_000))
        },
    );
}

#[test]
fn eight_threads_claim_as_a_serial_order_gives() {
    // Every claim keeps the ids of its list not kept before it, in order:
    // the sets kept are disjoint, together they are every id offered, and
    // each claim that kept any counts one delivery.
    let state = RecallState::default();
    let key = k(1);
    let claims: Vec<(Vec<PyStr>, Vec<PyStr>)> = thread::scope(|s| {
        let workers: Vec<_> = (0..8u64)
            .map(|t| {
                let (state, key) = (&state, &key);
                s.spawn(move || {
                    let mut out = Vec::new();
                    for round in 0..400u64 {
                        let offered: Vec<PyStr> = [round, round + t, (round * 7 + t) % 300]
                            .iter()
                            .map(|n| cps(&format!("i{n}")))
                            .collect();
                        let ev = cps(&format!("E{t}-{round}"));
                        let (kept, _) = state.claim_delivery(key, &offered, at(0), Some(&ev));
                        out.push((offered, kept));
                    }
                    out
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|w| w.join().unwrap())
            .collect()
    });
    let mut owner: BTreeMap<PyStr, usize> = BTreeMap::new();
    let mut offered_all = BTreeSet::new();
    for (n, (offered, kept)) in claims.iter().enumerate() {
        offered_all.extend(offered.iter().cloned());
        let mut rest = offered.iter();
        for id in kept {
            assert!(rest.any(|o| o == id), "kept in the order offered");
            let first = *owner.entry(id.clone()).or_insert(n);
            assert_eq!(first, n, "an id kept by two claims");
        }
    }
    let e = &state.entries()[0].1;
    assert_eq!(e.shown, offered_all);
    assert_eq!(owner.keys().cloned().collect::<BTreeSet<_>>(), offered_all);
    let delivered = claims.iter().filter(|(_, kept)| !kept.is_empty()).count();
    assert_eq!(e.deliveries, delivered as i128);
}

/// How long either thread of the rebuild test waits for the other before
/// it fails: a regression fails the test instead of hanging the binary.
const HANDOFF: Duration = Duration::from_secs(60);

/// A store whose first read says it began, then waits for another thread
/// to act on the state.
struct Waits {
    began: Option<Sender<()>>,
    go_on: Option<Receiver<()>>,
    rows: Vec<PyValue>,
}

impl SessionEvents for Waits {
    type Error = ();
    fn session_events_of_kinds(&mut self, _: &[u32], _: &[&str]) -> (Vec<PyValue>, Option<()>) {
        if let (Some(began), Some(go_on)) = (self.began.take(), self.go_on.take()) {
            began.send(()).expect("the main thread waits for the read");
            go_on
                .recv_timeout(HANDOFF)
                .expect("the main thread shows while the rebuild reads");
        }
        (self.rows.clone(), None)
    }
}

#[test]
fn a_session_made_during_another_threads_rebuild_is_kept() {
    // The rebuild runs outside the lock: the show the other thread makes
    // while it reads is what the session holds after it. Each hand-off
    // waits at most HANDOFF, and a reader that ends without reading drops
    // its sender, so a regression fails here instead of hanging.
    let state = RecallState::default();
    let key = k(1);
    let (began, began_rx) = channel();
    let (go_on, go_on_rx) = channel();
    let (state_ref, key_ref) = (&state, &key);
    let view = thread::scope(|s| {
        let reader = s.spawn(move || {
            let mut store = Waits {
                began: Some(began),
                go_on: Some(go_on_rx),
                rows: events(ROWS),
            };
            state_ref.segment(&mut store, key_ref, at(5)).unwrap()
        });
        let read = began_rx.recv_timeout(HANDOFF);
        if read.is_err() {
            drop(go_on);
            let ended = reader.join();
            panic!("the rebuild never read the store (reader ended: {ended:?})");
        }
        state.note_shown(&key, &ids(&["z"]), true, at(1), None, None);
        go_on.send(()).expect("the reader waits for the show");
        reader.join().expect("the reader's rebuild answers")
    });
    assert_eq!(view.shown, ids(&["z"]).into_iter().collect());
    assert_eq!(state.entries()[0].1.last_seen, at(5));
}

#[test]
fn eight_threads_over_fifty_sessions_with_a_cap_of_ten() {
    let state = RecallState::new(10, 60, None);
    thread::scope(|s| {
        for _ in 0..8 {
            s.spawn(|| drive(&state, 20_000, 50));
        }
    });
    assert!(state.len() <= 10);
}

#[test]
fn a_rebuild_raising_part_way_leaves_the_sweep_done() {
    let state = RecallState::new(1000, 60, None);
    let mut good = Rows::new(events(ROWS));
    state.segment(&mut good, &k(1), at(0)).unwrap();
    let mut bad = Rows::new(events(ROWS));
    bad.fail = Some(1);
    let err = state.segment(&mut bad, &k(2), at(61_000_000)).unwrap_err();
    assert_eq!(err, RebuildError::Read("OperationalError"));
    assert!(state.is_empty(), "the sweep ran, nothing was inserted");
    let mut pair = state.bind(&mut bad);
    assert_eq!(
        pair.segment(&k(3), &at(62_000_000)),
        Err(SettleError::State)
    );
    pair.note_review_roots(&k(3), &ids(&["r"]), &at(63_000_000));
    assert!(
        state.is_empty(),
        "the pair is a no-op until its error is taken"
    );
    assert!(pair.take_error().is_some());
}

#[test]
fn idle_bounds_at_the_ends_of_i64_raise_overflow() {
    for idle in [i64::MAX, i64::MIN, 400_000_000_000] {
        let state = RecallState::new(1000, idle, None);
        let mut rows = Rows::new(Vec::new());
        assert_eq!(
            state.segment(&mut rows, &k(1), at(0)),
            Err(RebuildError::Raised(PyError::OverflowError)),
            "idle {idle}"
        );
    }
    let state = RecallState::new(usize::MAX, 0, None);
    let mut rows = Rows::new(Vec::new());
    assert_eq!(
        state.segment(&mut rows, &k(1), at(0)).unwrap().deliveries,
        0
    );
}

fn claim_many(n: usize) -> usize {
    let state = RecallState::default();
    let offered: Vec<PyStr> = (0..n).map(|i| cps(&format!("m{i}"))).collect();
    let (kept, _) = state.claim_delivery(&k(1), &offered, at(0), None);
    let (again, _) = state.claim_delivery(&k(1), &offered, at(1), None);
    let (s, w, _) = state.claim_brief(&k(1), &offered, &offered, at(2), None);
    kept.len() + again.len() + s.len() + w.len()
}

#[test]
fn claims_of_a_hundred_thousand_ids_are_linear() {
    assert_eq!(claim_many(100_000), 100_000);
    timing::grows_within(
        "claim_delivery and claim_brief: 10,000 and 100,000 ids",
        timing::TENFOLD,
        &mut || timing::once(|| claim_many(10_000)),
        &mut || timing::once(|| claim_many(100_000)),
    );
}
