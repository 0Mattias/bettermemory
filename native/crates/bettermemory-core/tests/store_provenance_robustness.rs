//! The provenance readers on hostile stores: a typed outcome every time,
//! never a panic, a stack overflow or an unbounded read. Values of the
//! wrong type, NULLs, a label nobody writes, ids that are not ULIDs, a
//! pointed-at log payload nested 9,998 and 100,000 deep (on a 512 KiB
//! thread), an enormous payload, rekey rows and origin files nested past
//! the limit, key files missing, empty and oversized, and a blob payload. Each store is
//! the one `store_create.json` pins, made natively and then edited.

mod common;

use std::fs;
use std::path::{Path, PathBuf};

use bettermemory_core::store::provenance::{Provenance, ProvenanceError};
use bettermemory_core::store::StoreFile;
use common::provenance::{created_store, ring, scratch};
use rusqlite::Connection;

const ID: &str = "01J00000000000000000000001";

fn store_id() -> String {
    common::created::fixture()["fixed"]["store_id"]
        .as_str()
        .expect("id")
        .to_owned()
}

/// A created store with one record whose pointer names `mac`, edited by
/// `sql` (`?1` is the record id).
fn store_with(dir: &Path, sql: &str) -> (PathBuf, PathBuf) {
    let (file, keys) = created_store(dir);
    let conn = Connection::open(&file).expect("open");
    conn.execute(
        "INSERT INTO memories (id, created, updated, confidence, body, scopes_text, \
         scopes_json, provenance, source, log_mac) VALUES (?1, '2001', '2001', 'high', \
         'b', 'global', '[\"global\"]', 'local', 'explicit-statement', 'm1')",
        [ID],
    )
    .expect("record");
    for statement in sql.split(";\n").filter(|s| !s.trim().is_empty()) {
        if statement.contains("?1") {
            conn.execute(statement, [ID]).expect(statement);
        } else {
            conn.execute_batch(statement).expect(statement);
        }
    }
    (file, keys)
}

fn pointer_row(payload: &str) -> String {
    format!(
        "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) \
         VALUES (900, 't', NULL, 'memory_put', '{}', 'p', 'm1')",
        payload.replace('\'', "''")
    )
}

type Labels = Result<Vec<(String, String)>, ProvenanceError>;

fn labels(file: &Path, keys: &Path, verifies: bool) -> Labels {
    let store = StoreFile::open(file).expect("open");
    let ring = ring(keys, &store_id());
    let reader = Provenance::new(&store, &ring, verifies, false)?;
    let out = reader.provenance_for(&[ID])?;
    // Every other reader also gives a typed outcome.
    let _ = reader.trust_rows(&[ID]);
    let _ = reader.unaccounted_ids();
    let _ = reader.provenance_counts();
    let _ = reader.links_with_status(ID);
    let _ = reader.episode_provenance(&[ID]);
    Ok(out)
}

fn one(label: &str) -> Labels {
    Ok(vec![(ID.to_owned(), label.to_owned())])
}

fn recursion() -> Labels {
    Err(ProvenanceError::Raised {
        class: "RecursionError",
        text: None,
    })
}

/// `{"memory": {"id": [[...]]}}` nested `depth` deep in all.
fn nested(depth: usize) -> String {
    let lists = depth - 2;
    format!(
        "{{\"memory\":{{\"id\":{}{}}}}}",
        "[".repeat(lists),
        "]".repeat(lists)
    )
}

#[test]
fn wrong_types_nulls_and_unknown_labels_read_as_stored() {
    // (edit, verifies, answer): a label as stored when the reader does
    // not verify; a pointer that is not a non-empty str when it does.
    let cases: [(&str, bool, Labels); 6] = [
        (
            "UPDATE memories SET provenance = X'00ff27' WHERE id = ?1",
            false,
            one("b\"\\x00\\xff'\""),
        ),
        (
            "UPDATE memories SET provenance = 'not a label' WHERE id = ?1",
            false,
            one("not a label"),
        ),
        (
            "UPDATE memories SET log_mac = X'6d31' WHERE id = ?1",
            true,
            one("unaccounted"),
        ),
        (
            "UPDATE memories SET log_mac = NULL WHERE id = ?1",
            true,
            one("unaccounted"),
        ),
        (
            "UPDATE memories SET log_mac = 5 WHERE id = ?1",
            true,
            one("unaccounted"),
        ),
        (
            "UPDATE memories SET id = 'not a ulid' WHERE id = ?1",
            true,
            Ok(vec![]),
        ),
    ];
    for (sql, verifies, want) in cases {
        let dir = scratch("robust");
        let (file, keys) = store_with(&dir.0, &format!("{};\n{sql}", pointer_row("{}")));
        assert_eq!(labels(&file, &keys, verifies), want, "{sql}");
    }
}

