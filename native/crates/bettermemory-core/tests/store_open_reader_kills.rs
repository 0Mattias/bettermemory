//! Cases a plausible wrong `reader_keys` fails: a read key kept on the
//! fingerprint's word without the MAC, the key in hand not passed to the
//! segment map as the caller's key, the origin anchor read when the key
//! matches meta, the log read when an anchor is there, the anchor read
//! from the ring in memory (whose loader leaves out a file over 1 MiB)
//! rather than from its file, a present key answered as a reader's, a
//! withheld ring's path dropped, and the dispatch of `_open_connected`.
use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::log::{self, Key, KeyRing, LogRow, LogSource, VerifyError, REKEY};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::key_read::open_reader::{binding, reader_keys, Binding};
use bettermemory_core::store::key_state::{KeyState, KeyStatus};
use bettermemory_core::unicode::cps;
use std::cell::Cell;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

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

fn rekey(seq: i64, old: &str, new: &str, signer: &Key) -> LogRow {
    let payload = format!(r#"{{"new_fingerprint":"{new}","old_fingerprint":"{old}"}}"#);
    row(seq, REKEY, payload, signer)
}

fn created(signer: &Key) -> LogRow {
    row(1, "store_created", "{}".to_owned(), signer)
}

fn keys_dir() -> PathBuf {
    PathBuf::from("/nowhere/keys")
}

fn state(status: KeyStatus, expected: &str, held: Option<&Key>) -> KeyState {
    state_in(&keys_dir(), status, expected, held)
}

fn state_in(dir: &Path, status: KeyStatus, expected: &str, held: Option<&Key>) -> KeyState {
    KeyState {
        state: status,
        keyring: KeyFiles::new(dir, &PyValue::str(STORE_ID)).unwrap(),
        store_file: PathBuf::from("/nowhere/memory.sqlite"),
        expected: cps(expected),
        key: held.cloned(),
        error: None,
        bound_to: None,
        unread: None,
        unfit: None,
    }
}

fn ring(current: Option<&Key>) -> KeyRing {
    KeyRing {
        current: current.cloned(),
        ..KeyRing::default()
    }
}

static DIRS: AtomicUsize = AtomicUsize::new(0);

/// A keys directory whose ring holds an origin file with `origin`'s text,
/// removed when dropped. The ring in memory holds no anchor: the reader's
/// step reads the file.
struct Anchored(PathBuf);
impl Anchored {
    fn new(origin: &str) -> Self {
        let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!(
            "open-reader-kills-{}-{}",
            std::process::id(),
            DIRS.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(format!("{STORE_ID}.origin")), origin).unwrap();
        Self(dir)
    }
    fn state(&self, status: KeyStatus, expected: &str, held: Option<&Key>) -> KeyState {
        state_in(&self.0, status, expected, held)
    }
}
impl Drop for Anchored {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn deep() -> String {
    format!("{}{}", "[".repeat(100_000), "]".repeat(100_000))
}

/// An anchor that parses (`parse_origin`): a seq and a MAC.
fn anchor() -> String {
    format!(r#"{{"seq": 1, "mac": "{}"}}"#, "ab".repeat(32))
}

/// A log that counts its reads and fails each one.
#[derive(Default)]
struct Refusing {
    reads: Cell<usize>,
}
impl LogSource for Refusing {
    type Error = &'static str;
    fn first(&self) -> Result<Option<LogRow>, Self::Error> {
        self.reads.set(self.reads.get() + 1);
        Err("first")
    }
    fn rekeys(&self) -> Result<Vec<LogRow>, Self::Error> {
        self.reads.set(self.reads.get() + 1);
        Err("rekeys")
    }
    fn at_seq(&self, _: i64) -> Result<Option<LogRow>, Self::Error> {
        self.reads.set(self.reads.get() + 1);
        Err("at_seq")
    }
    fn each(
        &self,
        _: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, Self::Error> {
        self.reads.set(self.reads.get() + 1);
        Err("each")
    }
}

#[test]
fn a_rekey_row_naming_the_key_is_not_evidence_without_its_mac() {
    let (ours, theirs, other) = (key(1), key(2), key(3));
    // The row naming the key in hand is signed by another key.
    let rows = vec![
        created(&theirs),
        rekey(2, &fp(&theirs), &fp(&ours), &theirs),
    ];
    let st = state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    assert!(got.read_key().is_none());
    assert!(!got.can_verify());
    // Signed by the key in hand, the same row is evidence.
    let rows = vec![created(&theirs), rekey(2, &fp(&theirs), &fp(&ours), &ours)];
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    assert_eq!(got.read_key(), Some(&ours));
}

#[test]
fn a_leading_segment_named_for_the_key_needs_its_first_row_to_verify() {
    let (ours, theirs, other) = (key(1), key(2), key(3));
    // The first rekey row names the key in hand as the old one, so the
    // leading segment is under its fingerprint by name; its first row is
    // signed by another key.
    let rows = vec![created(&theirs), rekey(2, &fp(&ours), &fp(&other), &other)];
    let st = state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    assert!(got.read_key().is_none());
    let rows = vec![created(&ours), rekey(2, &fp(&ours), &fp(&other), &other)];
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    assert_eq!(got.read_key(), Some(&ours));
}

#[test]
fn the_key_in_hand_is_the_segment_maps_own_key() {
    let (ours, other) = (key(1), key(3));
    // The ring as read after the open holds no key (its file went away),
    // the state holds the key in hand: the leading segment's first row is tried
    // under it, as `_leading_key` tries the caller's key.
    let rows = vec![created(&ours)];
    let st = state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(rows.as_slice(), &ring(None), &st).unwrap();
    assert_eq!(got.read_key(), Some(&ours));
    // A rekey row naming the key in hand resolves to it from the per-name memo,
    // though the ring now holds another key.
    let theirs = key(2);
    let rows = vec![created(&theirs), rekey(2, &fp(&theirs), &fp(&ours), &ours)];
    let got = reader_keys(rows.as_slice(), &ring(Some(&other)), &st).unwrap();
    assert_eq!(got.read_key(), Some(&ours));
}

#[test]
fn a_matching_key_reads_neither_the_anchor_nor_the_log() {
    let ours = key(1);
    let log = Refusing::default();
    let deep = Anchored::new(&deep());
    for status in [KeyStatus::BoundElsewhere, KeyStatus::Unclaimed] {
        let st = deep.state(status, &fp(&ours), Some(&ours));
        let got = reader_keys(&log, &ring(Some(&ours)), &st).unwrap();
        assert_eq!(got.read_key(), Some(&ours));
        assert!(got.can_verify() && !got.has_key());
    }
    assert_eq!(log.reads.get(), 0);
}

#[test]
fn the_match_is_the_texts_own_not_its_case() {
    let ours = key(1);
    let deep = Anchored::new(&deep());
    let st = deep.state(KeyStatus::Foreign, &fp(&ours).to_uppercase(), Some(&ours));
    let got = reader_keys(&Refusing::default(), &ring(Some(&ours)), &st);
    assert!(matches!(got, Err(VerifyError::Py(PyError::RecursionError))));
}

#[test]
fn an_anchor_keeps_the_key_without_reading_the_log() {
    let (ours, other) = (key(1), key(3));
    let log = Refusing::default();
    let anchored = Anchored::new(&anchor());
    let st = anchored.state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(&log, &ring(Some(&ours)), &st).unwrap();
    assert_eq!(got.read_key(), Some(&ours));
    assert_eq!(log.reads.get(), 0);
    // The ring in memory is not where the anchor is read: one there and
    // none on disk is none.
    let in_memory = KeyRing {
        origin_file: Some(anchor().into_bytes()),
        ..ring(Some(&ours))
    };
    let st = state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(&log, &in_memory, &st);
    assert!(matches!(got, Err(VerifyError::Source(_))));
    // An anchor that does not parse is none: the log decides, and its
    // failure leaves the open.
    for text in ["not json", "{}", "[]", r#"{"seq": "1", "mac": "x"}"#] {
        let anchored = Anchored::new(text);
        let st = anchored.state(KeyStatus::Foreign, &fp(&other), Some(&ours));
        let got = reader_keys(&log, &ring(Some(&ours)), &st);
        assert!(matches!(got, Err(VerifyError::Source(_))), "{text}");
    }
    assert!(log.reads.get() > 0);
}

#[test]
fn an_anchor_file_over_a_mebibyte_is_read_whole() {
    // `KeyRing.read_origin` reads the file whatever its size: an anchor
    // that parses keeps the key without reading the log, and one nested
    // past the JSON limit raises.
    let (ours, other) = (key(1), key(3));
    let log = Refusing::default();
    let padded = format!("{}{}", anchor(), " ".repeat(1 << 20));
    let big = Anchored::new(&padded);
    let st = big.state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(&log, &ring(Some(&ours)), &st).unwrap();
    assert_eq!(got.read_key(), Some(&ours));
    assert_eq!(log.reads.get(), 0);
    let nested = format!("{}{}", "[".repeat(600_000), "]".repeat(600_000));
    let big_deep = Anchored::new(&nested);
    let st = big_deep.state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(&log, &ring(Some(&ours)), &st);
    assert!(matches!(got, Err(VerifyError::Py(PyError::RecursionError))));
    assert_eq!(log.reads.get(), 0);
}

#[test]
fn no_key_in_hand_reads_nothing() {
    let log = Refusing::default();
    let deep = Anchored::new(&deep());
    for status in [
        KeyStatus::Missing,
        KeyStatus::Unreadable,
        KeyStatus::BoundElsewhere,
        KeyStatus::Unclaimed,
    ] {
        let st = deep.state(status, &fp(&key(3)), None);
        let got = reader_keys(&log, &ring(Some(&key(1))), &st).unwrap();
        assert!(got.read_key().is_none() && !got.can_verify() && !got.has_key());
    }
    assert_eq!(log.reads.get(), 0);
}

#[test]
fn a_present_key_binds_the_store_with_it() {
    let ours = key(1);
    let log = Refusing::default();
    let deep = Anchored::new(&deep());
    let st = deep.state(KeyStatus::Present, &fp(&ours), Some(&ours));
    let got = reader_keys(&log, &ring(Some(&ours)), &st).unwrap();
    assert!(got.has_key() && got.can_verify());
    assert_eq!(got.key(), Some(&ours));
    assert_eq!(got.verify_key(), Some(&ours));
    assert!(got.read_key().is_none());
    assert!(got.bound_elsewhere().is_none());
    assert_eq!(log.reads.get(), 0);
}

#[test]
fn a_withheld_ring_is_named_for_both_states() {
    let ours = key(1);
    let rows = vec![created(&ours)];
    let mut st = state(KeyStatus::BoundElsewhere, &fp(&ours), Some(&ours));
    st.bound_to = Some(PathBuf::from("/elsewhere/memory.sqlite"));
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    assert_eq!(
        got.bound_elsewhere(),
        Some(Path::new("/elsewhere/memory.sqlite"))
    );
    let st = state(KeyStatus::Unclaimed, &fp(&ours), Some(&ours));
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    let binding_file = keys_dir().join(format!("{STORE_ID}.path"));
    assert_eq!(got.bound_elsewhere(), Some(binding_file.as_path()));
    // A foreign or missing key withholds nothing.
    for status in [KeyStatus::Foreign, KeyStatus::Missing] {
        let st = state(status, &fp(&key(3)), Some(&ours));
        let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
        assert!(got.bound_elsewhere().is_none());
    }
}

#[test]
fn a_segment_named_in_text_that_is_not_text_is_no_fingerprint() {
    let (ours, other) = (key(1), key(3));
    let named = "\\ud800".to_owned() + &fp(&ours);
    let rows = vec![created(&other), rekey(2, &fp(&other), &named, &ours)];
    let st = state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    assert!(got.read_key().is_none());
    // A later segment under the key in hand's name still counts.
    let rows = vec![
        created(&other),
        rekey(2, &fp(&other), "\\ud800", &other),
        rekey(3, "\\ud800", &fp(&ours), &ours),
    ];
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st).unwrap();
    assert_eq!(got.read_key(), Some(&ours));
}

#[test]
fn the_log_s_failures_leave_the_open() {
    let (ours, other) = (key(1), key(3));
    let st = state(KeyStatus::Foreign, &fp(&other), Some(&ours));
    let got = reader_keys(&Refusing::default(), &ring(Some(&ours)), &st);
    assert!(matches!(got, Err(VerifyError::Source("first"))));
    let rows = vec![created(&other), row(2, REKEY, deep(), &other)];
    let got = reader_keys(rows.as_slice(), &ring(Some(&ours)), &st);
    assert!(matches!(got, Err(VerifyError::Py(PyError::RecursionError))));
}

#[test]
fn the_dispatch_is_the_reference_s() {
    use KeyStatus::*;
    let ours = key(1);
    for (status, read_only, rekeying) in [
        (Present, Binding::Present, Binding::Present),
        (Missing, Binding::Reader, Binding::Owner),
        (Foreign, Binding::Reader, Binding::Owner),
        (Unreadable, Binding::Reader, Binding::Owner),
        (BoundElsewhere, Binding::Reader, Binding::Reader),
        (Unclaimed, Binding::Reader, Binding::Reader),
    ] {
        let mut st = state(status, &fp(&ours), Some(&ours));
        st.bound_to = Some(PathBuf::from("/elsewhere/memory.sqlite"));
        assert_eq!(binding(&st, false), read_only, "{status:?}");
        assert_eq!(binding(&st, true), rekeying, "{status:?}");
    }
}
