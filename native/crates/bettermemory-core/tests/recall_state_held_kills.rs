//! Cases a plausible wrong port of `RecallState` fails. Each `RecallState`
//! answer is the reference's (`RecallState` under CPython 3.13.13 over the
//! same calls, as `recall_state_held.jsonl` pins them); one session with
//! one hook passes every one of these mistakes, so each case is built to
//! tell one of them apart. The `the_bound_pair_*` cases pin the `Bound`
//! pair's own answers (README, unit 2's Decisions): the error it keeps,
//! the no-ops while it is kept and nothing claimed past `i64`; where the
//! reference raises, so do the pair's callers, and nothing is recorded.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall::choose::ClaimState;
use bettermemory_core::recall_settle::{
    review_unanswered, settle_shown, LoadFailure, Recorder, SettleError, SettleState, SettleStore,
    Shown,
};
use bettermemory_core::recall_state::{BoundError, RebuildError, RecallState, SURFACE_BRIEF};
use bettermemory_core::transcript::CompactMarker;
use bettermemory_core::unicode::cps;
use common::recall_state::{dt, events, HeldStore, Key, Rows, T0};
use serde_json::json;

const S1_ROWS: &str = r#"[
    {"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a","b"],"event_id":"E0","segment":0},
    {"kind":"prompt_recall","ts":"2026-01-01T12:02:00Z","shown_ids":["c"],"event_id":"E1"}]"#;

fn k(session: &str) -> Key {
    (cps("claude-code"), cps(session))
}

fn at(seconds: i128) -> bettermemory_core::datetime::DateTime {
    dt(T0).minus_micros(-seconds * 1_000_000).unwrap()
}

fn ids(items: &[&str]) -> Vec<PyStr> {
    items.iter().map(|i| cps(i)).collect()
}

fn set(items: &[&str]) -> BTreeSet<PyStr> {
    items.iter().map(|i| cps(i)).collect()
}

fn store() -> Rows {
    Rows::new(events(S1_ROWS))
}

fn sessions(state: &RecallState) -> Vec<String> {
    state
        .keys()
        .iter()
        .map(|(_, s)| String::from_utf8(s.iter().map(|&c| c as u8).collect()).unwrap())
        .collect()
}

fn marker(key: &str) -> CompactMarker {
    CompactMarker {
        key: cps(key),
        at: at(0),
    }
}

#[test]
fn the_sweep_stops_at_the_first_live_entry() {
    // A clock that went back leaves S2 (seen at 0) behind S1 (seen at 100):
    // a sweep at 150 with an idle of 60 stops at S1 and keeps S2. A sweep
    // that sorted or scanned the whole table would drop S2.
    let state = RecallState::new(1000, 60, None);
    let mut rows = store();
    state.segment(&mut rows, &k("S1"), at(100)).unwrap();
    state.segment(&mut rows, &k("S2"), at(0)).unwrap();
    state.segment(&mut rows, &k("S3"), at(150)).unwrap();
    assert_eq!(sessions(&state), ["S1", "S2", "S3"]);
    // S1 seen again goes to the back: the next sweep meets S2 first.
    state.segment(&mut rows, &k("S1"), at(150)).unwrap();
    state.segment(&mut rows, &k("S3"), at(150)).unwrap();
    assert_eq!(sessions(&state), ["S1", "S3"]);
}

#[test]
fn the_sweep_keeps_an_entry_seen_at_the_cutoff() {
    let state = RecallState::new(1000, 60, None);
    let mut rows = store();
    state.segment(&mut rows, &k("S1"), at(0)).unwrap();
    state.segment(&mut rows, &k("S2"), at(60)).unwrap();
    assert_eq!(sessions(&state), ["S1", "S2"]);
    let past = at(60).minus_micros(-1).unwrap();
    state.segment(&mut rows, &k("S2"), past).unwrap();
    assert_eq!(sessions(&state), ["S2"]);
}

#[test]
fn held_never_rebuilds() {
    // note_shown on a session not held makes an empty entry: no read, and
    // the rows' shows are not in it, nor are they read by the next segment.
    let state = RecallState::default();
    let mut rows = store();
    state.note_shown(&k("S1"), &ids(&["x"]), false, at(0), None, None);
    let view = state.segment(&mut rows, &k("S1"), at(1)).unwrap();
    assert_eq!(view.shown, set(&["x"]));
    assert!(rows.asked.is_empty(), "no session read");
}

