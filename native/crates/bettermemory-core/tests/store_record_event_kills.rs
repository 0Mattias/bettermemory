//! Cases a plausible wrong `store::record_event` fails: the retry's bound
//! and the deadline's strict comparison, the busy codes against their
//! neighbours, the errors the reference catches against those it raises,
//! the actor's set-default, an empty `ts` stamped now, a `LIKE` needle
//! whose wildcards match, and a named payload against one left alone.

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::record_event::{
    busy, caught, event_payload, named_payload, time_left, EVENT_RETRY_PAUSE_SECONDS,
};
use bettermemory_core::store::write::WriteError;
use rusqlite::ffi;

fn sqlite(code: i32) -> WriteError {
    WriteError::Sqlite(rusqlite::Error::SqliteFailure(ffi::Error::new(code), None))
}

fn none(_: &[u32]) -> Vec<(usize, usize, Vec<u32>)> {
    Vec::new()
}

#[test]
fn busy_is_the_primary_code_five_or_six() {
    assert!(busy(&sqlite(5)));
    assert!(busy(&sqlite(6)));
    // SQLITE_BUSY_SNAPSHOT (517) and SQLITE_LOCKED_SHAREDCACHE (262).
    assert!(busy(&sqlite(517)));
    assert!(busy(&sqlite(262)));
    assert!(!busy(&sqlite(8)));
    assert!(!busy(&sqlite(7)));
    assert!(!busy(&WriteError::RolledBack("x".into())));
}

#[test]
fn the_deadline_needs_more_than_the_pause_left() {
    assert!(time_left(None, 1e300));
    assert!(!time_left(Some(EVENT_RETRY_PAUSE_SECONDS), 0.0));
    assert!(time_left(Some(1.0), 0.9));
    assert!(!time_left(Some(1.0), 2.0));
    // 10.0 - 9.95 is a little over 0.05 in binary.
    assert!(time_left(Some(10.0), 9.95));
}

#[test]
fn the_caught_classes_are_sqlite_os_and_runtime() {
    assert!(caught(&sqlite(1)));
    assert!(caught(&WriteError::Clock(std::io::Error::other("x"))));
    assert!(caught(&WriteError::LaterWriter {
        path: "x".into(),
        needed: 99
    }));
    assert!(caught(&WriteError::RolledBack("store".into())));
    assert!(caught(&WriteError::Refused {
        class: "RecursionError",
        text: None
    }));
    assert!(!caught(&WriteError::Refused {
        class: "ValueError",
        text: None
    }));
    assert!(!caught(&WriteError::Refused {
        class: "TypeError",
        text: None
    }));
    assert!(!caught(&sqlite(7)), "SQLITE_NOMEM raises MemoryError");
    assert!(!caught(&WriteError::NotAnObject));
}

#[test]
fn the_actor_is_set_only_when_absent_and_not_empty() {
    let actor = vec![(PyValue::str("client"), PyValue::str("c"))];
    let with = event_payload(&[], Some(&actor), &mut none);
    assert_eq!(
        with,
        PyValue::Dict(vec![(PyValue::str("actor"), PyValue::Dict(actor.clone()))])
    );
    assert_eq!(
        event_payload(&[], Some(&[]), &mut none),
        PyValue::Dict(vec![])
    );
    let held = vec![(PyValue::str("actor"), PyValue::str("kept"))];
    assert_eq!(
        event_payload(&held, Some(&actor), &mut none),
        PyValue::Dict(held.clone())
    );
}

#[test]
fn a_stored_token_is_named_and_other_text_kept() {
    assert_eq!(
        named_payload(r#"{"note":"a <|im_start|> b"}"#).expect("named"),
        r#"{"note":"a im_start b"}"#
    );
    // Not canonical, nothing to name: the stored text as it is.
    assert_eq!(
        named_payload(r#"{ "b":1, "a":2 }"#).expect("kept"),
        r#"{ "b":1, "a":2 }"#
    );
    assert_eq!(named_payload("[1]").expect("list"), "[1]");
    assert_eq!(named_payload("not json").expect("text"), "not json");
}

#[test]
fn an_unchanged_nan_payload_keeps_its_stored_text() {
    for text in [
        r#"{ "query": NaN }"#,
        r#"{ "other": [NaN], "note": "ordinary" }"#,
        r#"{ "top_hits":[{"snippet":[NaN]}] }"#,
    ] {
        assert_eq!(named_payload(text).expect("named"), text);
    }
}

#[test]
fn a_keyless_event_checks_kind_then_warns_before_redaction() {
    use bettermemory_core::store::record_event::{record_event, EventCall, EventError, Logged};
    let fields = [(PyValue::str("query"), PyValue::str("unread"))];
    let mut log = Vec::new();
    for kind in ["memory_put", "search"] {
        let points = bettermemory_core::unicode::cps(kind);
        let call = EventCall {
            kind: &points,
            session: None,
            best_effort: false,
            attempts: 5,
            deadline: Some(10.0),
        };
        let got = record_event(
            Err(&[112, 0xdcfe]),
            &call,
            &fields,
            None,
            &mut |_| panic!("redacted a refused call"),
            &mut log,
        );
        if kind == "memory_put" {
            assert!(matches!(got, Err(EventError::Value(_))));
            assert!(log.is_empty());
        } else {
            assert!(matches!(got, Ok(None)));
            assert_eq!(log, [Logged::NotRecorded(vec![112, 0xdcfe])]);
        }
    }
}

#[test]
fn read_only_warnings_include_the_extended_sqlite_codes() {
    use bettermemory_core::store::record_event::warns_on_failure;
    assert!(warns_on_failure(&sqlite(1032), false));
    assert!(!warns_on_failure(&sqlite(517), false));
}
