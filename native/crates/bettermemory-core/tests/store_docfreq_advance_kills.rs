//! Wrong advances of a memo entry that the fixture's whole-entry
//! comparison catches, each pinned here on a store made by hand
//! (`common::docfreq_store::HandStore`) through
//! `DocFreqSource::advanced_inputs` itself: a moved row's bit left in its
//! old group or its length counted twice, a new row whose scopes are not
//! a list given a position, a row that left every group missing from
//! `changed`, the 256-row bound off by one or counted without the entry's
//! `changed`, a `memory_delete` or a rebuild followed, the MAC check
//! dropped, `digest_mark` not called (one warning where the reference
//! logs two), a digest row the log did not name read again, and the id
//! not taken as `str()` of the payload's value. Runs everywhere.

mod common;

use std::path::Path;

use bettermemory_core::store::docfreq::{DocFreqInputs, DocFreqMemo, DocFreqSource, Head, PyInt};
use bettermemory_core::store::docfreq_store::{self, StoreDocFreq, WarningKind};
use common::docfreq_store::{mask_positions, HandStore};
use rusqlite::{Connection, OpenFlags};

const PATH: &str = "/nowhere/memory.sqlite";

fn hand() -> HandStore {
    HandStore::new(Connection::open_in_memory().expect("memory"))
}

/// Records a (row 1, scopes g, length 5), b (2, g, 7), c (3, h, 11), and
/// the entry read at that head.
fn three() -> (HandStore, DocFreqInputs) {
    let mut s = hand();
    s.put("a", 1, "[\"g\"]", None, 5);
    s.put("b", 2, "[\"g\"]", None, 7);
    s.put("c", 3, "[\"h\"]", None, 11);
    let entry = {
        let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
        let head = src.memory_head().expect("head");
        src.read_inputs(&head).expect("read")
    };
    (s, entry)
}

fn advance(s: &HandStore, entry: &DocFreqInputs) -> Option<DocFreqInputs> {
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    let head = src.memory_head().expect("head");
    src.advanced_inputs(entry, &head).expect("advance")
}

fn at(s: &HandStore, entry: &DocFreqInputs, head: &Head) -> Option<DocFreqInputs> {
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    src.advanced_inputs(entry, head).expect("advance")
}

/// `changed` as the entry holds it: (version, row number, position).
fn changed(rows: &[(u64, i64, usize)]) -> Vec<(u64, PyInt, usize)> {
    rows.iter()
        .map(|&(v, r, p)| (v, PyInt::Small(r), p))
        .collect()
}

fn groups(inputs: &DocFreqInputs) -> Vec<Vec<usize>> {
    inputs
        .groups
        .iter()
        .map(|g| mask_positions(&g.mask))
        .collect()
}

#[test]
fn a_moved_row_leaves_its_old_group_and_its_length() {
    let (mut s, entry) = three();
    assert_eq!(groups(&entry), vec![vec![0, 1], vec![2]]);
    s.put("b", 2, "[\"h\"]", None, 7);
    let got = advance(&s, &entry).expect("advanced");
    assert_eq!(
        groups(&got),
        vec![vec![0], vec![1, 2]],
        "the old bit cleared"
    );
    assert_eq!(got.lengths, Some(vec![5, 18]), "the old length reduced");
    assert_eq!(got.group_of, Some(vec![0, 1, 1]));
    assert_eq!(got.length_of, Some(vec![5, 7, 11]));
    assert_eq!(got.changed, changed(&[(1, 2, 1)]));
    assert_eq!(got.version, 1);
    assert_eq!(got.keys, entry.keys, "no group made");
}

#[test]
fn a_new_key_makes_a_group_at_the_end() {
    let (mut s, entry) = three();
    s.put("a", 1, "[\"g\"]", Some("c9"), 5);
    s.put("d", 4, "[\"n\"]", None, 2);
    let got = advance(&s, &entry).expect("advanced");
    assert_eq!(groups(&got), vec![vec![1], vec![2], vec![0], vec![3]]);
    assert_eq!(got.lengths, Some(vec![7, 11, 5, 2]));
    assert_eq!(got.keys[2].2.as_deref(), Some("c9"));
    assert_eq!(
        got.groups[2]
            .actor
            .as_ref()
            .and_then(|a| a.client.as_deref()),
        Some("c9")
    );
    assert_eq!(got.index.get(&PyInt::Small(4)), Some(&3));
    assert_eq!(got.changed, changed(&[(1, 1, 0), (1, 4, 3)]));
}

