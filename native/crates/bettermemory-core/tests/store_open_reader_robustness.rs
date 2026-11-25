//! Hostile input for `reader_keys`: twenty thousand rekey rows naming the
//! key in hand, each tried under it, in time linear in the rows; a rekey
//! payload and an origin file nested 100,000 deep (RecursionError, as the
//! reference's `json.loads` raises, and no overflow of the stack); a first
//! row whose seq is at either end of i64; and no key's bytes or
//! fingerprint in any Debug output.
mod timing;

use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::log::{self, Key, KeyRing, LogRow, VerifyError, REKEY};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::key_read::open_reader::reader_keys;
use bettermemory_core::store::key_state::{KeyState, KeyStatus};
use bettermemory_core::unicode::cps;
use std::fs;
use std::path::PathBuf;

const STORE_ID: &str = "0123456789abcdef0123456789abcdef";

fn key(byte: u8) -> Key {
    Key::new(vec![byte; 32])
}

fn fp(key: &Key) -> String {
    log::fingerprint(key.bytes())
}

fn row(seq: i64, kind: &str, payload: String, signer: &Key) -> LogRow {
    let ts = "2026-01-01T00:00:00+00:00";
    let prev = log::GENESIS_MAC;
    let mac = log::compute_mac(signer.bytes(), seq, ts, None, kind, &payload, prev);
    LogRow {
        seq,
        ts: ts.to_owned(),
        session: None,
        kind: kind.to_owned(),
        payload,
        prev_mac: prev.to_owned(),
        mac,
    }
}

fn foreign(held: &Key, expected: &str) -> KeyState {
    KeyState {
        state: KeyStatus::Foreign,
        keyring: KeyFiles::new(&PathBuf::from("/nowhere/keys"), &PyValue::str(STORE_ID)).unwrap(),
        store_file: PathBuf::from("/nowhere/memory.sqlite"),
        expected: cps(expected),
        key: Some(held.clone()),
        error: None,
        bound_to: None,
        unread: None,
        unfit: None,
    }
}

fn ring(current: &Key) -> KeyRing {
    KeyRing {
        current: Some(current.clone()),
        ..KeyRing::default()
    }
}

fn deep() -> String {
    format!("{}{}", "[".repeat(100_000), "]".repeat(100_000))
}

/// A first row under `theirs`, then `n` rekey rows that each name `ours`
/// and are signed by `theirs`: every segment is under the fingerprint of
/// `ours` and none verifies under it.
fn named_unsigned(n: usize, theirs: &Key, ours: &Key) -> Vec<LogRow> {
    let mut rows = vec![row(1, "store_created", "{}".to_owned(), theirs)];
    let payload = format!(
        r#"{{"new_fingerprint":"{}","old_fingerprint":"{}"}}"#,
        fp(ours),
        fp(theirs)
    );
    for i in 0..n {
        rows.push(row(i as i64 + 2, REKEY, payload.clone(), theirs));
    }
    rows
}

#[test]
fn twenty_thousand_rekey_rows_grow_linearly() {
    let (ours, theirs) = (key(1), key(2));
    let st = foreign(&ours, &fp(&theirs));
    let r = ring(&ours);
    let small = named_unsigned(2_000, &theirs, &ours);
    let large = named_unsigned(20_000, &theirs, &ours);
    let got = reader_keys(large.as_slice(), &r, &st).unwrap();
    assert!(got.read_key().is_none());
    timing::grows_within(
        "reader_keys",
        timing::TENFOLD,
        &mut || timing::once(|| reader_keys(small.as_slice(), &r, &st)),
        &mut || timing::once(|| reader_keys(large.as_slice(), &r, &st)),
    );
}

#[test]
fn a_rekey_payload_nested_deep_is_a_recursion_error() {
    let (ours, theirs) = (key(1), key(2));
    let rows = vec![
        row(1, "store_created", "{}".to_owned(), &theirs),
        row(2, REKEY, deep(), &theirs),
    ];
    let got = reader_keys(rows.as_slice(), &ring(&ours), &foreign(&ours, &fp(&theirs)));
    assert!(matches!(got, Err(VerifyError::Py(PyError::RecursionError))));
}

#[test]
fn an_origin_file_nested_deep_is_a_recursion_error() {
    let (ours, theirs) = (key(1), key(2));
    let rows = vec![row(1, "store_created", "{}".to_owned(), &ours)];
    let mut st = foreign(&ours, &fp(&theirs));
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("open-reader-robustness-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    st.keyring = KeyFiles::new(&dir, &PyValue::str(STORE_ID)).unwrap();
    fs::write(st.keyring.origin_path(), deep()).unwrap();
    let got = reader_keys(rows.as_slice(), &ring(&ours), &st);
    let _ = fs::remove_dir_all(&dir);
    assert!(matches!(got, Err(VerifyError::Py(PyError::RecursionError))));
}

#[test]
fn a_first_row_at_either_end_of_i64() {
    let (ours, theirs) = (key(1), key(2));
    let st = foreign(&ours, &fp(&theirs));
    for seq in [i64::MIN, -1, 0, 1, i64::MAX] {
        let rows = vec![row(seq, "store_created", "{}".to_owned(), &ours)];
        let got = reader_keys(rows.as_slice(), &ring(&ours), &st).unwrap();
        assert_eq!(got.read_key(), Some(&ours), "{seq}");
        let rows = vec![row(seq, "store_created", "{}".to_owned(), &theirs)];
        let got = reader_keys(rows.as_slice(), &ring(&ours), &st).unwrap();
        assert!(got.read_key().is_none(), "{seq}");
    }
}

#[test]
fn no_key_shows_in_debug_output() {
    let ours = Key::new(
        (0u8..32)
            .map(|b| b.wrapping_mul(37).wrapping_add(11))
            .collect(),
    );
    let rows = vec![row(1, "store_created", "{}".to_owned(), &ours)];
    let mut st = foreign(&ours, &fp(&key(2)));
    let reader = reader_keys(rows.as_slice(), &ring(&ours), &st).unwrap();
    assert!(reader.read_key().is_some());
    st.state = KeyStatus::Present;
    let present = reader_keys(rows.as_slice(), &ring(&ours), &st).unwrap();
    assert!(present.has_key());
    let hex: String = ours.bytes().iter().map(|b| format!("{b:02x}")).collect();
    let listed = format!("{:?}", ours.bytes());
    let print = fp(&ours);
    for shown in [
        format!("{reader:?}"),
        format!("{reader:#?}"),
        format!("{present:?}"),
        format!("{present:#?}"),
    ] {
        assert!(!shown.contains(&hex[..16]), "{shown}");
        assert!(!shown.contains(&listed[1..20]), "{shown}");
        assert!(!shown.contains(&print[..8]), "{shown}");
    }
}
