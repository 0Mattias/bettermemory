//! Another connection to a store open for writing: `Store.reopen`
//! (`store.py` 1965-2002), with the open it makes, `Store.open(path,
//! keys_dir=keys_dir, allow_rekey=False)` on a writing connection
//! (`_connect`, `store_open._open_connected` with ``allow_rekey`` False,
//! `_open_reader`), wrapped as the reference's is: `reconciles_tokenizer`
//! with ``owner`` False (a drift said once a process, `_tell_drift`) and
//! `keeps_digest`, which does nothing for it.
//!
//! Such an open claims nothing and repairs nothing: no binding written
//! (`key_state(claim=False)` reads the binding, `_key_ring_elsewhere`), no
//! `_record_engine`, no foreign tail closed, no anchor, no restore row, no
//! digest. A present key binds the store with it; any other state opens a
//! reader on this writing connection, which keeps a key for checking only
//! where `_open_reader` keeps one. A later minimum writer holds the
//! connection to reading (`PRAGMA query_only`) and says why once a
//! process.
//!
//! The owner's `_bound_elsewhere` branch of `Store.reopen` is unreachable
//! here: the owner's native open refuses a ring bound elsewhere
//! (`NotPorted::BoundElsewhere`), so a [`WritableStore`] never is. The
//! unaccounted-id set the reference shares is the caller's
//! (`store::provenance::Provenance`, handed to both connections).

use std::collections::BTreeSet;
use std::convert::Infallible;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, PoisonError};

use rusqlite::{Connection, OptionalExtension};

use super::{
    admitted, checks, connect_pragmas, not_ported, stored_tokenizer, upkeep, DigestOutcome,
    NotPorted, OpenDiagnostics, OpenError, WritableStore,
};
use crate::fsutil::fsdecode;
use crate::lexicon;
use crate::log::ring::KeyFiles;
use crate::log::{fingerprint, VerifyError};
use crate::pyvalue::{PyStr, PyValue};
use crate::store::create::Provider;
use crate::store::key_read::open_reader::{binding, reader_keys, Binding, ReaderKeys};
use crate::store::key_read::{key_state_on, KeyLog, LogLevel, ReadError};
use crate::store::key_state::{KeyStatus, StateError};
use crate::store::provenance::ConnSource;
use crate::store::{keys, reads, restore_lock, BUSY_TIMEOUT};
use crate::unicode::cps;

/// `store_index._tell_drift`'s record for an open that does not re-spell
/// the index: the store, the tokenizer its log names, this build's, why.
pub const TOKENIZER_DRIFT: &str = "store %s: its search index is spelled by tokenizer %s and this \
build's is %s; this open does not re-spell it (%s), so a search may miss memories until the \
owner's next writing open does (the daemon's start, any writing command, or `bettermemory index \
rebuild`)";

/// `store_versions._READ_ONLY_TOLD`: the (path, minimum writer) pairs a
/// later writer's warning was said for in this process.
static READ_ONLY_TOLD: Mutex<BTreeSet<(PyStr, i64)>> = Mutex::new(BTreeSet::new());
/// `store_index._DRIFT_TOLD`: the (path, stored, live) a drift was said
/// for in this process.
static DRIFT_TOLD: Mutex<BTreeSet<[PyStr; 3]>> = Mutex::new(BTreeSet::new());

/// Whether `told` is new to `set`, which then holds it (`_first_told`).
fn first_told<T: Ord>(set: &Mutex<BTreeSet<T>>, told: T) -> bool {
    set.lock()
        .unwrap_or_else(PoisonError::into_inner)
        .insert(told)
}

fn warning(format: &'static str, args: Vec<PyStr>) -> KeyLog {
    KeyLog {
        level: LogLevel::Warning,
        format,
        args,
        cause: None,
    }
}

/// What a reopen opened: the store bound with its key, or a reader.
#[derive(Debug)]
pub enum Reopened {
    /// `KEY_PRESENT`: the key meta names, bound for writing.
    Writer(WritableStore),
    /// Any other key state: `_open_reader` on the writing connection.
    Reader(ReopenedReader),
}

/// A store a reopen bound with no key to write with (`Store._key` None):
/// the connection, and the keys `_open_reader` kept. It has no writes:
/// the reference's refuse for want of the key.
#[derive(Debug)]
pub struct ReopenedReader {
    conn: Connection,
    path: PathBuf,
    keys_dir: PathBuf,
    store_id: String,
    busy_timeout_seconds: f64,
    keys: ReaderKeys,
    state: KeyStatus,
}