#[test]
fn a_session_made_during_the_rebuild_is_kept() {
    // While S1's rows are read (outside the lock), another hook shows "z":
    // the rebuilt entry is dropped and the held one only marked seen.
    let state = RecallState::default();
    let spec = json!({
        "rows": {"S1": S1_ROWS},
        "during": {"S1": [{"op": "shown", "key": ["claude-code", "S1"], "ids": ["z"],
                            "delivery": true, "now": "2026-03-01T09:30:00+00:00"}]},
    });
    let mut held = HeldStore::new(&spec, &state);
    let view = state.segment(&mut held, &k("S1"), at(5)).unwrap();
    assert_eq!(view.shown, set(&["z"]));
    assert_eq!(view.deliveries, 1);
    assert_eq!(held.reads, 1);
    assert_eq!(state.entries()[0].1.last_seen, at(5));
}

#[test]
fn the_cap_evicts_the_least_recently_seen() {
    let state = RecallState::new(2, 86_400, None);
    let mut rows = store();
    state.segment(&mut rows, &k("S1"), at(0)).unwrap();
    state.segment(&mut rows, &k("S2"), at(1)).unwrap();
    state.segment(&mut rows, &k("S1"), at(2)).unwrap();
    state.segment(&mut rows, &k("S3"), at(3)).unwrap();
    assert_eq!(sessions(&state), ["S1", "S3"]);
    state.note_shown(&k("S4"), &ids(&["q"]), true, at(4), None, None);
    assert_eq!(sessions(&state), ["S3", "S4"]);
}

#[test]
fn a_compact_on_the_same_marker_holds_the_signal() {
    let state = RecallState::default();
    let mut rows = store();
    let (v, opened) = state
        .open_segment(
            &mut rows,
            &k("S1"),
            &cps("compact"),
            at(0),
            Some(&marker("m1")),
        )
        .unwrap();
    assert!(opened);
    assert_eq!(v.segment, PyValue::int(1));
    state.claim_delivery(&k("S1"), &ids(&["p"]), at(1), None);
    let (v, opened) = state
        .open_segment(
            &mut rows,
            &k("S1"),
            &cps("compact"),
            at(2),
            Some(&marker("m1")),
        )
        .unwrap();
    assert!(!opened);
    assert_eq!(v.segment, PyValue::int(1));
    assert_eq!(v.shown, set(&["p"]));
    assert_eq!(
        state
            .pending_compaction(&mut rows, &k("S1"), at(3))
            .unwrap(),
        Some(at(2))
    );
}

#[test]
fn the_signal_is_matched_by_instant() {
    let state = RecallState::default();
    let mut rows = store();
    state
        .open_segment(&mut rows, &k("S1"), &cps("compact"), at(0), None)
        .unwrap();
    // The same wall time in another offset is another instant.
    let same_wall = dt("2026-03-01T09:30:00+05:30");
    assert_eq!(
        state.resolve_compaction(&k("S1"), same_wall, Some(&marker("m2"))),
        None
    );
    let naive = dt("2026-03-01T09:30:00");
    assert_eq!(
        state.resolve_compaction(&k("S1"), naive, Some(&marker("m2"))),
        None
    );
    // The same instant in another offset resolves, opening at that `at`.
    let same_instant = dt("2026-03-01T15:00:00+05:30");
    let v = state
        .resolve_compaction(&k("S1"), same_instant, Some(&marker("m2")))
        .expect("resolved");
    assert_eq!(v.segment, PyValue::int(1));
    assert_eq!(v.started_at, same_instant);
    assert_eq!(v.marker, Some(cps("m2")));
}

#[test]
fn an_empty_claim_counts_no_delivery() {
    let state = RecallState::default();
    let mut rows = store();
    state.segment(&mut rows, &k("S1"), at(0)).unwrap();
    let (kept, seg) =
        state.claim_delivery(&k("S1"), &ids(&["a", "c"]), at(1), Some(&cps("E2")[..]));
    assert!(kept.is_empty());
    assert_eq!(seg, PyValue::int(0));
    let (kept, _) = state.claim_delivery(&k("S1"), &ids(&["x", "x", "a"]), at(2), None);
    assert_eq!(kept, ids(&["x", "x"]));
    assert_eq!(
        state
            .segment(&mut rows, &k("S1"), at(3))
            .unwrap()
            .deliveries,
        2
    );
}

#[test]
fn claim_brief_filters_both_lists_first() {
    let state = RecallState::default();
    let (standing, work, _) =
        state.claim_brief(&k("S1"), &ids(&["s", "s"]), &ids(&["s", "w"]), at(0), None);
    assert_eq!(standing, ids(&["s", "s"]));
    assert_eq!(work, ids(&["s", "w"]));
    assert_eq!(state.entries()[0].1.deliveries, 0);
}

