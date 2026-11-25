//! A reader's open of a signed store against the reference's
//! `Store.open(read_only=True)`: each case's store and key ring are built
//! here as `gen_store_open_reader` builds them, opened by the built steps
//! (`StoreFile::open`, meta and `check_opened_as`, `key_state_at`, the ring
//! loaded after it) and then `reader_keys`. Which of the origin anchor and
//! the log the reader's step reads is asked of the step itself: once more
//! with the anchor's file nested past the JSON limit, and once with a log
//! that fails when read.
mod common;
mod key_read_support;
use bettermemory_core::log::{
    compute_mac, fingerprint, ring::KeyFiles, Key, LogRow, LogSource, VerifyError, REKEY,
};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::{
    create::{create, System},
    key_read::open_reader::{binding, reader_keys, Binding, ReaderKeys},
    key_read::ReaderRing,
    key_state::KeyState,
    keys::load_key_ring,
    StoreError, StoreFile,
};
use bettermemory_core::unicode::cps;
use common::{parity::Ledger, stores::scratch};
use key_read_support::Signed;
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};

const TS: &str = "2026-01-01T00:00:00+00:00";
const DEEP: usize = 100_000;
/// Nested past the JSON limit, in a file over 1 MiB.
const BIG_DEEP: usize = 600_000;
/// Spaces after an anchor that parses, making its file over 1 MiB.
const BIG_PAD: usize = 1 << 20;

/// A key of 32 bytes named by `label`.
fn key(label: &str) -> Key {
    Key::new(fingerprint(label.as_bytes()).as_bytes()[..32].to_vec())
}

fn deep_text() -> String {
    format!("{}{}", "[".repeat(DEEP), "]".repeat(DEEP))
}

/// An anchor that parses (a seq and a MAC), in a file over 1 MiB.
fn big_anchor() -> String {
    format!(
        r#"{{"seq": 1, "mac": "{}"}}{}"#,
        "0".repeat(64),
        " ".repeat(BIG_PAD)
    )
}

fn big_deep_text() -> String {
    format!("{}{}", "[".repeat(BIG_DEEP), "]".repeat(BIG_DEEP))
}

