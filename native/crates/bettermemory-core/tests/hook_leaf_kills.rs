//! Cases a plausible wrong port of the hook's leaves answers otherwise
//! than the reference: an ASCII-only strip, catching every JSON error, an
//! integer window test, `is not None` where the reference reads
//! truthiness, a skip of an unhashable session, a sort by UTF-16 or by
//! retrieval order, a join asked when the server session is given.

use std::collections::BTreeSet;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::hook::{
    emit_hook_attributions, payload_text, pending_retrievals, read_payload, EmitError, RowSink,
    ScanError, Turn,
};
use bettermemory_core::json::{loads, LoadError};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn events(text: &str) -> Vec<PyValue> {
    match &loads(&cps(text)).expect("JSON") {
        PyValue::List(v) => v.clone(),
        _ => panic!("a list"),
    }
}

fn clock(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).expect("a clock")
}

fn set(items: &[&[u32]]) -> BTreeSet<PyStr> {
    items.iter().map(|s| s.to_vec()).collect()
}

const NOW: &str = "2026-11-21T12:00:00+00:00";

fn pending(evs: &str, now: &str) -> Result<BTreeSet<PyStr>, ScanError> {
    let s = cps("S");
    let t = cps("T");
    pending_retrievals(&events(evs), &clock(now), &s, &[&s, &t], 600)
}

#[test]
fn strip_reads_unicode_space() {
    let text: Vec<u32> = [0x3000, 0x85]
        .into_iter()
        .chain(cps("{\"a\": 1}"))
        .chain([0x2029])
        .collect();
    assert_eq!(read_payload(&text).ok(), loads(&cps("{\"a\": 1}")).ok());
    // U+200B is not space: the text does not parse.
    let zw: Vec<u32> = [0x200B].into_iter().chain(cps("{}")).collect();
    assert_eq!(read_payload(&zw), Ok(PyValue::Dict(Vec::new())));
}

#[test]
fn only_decode_errors_are_caught() {
    let digits = format!("{{\"n\": {}}}", "9".repeat(4301));
    assert_eq!(read_payload(&cps(&digits)), Err(LoadError::IntDigits));
    let fine = format!("{{\"n\": {}}}", "9".repeat(4300));
    assert!(matches!(&read_payload(&cps(&fine)), Ok(PyValue::Dict(d)) if d.len() == 1));
    assert_eq!(read_payload(&cps("[1]")), Ok(PyValue::Dict(Vec::new())));
}

#[test]
fn payload_text_wants_a_non_empty_str() {
    let p = loads(&cps("{\"a\": \"\", \"b\": 1, \"c\": \"x\", \"c\": \"y\"}")).expect("JSON");
    assert_eq!(payload_text(&p, "a"), None);
    assert_eq!(payload_text(&p, "b"), None);
    assert_eq!(payload_text(&p, "c"), Some(&cps("y")));
}

#[test]
fn the_window_is_a_float_comparison() {
    // In year 9000 a microsecond under the cutoff rounds onto it.
    let evs = r#"[{"kind": "search", "session": "S", "ts": "9000-01-02T02:07:30.323159+00:00", "returned": ["far"]}]"#;
    let got = pending(evs, "9000-01-02T02:17:30.323160+00:00").expect("scan");
    assert_eq!(got, set(&[&cps("far")]));
    // The edge itself is inside; one microsecond past it is out.
    let evs = r#"[{"kind": "search", "session": "S", "ts": "2026-11-21T11:50:00+00:00", "returned": ["edge"]},
                 {"kind": "search", "session": "S", "ts": "2026-11-21T11:49:59.999999+00:00", "returned": ["out"]}]"#;
    assert_eq!(pending(evs, NOW).expect("scan"), set(&[&cps("edge")]));
}

#[test]
fn fallbacks_read_truthiness() {
    let evs = r#"[{"kind": "search", "session": "", "session_id": "S", "ts": "2026-11-21T11:59:00Z", "returned": [], "memory_ids": ["m"]},
                 {"kind": "search", "session": 0, "session_id": "S", "ts": "2026-11-21T11:59:00Z", "returned": null, "hit_ids": ["h"]}]"#;
    assert_eq!(
        pending(evs, NOW).expect("scan"),
        set(&[&cps("h"), &cps("m")])
    );
}