#[test]
fn note_shown_keeps_the_first_show() {
    let state = RecallState::default();
    state.note_shown(
        &k("S1"),
        &ids(&["p"]),
        true,
        at(0),
        Some(&[][..]),
        Some(&cps("E1")[..]),
    );
    state.note_shown(
        &k("S1"),
        &ids(&["p", "q"]),
        false,
        at(1),
        Some(&[][..]),
        Some(&cps("E2")[..]),
    );
    let e = &state.entries()[0].1;
    assert_eq!(
        e.shown_by.get(&cps("p")),
        Some(&(cps("recall"), Some(cps("E1"))))
    );
    assert_eq!(
        e.shown_by.get(&cps("q")),
        Some(&(cps("brief"), Some(cps("E2"))))
    );
    assert_eq!(e.deliveries, 1);
}

#[test]
fn attribute_compaction_answers_its_ids_sorted() {
    // The brief's ids enter shown_by in the order c, b; the answer is sorted.
    let rows = r#"[
        {"kind":"session_brief","ts":"2026-01-01T12:04:00Z","ids":["c","b"],"event_id":"E4","marker_absent":true},
        {"kind":"prompt_recall","ts":"2026-01-01T12:05:00Z","shown_ids":["d"],"event_id":"E5"}]"#;
    let state = RecallState::default();
    let mut rows = Rows::new(events(rows));
    let signal = dt("2026-01-01T12:04:00+00:00");
    assert_eq!(
        state
            .pending_compaction(&mut rows, &k("S1"), at(0))
            .unwrap(),
        Some(signal)
    );
    let (got, event) = state.attribute_compaction(&k("S1"), signal).unwrap();
    assert_eq!(got, ids(&["b", "c"]));
    assert_eq!(event, Some(cps("E4")));
    let e = &state.entries()[0].1;
    assert_eq!(e.shown, set(&["d"]));
    assert!(e.signal.is_none());
    assert_eq!(cps(SURFACE_BRIEF), cps("brief"));
}

#[test]
fn attribute_compaction_takes_only_the_briefs_shows() {
    // Ids the recall showed under the brief's event id are not the
    // brief's: shown_by must match ("brief", event id), not the id alone.
    let rows = r#"[
        {"kind":"session_brief","ts":"2026-01-01T12:04:00Z","ids":["c","b"],"event_id":"E4","marker_absent":true}]"#;
    let state = RecallState::default();
    let mut rows = Rows::new(events(rows));
    let signal = dt("2026-01-01T12:04:00+00:00");
    state
        .pending_compaction(&mut rows, &k("S1"), at(0))
        .unwrap();
    let e4 = cps("E4");
    state.note_shown(&k("S1"), &ids(&["x"]), true, at(1), None, Some(&e4));
    let (kept, _) = state.claim_delivery(&k("S1"), &ids(&["y"]), at(2), Some(&e4));
    assert_eq!(kept, ids(&["y"]));
    let (got, event) = state.attribute_compaction(&k("S1"), signal).unwrap();
    assert_eq!(got, ids(&["b", "c"]));
    assert_eq!(event, Some(e4));
    assert_eq!(state.entries()[0].1.shown, set(&["x", "y"]));
}

#[test]
fn a_clear_restarts_on_the_entrys_own_marker() {
    let state = RecallState::default();
    let mut rows = store();
    state
        .open_segment(
            &mut rows,
            &k("S1"),
            &cps("compact"),
            at(0),
            Some(&marker("m1")),
        )
        .unwrap();
    let (v, opened) = state
        .open_segment(
            &mut rows,
            &k("S1"),
            &cps("clear"),
            at(1),
            Some(&marker("m9")),
        )
        .unwrap();
    assert!(opened);
    assert_eq!(v.marker, Some(cps("m1")));
    assert_eq!(v.segment, PyValue::int(2));
    assert_eq!(
        state.take_closed(&k("S1")).unwrap().segment,
        PyValue::int(1)
    );
    assert_eq!(state.take_closed(&k("S1")), None);
}

#[test]
fn the_bound_pair_keeps_the_first_error() {
    let state = RecallState::default();
    let mut rows = store();
    rows.fail = Some(1);
    let mut pair = state.bind(&mut rows);
    assert_eq!(pair.segment(&k("S1"), &at(0)), Err(SettleError::State));
    // Every later call raises again or is a no-op until the error is taken.
    assert_eq!(pair.segment(&k("S2"), &at(0)), Err(SettleError::State));
    pair.note_settled(&k("S2"), &ids(&["a"]), &at(1));
    assert_eq!(
        pair.claim_delivery(&k("S2"), &ids(&["a"]), &at(2), &cps("E")),
        (vec![], 0)
    );
    assert_eq!(
        pair.take_error(),
        Some(BoundError::Rebuild(RebuildError::Read("OperationalError")))
    );
    assert_eq!(pair.take_error(), None);
    assert!(state.is_empty());
    let (kept, seg) = pair.claim_delivery(&k("S2"), &ids(&["a"]), &at(3), &cps("E"));
    assert_eq!((kept, seg), (ids(&["a"]), 0));
}