/// A path as the reference spells it: no verbatim prefix.
fn plain(path: &Path) -> PathBuf {
    let text = path.to_str().unwrap();
    PathBuf::from(text.strip_prefix(r"\\?\").unwrap_or(text))
}

struct Case {
    root: PathBuf,
    path: PathBuf,
    original: PathBuf,
    ring: KeyFiles,
    store_id: String,
}

impl Case {
    fn conn(&self) -> Connection {
        Connection::open(&self.path).unwrap()
    }
    fn set_meta(&self, value: &str) {
        self.conn()
            .execute(
                "UPDATE meta SET value = ?1 WHERE key = 'key_fingerprint'",
                [value],
            )
            .unwrap();
    }
    fn append(&self, signer: &Key, kind: &str, payload: &str) {
        let conn = self.conn();
        let (seq, prev): (i64, String) = conn
            .query_row(
                "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        let mac = compute_mac(signer.bytes(), seq + 1, TS, None, kind, payload, &prev);
        conn.execute(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) \
             VALUES (?1, ?2, NULL, ?3, ?4, ?5, ?6)",
            params![seq + 1, TS, kind, payload, prev, mac],
        )
        .unwrap();
    }
    fn rekey(&self, signer: &Key, old: &Key, new: &Key) {
        let payload = format!(
            r#"{{"new_fingerprint":"{}","old_fingerprint":"{}"}}"#,
            fingerprint(new.bytes()),
            fingerprint(old.bytes())
        );
        self.append(signer, REKEY, &payload);
    }
    fn current(&self) -> Key {
        self.ring.load_current().unwrap().unwrap()
    }
    fn origin(&self, text: Option<&str>) {
        match text {
            None => fs::remove_file(self.ring.origin_path()).unwrap(),
            Some(text) => fs::write(self.ring.origin_path(), text).unwrap(),
        }
    }
    fn edit_meta(&self, label: &str) {
        self.set_meta(&fingerprint(key(label).bytes()));
    }
    fn bad_first_row(&self) {
        self.conn()
            .execute(
                "UPDATE log SET ts = CAST(X'FF' AS TEXT) WHERE seq = \
                 (SELECT MIN(seq) FROM log WHERE seq > 0)",
                [],
            )
            .unwrap();
    }
    fn new_ring_key(&self, label: &str) {
        fs::write(self.ring.current_path(), key(label).bytes()).unwrap();
    }
    /// A copy of the store; the ring stays bound to the original.
    fn copy(&mut self) {
        let copy = self.root.join("copy").join("memory.sqlite");
        fs::create_dir_all(copy.parent().unwrap()).unwrap();
        self.conn()
            .execute("VACUUM INTO ?1", [copy.to_str().unwrap()])
            .unwrap();
        self.path = copy;
    }
    fn drop_tail(&self) {
        self.conn()
            .execute("DELETE FROM log WHERE seq = (SELECT MAX(seq) FROM log)", [])
            .unwrap();
    }
}

/// A store this machine created, with one event after its first row.
fn own(root: &Path) -> Case {
    let signed = Signed::new(root);
    Case {
        root: root.to_path_buf(),
        path: signed.path.clone(),
        original: signed.path,
        store_id: signed.ring.store_id().to_owned(),
        ring: signed.ring,
    }
}

/// A store another machine created; this machine's ring holds only a key
/// of its own, bound to no file. Returns the case, the other machine's
/// key and this machine's key.
fn theirs(root: &Path) -> (Case, Key, Key) {
    let path = root.join("memory.sqlite");
    let mut clock = System {
        engine_version: "fixture".into(),
    };
    let created = create(&path, &root.join("theirs"), &mut clock).unwrap();
    let their_ring = KeyFiles::new(&root.join("theirs"), &PyValue::str(&created.store_id)).unwrap();
    let their_key = their_ring.load_current().unwrap().unwrap();
    let keys = root.join("keys");
    fs::create_dir_all(&keys).unwrap();
    let ring = KeyFiles::new(&keys, &PyValue::str(&created.store_id)).unwrap();
    let ours = key(&format!("ours {}", root.display()));
    fs::write(ring.current_path(), ours.bytes()).unwrap();
    let case = Case {
        root: root.to_path_buf(),
        original: path.clone(),
        path,
        ring,
        store_id: created.store_id,
    };
    (case, their_key, ours)
}

fn build(name: &str, root: &Path) -> Case {
    let deep = deep_text();
    match name {
        "present" => own(root),
        "present-deep-anchor" => {
            let case = own(root);
            case.origin(Some(&deep));
            case
        }
        "missing" | "missing-deep-anchor" => {
            let case = own(root);
            fs::remove_file(case.ring.current_path()).unwrap();
            if name == "missing-deep-anchor" {
                case.origin(Some(&deep));
            }
            case
        }
        "unreadable" => {
            let case = own(root);
            fs::remove_file(case.ring.current_path()).unwrap();
            fs::create_dir(case.ring.current_path()).unwrap();
            case
        }
        "foreign-anchored" | "foreign-anchored-bad-log" => {
            let case = own(root);
            case.edit_meta("editor");
            if name == "foreign-anchored-bad-log" {
                case.bad_first_row();
            }
            case
        }
        "foreign-uppercase-meta" => {
            let case = own(root);
            case.set_meta(&fingerprint(case.current().bytes()).to_uppercase());
            case
        }
        "foreign-edited"
        | "anchor-not-json"
        | "anchor-not-an-anchor"
        | "first-row-unreadable"
        | "anchor-deep-foreign" => {
            let case = own(root);
            case.edit_meta("editor");
            case.origin(None);
            match name {
                "anchor-not-json" => case.origin(Some("not json")),
                "anchor-not-an-anchor" => case.origin(Some("{}")),
                "first-row-unreadable" => case.bad_first_row(),
                "anchor-deep-foreign" => case.origin(Some(&deep)),
                _ => {}
            }
            case
        }
        "foreign-leading" => {
            let case = own(root);
            let ours = case.current();
            let other = key("other");
            case.rekey(&other, &ours, &other);
            case.set_meta(&fingerprint(other.bytes()));
            case.origin(None);
            case
        }
        "foreign-later" => {
            let (case, their, ours) = theirs(root);
            let last = key("last");
            case.rekey(&ours, &their, &ours);
            case.rekey(&last, &ours, &last);
            case.set_meta(&fingerprint(last.bytes()));
            case
        }
        "foreign-named-unsigned" => {
            let (case, their, ours) = theirs(root);
            case.rekey(&their, &their, &ours);
            case
        }
        "foreign-signs-nothing" => theirs(root).0,
        "foreign-retired-meta" => {
            // Meta names a retired key the ring holds; the first row is
            // signed by the ring's current key, the key in hand.
            let case = own(root);
            let retired = key("retired");
            let named = fingerprint(retired.bytes());
            fs::write(case.ring.retired_path(&named), retired.bytes()).unwrap();
            case.set_meta(&named);
            case.origin(None);
            case
        }
        "foreign-big-anchor" | "foreign-big-deep-anchor" => {
            let case = theirs(root).0;
            case.ring.rebind_store(&case.path, b"/").unwrap();
            match name {
                "foreign-big-anchor" => case.origin(Some(&big_anchor())),
                _ => case.origin(Some(&big_deep_text())),
            }
            case
        }
        "anchor-not-json-signs-nothing" => {
            let case = theirs(root).0;
            fs::write(case.ring.origin_path(), "not json").unwrap();
            case
        }
        "rekey-deep-payload" => {
            let (case, their, _) = theirs(root);
            case.append(&their, REKEY, &deep);
            case
        }
        "bound-elsewhere-match"
        | "bound-elsewhere-match-deep-anchor"
        | "bound-elsewhere-foreign-anchored"
        | "bound-elsewhere-foreign"
        | "bound-elsewhere-missing" => {
            let mut case = own(root);
            case.copy();
            match name {
                "bound-elsewhere-match-deep-anchor" => case.origin(Some(&deep)),
                "bound-elsewhere-foreign-anchored" => case.edit_meta("editor"),
                "bound-elsewhere-foreign" => {
                    case.new_ring_key("replaced");
                    case.origin(None);
                }
                "bound-elsewhere-missing" => fs::remove_file(case.ring.current_path()).unwrap(),
                _ => {}
            }
            case
        }
        "unclaimed-match" | "unclaimed-foreign" => {
            let mut case = own(root);
            case.copy();
            case.drop_tail();
            fs::remove_file(case.ring.binding_path()).unwrap();
            if name == "unclaimed-foreign" {
                case.new_ring_key("replaced");
                case.origin(None);
            }
            case
        }
        other => panic!("no case {other}"),
    }
}

/// A log that fails whenever it is read.
struct Unread;
impl LogSource for Unread {
    type Error = ();
    fn first(&self) -> Result<Option<LogRow>, ()> {
        Err(())
    }
    fn rekeys(&self) -> Result<Vec<LogRow>, ()> {
        Err(())
    }
    fn at_seq(&self, _: i64) -> Result<Option<LogRow>, ()> {
        Err(())
    }
    fn each(
        &self,
        _: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, ()> {
        Err(())
    }
}

fn class(error: &VerifyError<StoreError>) -> &'static str {
    match error {
        VerifyError::Py(e) => e.name(),
        VerifyError::Source(StoreError::NotUtf8 { .. }) => "OperationalError",
        VerifyError::Source(e) => panic!("unexpected source failure {e:?}"),
    }
}

/// The built steps of a read-only open, then the reader's keys.
fn open(case: &Case) -> Result<(StoreFile, KeyState, ReaderKeys), String> {
    let store = StoreFile::open(&case.path).map_err(|e| format!("{e:?}"))?;
    let meta = store.meta().map_err(|e| format!("{e:?}"))?;
    let shown = cps(case.path.to_str().unwrap());
    let checked = store
        .check_opened_as(&meta, &shown, "9.0.0")
        .map_err(|e| format!("{e:?}"))?;
    let keys_dir = case.ring.keys_dir().to_path_buf();
    let reader = ReaderRing::new(
        &cps(keys_dir.to_str().unwrap()),
        &PyValue::str(&checked.opened.store_id),
    )
    .unwrap();
    let physical = reader
        .key_state_at(
            store.connection(),
            &case.path,
            &shown,
            &cps(&checked.opened.key_fingerprint),
            true,
            |_| Ok::<_, ()>(()),
        )
        .map_err(|e| format!("{e:?}"))?;
    let state = physical.state;
    assert_ne!(binding(&state, false), Binding::Owner);
    let ring = load_key_ring(&keys_dir, &case.store_id).unwrap();
    let keys = reader_keys(&store, &ring, &state).map_err(|e| class(&e).to_owned())?;
    Ok((store, state, keys))
}

fn withheld(case: &Case, path: Option<&Path>) -> Value {
    match path {
        None => Value::Null,
        Some(p) if plain(p) == plain(&case.original) => json!("original"),
        Some(p) if plain(p) == plain(&case.ring.binding_path()) => json!("binding"),
        Some(_) => json!("other"),
    }
}

fn observe(case: &Case) -> Value {
    let (store, state, keys) = match open(case) {
        Ok(opened) => opened,
        Err(raised) => return json!({ "raised": raised }),
    };
    let ring = load_key_ring(case.ring.keys_dir(), &case.store_id).unwrap();
    // The anchor's file nested past the JSON limit for one more call, then
    // put back as it was.
    let origin_path = case.ring.origin_path();
    let kept = fs::read(&origin_path).ok();
    fs::write(&origin_path, deep_text()).unwrap();
    let probed = reader_keys(&store, &ring, &state);
    match &kept {
        Some(bytes) => fs::write(&origin_path, bytes).unwrap(),
        None => fs::remove_file(&origin_path).unwrap(),
    }
    let origin_read = match probed {
        Err(VerifyError::Py(PyError::RecursionError)) => true,
        Ok(_) => false,
        Err(e) => panic!("{}", class(&e)),
    };
    let log_read = reader_keys(&Unread, &ring, &state).is_err();
    let current = case.ring.load_current().ok().flatten();
    json!({
        "state": state.state.name(),
        "has_key": keys.has_key(),
        "can_verify": keys.can_verify(),
        "read_key": keys.read_key().is_some(),
        "read_key_current": keys.read_key().is_some() && keys.read_key() == current.as_ref(),
        "bound_elsewhere": withheld(case, keys.bound_elsewhere()),
        "origin_read": origin_read,
        "log_read": log_read,
    })
}

fn compare(test: &str, file: &str) {
    let root = scratch(test);
    let rows = common::jsonl(file);
    let mut ledger = Ledger::new(test, file);
    for row in &rows {
        let name = row["case"].as_str().unwrap();
        let dir = root.0.join(name);
        fs::create_dir_all(&dir).unwrap();
        let case = build(name, &dir);
        ledger.case(name, || assert_eq!(observe(&case), row["result"], "{name}"));
    }
    ledger.finish(rows.len());
}

#[test]
fn opens_readers_as_the_reference_does() {
    compare(
        "opens_readers_as_the_reference_does",
        "store_open_reader.jsonl",
    );
}

#[test]
fn opens_readers_past_the_json_limit_as_3_13_does() {
    compare(
        "opens_readers_past_the_json_limit_as_3_13_does",
        "store_open_reader_python.jsonl",
    );
}