#[test]
fn a_new_row_whose_scopes_are_not_a_list_takes_no_position() {
    let (mut s, entry) = three();
    s.put("d", 4, "\"x\"", None, 3);
    let got = advance(&s, &entry).expect("advanced");
    assert_eq!(got.index.get(&PyInt::Small(4)), None);
    assert_eq!(got.index.len(), 3);
    assert_eq!(got.group_of, entry.group_of);
    assert!(got.changed.is_empty());
    assert_eq!(got.version, 1);
}

#[test]
fn a_row_that_leaves_every_group_is_still_changed() {
    let (mut s, entry) = three();
    s.put("a", 1, "{}", None, 5);
    let got = advance(&s, &entry).expect("advanced");
    assert_eq!(groups(&got), vec![vec![1], vec![2]]);
    assert_eq!(got.group_of, Some(vec![-1, 0, 1]));
    assert_eq!(got.length_of, Some(vec![0, 7, 11]));
    assert_eq!(got.lengths, Some(vec![7, 11]));
    assert_eq!(got.changed, changed(&[(1, 1, 0)]));
    // And a list again: back into the group its key names.
    let mut s = s;
    s.put("a", 1, "[\"g\"]", None, 6);
    let again = advance(&s, &got).expect("advanced");
    assert_eq!(groups(&again), vec![vec![0, 1], vec![2]]);
    assert_eq!(again.changed, changed(&[(1, 1, 0), (2, 1, 0)]));
    assert_eq!(again.lengths, Some(vec![13, 11]));
}

#[test]
fn the_bound_counts_the_ids_and_the_entrys_changed() {
    let (mut s, entry) = three();
    s.put("a", 1, "[\"g\"]", None, 5);
    s.put("b", 2, "[\"g\"]", None, 7);
    s.put("c", 3, "[\"h\"]", None, 11);
    let three_changed = advance(&s, &entry).expect("advanced");
    assert_eq!(three_changed.changed.len(), 3);
    let base = s.seq;
    s.named(257, |i| format!("p{i}"));
    let head = |n: i64| (base + n, format!("m{}", base + n));
    assert!(at(&s, &three_changed, &head(253)).is_some(), "253 + 3");
    assert!(at(&s, &three_changed, &head(254)).is_none(), "254 + 3");
    let mut none_changed = three_changed.clone();
    none_changed.changed.clear();
    assert!(at(&s, &none_changed, &head(256)).is_some(), "256");
    assert!(at(&s, &none_changed, &head(257)).is_none(), "257");
}

#[test]
fn a_name_repeated_counts_once() {
    let (mut s, entry) = three();
    s.named(600, |i| format!("q{}", i % 256));
    assert!(advance(&s, &entry).is_some());
    s.named(1, |_| "q-new".to_owned());
    assert!(advance(&s, &entry).is_none());
}

#[test]
fn a_memory_delete_or_a_rebuild_is_not_followed() {
    for kind in ["memory_delete", "index_rebuilt"] {
        let (mut s, entry) = three();
        s.log_row(kind, "{\"id\": \"zz\"}", false);
        assert!(advance(&s, &entry).is_none(), "{kind}");
        // Named after it, a put does not make it followed.
        s.put("d", 4, "[\"g\"]", None, 1);
        assert!(advance(&s, &entry).is_none(), "{kind} then a put");
    }
}

#[test]
fn another_mac_at_the_entrys_head_is_another_history() {
    for gone in [false, true] {
        let (mut s, _) = three();
        s.home("a", Some("p"));
        let entry = {
            let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
            let head = src.memory_head().expect("head");
            src.read_inputs(&head).expect("read")
        };
        let row = entry.head.0;
        s.home("a", None);
        assert!(advance(&s, &entry).is_some(), "the same history");
        let statement = if gone {
            "DELETE FROM log WHERE seq = ?"
        } else {
            "UPDATE log SET mac = 'other' WHERE seq = ?"
        };
        s.conn.execute(statement, [row]).expect("edit");
        assert!(advance(&s, &entry).is_none(), "gone {gone}");
    }
}

#[test]
fn a_digest_row_the_log_does_not_name_keeps_its_group() {
    let (mut s, entry) = three();
    s.conn
        .execute("UPDATE digest_records SET client = 'zz' WHERE id = 'a'", [])
        .expect("planted");
    s.put("d", 4, "[\"g\"]", None, 1);
    let got = advance(&s, &entry).expect("advanced");
    assert_eq!(groups(&got), vec![vec![0, 1, 3], vec![2]]);
    assert_eq!(got.keys.len(), 2, "no group for the planted client");
    assert_eq!(got.changed, changed(&[(1, 4, 3)]));
}