#[test]
fn the_bound_pair_claims_nothing_past_i64() {
    let rows = r#"[{"kind":"prompt_recall","ts":"2026-01-01T12:01:00Z","shown_ids":["a"],"segment":9223372036854775808}]"#;
    let state = RecallState::default();
    let mut rows = Rows::new(events(rows));
    let mut pair = state.bind(&mut rows);
    assert_eq!(
        pair.segment(&k("S1"), &at(0)).unwrap().segment,
        PyValue::int_from_decimal("9223372036854775808").unwrap()
    );
    assert_eq!(
        pair.claim_delivery(&k("S1"), &ids(&["b"]), &at(1), &cps("E")),
        (vec![], 0)
    );
    assert!(matches!(
        pair.take_error(),
        Some(BoundError::SegmentPastI64(_))
    ));
    assert_eq!(state.entries()[0].1.shown, set(&["a"]));
}

#[test]
fn the_bound_pair_forgets_and_takes_nothing_while_its_error_is_kept() {
    let state = RecallState::default();
    let mut good = store();
    state.segment(&mut good, &k("S1"), at(0)).unwrap();
    state
        .open_segment(&mut good, &k("S1"), &cps("clear"), at(1), None)
        .unwrap();
    let mut bad = store();
    bad.fail = Some(0);
    let mut pair = state.bind(&mut bad);
    assert_eq!(pair.segment(&k("S2"), &at(2)), Err(SettleError::State));
    assert_eq!(pair.take_closed(&k("S1")), None);
    pair.forget(&k("S1"));
    assert!(pair.take_error().is_some());
    assert_eq!(sessions(&state), ["S1"]);
    assert_eq!(
        state.take_closed(&k("S1")).unwrap().segment,
        PyValue::int(0)
    );
}

/// The settle store: one `review_issued` row, nothing else.
struct Log(Vec<PyValue>);

impl SettleStore for Log {
    fn events_of_kinds(&mut self, _: &[&str], _: &DateTime) -> Vec<PyValue> {
        self.0.clone()
    }
    fn events_since(&mut self, _: &DateTime) -> Vec<PyValue> {
        Vec::new()
    }
    fn load_one(&mut self, _: &[u32]) -> Result<PyStr, LoadFailure> {
        Err(LoadFailure::NotFound)
    }
}

struct Rec(Vec<String>);

impl Recorder for Rec {
    fn record(&mut self, kind: &str, _: Vec<(PyValue, PyValue)>) {
        self.0.push(kind.to_string());
    }
}

#[test]
fn the_bound_pairs_callers_raise_before_recording() {
    // The reference's review_unanswered and settle_shown call
    // state.segment with no try: a store error raises before any row.
    let issued = r#"[{"kind":"review_issued","ts":"2026-03-01T09:10:00+00:00","session":"S1","review_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV"}]"#;
    let now = dt("2026-03-01T09:30:00+00:00");
    let since = dt("2026-03-01T09:00:00+00:00");
    let state = RecallState::default();
    let mut failing = store();
    failing.fail = Some(0);
    let mut pair = state.bind(&mut failing);
    let mut log = Log(events(issued));
    let mut rec = Rec(Vec::new());
    let mut asked = 0;
    let got = review_unanswered(
        &mut log,
        &mut rec,
        &mut pair,
        &cps("S1"),
        &set(&["S1"]),
        &since,
        None,
        &now,
        |_| {
            asked += 1;
            (true, false)
        },
    );
    assert_eq!(got, Err(SettleError::State));
    assert_eq!((rec.0.len(), asked), (0, 0));
    assert_eq!(
        pair.take_error(),
        Some(BoundError::Rebuild(RebuildError::Read("OperationalError")))
    );
    let shown = Shown {
        session_id: &cps("S1"),
        assistant_response: &cps("reply"),
        sessions: &[],
        harness: None,
        exclude: &[],
        prompt_id: None,
        client_model: None,
        now: &now,
    };
    assert_eq!(
        settle_shown(&mut log, &mut rec, &mut pair, &shown),
        Err(SettleError::State)
    );
    assert!(rec.0.is_empty());
    assert!(pair.take_error().is_some());
    assert!(state.is_empty());
    // With a store that reads, the same call records its row.
    let mut rows = Rows::new(Vec::new());
    let mut pair = state.bind(&mut rows);
    let got = review_unanswered(
        &mut log,
        &mut rec,
        &mut pair,
        &cps("S1"),
        &set(&["S1"]),
        &since,
        None,
        &now,
        |_| (true, false),
    );
    assert_eq!(got, Ok(ids(&["01ARZ3NDEKTSV4RRFFQ69G5FAV"])));
    assert_eq!(rec.0, ["review_unanswered"]);
}
