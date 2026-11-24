//! Cases a plausible wrong port of `chain_segments`, the two history
//! leaves or the history-replaced payload fails: the caller's key tried
//! first (origin check and leading key), the memo seeded with it, the
//! leading segment dropped under a rekey at the first row's seq,
//! `_anchored`'s bound at the anchor's seq exactly, `is True`, `type(seq)
//! is int`, and the payload's keys under `dict.update`.

use bettermemory_core::json;
use bettermemory_core::log::replaced::{
    history_replaced_kind, history_replaced_row, replaced_payload,
};
use bettermemory_core::log::segments::{chain_segments, chain_trust_with, Segment};
use bettermemory_core::log::{self, Key, KeyRing, LogRow, GENESIS_MAC, REKEY};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;

fn key(n: u8) -> Key {
    Key::new((n..n + 32).collect())
}

fn fp(k: &Key) -> String {
    log::fingerprint(k.bytes())
}

/// Rows at seq 1, 2, ... each signed by its key and linked.
fn chain(specs: &[(&Key, &str, String)]) -> Vec<LogRow> {
    let mut rows: Vec<LogRow> = Vec::new();
    for (i, (k, kind, payload)) in specs.iter().enumerate() {
        let seq = i as i64 + 1;
        let prev = rows
            .last()
            .map_or(GENESIS_MAC.to_owned(), |r| r.mac.clone());
        let ts = "2026-01-02T03:04:05.000Z";
        let mac = log::compute_mac(k.bytes(), seq, ts, None, kind, payload, &prev);
        rows.push(LogRow {
            seq,
            ts: ts.to_owned(),
            session: None,
            kind: (*kind).to_owned(),
            payload: payload.clone(),
            prev_mac: prev,
            mac,
        });
    }
    rows
}

fn rekey(old: &Key, new: &Key) -> String {
    format!(
        "{{\"new_fingerprint\":\"{}\",\"old_fingerprint\":\"{}\"}}",
        fp(new),
        fp(old)
    )
}

fn note() -> String {
    "{}".to_owned()
}

fn ring(current: Option<&Key>, retired: &[&Key]) -> KeyRing {
    let mut r = KeyRing {
        current: current.cloned(),
        ..KeyRing::default()
    };
    for k in retired {
        r.retire((*k).clone());
    }
    r
}

fn segs(rows: &[LogRow], ring: &KeyRing, meta: &str, caller: Option<&Key>) -> Vec<Segment> {
    chain_segments(rows, ring, &unicode::cps(meta), caller).unwrap()
}

fn origin_file(row: &LogRow, named: Option<&Key>) -> Vec<u8> {
    match named {
        Some(k) => format!(
            "{{\"seq\": {}, \"mac\": \"{}\", \"key_fingerprint\": \"{}\"}}",
            row.seq,
            row.mac,
            fp(k)
        ),
        None => format!("{{\"seq\": {}, \"mac\": \"{}\"}}", row.seq, row.mac),
    }
    .into_bytes()
}

#[test]
fn the_callers_key_verifies_the_anchor_the_ring_does_not_hold() {
    let (b, d) = (key(32), key(96));
    let rows = chain(&[(&d, "store_created", note()), (&d, "note", note())]);
    let mut r = ring(Some(&b), &[]);
    r.origin_file = Some(origin_file(&rows[0], Some(&d)));
    let held_only = chain_trust_with(rows.as_slice(), &r, &unicode::cps(&fp(&d)), None).unwrap();
    assert_eq!(held_only.origin.status, "unverifiable");
    let trust = chain_trust_with(rows.as_slice(), &r, &unicode::cps(&fp(&d)), Some(&d)).unwrap();
    assert_eq!(trust.origin.status, "ok");
    assert_eq!(trust.origin.signer, Some(fp(&d)));
}

#[test]
fn the_callers_key_names_the_leading_segment_when_the_ring_lacks_it() {
    let (a, b, d) = (key(0), key(32), key(96));
    // The rekey row says the leading key was A, which no one holds; the
    // first row verifies under the caller's D alone.
    let rows = chain(&[(&d, "store_created", note()), (&b, REKEY, rekey(&a, &b))]);
    let r = ring(Some(&b), &[]);
    let s = segs(&rows, &r, &fp(&b), Some(&d));
    assert_eq!(s[0].key_fingerprint, unicode::cps(&fp(&d)));
    assert_eq!(s[0].key.as_ref(), Some(&d));
    let none = segs(&rows, &r, &fp(&b), None);
    assert_eq!(none[0].key_fingerprint, unicode::cps(&fp(&a)));
    assert!(!none[0].held());
}