#[test]
fn the_id_is_str_of_the_payloads_value() {
    let (mut s, entry) = three();
    // A digest row whose id is the text a number's str() spells.
    s.conn
        .execute(
            "INSERT INTO digest_records (id, rid, scopes, dl) VALUES ('7', 9, '[\"g\"]', 4)",
            [],
        )
        .expect("row");
    s.log_row("home_delete", "{\"record_id\": 7}", false);
    let got = advance(&s, &entry).expect("advanced");
    assert_eq!(got.index.get(&PyInt::Small(9)), Some(&3));
    // A list's str() is its repr.
    s.conn
        .execute(
            "INSERT INTO digest_records (id, rid, scopes, dl) VALUES ('[1, ''a'']', 10, '[\"g\"]', 4)",
            [],
        )
        .expect("row");
    s.log_row("home_put", "{\"home\": {\"record_id\": [1, \"a\"]}}", false);
    let got = advance(&s, &entry).expect("advanced");
    assert_eq!(got.index.get(&PyInt::Small(10)), Some(&4));
}

#[test]
fn a_payload_that_does_not_read_is_read_again() {
    for payload in ["no", "{}", "{\"home\": []}", "[]", "{\"home\": {}}"] {
        let (mut s, entry) = three();
        s.log_row("home_put", payload, false);
        assert!(advance(&s, &entry).is_none(), "{payload}");
    }
}

#[test]
fn the_digest_is_brought_current_first_and_warns_on_a_reader() {
    let dir = common::stores::scratch("dfa");
    let file = dir.0.join("memory.sqlite");
    let mut s = HandStore::new(Connection::open(&file).expect("open"));
    s.put("a", 1, "[\"g\"]", None, 5);
    let memo = DocFreqMemo::new();
    let t = vec!["x".to_owned()];
    {
        let src = StoreDocFreq::over(&s.conn, &file, "id");
        docfreq_store::ranking_statistics(&src, &memo, &t, &mut |_, _, _| true).expect("kept");
        assert!(src.warnings().is_empty());
    }
    // A record mutation the digest is behind by.
    s.conn
        .execute(
            "INSERT INTO memories (rowid, id, scopes_json) VALUES (2, 'b', '[\"g\"]')",
            [],
        )
        .expect("row");
    s.log_row("memory_put", "{\"memory\": {\"id\": \"b\"}}", true);
    let ro = Connection::open_with_flags(&file, OpenFlags::SQLITE_OPEN_READ_ONLY).expect("ro");
    let src = StoreDocFreq::over(&ro, &file, "id");
    let entry = memo.entry(&src.store_key()).expect("kept").inputs.clone();
    let head = src.memory_head().expect("head");
    assert!(src
        .advanced_inputs(&entry, &head)
        .expect("advance")
        .is_none());
    let kinds: Vec<WarningKind> = src.warnings().iter().map(|w| w.kind).collect();
    assert_eq!(kinds, vec![WarningKind::NotCurrent], "the advance's own");
    docfreq_store::ranking_statistics(&src, &memo, &t, &mut |_, _, _| true).expect("read");
    let kinds: Vec<WarningKind> = src.warnings().iter().map(|w| w.kind).collect();
    assert_eq!(
        kinds,
        vec![WarningKind::NotCurrent, WarningKind::NotCurrent]
    );
}

#[test]
fn a_digest_text_that_is_not_utf8_is_read_again_with_no_warning() {
    // CPython 3.13.13, `_advanced_inputs` called directly: a named row's
    // scopes or client that does not decode is sqlite3's OperationalError,
    // caught by the read: None, and nothing logged.
    for column in ["scopes", "client"] {
        let (mut s, entry) = three();
        s.conn
            .execute(
                &format!("UPDATE digest_records SET {column} = CAST(X'FF' AS TEXT) WHERE id = 'a'"),
                [],
            )
            .expect("planted");
        s.home("a", Some("p"));
        let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
        let head = src.memory_head().expect("head");
        assert_eq!(src.advanced_inputs(&entry, &head), Ok(None), "{column}");
        assert!(src.warnings().is_empty(), "{column}");
    }
}

#[test]
fn a_head_at_the_entrys_own_seq_is_read_again() {
    // Through the memo the advance is asked only for another head, and
    // the same seq with another MAC fails the MAC check; asked directly,
    // `head[0] <= seq` answers None before the log is read.
    let (s, entry) = three();
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    let same_seq = (entry.head.0, "other".to_owned());
    assert_eq!(src.advanced_inputs(&entry, &same_seq), Ok(None));
    assert_eq!(src.advanced_inputs(&entry, &entry.head), Ok(None));
}