#[test]
fn a_record_id_that_is_not_utf8_is_refused() {
    {
        let sql = "UPDATE memories SET id = CAST(X'ff' AS TEXT) WHERE id = ?1";
        let dir = scratch("robust");
        let (file, keys) = store_with(&dir.0, &format!("{};\n{sql}", pointer_row("{}")));
        let store = StoreFile::open(&file).expect("open");
        let ring = ring(&keys, &store_id());
        let reader = Provenance::new(&store, &ring, true, false).expect("reader");
        let got = reader.unaccounted_ids().map(|_| ());
        assert!(
            matches!(got, Err(ProvenanceError::NotPorted(_))),
            "{sql}: {got:?}"
        );
    }
}

#[test]
fn a_blob_payload_is_decoded_not_refused() {
    // `{}` as bytes: parsed, no record, so the pointer fails.
    let dir = scratch("robust");
    let sql = format!(
        "{};\nUPDATE log SET payload = X'7b7d' WHERE seq = 900",
        pointer_row("{}")
    );
    let (file, keys) = store_with(&dir.0, &sql);
    assert_eq!(labels(&file, &keys, true), one("unaccounted"));
}

#[test]
fn deep_payloads_on_a_small_stack() {
    let cases: [(usize, Labels); 3] = [
        (9_998, one("unaccounted")),
        (9_999, recursion()),
        (100_000, recursion()),
    ];
    for (depth, want) in cases {
        let dir = scratch("robust-deep");
        let (file, keys) = store_with(&dir.0, &pointer_row(&nested(depth)));
        let got = std::thread::Builder::new()
            .stack_size(512 * 1024)
            .spawn(move || labels(&file, &keys, true))
            .expect("thread")
            .join()
            .expect("no overflow, no panic");
        assert_eq!(got, want, "depth {depth}");
    }
}

#[test]
fn an_enormous_payload_is_read_once_and_answers() {
    let dir = scratch("robust-big");
    let big = format!("{{\"memory\":{{\"id\":\"{}\"}}}}", "a".repeat(16 << 20));
    let (file, keys) = store_with(&dir.0, &pointer_row(&big));
    assert_eq!(labels(&file, &keys, true), one("unaccounted"));
}

#[test]
fn rekey_rows_and_origin_files_nested_past_the_limit_raise() {
    let id = store_id();
    // A rekey row's payload nested 100,000 deep: the segment map raises.
    let dir = scratch("robust-rekey");
    let rekey = format!(
        "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) \
         VALUES (901, 't', NULL, 'rekey', '{}', 'p', 'r')",
        "[".repeat(100_000) + &"]".repeat(100_000)
    );
    let payload = format!("{{\"memory\":{{\"id\":\"{ID}\"}}}}");
    let (file, keys) = store_with(&dir.0, &format!("{};\n{rekey}", pointer_row(&payload)));
    assert_eq!(labels(&file, &keys, true), recursion());
    // The origin file nested past the limit, and one that is junk.
    for (origin, want) in [
        ("[".repeat(100_000) + &"]".repeat(100_000), recursion()),
        ("not json".to_owned(), one("unaccounted")),
    ] {
        let dir = scratch("robust-origin");
        let (file, keys) = store_with(&dir.0, &pointer_row(&payload));
        fs::write(keys.join(format!("{id}.origin")), origin).expect("origin");
        assert_eq!(labels(&file, &keys, true), want);
    }
}

#[test]
fn key_files_missing_empty_and_oversized() {
    let id = store_id();
    let payload = format!("{{\"memory\":{{\"id\":\"{ID}\"}}}}");
    // Missing: no current key. A reader that does not verify reads the
    // label as stored; one asked to verify is refused.
    // Over the ring loader's 1 MiB bound (`keys::MAX_FILE_BYTES`): a
    // native refusal where the reference answers. Its `load_current` reads
    // the whole file and verifies under it, so `provenance_for` gives the
    // stored label there (a product key is 32 bytes); here a reader asked
    // to verify, as the reference's would, is refused, never answered as
    // if no key were held.
    for content in [None, Some(vec![7u8; (1 << 20) + 1])] {
        let dir = scratch("robust-keys");
        let (file, keys) = store_with(&dir.0, &pointer_row(&payload));
        let key = keys.join(format!("{id}.key"));
        match &content {
            None => fs::remove_file(&key).expect("rm"),
            Some(bytes) => fs::write(&key, bytes).expect("write"),
        }
        fs::write(keys.join(format!("{id}.0000000000000000.key")), b"").expect("retired");
        if content.is_none() {
            assert_eq!(labels(&file, &keys, false), one("local"));
        }
        let refused = labels(&file, &keys, true);
        assert!(
            matches!(&refused, Err(ProvenanceError::NotPorted(why)) if why.contains("1 MiB")),
            "{refused:?}"
        );
    }
    // An empty current key verifies no row: the leading segment, the
    // created store's, is then one whose key is not held, so the label
    // stands (the copied-store rule), and the anchor is unverifiable.
    let dir = scratch("robust-keys");
    let (file, keys) = store_with(&dir.0, &pointer_row(&payload));
    fs::write(keys.join(format!("{id}.key")), b"").expect("empty");
    assert_eq!(labels(&file, &keys, true), one("local"));
}