#[test]
fn the_callers_key_answers_its_own_name_before_the_ring() {
    let (a, b, c) = (key(0), key(32), key(64));
    // The ring's current file holds B; the caller opened under C, the meta
    // name, and a rekey row names C.
    let rows = chain(&[
        (&a, "store_created", note()),
        (&b, REKEY, rekey(&a, &b)),
        (&c, REKEY, rekey(&b, &c)),
    ]);
    let r = ring(Some(&b), &[&a]);
    let s = segs(&rows, &r, &fp(&c), Some(&c));
    assert_eq!(s.len(), 3);
    assert_eq!(s[2].key.as_ref(), Some(&c));
    assert!(!s[2].foreign);
    let without = segs(&rows, &r, &fp(&c), None);
    assert!(without[2].foreign, "under the ring alone C is not held");
}

#[test]
fn a_rekey_at_the_first_rows_seq_drops_the_leading_segment() {
    let (a, b) = (key(0), key(32));
    let rows = chain(&[(&b, REKEY, rekey(&a, &b)), (&b, "note", note())]);
    let s = segs(&rows, &ring(Some(&b), &[]), &fp(&b), Some(&b));
    assert_eq!(s.len(), 1);
    assert_eq!(s[0].first_seq, 1);
    assert_eq!(s[0].first_row.as_ref().unwrap().kind, REKEY);
}

#[test]
fn a_segment_ending_at_the_anchors_seq_ends_before_it() {
    let (a, b, d) = (key(0), key(32), key(96));
    // The leading segment is A's by name and its first row is D's, so no
    // held key holds it; the row after it is B's, so an anchor there holds.
    let rows = chain(&[
        (&d, "store_created", note()),
        (&b, "note", note()),
        (&b, REKEY, rekey(&a, &b)),
        (&b, "note", note()),
    ]);
    for (anchor, foreign) in [(1usize, true), (2, false), (3, false)] {
        let mut r = ring(Some(&b), &[]);
        r.origin_file = Some(origin_file(&rows[anchor], Some(&b)));
        let t = chain_trust_with(rows.as_slice(), &r, &unicode::cps(&fp(&b)), Some(&b)).unwrap();
        assert_eq!(t.origin.status, "ok", "anchor at {}", anchor + 1);
        assert_eq!(t.segments[0].foreign, foreign, "anchor at {}", anchor + 1);
    }
}

fn entry(text: &str) -> PyValue {
    json::loads(&unicode::cps(text)).unwrap()
}

#[test]
fn accepted_history_must_be_the_object_true() {
    for (text, kind) in [
        (r#"{"accepted_history": true}"#, "history_accepted"),
        (r#"{"accepted_history": 1}"#, "store_restored"),
        (r#"{"accepted_history": 1.0}"#, "store_restored"),
        (r#"{"accepted_history": "true"}"#, "store_restored"),
        (
            r#"{"kind": "rekey", "accepted_history": true}"#,
            "history_accepted",
        ),
        (
            r#"{"kind": "store_restored", "accepted_history": true}"#,
            "store_restored",
        ),
    ] {
        assert_eq!(history_replaced_kind(&entry(text)), kind, "{text}");
    }
}

#[test]
fn a_seq_must_be_an_int_and_not_a_bool() {
    let row = |text: &str| history_replaced_row(&entry(text)).map(|r| r.to_value());
    assert_eq!(
        row(r#"{"row": {"seq": true, "mac": "m"}, "seq": 4}"#),
        Some(entry(r#"{"seq": 4}"#))
    );
    assert_eq!(row(r#"{"row": {"seq": 5.0}, "seq": false}"#), None);
    assert_eq!(
        row(r#"{"row": {"seq": 5, "mac": 1}}"#),
        Some(entry(r#"{"seq": 5}"#))
    );
    assert_eq!(
        row(r#"{"row": {"seq": 123456789012345678901234567890, "mac": "m"}}"#),
        Some(entry(
            r#"{"seq": 123456789012345678901234567890, "mac": "m"}"#
        ))
    );
    assert_eq!(
        row(r#"{"row": [1], "seq": 2}"#),
        Some(entry(r#"{"seq": 2}"#))
    );
}

#[test]
fn the_payload_keeps_a_present_kind_in_place_and_drops_row() {
    let got = replaced_payload(
        &entry(r#"{"engine_version": "0", "row": 1, "kind": "x", "a": 1}"#),
        "store_restored",
        "9.0.0",
    );
    assert_eq!(
        got,
        entry(r#"{"engine_version": "9.0.0", "kind": "store_restored", "a": 1}"#)
    );
    let PyValue::Dict(items) = &got else { panic!() };
    let keys: Vec<_> = items.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(
        keys,
        ["engine_version", "kind", "a"].map(PyValue::str).to_vec()
    );
    let fresh = replaced_payload(&entry(r#"{"a": 1}"#), "history_accepted", "9");
    assert_eq!(
        fresh,
        entry(r#"{"a": 1, "kind": "history_accepted", "engine_version": "9"}"#)
    );
}