#[test]
fn unhashable_use_session_raises() {
    let evs = r#"[{"kind": "use", "session": ["S"], "ts": "2026-11-21T11:59:00Z", "ids": ["a"]}]"#;
    assert_eq!(pending(evs, NOW), Err(ScanError::Unhashable));
    // On a retrieval the same session only fails to match.
    let evs = r#"[{"kind": "search", "session": ["S"], "ts": "2026-11-21T11:59:00Z", "returned": ["a"]}]"#;
    assert_eq!(pending(evs, NOW), Ok(BTreeSet::new()));
    // Out of the window the row is skipped before the session is read.
    let evs = r#"[{"kind": "use", "session": ["S"], "ts": "2026-11-21T10:00:00Z", "ids": ["a"]}]"#;
    assert_eq!(pending(evs, NOW), Ok(BTreeSet::new()));
    assert_eq!(pending("[3]", NOW), Err(ScanError::NotADict));
}

#[test]
fn a_tuple_holding_a_list_is_unhashable() {
    // JSON makes no tuple; the reference's hash of a tuple hashes its items.
    let row = |session: PyValue| {
        vec![PyValue::Dict(vec![
            (PyValue::str("kind"), PyValue::str("use")),
            (PyValue::str("session"), session),
            (PyValue::str("ts"), PyValue::str("2026-11-21T11:59:00Z")),
            (PyValue::str("ids"), PyValue::List(vec![PyValue::str("a")])),
        ])]
    };
    let s = cps("S");
    let scan = |evs: Vec<PyValue>| pending_retrievals(&evs, &clock(NOW), &s, &[&s], 600);
    let bad = PyValue::Tuple(vec![PyValue::str("S"), PyValue::List(Vec::new())]);
    assert_eq!(scan(row(bad)), Err(ScanError::Unhashable));
    let nested = PyValue::Tuple(vec![PyValue::Tuple(vec![PyValue::Dict(Vec::new())])]);
    assert_eq!(scan(row(nested)), Err(ScanError::Unhashable));
    let fine = PyValue::Tuple(vec![PyValue::str("S"), PyValue::Tuple(Vec::new())]);
    assert_eq!(scan(row(fine)), Ok(BTreeSet::new()));
}

struct Rows(Vec<Vec<(PyStr, PyValue)>>);

impl RowSink for Rows {
    type Error = ();
    fn record(&mut self, _: &str, fields: Vec<(PyStr, PyValue)>) -> Result<(), ()> {
        self.0.push(fields);
        Ok(())
    }
}

#[test]
fn bodies_go_in_code_point_order_and_the_join_waits() {
    let evs = events(
        r#"[{"kind": "search", "session": "S", "ts": "2026-11-21T11:59:00Z", "returned": ["\ud800", "￿", "😀"]}]"#,
    );
    let now = clock(NOW);
    let sid = cps("T");
    let server = cps("S");
    let turn = Turn {
        recent_events: &evs,
        session_id: &sid,
        assistant_response: &cps("nothing"),
        worktree_root: None,
        client_model: None,
        server_session: Some(&server),
        now: &now,
    };
    let mut rows = Rows(Vec::new());
    let mut order = Vec::new();
    let got: Result<_, EmitError<()>> = emit_hook_attributions(
        &turn,
        |id: &PyStr| {
            order.push(id.clone());
            Ok(Some(cps("body")))
        },
        &mut rows,
        |_: &[PyValue], _: Option<&PyStr>| panic!("the join is not asked"),
    );
    assert_eq!(got.expect("emit").len(), 3);
    // UTF-16 order would put U+1F600 before U+FFFF.
    assert_eq!(order, vec![vec![0xD800], vec![0xFFFF], vec![0x1F600]]);
    assert_eq!(rows.0.len(), 1);
    assert_eq!(rows.0[0][2], (cps("auto"), PyValue::Bool(true)));
}