impl ReopenedReader {
    pub fn connection(&self) -> &Connection {
        &self.conn
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn keys_dir(&self) -> &Path {
        &self.keys_dir
    }
    pub fn store_id(&self) -> &str {
        &self.store_id
    }
    /// `_read_key` and `_bound_elsewhere` (`has_key` is false).
    pub fn keys(&self) -> &ReaderKeys {
        &self.keys
    }
    /// The key state the open read (`Store.key_state`).
    pub fn key_state(&self) -> KeyStatus {
        self.state
    }
    pub fn close(self) -> Result<(), rusqlite::Error> {
        self.conn.close().map_err(|(_, e)| e)
    }
}

impl Reopened {
    pub fn connection(&self) -> &Connection {
        match self {
            Reopened::Writer(s) => s.connection(),
            Reopened::Reader(r) => &r.conn,
        }
    }
    /// `Store.has_key`.
    pub fn has_key(&self) -> bool {
        matches!(self, Reopened::Writer(_))
    }
    /// `Store._verify_key`: the key a writer holds, a reader's read key.
    pub fn verify_key(&self) -> Option<&crate::log::Key> {
        match self {
            Reopened::Writer(s) => Some(&s.key),
            Reopened::Reader(r) => r.keys.read_key(),
        }
    }
    /// `Store.can_verify`.
    pub fn can_verify(&self) -> bool {
        self.verify_key().is_some()
    }
    /// `Store.busy_timeout`, in seconds.
    pub fn busy_timeout(&self) -> f64 {
        match self {
            Reopened::Writer(s) => s.busy_timeout_seconds,
            Reopened::Reader(r) => r.busy_timeout_seconds,
        }
    }
    fn set_busy_timeout(&mut self, seconds: f64) -> Result<(), OpenError> {
        reads::set_busy_timeout(self.connection(), seconds).map_err(|e| match e {
            reads::ReadError::Worded(class, text) => OpenError::Raised {
                class,
                message: cps(&text),
            },
            reads::ReadError::Sqlite(e) => OpenError::Sqlite(e),
            // `set_busy_timeout` raises only its two worded errors and
            // SQLite's: a `Raised` is not one of them.
            other => {
                debug_assert!(false, "set_busy_timeout raised {other:?}");
                OpenError::Raised {
                    class: other.class(),
                    message: cps(&format!(
                        "set_busy_timeout raised {}, which it never raises",
                        other.class()
                    )),
                }
            }
        })?;
        match self {
            Reopened::Writer(s) => s.busy_timeout_seconds = seconds,
            Reopened::Reader(r) => r.busy_timeout_seconds = seconds,
        }
        Ok(())
    }
    /// `Store.close`.
    pub fn close(self) -> Result<(), rusqlite::Error> {
        match self {
            Reopened::Writer(s) => s.close(),
            Reopened::Reader(r) => r.close(),
        }
    }
}

impl WritableStore {
    /// `Store.reopen(busy_timeout=busy_timeout)`: another connection to
    /// this store's file under the same keys directory, never a rekey; its
    /// busy timeout `busy_timeout` (None: this connection's). `provider`
    /// gives the engine version the version checks name. What the open
    /// logs is pushed onto `logged` in the reference's order (a later
    /// writer, the key's state, a tokenizer's drift), kept when it fails.
    /// A busy timeout that does not set fails it, the new connection
    /// closed.
    pub fn reopen(
        &self,
        provider: &dyn Provider,
        busy_timeout: Option<f64>,
        logged: &mut Vec<KeyLog>,
    ) -> Result<Reopened, OpenError> {
        let wait = busy_timeout.unwrap_or(self.busy_timeout_seconds);
        let mut other = open_not_owning(&self.path, &self.keys_dir, provider, logged)?;
        other.set_busy_timeout(wait)?;
        Ok(other)
    }
}

/// `Store.open(file, keys_dir=keys_dir, allow_rekey=False)` with its two
/// wrappers.
fn open_not_owning(
    file: &Path,
    keys_dir: &Path,
    provider: &dyn Provider,
    logged: &mut Vec<KeyLog>,
) -> Result<Reopened, OpenError> {
    if !file.is_file() {
        return Err(OpenError::NotFound(file.to_path_buf()));
    }
    // `_connect`, in its order: admitted, the pragmas and modes, then the
    // restore lock's `confirm`.
    let held = restore_lock::admit(
        file,
        restore_lock::OPEN_WAIT,
        restore_lock::this_thread_holds,
    )?;
    let (conn, deadline) = admitted(file, false)?;
    connect_pragmas(&conn, file)?;
    held.confirm()?;
    drop(held);
    let version = provider.engine_version();
    let checked = checks(
        &conn,
        &deadline,
        file,
        &version,
        false,
        &mut OpenDiagnostics::default(),
    )?;
    let shown = fsdecode(file.as_os_str());
    if let Some((reason, min_writer)) = checked.read_only {
        conn.execute_batch("PRAGMA query_only = ON")?;
        if first_told(&READ_ONLY_TOLD, (shown.clone(), min_writer)) {
            logged.push(warning("%s", vec![reason]));
        }
    }
    // `key_state(claim=False)`: the rotation finished, the key loaded, the
    // binding read and never written.
    let files = KeyFiles::new(keys_dir, &PyValue::str(&checked.store_id))
        .map_err(|_| not_ported(NotPorted::Unreadable("the key ring")))?;
    let read = key_state_on(&conn, &files, file, &cps(&checked.expected), true, |l| {
        logged.push(l);
        Ok::<(), Infallible>(())
    });
    let state = read.map_err(|e| match e {
        ReadError::Path(e) => OpenError::Raised {
            class: e.class(),
            message: e.message(),
        },
        ReadError::State(StateError::Raised { class, message }) => {
            OpenError::Raised { class, message }
        }
        ReadError::State(StateError::Read(e)) => e.into(),
        ReadError::Logger(never) => match never {},
    })?;
    let store = match binding(&state.state, false) {
        Binding::Present => {
            let key = state.state.key.clone().expect("a present key");
            let head = conn
                .query_row(
                    "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1",
                    [],
                    |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
                )
                .optional()
                .unwrap_or(None);
            Reopened::Writer(WritableStore {
                conn,
                busy_timeout_seconds: BUSY_TIMEOUT.as_secs_f64(),
                path: file.to_path_buf(),
                keys_dir: keys_dir.to_path_buf(),
                store_id: checked.store_id,
                key_fingerprint: fingerprint(key.bytes()),
                key,
                head,
                digest: DigestOutcome::Untouched,
                digest_reason: None,
                version_diagnostic: None,
                bound: false,
                upkeep: Vec::new(),
            })
        }
        // `_open_reader`; the owner's path is not an open that may not
        // rekey.
        Binding::Reader | Binding::Owner => {
            let ring = keys::load_key_ring(keys_dir, &checked.store_id)
                .map_err(|_| not_ported(NotPorted::Unreadable("the key ring")))?;
            // What `_open_reader` lets through: RecursionError out of
            // `keyring.read_origin()` (the anchor) or out of
            // `chain_segments` (a rekey payload), worded as `json.loads`
            // words it for the text that raised, as the owner's
            // dispatch words it (`rekey::decide`).
            let keys =
                reader_keys(&ConnSource(&conn), &ring, &state.state).map_err(|e| match e {
                    VerifyError::Py(_) => match files.read_origin() {
                        Err(_) => upkeep::anchor_recursion(keys_dir, &checked.store_id),
                        Ok(_) => upkeep::segments_recursion(&conn),
                    },
                    VerifyError::Source(e) => e.into(),
                })?;
            Reopened::Reader(ReopenedReader {
                conn,
                path: file.to_path_buf(),
                keys_dir: keys_dir.to_path_buf(),
                store_id: checked.store_id,
                busy_timeout_seconds: BUSY_TIMEOUT.as_secs_f64(),
                keys,
                state: state.state.state,
            })
        }
    };
    // `reconciles_tokenizer` with ``owner`` False: `settle_tokenizer` says
    // a drift (`_tell_drift`) and re-spells nothing.
    // What the read raises closes the new connection and fails the reopen.
    let stored = stored_tokenizer(store.connection())?;
    let live = cps(&lexicon::python_tokenizer_fingerprint());
    if stored.as_deref() != Some(live.as_slice()) {
        let query_only: i64 = store
            .connection()
            .query_row("PRAGMA query_only", [], |r| r.get(0))?;
        let why = if !store.has_key() {
            "it was opened without the store's key"
        } else if query_only != 0 {
            "this build may only read the store"
        } else {
            "not the owner's writing open"
        };
        let stored = stored.unwrap_or_default();
        if first_told(&DRIFT_TOLD, [shown.clone(), stored.clone(), live.clone()]) {
            let named = if stored.is_empty() {
                cps("none")
            } else {
                stored
            };
            let lead = |t: &[u32]| t[..t.len().min(16)].to_vec();
            logged.push(warning(
                TOKENIZER_DRIFT,
                vec![shown, lead(&named), lead(&live), cps(why)],
            ));
        }
    }
    // `keeps_digest`: nothing without ``allow_rekey``.
    Ok(store)
}
