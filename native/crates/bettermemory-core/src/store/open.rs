//! The owner's writing open of an existing store: `Store.open(path,
//! keys_dir=keys_dir, allow_rekey=True)`, the open `open_or_create` makes
//! for the hooks, the daemon and the CLI, with its wrappers
//! (`store_index.reconciles_tokenizer`, `store_digest.keeps_digest`), as
//! far as the plain states go.
//!
//! The reference's order (`store.py` `Store.open`, `_connect`;
//! `store_open._open_connected`) and what each step writes is in the
//! crate's README, "Opening a store for writing". This port reads
//! everything the open decides by first, on a connection that has written
//! nothing, and refuses ([`OpenError::NotPorted`]) every state for which
//! the reference would write something this unit does not port: then
//! nothing at all is written. Only then does it write, in the reference's
//! order: `_connect`'s pragmas and modes, `engine_version`
//! (`_record_engine`), the key ring's binding (`_claim_key_ring`), the
//! upkeep with the key present (`upkeep`: `_close_foreign_tail`, then
//! `_settle_origin`), a restore's late rows (`restore_rows.settle`, by
//! [`open_for_writing_mut`]), and the digest brought current
//! (`digest_mark`: the record mutations after its mark applied, or the
//! digest built from the records).
//!
//! [`WritableStore`] holds the connection, the key and what the open
//! established; `WritableStore::reopen` (`open/reopen.rs`) opens another
//! connection to the same file as `Store.reopen` does.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use rusqlite::config::DbConfig;
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use zeroize::Zeroizing;

use super::admission::{self, AdmitError};
use super::create::{
    self, new_private_file, owner_only_dir, parent_of, store_path, sweep_temps, sync_dir,
    with_suffix, Provider, PAGE_SIZE,
};
use super::digest::{self, DigestError};
use super::restore_lock;
use super::restore_rows::{self, RestoreError};
use super::schema;
use super::versions::{self, DiagnosticError, VersionDiagnostic};
use super::write::WriteError;
use super::{keys, log_row, meta_get, sql_value, SqlValue, StoreError};
use super::{StoreVersionError, BUSY_TIMEOUT, LOG_SELECT, SCHEMA_REVISION};
use crate::json;
use crate::lexicon;
use crate::log::ring::binding_path::{self, BindingError, BindingPath};
use crate::log::ring::KeyFiles;
use crate::log::{self, fingerprint, HistoryOrigin, Key, KeyRing, LogRow};
use crate::pyvalue::{self, PyError, PyStr, PyValue};
use crate::unicode;

mod rekey;
mod reopen;
mod upkeep;

pub use reopen::{Reopened, ReopenedReader, TOKENIZER_DRIFT};
pub use upkeep::{Arg as UpkeepArg, Cause as UpkeepCause, Warning as UpkeepWarning};

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A state the reference's writing open would write something for that
/// this unit does not port. The open refuses it with nothing written,
/// save what any connection that can write does to a store's sidecars at
/// its first read and close: a hot rollback journal rolled back (and
/// removed), a stale `-shm` beside a `-wal` rebuilt (or a missing one
/// made), an empty `-wal` and its `-shm` removed. A `-wal` with frames is
/// left in place, not checkpointed. A ring bound elsewhere found by the
/// binding's locked re-read is the exception: it refuses after `_connect`'s
/// writes and `engine_version`, as the reference's open turns into a
/// reader's there (the README's "What a refusal can change").
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NotPorted {
    /// meta's `min_writer_revision` is above this build's: the reference
    /// opens for reading only (`PRAGMA query_only`, no key ring claim).
    LaterWriter(String),
    /// Schema steps above the store's revision (`store_steps.run_steps`).
    SchemaSteps { revision: i64 },
    /// `_add_membership_tables` has something to add (an object, the
    /// membership store id, an optional column).
    MembershipStep(Vec<String>),
    /// A `key rotate` left `<id>.key.pending` (`finish_rotation`).
    RotationPending,
    /// The key ring is bound to no file, records a history, and this file
    /// does not carry it (`KEY_UNCLAIMED`): the reference opens a reader.
    Unclaimed,
    /// The binding names another file that still holds this store id, or
    /// that cannot be read to tell (`_store_id_at` raised: `unread` says
    /// why): the reference opens this file as a reader's
    /// (`_claim_key_ring`'s ``elsewhere``). Found when the open decides, or
    /// by the binding's locked re-read just before it would write one (a
    /// second opener bound the ring meanwhile).
    BoundElsewhere {
        keys_dir: PathBuf,
        store_id: String,
        other: PathBuf,
        /// Present when the original Python spelling differs from the OS projection.
        original_spelling: Option<Box<BindingPath>>,
        unread: Option<String>,
    },
    /// No key file: the reference retires nothing, writes a new key and
    /// appends a `rekey` row (`_open_rekeyed`). Refused by the entries that
    /// share the provider (they draw no key); performed by
    /// [`open_for_writing_mut`].
    KeyMissing,
    /// A key whose fingerprint is not meta's: `_reclaim` or `_open_rekeyed`.
    /// Refused by the entries that share the provider; performed by
    /// [`open_for_writing_mut`].
    KeyForeign,
    /// The last `rekey` row names another key (`_close_foreign_tail`
    /// appends a `rekey` row, stamped by the log's clock): refused by the
    /// entries that share the provider, closed by [`open_for_writing_mut`].
    ForeignTail,
    /// The restore record holds a `store_restored` entry for this store
    /// whose row is null (`restore_rows.pending`): `restore_rows.settle`
    /// appends its row, stamped by the log's clock. Refused by the entries
    /// that share the provider; settled by [`open_for_writing_mut`].
    RestoreRecord,
    /// The index is spelled by another tokenizer (`settle_tokenizer`
    /// re-spells it and appends `index_rebuilt`).
    TokenizerDrift { stored: Option<String> },
    /// A file the decision reads that does not read as the reference reads
    /// it (a head file with a seq past an int, a log row that is not one).
    Unreadable(&'static str),
}

impl NotPorted {
    /// What is missing, in words.
    pub fn what(&self) -> String {
        match self {
            NotPorted::LaterWriter(reason) => format!("a reader's open ({reason})"),
            NotPorted::SchemaSteps { revision } => {
                format!("the schema steps above revision {revision}")
            }
            NotPorted::MembershipStep(wanted) => {
                format!("the membership step ({})", wanted.join(", "))
            }
            NotPorted::RotationPending => "finishing a key rotation".to_owned(),
            NotPorted::Unclaimed => "a key ring bound to no file whose history this file \
                                     does not carry (a reader's open)"
                .to_owned(),
            NotPorted::BoundElsewhere {
                keys_dir,
                store_id,
                other,
                original_spelling,
                unread,
            } => {
                // `_bound_words`, the summary and the remedy.
                let holds = match unread {
                    Some(why) => format!(
                        "which could not be read to tell whether it still holds store id \
                         {store_id} ({why})"
                    ),
                    None => format!("which still holds store id {store_id}"),
                };
                format!(
                    "a reader's open: the key ring under {} is bound elsewhere: it belongs to \
                     the store at {}, {holds}. Move or remove one of the two stores, or run \
                     `bettermemory key adopt --yes` with BETTERMEMORY_DIR naming this file's \
                     directory, which binds the key ring to this one on the record (it \
                     rewrites {})",
                    keys_dir.display(),
                    crate::native_text::utf8_text(&original_spelling.as_ref().map_or_else(
                        || crate::fsutil::fsdecode(other.as_os_str()),
                        |p| p.text().to_vec()
                    )),
                    keys_dir.join(format!("{store_id}.path")).display()
                )
            }
            NotPorted::KeyMissing => "a rekey for a missing key".to_owned(),
            NotPorted::KeyForeign => "a reclaim or rekey for a foreign key".to_owned(),
            NotPorted::ForeignTail => "closing a foreign tail".to_owned(),
            NotPorted::RestoreRecord => "settling a restore's rows".to_owned(),
            NotPorted::TokenizerDrift { .. } => "re-spelling the index".to_owned(),
            NotPorted::Unreadable(what) => format!("reading {what}"),
        }
    }

    /// What the user can do about it, in words. Every state but a ring
    /// bound elsewhere is one the reference's own writing open settles and
    /// this build then opens; a ring bound elsewhere stays so for both, and
    /// its remedy is the reference's (in [`NotPorted::what`]).
    pub fn remedy(&self) -> &'static str {
        match self {
            NotPorted::BoundElsewhere { .. } => "",
            NotPorted::LaterWriter(_) => {
                " Open it with a build at least as new as the one that last wrote it."
            }
            _ => {
                " Open it once with the Python build (`bettermemory`), whose writing open \
                 settles this state; this build then opens it."
            }
        }
    }

    /// A short stable name, as the fixture names the state.
    pub fn name(&self) -> &'static str {
        match self {
            NotPorted::LaterWriter(_) => "later_writer",
            NotPorted::SchemaSteps { .. } => "schema_steps",
            NotPorted::MembershipStep(_) => "membership_step",
            NotPorted::RotationPending => "rotation_pending",
            NotPorted::Unclaimed => "unclaimed",
            NotPorted::BoundElsewhere { .. } => "bound_elsewhere",
            NotPorted::KeyMissing => "key_missing",
            NotPorted::KeyForeign => "key_foreign",
            NotPorted::ForeignTail => "foreign_tail",
            NotPorted::RestoreRecord => "restore_record",
            NotPorted::TokenizerDrift { .. } => "tokenizer_drift",
            NotPorted::Unreadable(_) => "unreadable",
        }
    }
}

/// Why the writing open refused or failed.
#[derive(Debug)]
pub enum OpenError {
    /// `FileNotFoundError("no store at <path>")`.
    NotFound(PathBuf),
    /// `RestoreRunningError`.
    RestoreRunning,
    /// `RestoreLockedError` (`restore_lock.admit`, `confirm`): its text.
    RestoreLocked(String),
    /// RecursionError out of reading a restore's marker nested past the
    /// decoder's depth (`restore_lock.admit` lets it through).
    MarkerRecursion,
    /// Exception raised by the JSON decoder reading the ring binding.
    BindingRecursion(&'static str),
    /// `StoreRefusedError` / `LaterStoreError` (`admit_store`).
    Admission(admission::Refusal),
    /// `ValueError` from `_open_connected`'s opening checks: the message.
    Refused(String),
    /// `StoreVersionError`.
    Version(StoreVersionError),
    /// `KeyUnreadableError`: the key file is there and cannot be read.
    KeyUnreadable { path: PathBuf, error: io::Error },
    /// An error SQLite raised where the reference's open raises it.
    Sqlite(rusqlite::Error),
    /// An OS error where the reference raises one: the step and the error.
    Io {
        step: &'static str,
        error: io::Error,
    },
    /// A state this unit does not port; nothing was written.
    NotPorted(NotPorted),
    /// `OverflowError` out of `keeps_digest`: `digest_state.seq` past an
    /// SQLite INTEGER. Raised after `_connect`'s writes, the engine's and
    /// the binding, as the reference raises it.
    Overflow,
    /// RecursionError out of the upkeep: a rekey payload or the origin
    /// anchor nested past the JSON depth limit (`last_rekey_name`,
    /// `chain_segments`, `read_origin`, which no `except` there catches),
    /// after the binding; `json.loads`'s text for the text that raised, and
    /// what the upkeep logged before it (a closed tail).
    Recursion {
        text: &'static str,
        logged: Vec<UpkeepWarning>,
    },
    /// What the foreign tail's transaction raised that
    /// `_close_foreign_tail` does not catch (neither `sqlite3.Error` nor
    /// `OSError`: a writer check, a callback's exception).
    Write(Box<WriteError>),
    /// `sqlite3`'s OperationalError for a log row the open reads whose text
    /// is not UTF-8 (`chain_segments` in the dispatch for a missing or
    /// foreign key, which nothing catches), in the module's words.
    Decode(String),
    /// An exception of the reference's that no other variant names: its
    /// class and text (a key state or log read `_open_reader` lets
    /// through, `set_busy_timeout`'s ValueError, what `restore_rows.settle`
    /// does not catch).
    Raised { class: &'static str, message: PyStr },
    /// Any other error, raised after the upkeep logged `logged` (a tail
    /// closed, a store reclaimed or rekeyed): `error` is the error itself.
    Logged {
        error: Box<OpenError>,
        logged: Vec<UpkeepWarning>,
    },
}

impl OpenError {
    /// What the upkeep logged before the open raised (`Recursion`).
    pub fn upkeep(&self) -> &[UpkeepWarning] {
        match self {
            OpenError::Recursion { logged, .. } | OpenError::Logged { logged, .. } => logged,
            _ => &[],
        }
    }

    /// The error itself, seen through [`OpenError::Logged`]: a caller that
    /// matches a variant (a `NotPorted` to fall back on, say) matches this,
    /// since an error raised after the upkeep logged something comes back
    /// wrapped with what it logged.
    pub fn inner(&self) -> &OpenError {
        match self {
            OpenError::Logged { error, .. } => error.inner(),
            other => other,
        }
    }

    /// `self` with what the upkeep logged before it was raised.
    fn after(self, logged: Vec<UpkeepWarning>) -> OpenError {
        match self {
            _ if logged.is_empty() => self,
            OpenError::Recursion { text, .. } => OpenError::Recursion { text, logged },
            error => OpenError::Logged {
                error: Box::new(error),
                logged,
            },
        }
    }

    /// The reference's exception class, or `NotPorted`.
    pub fn class(&self) -> &str {
        match self {
            OpenError::NotFound(_) => "FileNotFoundError",
            OpenError::RestoreRunning => "RestoreRunningError",
            OpenError::RestoreLocked(_) => "RestoreLockedError",
            OpenError::MarkerRecursion | OpenError::BindingRecursion(_) => "RecursionError",
            OpenError::Admission(r) => {
                if r.later.is_some() {
                    "LaterStoreError"
                } else {
                    "StoreRefusedError"
                }
            }
            OpenError::Refused(_) => "ValueError",
            OpenError::Version(_) => "StoreVersionError",
            OpenError::KeyUnreadable { .. } => "KeyUnreadableError",
            OpenError::Sqlite(e) => match e {
                rusqlite::Error::SqliteFailure(f, _)
                    if f.code == rusqlite::ErrorCode::NotADatabase =>
                {
                    "DatabaseError"
                }
                _ => "OperationalError",
            },
            OpenError::Io { error, .. } => match error.kind() {
                io::ErrorKind::NotFound => "FileNotFoundError",
                io::ErrorKind::PermissionDenied => "PermissionError",
                _ => "OSError",
            },
            OpenError::NotPorted(_) => "NotPorted",
            OpenError::Overflow => "OverflowError",
            OpenError::Recursion { .. } => "RecursionError",
            OpenError::Decode(_) => "OperationalError",
            OpenError::Raised { class, .. } => class,
            OpenError::Logged { error, .. } => error.class(),
            OpenError::Write(e) => match &**e {
                WriteError::Version(_) => "StoreVersionError",
                WriteError::Refused { class, .. } => class,
                WriteError::NotPorted(_) => "NotPorted",
                WriteError::Callback(raised) => &raised.exception.class,
                // `_close_foreign_tail` logs SQLite's, the clock's, a
                // rollback's and a later writer's; the rekey payload is an
                // object.
                other => other.class(),
            },
        }
    }
}

impl std::fmt::Display for OpenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OpenError::NotFound(p) => write!(f, "no store at {}", p.display()),
            OpenError::RestoreRunning => write!(f, "{}", restore_lock::RESTORE_RUNNING_WHY),
            OpenError::RestoreLocked(m) | OpenError::Refused(m) => write!(f, "{m}"),
            OpenError::BindingRecursion(message) => f.write_str(message),
            OpenError::MarkerRecursion => write!(f, "{}", restore_lock::RECURSION_TEXT),
            OpenError::Admission(r) => write!(f, "is not opened: {}", r.why),
            OpenError::Version(e) => write!(f, "{}", e.message),
            OpenError::KeyUnreadable { path, error } => write!(
                f,
                "the key file {} is there and cannot be read ({error})",
                path.display()
            ),
            OpenError::Sqlite(e) => write!(f, "{e}"),
            OpenError::Io { step, error } => write!(f, "{step}: {error}"),
            OpenError::NotPorted(n) => write!(f, "not ported: {}.{}", n.what(), n.remedy()),
            OpenError::Overflow => write!(f, "{}", digest::OVERFLOW_TEXT),
            OpenError::Recursion { text, .. } => f.write_str(text),
            OpenError::Decode(text) => f.write_str(text),
            OpenError::Raised { message, .. } => f.write_str(&json::utf8_text(message)),
            OpenError::Logged { error, .. } => write!(f, "{error}"),
            OpenError::Write(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for OpenError {}

impl From<rusqlite::Error> for OpenError {
    fn from(e: rusqlite::Error) -> Self {
        OpenError::Sqlite(e)
    }
}

impl From<StoreError> for OpenError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::Refused(m) => OpenError::Refused(m),
            StoreError::Version(v) => OpenError::Version(v),
            StoreError::Admission(r) => OpenError::Admission(r),
            StoreError::Sqlite(e) => OpenError::Sqlite(e),
            StoreError::Io(error) => OpenError::Io {
                step: "read the store",
                error,
            },
            StoreError::RestoreRunning => OpenError::RestoreRunning,
            _ => OpenError::NotPorted(NotPorted::Unreadable("the store's meta")),
        }
    }
}

impl From<restore_lock::AdmitError> for OpenError {
    fn from(e: restore_lock::AdmitError) -> Self {
        match e {
            restore_lock::AdmitError::Locked(l) => OpenError::RestoreLocked(l.to_string()),
            restore_lock::AdmitError::Recursion => OpenError::MarkerRecursion,
        }
    }
}

fn not_ported(n: NotPorted) -> OpenError {
    OpenError::NotPorted(n)
}

/// What `restore_rows.settle` raised past its own `except`: OverflowError
/// (a seq past SQLite's INTEGER), a write this build refuses, an SQLite
/// error it does not catch (MemoryError).
///
/// Nothing else reaches here: both callers ask `RestoreError::cause`
/// first, which catches a text that is not UTF-8, the record's lock and
/// every Python class but OverflowError and TypeError; and the TypeErrors
/// of `append_missing` (an entry that is not a dict, a head whose seq is
/// not an int) are filtered out by `pending` and `_seq_mac` before they
/// could raise. The fallback names what it met, for a build in which that
/// stopped holding.
fn restore_failure(error: RestoreError) -> OpenError {
    match error {
        RestoreError::Write(e) => OpenError::Write(Box::new(e)),
        RestoreError::Sqlite(e) => OpenError::Sqlite(e),
        RestoreError::Py(PyError::OverflowError) => OpenError::Overflow,
        other => {
            debug_assert!(false, "restore_rows.settle let through {other:?}");
            let class = match &other {
                RestoreError::Py(e) => e.name(),
                _ => "OperationalError",
            };
            OpenError::Raised {
                class,
                message: unicode::cps(&format!(
                    "restore_rows.settle raised {class}, which it never lets through \
                     ({other:?})"
                )),
            }
        }
    }
}

/// `_connect`'s owner-only modes on the file and its WAL siblings, which
/// the reference sets after its pragmas and before `confirm` and the
/// reads of meta: an open refused by `confirm` or by the reference's own
/// checks after `_connect` (a meta without a key fingerprint, a later
/// reader, an unreadable key) leaves the file 0600 as the reference's
/// does. A not-ported refusal leaves it as it was, and so does a refusal
/// of a store whose header does not say WAL: there the reference's
/// `PRAGMA journal_mode = WAL` comes first and needs the file's exclusive
/// lock, so another connection reading it fails the open ("database is
/// locked") before the modes change. This port runs no pragma before it
/// decides, so it cannot tell whether that switch would have been taken,
/// and leaves both the journal mode and the modes as they were (the
/// README's "Not built yet").
fn owner_only(file: &Path, error: OpenError) -> OpenError {
    if !matches!(error, OpenError::NotPorted(_)) && super::says_wal(file).unwrap_or(false) {
        owner_only_modes(file);
    }
    error
}

/// A point between two of the open's steps (the kill test stops the
/// process there).
#[doc(hidden)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Boundary {
    /// A dead create's temporary files swept.
    Swept,
    /// Connected, admitted and every decision read; nothing written.
    Checked,
    /// `_connect`'s pragmas set (WAL from here on) and the modes made 0600.
    Connected,
    /// `engine_version` named in meta (`_record_engine`), when newer.
    EngineRecorded,
    /// The key ring bound to this file, when it was not.
    Bound,
    /// A foreign tail named by the last `rekey` row, before its
    /// transaction takes the lock ([`open_for_writing_mut`] only).
    TailNamed,
    /// `_close_foreign_tail` done: a `rekey` row committed and the head
    /// moved, when a foreign tail was closed.
    TailClosed,
    /// A key that is not meta's and signs no segment retired
    /// (`retire_current`): no current key from here until the next.
    KeyRetired,
    /// A new key written (`create_key`), which meta does not name yet.
    KeyCreated,
    /// The rekey's transaction committed (`_open_rekeyed`), or the
    /// reclaim's done (`_reclaim`: meta set back, a `rekey` row when one
    /// was due, or the failure logged).
    Rekeyed,
    /// `_take_over_origin` done after a rekey: an anchor on the `rekey`
    /// row, or the one there kept (its key named when it named none).
    OriginTaken,
    /// `_settle_origin` done: an anchor written, or its key named.
    OriginSettled,
    /// `restore_rows.settle` done after the upkeep, on each of the owner's
    /// three paths: a pending restore's `store_restored` row appended and
    /// named in the record, or kept off the log (`row_withheld`).
    RestoreSettled,
    /// The digest's record mutations after its mark applied (one
    /// transaction).
    DigestIngested,
    /// A build's first transaction: the digest dropped and made again.
    BuildBegun,
    /// One batch of a build's records indexed.
    BuildBatch,
    /// A build's last transaction: the state at the log's head.
    BuildEnded,
    /// The open has returned and its connection closed (the caller's:
    /// the open passes it to no hook itself).
    Closed,
}

impl Boundary {
    /// Every boundary, in the order an open passes them (a build's batch
    /// once per batch; a store whose digest is current passes none of the
    /// digest's).
    pub const ALL: [Boundary; 18] = [
        Boundary::Swept,
        Boundary::Checked,
        Boundary::Connected,
        Boundary::EngineRecorded,
        Boundary::Bound,
        Boundary::TailNamed,
        Boundary::TailClosed,
        Boundary::KeyRetired,
        Boundary::KeyCreated,
        Boundary::Rekeyed,
        Boundary::OriginTaken,
        Boundary::OriginSettled,
        Boundary::RestoreSettled,
        Boundary::BuildBegun,
        Boundary::BuildBatch,
        Boundary::BuildEnded,
        Boundary::DigestIngested,
        Boundary::Closed,
    ];
}

/// What became of the digest at the open.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DigestOutcome {
    /// Current already: nothing written.
    Current,
    /// The record mutations after its mark applied.
    Ingested,
    /// Built from the records, then brought to the log's head.
    Built,
    /// Not asked: a reopen leaves the digest as it stands (`keeps_digest`
    /// brings it current for the owner's open only).
    Untouched,
    /// Left as it stood: the reference's `digest_mark` answers None
    /// (another build took it over, a busy store, a record that does not
    /// read). The text says why.
    Left(String),
}

/// A store open for writing: the connection as the reference's writing
/// connection makes it, and what the open established. Its writes are
/// `store::write` (`mutate`).
///
/// It holds the key the open read, as the reference's `Store` holds
/// `_key`: `Store._signing_key` signs with that key for as long as meta
/// names it, and reads the key ring again only when meta names another
/// key (a rekey or rotation another connection committed), so a key file
/// replaced or removed after the open does not change what signs (the
/// writing unit's README section). The key is never printed: `Debug`
/// leaves it out.
pub struct WritableStore {
    pub(super) conn: Connection,
    pub(super) busy_timeout_seconds: f64,
    path: PathBuf,
    keys_dir: PathBuf,
    store_id: String,
    key_fingerprint: String,
    pub(super) key: Key,
    pub(super) head: Option<(i64, String)>,
    digest: DigestOutcome,
    digest_reason: Option<PyStr>,
    pub(super) version_diagnostic: Option<Box<VersionDiagnostic>>,
    bound: bool,
    upkeep: Vec<UpkeepWarning>,
}

impl std::fmt::Debug for WritableStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WritableStore")
            .field("path", &self.path)
            .field("store_id", &self.store_id)
            .field("key_fingerprint", &self.key_fingerprint)
            .field("head", &self.head)
            .finish_non_exhaustive()
    }
}

impl WritableStore {
    /// The connection, for reads.
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
    /// meta's `key_fingerprint`, which the held key's is.
    pub fn key_fingerprint(&self) -> &str {
        &self.key_fingerprint
    }
    /// The log's last row as the open left it, `(seq, mac)`.
    pub fn head(&self) -> Option<&(i64, String)> {
        self.head.as_ref()
    }
    pub fn digest(&self) -> &DigestOutcome {
        &self.digest
    }

    /// The exact reason a failed digest was left, including raw filesystem codepoints.
    pub fn digest_reason(&self) -> Option<&[u32]> {
        self.digest_reason.as_deref()
    }
    /// Run a single store operation while retaining its locked writer-check error
    /// alongside its normal result. An operation which never begins a transaction
    /// has no such error. The capture is local to this exclusive store borrow.
    pub fn with_version_diagnostic<T>(
        &mut self,
        operation: impl FnOnce(&mut Self) -> T,
    ) -> (T, Option<Box<VersionDiagnostic>>) {
        self.version_diagnostic = None;
        let result = operation(self);
        (result, self.version_diagnostic.take())
    }

    /// Whether this open wrote the key ring's binding.
    pub fn bound(&self) -> bool {
        self.bound
    }
    /// What the open's upkeep logged (`_close_foreign_tail`,
    /// `_settle_origin`, `_reclaim`, `_open_rekeyed`, then
    /// `restore_rows.settle`), in order.
    pub fn upkeep(&self) -> &[UpkeepWarning] {
        &self.upkeep
    }
    /// `Store.close`.
    pub fn close(self) -> Result<(), rusqlite::Error> {
        self.conn.close().map_err(|(_, e)| e)
    }
}

/// `Store.open(path, keys_dir=keys_dir, allow_rekey=True)`: the owner's
/// writing open. `provider` gives the engine version (`__version__`).
pub fn open_for_writing(
    path: &Path,
    keys_dir: &Path,
    provider: &dyn Provider,
) -> Result<WritableStore, OpenError> {
    open_for_writing_with(path, keys_dir, provider, &mut |_| {})
}

/// [`open_for_writing`] with the provider the open may draw on: its log
/// clock stamps the `rekey` row that closes a foreign tail
/// (`_close_foreign_tail`), reclaims the store for the key in hand
/// (`_reclaim`) or rekeys it, and its key draw is the new key
/// (`_open_rekeyed`); the entries that share the provider refuse those
/// states ([`NotPorted::ForeignTail`], [`NotPorted::KeyMissing`],
/// [`NotPorted::KeyForeign`]) with nothing written.
pub fn open_for_writing_mut(
    path: &Path,
    keys_dir: &Path,
    provider: &mut dyn Provider,
) -> Result<WritableStore, OpenError> {
    open_for_writing_mut_with(path, keys_dir, provider, &mut |_| {})
}

/// [`open_for_writing_mut`] with `hook` shown each [`Boundary`].
#[doc(hidden)]
pub fn open_for_writing_mut_with(
    path: &Path,
    keys_dir: &Path,
    provider: &mut dyn Provider,
    hook: &mut dyn FnMut(Boundary),
) -> Result<WritableStore, OpenError> {
    open_writer(
        path,
        keys_dir,
        Given::Owned(provider),
        hook,
        &mut OpenDiagnostics::default(),
    )
}

/// The provider an entry gives: shared (the engine version), or the
/// caller's own (its log clock as well).
enum Given<'p> {
    Shared(&'p dyn Provider),
    Owned(&'p mut dyn Provider),
}

impl Given<'_> {
    fn engine(&self) -> &dyn Provider {
        match self {
            Given::Shared(p) => *p,
            Given::Owned(p) => &**p,
        }
    }
    fn clock(&mut self) -> Option<&mut dyn Provider> {
        match self {
            Given::Shared(_) => None,
            Given::Owned(p) => Some(&mut **p),
        }
    }
}

/// [`open_for_writing`] with `hook` shown each [`Boundary`] as it is passed
/// (the kill test exits the process there).
#[doc(hidden)]
pub fn open_for_writing_with(
    path: &Path,
    keys_dir: &Path,
    provider: &dyn Provider,
    hook: &mut dyn FnMut(Boundary),
) -> Result<WritableStore, OpenError> {
    open_writer(
        path,
        keys_dir,
        Given::Shared(provider),
        hook,
        &mut OpenDiagnostics::default(),
    )
}

/// The ordinary writing-open error together with any original high-writer verdict.
/// A malformed revision carries its exact source fields in `version`.
#[derive(Debug)]
pub struct OpenFailure {
    pub error: Box<OpenError>,
    pub read_only_reason: Option<PyStr>,
    pub version: Option<Box<VersionDiagnostic>>,
}

/// The writing open with the complete reader-only reason if an initial
/// writer minimum causes the supported opener to decline.
pub fn open_for_writing_diagnostic(
    path: &Path,
    keys_dir: &Path,
    provider: &dyn Provider,
) -> Result<WritableStore, OpenFailure> {
    let mut diagnostics = OpenDiagnostics::default();
    open_writer(
        path,
        keys_dir,
        Given::Shared(provider),
        &mut |_| {},
        &mut diagnostics,
    )
    .map_err(|error| OpenFailure {
        error: Box::new(error),
        read_only_reason: diagnostics.read_only_reason,
        version: diagnostics.version,
    })
}

#[derive(Default)]
struct OpenDiagnostics {
    read_only_reason: Option<PyStr>,
    version: Option<Box<VersionDiagnostic>>,
}

fn open_writer(
    path: &Path,
    keys_dir: &Path,
    mut provider: Given<'_>,
    hook: &mut dyn FnMut(Boundary),
    diagnostics: &mut OpenDiagnostics,
) -> Result<WritableStore, OpenError> {
    let file = store_path(path);
    // A create refuses before it sweeps when a store exists, so a dead
    // create's leftovers beside a live store go here.
    sweep_temps(&parent_of(&file));
    hook(Boundary::Swept);
    if !file.is_file() {
        return Err(OpenError::NotFound(file));
    }
    // `restore_lock.admit`, held from before SQLite opens the file until
    // the connection is made (`_connect`'s `held.close()`), and dropped
    // on every return before that.
    let held = restore_lock::admit(
        &file,
        restore_lock::OPEN_WAIT,
        restore_lock::this_thread_holds,
    )?;
    // A `-wal` with frames beside the file (a writer has it open, or died
    // with it open): until the open decides to write, its close must not
    // checkpoint those frames into the file and remove the `-wal`, so a
    // refusal leaves the file, its `-wal` and its `-shm` as they were (the
    // README's "What a refusal can change"). Without one, the close
    // checkpoints nothing and removes the `-wal` and `-shm` (an empty pair
    // a reader left, or the pair this connection made).
    let had_wal = fs::metadata(super::beside(&file, "-wal")).is_ok_and(|m| m.len() > 0);
    let (conn, deadline) = admitted(&file, had_wal)?;
    // `_connect`'s `held.confirm()`: the connection has read the store,
    // and nothing of its meta or keys is read yet. Either refusal comes
    // after `_connect`'s modes in the reference's order.
    let plan = held
        .confirm()
        .map_err(OpenError::from)
        .and_then(|()| {
            decide(
                &conn,
                &deadline,
                &file,
                keys_dir,
                provider.engine(),
                matches!(provider, Given::Owned(_)),
                diagnostics,
            )
        })
        .map_err(|e| owner_only(&file, e))?;
    if had_wal {
        // Decided to write: the close checkpoints as the reference's does.
        conn.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, false)?;
    }
    hook(Boundary::Checked);
    connect_pragmas(&conn, &file)?;
    drop(held);
    hook(Boundary::Connected);
    if plan.record_engine {
        record_engine(&conn, &provider.engine().engine_version());
    }
    hook(Boundary::EngineRecorded);
    let bound = match &plan.bind {
        Some(resolved) => bind(keys_dir, &plan.store_id, resolved)?,
        None => false,
    };
    hook(Boundary::Bound);
    // A missing or foreign key: the key is retired and a new one written
    // before the store is bound under it (`_open_rekeyed`).
    let path = match plan.path {
        rekey::Dispatch::Raise(error) => return Err(error),
        path => path,
    };
    let key = match (&path, plan.key) {
        (rekey::Dispatch::Rekey { .. }, key) => {
            let drawing = provider
                .clock()
                .ok_or_else(|| not_ported(NotPorted::KeyMissing))?;
            rekey::new_key(keys_dir, &plan.store_id, key.is_some(), drawing, hook)?
        }
        (_, Some(key)) => key,
        (_, None) => return Err(not_ported(NotPorted::KeyMissing)),
    };
    let mut store = WritableStore {
        conn,
        busy_timeout_seconds: BUSY_TIMEOUT.as_secs_f64(),
        path: file,
        keys_dir: keys_dir.to_path_buf(),
        store_id: plan.store_id,
        key_fingerprint: fingerprint(key.bytes()),
        key,
        head: None,
        digest: DigestOutcome::Current,
        digest_reason: None,
        version_diagnostic: None,
        bound,
        upkeep: Vec::new(),
    };
    let mut log = Vec::new();
    let upkept = match path {
        // KEY_PRESENT with ``allow_rekey``: `_close_foreign_tail`, then
        // `_settle_origin`; `restore_rows.settle` below.
        rekey::Dispatch::Present => {
            upkeep::close_foreign_tail(&mut store, provider.clock(), hook, &mut log)
                .and_then(|()| {
                    hook(Boundary::TailClosed);
                    upkeep::settle_origin(&store, &mut log)
                })
                .map(|()| hook(Boundary::OriginSettled))
        }
        // `_reclaim`, then `_settle_origin`.
        rekey::Dispatch::Reclaim { tail, foreign } => provider
            .clock()
            .ok_or_else(|| not_ported(NotPorted::KeyForeign))
            .and_then(|clock| {
                rekey::reclaim(
                    &mut store,
                    clock,
                    &plan.meta_fingerprint,
                    tail,
                    foreign,
                    &mut log,
                )
            })
            .and_then(|()| {
                hook(Boundary::Rekeyed);
                upkeep::settle_origin(&store, &mut log)
            })
            .map(|()| hook(Boundary::OriginSettled)),
        // The rekey's transaction, then `_take_over_origin`.
        rekey::Dispatch::Rekey { previous } => provider
            .clock()
            .ok_or_else(|| not_ported(NotPorted::KeyMissing))
            .and_then(|clock| rekey::rekeyed(&mut store, clock, previous, hook, &mut log)),
        rekey::Dispatch::Raise(error) => Err(error),
    };
    // `restore_rows.settle` after the upkeep on each path
    // (`store_open.py` 98, 131, 248).
    let settled = upkept.and_then(|()| settle(&mut store, provider.clock(), &mut log));
    if let Err(e) = settled {
        return Err(e.after(log));
    }
    hook(Boundary::RestoreSettled);
    store.upkeep = log;
    // A refusal the decision held back because it read the upkeep as
    // raising first, when it did not (another opener changed the log or
    // the anchor in between).
    plan.later.map_err(|e| e.after(store.upkeep.clone()))?;
    let mut digest_reason = None;
    let outcome = digest_mark(&store.conn, &store.path, hook, &mut digest_reason)
        .map_err(|e| e.after(store.upkeep.clone()))?;
    if let DigestOutcome::Left(text) = &outcome {
        digest_reason.get_or_insert_with(|| unicode::cps(text));
    }
    store.head = store
        .conn
        .query_row(
            "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1",
            [],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()
        .unwrap_or(None);
    store.digest = outcome;
    store.digest_reason = digest_reason;
    Ok(store)
}

/// `_connect`'s `sqlite3.connect` (the 5 s busy timeout) and
/// `admit_store`; with `keep_wal`, a close that checkpoints nothing.
fn admitted(
    file: &Path,
    keep_wal: bool,
) -> Result<(Connection, std::sync::Arc<admission::Deadline>), OpenError> {
    let mut conn = Connection::open_with_flags(
        file,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    conn.set_prepared_statement_cache_capacity(super::STATEMENT_CACHE_CAPACITY);
    conn.busy_timeout(BUSY_TIMEOUT)?;
    if keep_wal {
        conn.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)?;
    }
    let deadline = admission::install(&mut conn)?;
    match admission::admit(&conn, &deadline) {
        Ok(()) => Ok((conn, deadline)),
        Err(AdmitError::Refused(r)) => Err(OpenError::Admission(r)),
        Err(AdmitError::Sqlite(e)) => Err(e.into()),
        Err(AdmitError::Store(e)) => Err((*e).into()),
    }
}

/// What the open decided before writing anything.
struct Plan {
    store_id: String,
    /// meta's `key_fingerprint`.
    meta_fingerprint: String,
    record_engine: bool,
    /// The current key: meta's (`Dispatch::Present`), the one a reclaim
    /// keeps, the one a rekey retires, or none.
    key: Option<Key>,
    /// What the open does with it.
    path: rekey::Dispatch,
    /// The resolved store file to bind the ring to, when it is bound to
    /// none (or to a file that is gone) and this file fits it.
    bind: Option<PathBuf>,
    /// The refusal of a state after the upkeep (a restore record, the
    /// tokenizer), held back when the upkeep raises first, as the
    /// reference's does: Ok when there is none.
    later: Result<(), OpenError>,
}

/// `read_store_meta` under the admission deadline.
fn read_meta(
    conn: &Connection,
    deadline: &admission::Deadline,
) -> Result<Vec<(String, String)>, OpenError> {
    let result = admission::with_deadline(conn, deadline, "meta table", None, |conn| {
        let mut statement = conn.prepare("SELECT key, value FROM meta")?;
        let mut rows = statement.query([])?;
        let mut out: Vec<(String, String)> = Vec::new();
        while let Some(row) = rows.next()? {
            let key = sql_value(row.get_ref(0)?).map_err(AdmitError::from_store)?;
            let value = sql_value(row.get_ref(1)?).map_err(AdmitError::from_store)?;
            let (key, value) = (key.py_str(), value.py_str());
            match out.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = value,
                None => out.push((key, value)),
            }
        }
        Ok(out)
    });
    match result {
        Ok(out) => Ok(out),
        Err(AdmitError::Refused(r)) => Err(OpenError::Admission(r)),
        Err(AdmitError::Sqlite(e)) => Err(e.into()),
        Err(AdmitError::Store(e)) => Err((*e).into()),
    }
}

/// What `_open_connected` reads before the key ring: meta, the opening
/// checks, `check_versions` and `_add_membership_tables`' question.
struct Checked {
    meta: Vec<(String, String)>,
    store_id: String,
    expected: String,
    /// A later minimum writer, for an open that may not rekey: the
    /// reason as `check_versions` words it, and that minimum.
    read_only: Option<(PyStr, i64)>,
}

/// [`decide`]'s first steps, shared with a reopen (``allow_rekey`` False):
/// a later minimum writer refuses the owner's open (a reader's open, not
/// ported) and is the reopen's `read_only`.
fn checks(
    conn: &Connection,
    deadline: &admission::Deadline,
    file: &Path,
    version: &str,
    allow_rekey: bool,
    diagnostics: &mut OpenDiagnostics,
) -> Result<Checked, OpenError> {
    let meta = read_meta(conn, deadline)?;
    let shown = versions::path_text(file);
    let checked = super::open_checks_points(&meta, &shown, version).map_err(|e| {
        diagnostics.version = e.version;
        e.error
    })?;
    let opened = checked.opened;
    let mut read_only = None;
    if let Some(reason) = opened.verdict.read_only {
        if allow_rekey {
            diagnostics.read_only_reason = checked.read_only_reason;
            return Err(not_ported(NotPorted::LaterWriter(reason)));
        }
        let points = checked
            .read_only_reason
            .unwrap_or_else(|| unicode::cps(&reason));
        read_only = Some((points, opened.verdict.min_writer));
    } else if opened.verdict.revision < SCHEMA_REVISION {
        return Err(not_ported(NotPorted::SchemaSteps {
            revision: opened.verdict.revision,
        }));
    }
    let wanted = membership_wanted(conn, &meta)?;
    if !wanted.is_empty() {
        return Err(not_ported(NotPorted::MembershipStep(wanted)));
    }
    Ok(Checked {
        meta,
        store_id: opened.store_id,
        expected: opened.key_fingerprint,
        read_only,
    })
}

/// Every decision `_open_connected` and its wrappers make, read with
/// nothing written, refusing what this unit does not port.
fn decide(
    conn: &Connection,
    deadline: &admission::Deadline,
    file: &Path,
    keys_dir: &Path,
    provider: &dyn Provider,
    may_draw: bool,
    diagnostics: &mut OpenDiagnostics,
) -> Result<Plan, OpenError> {
    let version = provider.engine_version();
    let Checked {
        meta,
        store_id,
        expected,
        ..
    } = checks(conn, deadline, file, &version, true, diagnostics)?;
    let record_engine = newer_engine(meta_get(&meta, "engine_version"), &version);

    // `key_state(claim=True)`: `finish_rotation`, `load_current`, the
    // unclaimed check, the claim, the key's state.
    if rotation_to_finish(keys_dir, &store_id, &expected) {
        return Err(not_ported(NotPorted::RotationPending));
    }
    let key = load_current(keys_dir, &store_id)?;
    let ring = keys::load_key_ring(keys_dir, &store_id)
        .map_err(|_| not_ported(NotPorted::Unreadable("the key ring")))?;
    let resolved = fs::canonicalize(file).map_err(|error| OpenError::Io {
        step: "resolve the store's path",
        error,
    })?;
    let bind = planned_binding(keys_dir, &store_id, &resolved, || {
        let origin = ring
            .read_origin()
            .map_err(|_| not_ported(NotPorted::Unreadable("the origin anchor")))?;
        let head = ring
            .read_head()
            .map_err(|_| not_ported(NotPorted::Unreadable("the head")))?;
        if (origin.is_some() || head.is_some()) && !fits(conn, &ring, origin.as_ref())? {
            return Err(not_ported(NotPorted::Unclaimed));
        }
        Ok(())
    })?;
    let key = match key {
        Some(key) if fingerprint(key.bytes()) == expected => key,
        // KEY_MISSING, KEY_FOREIGN: the dispatch (`chain_segments`, then
        // `_signs_a_segment`), read now; `_reclaim` or `_open_rekeyed`
        // after the binding. A refusal of the later steps is held back
        // when `chain_segments` raises first.
        key => {
            if !may_draw {
                return Err(not_ported(if key.is_none() {
                    NotPorted::KeyMissing
                } else {
                    NotPorted::KeyForeign
                }));
            }
            let files = KeyFiles::new(keys_dir, &PyValue::str(&store_id))
                .map_err(|_| not_ported(NotPorted::Unreadable("the key ring")))?;
            let path = rekey::decide(conn, &ring, &files, &expected, key.as_ref());
            let later = match after_upkeep(conn, keys_dir, &store_id, may_draw) {
                Err(e)
                    if !matches!(path, rekey::Dispatch::Raise(_)) && !raised_after_writes(&e) =>
                {
                    return Err(e)
                }
                later => later,
            };
            return Ok(Plan {
                store_id,
                meta_fingerprint: expected,
                record_engine,
                key,
                path,
                bind,
                later,
            });
        }
    };
    // KEY_PRESENT, with ``allow_rekey``: `_close_foreign_tail`,
    // `_settle_origin` (both performed after the binding: open/upkeep.rs),
    // `restore_rows.settle`. A refusal of the later steps is held back
    // when the upkeep raises RecursionError first: the last rekey payload,
    // the anchor or (with no anchor) a rekey payload nests past the JSON
    // limit.
    let named = upkeep::last_rekey_name(conn);
    match &named {
        Ok(Some(named)) if *named != unicode::cps(&expected) && !may_draw => {
            return Err(not_ported(NotPorted::ForeignTail));
        }
        Err(upkeep::NameError::Store(_)) => {
            return Err(not_ported(NotPorted::Unreadable("a rekey row")))
        }
        _ => {}
    }
    let later = match after_upkeep(conn, keys_dir, &store_id, may_draw) {
        Err(e)
            if named.is_ok()
                && !raised_after_writes(&e)
                && !upkeep::settle_raises(conn, keys_dir, &store_id, &ring, &key) =>
        {
            return Err(e)
        }
        later => later,
    };
    Ok(Plan {
        store_id,
        meta_fingerprint: expected,
        record_engine,
        key: Some(key),
        path: rekey::Dispatch::Present,
        bind,
        later,
    })
}

/// The refusals of the steps after the upkeep: a restore record to settle
/// (`restore_rows.settle`), by the entries that cannot draw on the log's
/// clock; an index another tokenizer spelled (`reconciles_tokenizer`).
/// What the tokenizer's read raises is the reference's own exception,
/// raised once the open's writes are done ([`raised_after_writes`]).
fn after_upkeep(
    conn: &Connection,
    keys_dir: &Path,
    store_id: &str,
    may_draw: bool,
) -> Result<(), OpenError> {
    if !may_draw && restore_to_settle(keys_dir, store_id) {
        return Err(not_ported(NotPorted::RestoreRecord));
    }
    let stored = stored_tokenizer(conn)?;
    if stored.as_deref() != Some(unicode::cps(&lexicon::python_tokenizer_fingerprint()).as_slice())
    {
        return Err(not_ported(NotPorted::TokenizerDrift {
            stored: stored.map(|s| json::utf8_text(&s)),
        }));
    }
    Ok(())
}

/// Whether `error` is the reference's own exception out of
/// `reconciles_tokenizer` (the tokenizer's read: RecursionError,
/// TypeError), which the reference raises after `_open_connected`'s
/// writes (the engine's, the binding, the upkeep, a restore's rows) and
/// before `keeps_digest`: held back to that point, not refused first.
fn raised_after_writes(error: &OpenError) -> bool {
    matches!(
        error,
        OpenError::Recursion { .. } | OpenError::Raised { .. }
    )
}

/// `store_keys.finish_rotation`'s question: whether it would rename a
/// pending key over the current one. Only when `<id>.key.pending` reads,
/// holds 32 bytes and is the key meta names: a pending key that does not
/// read or that meta does not name is a rotation that did not commit, and
/// the reference leaves it alone (writing nothing). Whether the ring is
/// bound here is not asked: a pending key meta names is refused either
/// way.
fn rotation_to_finish(keys_dir: &Path, store_id: &str, recorded: &str) -> bool {
    match fs::read(keys_dir.join(format!("{store_id}.key.pending"))) {
        Ok(bytes) => {
            let bytes = Zeroizing::new(bytes);
            bytes.len() == create::KEY_BYTES && fingerprint(&bytes) == recorded
        }
        Err(_) => false,
    }
}

/// `restore_rows.pending`'s question: whether the restore record
/// (`<id>.backup_restore.json`) holds a `store_restored` entry for this
/// store whose row is there and null, which `restore_rows.settle` would
/// append. A record that is absent, over its cap or does not read names
/// none (the reference logs it and appends nothing).
fn restore_to_settle(keys_dir: &Path, store_id: &str) -> bool {
    let path = restore_rows::restore_record_path(keys_dir, store_id);
    let Ok(document) = restore_rows::read_record(&path) else {
        return false;
    };
    let Some(PyValue::List(restores)) = document.get("restores") else {
        return false;
    };
    let id = PyValue::str(store_id);
    restores.iter().any(|entry| {
        matches!(entry, PyValue::Dict(_))
            && crate::log::replaced::history_replaced_kind(entry)
                == crate::log::replaced::STORE_RESTORED
            && matches!(entry.get("row"), Some(PyValue::None))
            && !crate::history_rows::py_ne(entry.get("store_id").unwrap_or(&PyValue::None), &id)
    })
}

/// `restore_rows.settle(store)` on the owner's open, its warnings after
/// the upkeep's. The entries that share the provider (no clock) refused
/// any pending entry when they decided; they read the record as `pending`
/// does, saying what it says, and refuse an entry another process wrote
/// since.
fn settle(
    store: &mut WritableStore,
    clock: Option<&mut dyn Provider>,
    log: &mut Vec<UpkeepWarning>,
) -> Result<(), OpenError> {
    let mut said = Vec::new();
    let out = match clock {
        Some(clock) => restore_rows::settle(store, clock, &mut said).map_err(restore_failure),
        None => match restore_rows::pending(store, &mut said) {
            Ok(entries) if entries.is_empty() => Ok(()),
            Ok(_) => Err(not_ported(NotPorted::RestoreRecord)),
            Err(e) => match e.cause() {
                Some(cause) => {
                    said.push(restore_rows::Warning::NotAppended(cause));
                    Ok(())
                }
                None => Err(restore_failure(e)),
            },
        },
    };
    log.extend(said.into_iter().map(UpkeepWarning::Restore));
    out
}

/// `_add_membership_tables`' `wanted` (`schema::wanted`) over this
/// connection's `sqlite_master`.
fn membership_wanted(
    conn: &Connection,
    meta: &[(String, String)],
) -> Result<Vec<String>, OpenError> {
    let master: Vec<(String, String)> = {
        let mut statement =
            conn.prepare("SELECT name, sql FROM sqlite_master WHERE name IS NOT NULL")?;
        let mut rows = statement.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let name = sql_value(row.get_ref(0)?)?.py_str();
            let sql = match sql_value(row.get_ref(1)?)? {
                SqlValue::Null => String::new(),
                v => v.py_str(),
            };
            out.push((name, sql));
        }
        out
    };
    Ok(schema::wanted(&master, meta))
}

/// `_daemon_client.version_key`.
fn version_key(text: &str) -> Option<Vec<i128>> {
    let cps = unicode::cps(text);
    let start = cps.iter().position(|&c| !unicode::is_space(c))?;
    let end = cps.len() - cps.iter().rev().position(|&c| !unicode::is_space(c))?;
    let t = &cps[start..end];
    let mut i = 0;
    let number = |i: &mut usize| -> Option<i128> {
        let from = *i;
        while *i < t.len() && unicode::is_re_digit(t[*i]) {
            *i += 1;
        }
        if *i == from {
            return None;
        }
        match &pyvalue::int_from_str(&t[from..*i]) {
            Ok(PyValue::Int(d)) => d.parse().ok(),
            _ => None,
        }
    };
    let dot =
        |i: &mut usize| -> Option<()> { (t.get(*i) == Some(&u32::from(b'.'))).then(|| *i += 1) };
    let major = number(&mut i)?;
    dot(&mut i)?;
    let minor = number(&mut i)?;
    dot(&mut i)?;
    let patch = number(&mut i)?;
    let mut pre = None;
    for (word, rank) in [("rc", 2), ("a", 0), ("b", 1)] {
        let w = unicode::cps(word);
        if t[i..].starts_with(&w) {
            let mut j = i + w.len();
            if let Some(n) = number(&mut j) {
                pre = Some((rank, n));
                i = j;
            }
            break;
        }
    }
    let mut dev = None;
    let tail = unicode::cps(".dev");
    if t[i..].starts_with(&tail) {
        let mut j = i + tail.len();
        if let Some(n) = number(&mut j) {
            dev = Some(n);
            i = j;
        }
    }
    if i != t.len() {
        return None;
    }
    let pre_key = match (pre, dev) {
        (Some((rank, n)), _) => (rank, n),
        (None, Some(_)) => (-1, 0),
        (None, None) => (3, 0),
    };
    let dev_key = match dev {
        Some(n) => (0, n),
        None => (1, 0),
    };
    Some(vec![
        major, minor, patch, pre_key.0, pre_key.1, dev_key.0, dev_key.1,
    ])
}

/// `store_versions._newer_engine(stored, version)`.
fn newer_engine(stored: Option<&str>, version: &str) -> bool {
    let release = |v: &str| version_key(v.split('+').next().unwrap_or(""));
    match release(version) {
        None => false,
        Some(ours) => match stored.and_then(release) {
            None => true,
            Some(theirs) => ours > theirs,
        },
    }
}

/// `KeyRing.load_current`: the key, None when there is no key file;
/// `KeyUnreadableError` when it is there and cannot be read, or is a
/// dangling link (itself or the keys directory).
fn load_current(keys_dir: &Path, store_id: &str) -> Result<Option<Key>, OpenError> {
    let path = keys_dir.join(format!("{store_id}.key"));
    match fs::read(&path) {
        Ok(bytes) => {
            let mut bytes = Zeroizing::new(bytes);
            Ok(Some(Key::new(std::mem::take(&mut *bytes))))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let dangling = |p: &Path| {
                fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink())
                    && fs::metadata(p).is_err()
            };
            if dangling(&path) || dangling(keys_dir) {
                return Err(OpenError::KeyUnreadable { path, error: e });
            }
            Ok(None)
        }
        Err(error) => Err(OpenError::KeyUnreadable { path, error }),
    }
}

fn row_at(conn: &Connection, seq: i64) -> Result<Option<LogRow>, OpenError> {
    let mut statement = conn.prepare(&format!("{LOG_SELECT} WHERE seq = ?"))?;
    let mut rows = statement.query([seq])?;
    match rows.next()? {
        None => Ok(None),
        Some(row) => log_row(row)
            .map(Some)
            .map_err(|_| not_ported(NotPorted::Unreadable("a log row"))),
    }
}

fn last_row(conn: &Connection) -> Result<Option<LogRow>, OpenError> {
    let mut statement = conn.prepare(&format!("{LOG_SELECT} ORDER BY seq DESC LIMIT 1"))?;
    let mut rows = statement.query([])?;
    match rows.next()? {
        None => Ok(None),
        Some(row) => log_row(row)
            .map(Some)
            .map_err(|_| not_ported(NotPorted::Unreadable("a log row"))),
    }
}

/// `ring_fit(...).fits`: the anchor is not broken (`check_origin`) and the
/// log reaches the head (`head_status` absent, ok or stale).
fn fits(
    conn: &Connection,
    ring: &KeyRing,
    origin: Option<&HistoryOrigin>,
) -> Result<bool, OpenError> {
    let anchored = match origin {
        Some(o) => row_at(conn, o.seq)?,
        None => None,
    };
    if log::origin_status(origin, anchored.as_ref(), ring) == "mismatch" {
        return Ok(false);
    }
    let head = ring
        .read_head()
        .map_err(|_| not_ported(NotPorted::Unreadable("the head")))?;
    let Some(head) = head else {
        return Ok(true);
    };
    let seq = match &head.seq {
        PyValue::Int(d) => d.parse::<i64>().ok(),
        _ => None,
    };
    let Some(last) = last_row(conn)? else {
        return Ok(false);
    };
    let Some(seq) = seq else {
        // A seq past an i64 is past any log's end.
        return Ok(false);
    };
    if seq > last.seq {
        return Ok(false);
    }
    if seq == last.seq {
        return Ok(log::same_mac(&head.mac, &unicode::cps(&last.mac)));
    }
    // `next(iter_rows(conn, since_seq=head.seq - 1))`.
    let mut statement = conn.prepare(&format!(
        "{LOG_SELECT} WHERE seq > ? AND seq > 0 ORDER BY seq LIMIT 1"
    ))?;
    let mut rows = statement.query([seq - 1])?;
    let named = match rows.next()? {
        None => None,
        Some(row) => {
            Some(log_row(row).map_err(|_| not_ported(NotPorted::Unreadable("a log row")))?)
        }
    };
    Ok(named.is_some_and(|n| n.seq == seq && log::same_mac(&head.mac, &unicode::cps(&n.mac))))
}

/// `store_index.stored_fingerprint` as the open reads it
/// (`store::stored_tokenizer`): the tokenizer the newest `index_rebuilt`
/// row (`new_fingerprint`) or the `store_created` row names, whichever is
/// later; meta's copy for a log that names none. What the read raises
/// (`_load_json` catches only ValueError) fails the open, worded as the
/// reference's exception: RecursionError for a payload nested past the
/// JSON limit, TypeError for a number. A text column that is not UTF-8
/// is a state this unit does not port.
pub(super) fn stored_tokenizer(conn: &Connection) -> Result<Option<PyStr>, OpenError> {
    super::stored_tokenizer(conn).map_err(|e| match e {
        StoreError::Py(PyError::RecursionError) => upkeep::recursion(tokenizer_raised(conn).0),
        StoreError::Py(PyError::TypeError) => OpenError::Raised {
            class: "TypeError",
            message: unicode::cps(&format!(
                "the JSON object must be str, bytes or bytearray, not {}",
                tokenizer_raised(conn).1
            )),
        },
        StoreError::Sqlite(e) => OpenError::Sqlite(e),
        _ => not_ported(NotPorted::Unreadable("a log payload")),
    })
}

/// Which payload `stored_fingerprint` raised for, read again in its
/// order (the newest `index_rebuilt` row, then `store_created`): the
/// RecursionError's text for the first that nests past the limit, and
/// the type name of the first that is a number.
fn tokenizer_raised(conn: &Connection) -> (&'static str, &'static str) {
    let mut nested = None;
    let mut number = None;
    for kind in ["index_rebuilt", "store_created"] {
        let payload = conn
            .query_row(
                "SELECT payload FROM log WHERE kind = ? ORDER BY seq DESC LIMIT 1",
                [kind],
                |r| Ok(sql_value(r.get_ref(0)?)),
            )
            .optional();
        let points = match payload {
            Ok(Some(Ok(SqlValue::Text(t)))) => Some(unicode::cps(&t)),
            Ok(Some(Ok(SqlValue::Blob(b)))) => super::provenance::decode_json_bytes(&b),
            Ok(Some(Ok(SqlValue::Integer(_)))) => {
                number.get_or_insert("int");
                None
            }
            Ok(Some(Ok(SqlValue::Real(_)))) => {
                number.get_or_insert("float");
                None
            }
            _ => None,
        };
        if let Some(points) = points {
            if nested.is_none() && matches!(json::loads(&points), Err(PyError::RecursionError)) {
                nested = Some(json::reading_recursion_message(&points));
            }
        }
    }
    (
        nested.unwrap_or("maximum recursion depth exceeded"),
        number.unwrap_or("int"),
    )
}

/// `_connect`'s pragmas, in its order, then owner-only modes on the file
/// and its WAL siblings.
fn connect_pragmas(conn: &Connection, file: &Path) -> Result<(), OpenError> {
    conn.execute_batch(&format!("PRAGMA page_size = {PAGE_SIZE}"))?;
    conn.execute_batch("PRAGMA foreign_keys = ON")?;
    conn.query_row("PRAGMA journal_mode = WAL", [], |_| Ok(()))?;
    conn.query_row("PRAGMA schema_version", [], |_| Ok(()))?;
    // FULL: every COMMIT syncs the WAL before the head that names it is
    // written (ENG-328); NORMAL left the WAL to the next checkpoint.
    conn.execute_batch("PRAGMA synchronous = FULL")?;
    owner_only_modes(file);
    Ok(())
}

/// `_connect`'s chmod 0600 of the file and its WAL siblings that exist,
/// each failure suppressed.
fn owner_only_modes(file: &Path) {
    #[cfg(unix)]
    for sibling in [
        file.to_path_buf(),
        with_suffix(file, "-wal"),
        with_suffix(file, "-shm"),
    ] {
        use std::os::unix::fs::PermissionsExt;
        if sibling.exists() {
            let _ = fs::set_permissions(&sibling, fs::Permissions::from_mode(0o600));
        }
    }
    #[cfg(not(unix))]
    let _ = file;
}

/// `_record_engine`: name `version` in meta when it is newer than the
/// stored one, read again under the write lock. A failure is logged by
/// the reference and the open goes on; so here.
fn record_engine(conn: &Connection, version: &str) {
    let run = || -> rusqlite::Result<()> {
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let stored: Option<SqlValue> = conn
            .query_row(
                "SELECT value FROM meta WHERE key = 'engine_version'",
                [],
                |r| Ok(sql_value(r.get_ref(0)?).ok()),
            )
            .optional()?
            .flatten();
        let stored = stored.map(|v| v.py_str());
        if newer_engine(stored.as_deref(), version) {
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) \
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ["engine_version", version],
            )?;
        }
        conn.execute_batch("COMMIT")
    };
    if run().is_err() && !conn.is_autocommit() {
        let _ = conn.execute_batch("ROLLBACK");
    }
}

/// The writer decides whether to bind, consulting the store's history only
/// when the binding is absent. Its locked re-read below uses the same decoder.
fn planned_binding(
    keys_dir: &Path,
    store_id: &str,
    resolved: &Path,
    unbound: impl FnOnce() -> Result<(), OpenError>,
) -> Result<Option<PathBuf>, OpenError> {
    let path = keys_dir.join(format!("{store_id}.path"));
    let needed = match binding_path::read(&path, binding_path::HOST) {
        Ok(Some(bound)) => needs_binding(keys_dir, store_id, bound, resolved)?,
        Ok(None) => {
            unbound()?;
            true
        }
        Err(BindingError::Os(_)) => false,
        Err(error) => return Err(binding_failure(error)),
    };
    Ok(needed.then(|| resolved.to_owned()))
}

/// `KeyRing.bind_store`'s write: under `<id>.path.lock`, the binding read
/// again (another opener may have bound it meanwhile), then written
/// atomically and owner-only: `{"path": <the store file, resolved>}`.
/// Ok(false) when nothing was written (bound to this file meanwhile, or an
/// OS error, which the reference logs and goes on past). A re-read that
/// finds the ring bound meanwhile to another file that holds the store
/// (or cannot be read to tell) is the reference's ``elsewhere``: its open
/// goes on as a reader's, so this one refuses, before the digest is
/// touched and with no key handed out.
fn bind(keys_dir: &Path, store_id: &str, resolved: &Path) -> Result<bool, OpenError> {
    let path = keys_dir.join(format!("{store_id}.path"));
    let open_lock = || -> io::Result<fs::File> {
        owner_only_dir(keys_dir)?;
        create::sweep_ring_temps(keys_dir);
        let lock_path = with_suffix(&path, ".lock");
        let lock = match new_private_file(&lock_path) {
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&lock_path)?,
            result => result?,
        };
        lock.lock()?;
        Ok(lock)
    };
    let Ok(_guard) = open_lock() else {
        return Ok(false);
    };
    match binding_path::read(&path, binding_path::HOST) {
        Ok(Some(bound)) => {
            if !needs_binding(keys_dir, store_id, bound, resolved)? {
                return Ok(false);
            }
        }
        Ok(None) => {}
        Err(BindingError::Os(_)) => return Ok(false),
        Err(error) => return Err(binding_failure(error)),
    }
    Ok(BindingPath::resolved(resolved)
        .json()
        .and_then(|text| replace_atomic(&path, text.as_bytes()))
        .is_ok())
}

fn binding_failure(error: BindingError) -> OpenError {
    match error {
        BindingError::Recursion(message) => OpenError::BindingRecursion(message),
        BindingError::Value => OpenError::Refused("invalid binding path".to_owned()),
        BindingError::Os(error) => OpenError::Io {
            step: "read the binding",
            error,
        },
    }
}

fn needs_binding(
    keys_dir: &Path,
    store_id: &str,
    bound: BindingPath,
    resolved: &Path,
) -> Result<bool, OpenError> {
    if bound.same_file(&BindingPath::resolved(resolved)) {
        return Ok(false);
    }
    // _store_id_at catches ValueError from stat on an unencodable path.
    let Some(other) = bound.host_path() else {
        return Ok(true);
    };
    let unread = match holds_store(&other, &bound, store_id) {
        Ok(false) => return Ok(true),
        Ok(true) => None,
        Err(reason) => Some(reason),
    };
    let original_spelling =
        (crate::fsutil::fsdecode(other.as_os_str()) != bound.text()).then(|| Box::new(bound));
    Err(not_ported(NotPorted::BoundElsewhere {
        keys_dir: keys_dir.to_owned(),
        store_id: store_id.to_owned(),
        other,
        original_spelling,
        unread,
    }))
}

/// `_bound_store_check`'s ``holds_store``: does the store file at `other`
/// still hold store id `store_id` (`_store_id_at(other) == store_id`)?
/// Err(why) when it cannot be read to tell, which the reference takes as
/// holding it (and says why).
fn holds_store(other: &Path, spelling: &BindingPath, store_id: &str) -> Result<bool, String> {
    store_id_at(other, spelling)
        .map(|id| id.as_deref() == Some(store_id))
        .map_err(|error| match error {
            SecondaryError::Stat(e) => e.to_string(),
            SecondaryError::Reason(reason) => crate::native_text::utf8_text(&reason),
        })
}

pub(super) enum SecondaryError {
    Stat(io::Error),
    Reason(crate::pyvalue::PyStr),
}

/// `store_keys._store_id_at`: the store id meta records in the file at
/// `path`, read without opening it as a store: read-only, `immutable=1`
/// (no lock, no `-wal` or `-shm` made), or `mode=ro` first when its `-wal`
/// and `-shm` are both there; admitted (`admit_store`) and meta read under
/// the admission deadline, a 2 s busy timeout. Ok(None) when no file is
/// there or it does not read as a store; Err when it cannot be read to
/// tell (the reference's OSError text).
pub(super) fn store_id_at(
    path: &Path,
    spelling: &BindingPath,
) -> Result<Option<String>, SecondaryError> {
    let status = match fs::metadata(path) {
        Ok(status) => status,
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::NotFound
                    | io::ErrorKind::NotADirectory
                    | io::ErrorKind::InvalidInput
            ) =>
        {
            return Ok(None)
        }
        // ELOOP (macOS 62, Linux 40), or Windows' reparse-point cycle.
        Err(e) if is_link_loop(&e) => return Ok(None),
        Err(e) => return Err(SecondaryError::Stat(e)),
    };
    if !status.is_file() {
        return Ok(None);
    }
    let resolved = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut queries = vec!["mode=ro&immutable=1"];
    if super::beside(&resolved, "-wal").exists() && super::beside(&resolved, "-shm").exists() {
        queries.insert(0, "mode=ro");
    }
    let mut failed: Option<crate::pyvalue::PyStr> = None;
    for query in queries {
        let uri = format!("{}?{query}", super::file_uri(&resolved));
        let flags = OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let opened = Connection::open_with_flags(&uri, flags).and_then(|mut conn| {
            conn.busy_timeout(STORE_ID_TIMEOUT)?;
            let deadline = admission::install(&mut conn)?;
            Ok((conn, deadline))
        });
        let (conn, deadline) = match opened {
            Ok(opened) => opened,
            Err(e) => {
                // Nothing of the file was read: no answer.
                failed = Some(crate::unicode::cps(&e.to_string()));
                continue;
            }
        };
        let read = match admission::admit(&conn, &deadline) {
            Ok(()) => read_meta(&conn, &deadline),
            Err(AdmitError::Refused(r)) => Err(OpenError::Admission(r)),
            Err(AdmitError::Sqlite(e)) => Err(OpenError::Sqlite(e)),
            Err(AdmitError::Store(e)) => Err((*e).into()),
        };
        drop(conn);
        match read {
            Ok(meta) => {
                if let Some(id) = meta_get(&meta, "store_id") {
                    return Ok(Some(id.to_owned()));
                }
            }
            // A schema SQLite cannot read, or a refused file with no meta
            // table, holds no store; a refused file with one, or a read
            // past the deadline, cannot be read to tell.
            Err(OpenError::Admission(r)) => {
                if !r.timed_out && (r.holds_meta == Some(false) || unreadable_schema(&r)) {
                    return Ok(None);
                }
                let mut why = super::key_state::resolve_spelling(spelling.text())
                    .unwrap_or_else(|_| BindingPath::resolved(&resolved).text().to_vec());
                why.extend(crate::unicode::cps(" is not opened: "));
                why.extend(crate::unicode::cps(&r.why));
                failed = Some(why);
            }
            Err(OpenError::Sqlite(e)) => {
                if !not_a_store_code(&e) {
                    failed = Some(crate::unicode::cps(&e.to_string()));
                }
            }
            Err(e) => failed = Some(crate::unicode::cps(&e.to_string())),
        }
    }
    match failed {
        Some(why) => {
            let mut message = spelling.text().to_vec();
            message.extend(crate::unicode::cps(" could not be read ("));
            message.extend(why);
            message.push(u32::from(b')'));
            Err(SecondaryError::Reason(message))
        }
        None => Ok(None),
    }
}

/// `_store_id_at`'s ``timeout=2.0``.
const STORE_ID_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);

/// `store_keys._NOT_A_STORE_CODES`: SQLITE_ERROR, SQLITE_CORRUPT,
/// SQLITE_NOTADB (primary codes).
fn not_a_store_code(e: &rusqlite::Error) -> bool {
    matches!(e, rusqlite::Error::SqliteFailure(f, _) if matches!(f.extended_code & 0xff, 1 | 11 | 26))
}

/// A refusal `admit_store` raised from SQLITE_CORRUPT or SQLITE_NOTADB
/// reading the schema (its cause's code is one of `_NOT_A_STORE_CODES`).
fn unreadable_schema(r: &admission::Refusal) -> bool {
    r.reference_raises.is_none() && r.later.is_none() && r.why.contains("schema cannot be read (")
}

fn is_link_loop(e: &io::Error) -> bool {
    #[cfg(target_vendor = "apple")]
    let code = 62;
    #[cfg(all(unix, not(target_vendor = "apple")))]
    let code = 40;
    #[cfg(not(unix))]
    let code = 1921;
    e.raw_os_error() == Some(code)
}

/// `_fsutil.atomic_write_bytes(path, data, mode_before_rename=0o600)`: a
/// private temporary file beside it, synced, renamed over the name, the
/// directory synced.
pub(super) fn replace_atomic(path: &Path, data: &[u8]) -> io::Result<()> {
    replace_atomic_with(path, data, |e, _, _| e)
}

/// Keep failing syscall arguments for callers that report the Python OSError.
pub(super) fn replace_atomic_with<E>(
    path: &Path,
    data: &[u8],
    failure: impl Fn(io::Error, Option<&Path>, Option<&Path>) -> E,
) -> Result<(), E> {
    replace_atomic_synced(path, data, bettermemory_sys::fsync, failure)
}

/// [`replace_atomic_with`] with the file's sync given: `fsync_file`
/// suppresses its OSError, so a sync that fails still writes.
fn replace_atomic_synced<E>(
    path: &Path,
    data: &[u8],
    sync: impl Fn(&fs::File) -> io::Result<()>,
    failure: impl Fn(io::Error, Option<&Path>, Option<&Path>) -> E,
) -> Result<(), E> {
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let tmp = with_suffix(path, &format!(".{}x{n}.tmp", std::process::id()));
    let mut file = new_private_file(&tmp).map_err(|e| failure(e, Some(&tmp), None))?;
    let staged = file.write_all(data);
    if staged.is_ok() {
        let _ = sync(&file);
    }
    drop(file);
    let written = staged
        .map_err(|e| failure(e, None, None))
        .and_then(|()| rename_retrying(&tmp, path).map_err(|e| failure(e, Some(&tmp), Some(path))));
    if let Err(error) = written {
        let _ = fs::remove_file(&tmp);
        return Err(error);
    }
    sync_dir(&parent_of(path));
    Ok(())
}

/// `_fsutil.replace_atomic`: `os.replace`, which on Windows tries a
/// rename a reader holds open again (PermissionError only), at most
/// `_REPLACE_ATTEMPTS` (5) times with a pause doubling from 10 ms.
fn rename_retrying(from: &Path, to: &Path) -> io::Result<()> {
    let attempts = if cfg!(windows) { REPLACE_ATTEMPTS } else { 1 };
    let mut delay = std::time::Duration::from_millis(10);
    let mut attempt = 1;
    loop {
        match fs::rename(from, to) {
            Err(e) if attempt < attempts && e.kind() == io::ErrorKind::PermissionDenied => {
                attempt += 1;
                std::thread::sleep(delay);
                delay = delay.saturating_mul(2);
            }
            out => return out,
        }
    }
}

/// `_fsutil._REPLACE_ATTEMPTS`.
pub(crate) const REPLACE_ATTEMPTS: u32 = 5;

/// `require_current_writer` inside a transaction: meta's minimum writer
/// read again under the lock; above this build's, the connection is held
/// to reading (`query_only`) and the transaction refused.
// The diagnostic travels with the write error the transaction returns.
#[allow(clippy::result_large_err)]
pub fn require_current_writer(
    conn: &Connection,
    path: &Path,
) -> Result<(), DiagnosticError<WriteError>> {
    const KEY: &str = "min_writer_revision";
    let Some(raw) = current_writer_value(conn, KEY)? else {
        return Ok(());
    };
    let needed = versions::stored_revision(Some(&raw), KEY, || versions::path_text(path))
        .map_err(|e| DiagnosticError::version(e, WriteError::Version))?;
    if needed > SCHEMA_REVISION {
        conn.execute_batch("PRAGMA query_only = ON")
            .map_err(WriteError::from)?;
        return Err(WriteError::LaterWriter {
            path: path.to_owned(),
            needed,
        }
        .into());
    }
    Ok(())
}

fn current_writer_value(conn: &Connection, key: &str) -> Result<Option<String>, WriteError> {
    let mut stmt = conn.prepare("SELECT value FROM meta WHERE key = ?")?;
    let mut rows = stmt.query([key])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    let value = row.get_ref(0)?;
    let raw = sql_value(value)
        .map_err(|_| {
            // pysqlite's 198-byte diagnostic treats each non-ASCII byte
            // as a replacement and the first NUL as the end of the value.
            let bytes = value.as_bytes().expect("only invalid TEXT fails sql_value");
            let message = super::sqlite_text_error("value", bytes);
            WriteError::Refused {
                class: "OperationalError",
                text: Some(message),
            }
        })?
        .py_str();
    Ok(Some(raw))
}

/// `Store._transaction` around `body`: `BEGIN IMMEDIATE`, the writer
/// check, the body, `COMMIT`; any failure rolled back.
fn transaction<T>(
    conn: &Connection,
    path: &Path,
    source: &mut Option<Box<VersionDiagnostic>>,
    body: impl FnOnce(&Connection) -> Result<T, DigestError>,
) -> Result<T, DigestError> {
    conn.execute_batch("BEGIN IMMEDIATE")?;
    let out = require_current_writer(conn, path)
        .map_err(|e| {
            *source = e.version;
            DigestError::Writer(e.error)
        })
        .and_then(|()| body(conn))
        .and_then(|v| conn.execute_batch("COMMIT").map(|()| v).map_err(Into::into));
    if out.is_err() && !conn.is_autocommit() {
        let _ = conn.execute_batch("ROLLBACK");
    }
    out
}

/// `digest_mark()` as `keeps_digest` calls it (`build=True`): nothing when
/// the digest is current; else, in one transaction, the record mutations
/// after its mark applied; or, where it has to be rebuilt, `_build` (its
/// own transactions, one per batch) and then the mutations after the
/// build's start. Every failure is what `digest_mark` catches: the digest
/// is left as its last committed transaction left it.
fn digest_mark(
    conn: &Connection,
    path: &Path,
    hook: &mut dyn FnMut(Boundary),
    exact_reason: &mut Option<PyStr>,
) -> Result<DigestOutcome, OpenError> {
    // An overflow is not among what `digest_mark` catches: it leaves the
    // open.
    let mut source = None;
    let mut left = |e: DigestError, version: Option<Box<VersionDiagnostic>>| match e {
        DigestError::Overflow => Err(OpenError::Overflow),
        e => {
            let text = match &e {
                DigestError::Writer(error) => {
                    version.map_or_else(|| error.message_points(), |e| e.exact_message)
                }
                other => unicode::cps(&other.to_string()),
            };
            let legacy = versions::legacy_text(&text);
            *exact_reason = Some(text);
            Ok(DigestOutcome::Left(legacy))
        }
    };
    match digest::current(conn) {
        Ok(true) => return Ok(DigestOutcome::Current),
        Ok(false) => {}
        Err(e) => return left(e, source),
    }
    let ingested = transaction(conn, path, &mut source, |conn| {
        let state = digest::state(conn)?;
        match digest::built(conn, state.as_deref())? {
            Some(seen) => digest::ingest(conn, seen).map(|()| true),
            None => Ok(false),
        }
    });
    match ingested {
        Ok(true) => {
            hook(Boundary::DigestIngested);
            return Ok(DigestOutcome::Ingested);
        }
        Ok(false) => {}
        Err(e) => return left(e, source),
    }
    // `_build`: its name is never a final byte (the last transaction
    // removes it).
    let token = format!(
        "{:08x}{:08x}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    let start = match transaction(conn, path, &mut source, |conn| {
        digest::build_begin(conn, &token)
    }) {
        Ok(start) => start,
        Err(e) => return left(e, source),
    };
    hook(Boundary::BuildBegun);
    for removed in [false, true] {
        let mut after = 0;
        loop {
            let batch = transaction(conn, path, &mut source, |conn| {
                if !digest::build_owns(conn, &token)? {
                    return Ok(None);
                }
                digest::build_batch(conn, removed, after).map(Some)
            });
            match batch {
                Ok(Some((last, full))) => {
                    hook(Boundary::BuildBatch);
                    if !full {
                        break;
                    }
                    after = last;
                }
                Ok(None) => {
                    return Ok(DigestOutcome::Left("another build took it over".to_owned()))
                }
                Err(e) => return left(e, source),
            }
        }
    }
    let ended = transaction(conn, path, &mut source, |conn| {
        if !digest::build_owns(conn, &token)? {
            return Ok(false);
        }
        digest::build_end(conn, start).map(|()| true)
    });
    match ended {
        Ok(true) => hook(Boundary::BuildEnded),
        Ok(false) => return Ok(DigestOutcome::Left("another build took it over".to_owned())),
        Err(e) => return left(e, source),
    }
    let caught = transaction(conn, path, &mut source, |conn| {
        let state = digest::state(conn)?;
        match digest::built(conn, state.as_deref())? {
            Some(seen) => digest::ingest(conn, seen).map(|()| true),
            None => Ok(false),
        }
    });
    match caught {
        Ok(true) => {
            hook(Boundary::DigestIngested);
            Ok(DigestOutcome::Built)
        }
        Ok(false) => Ok(DigestOutcome::Left("the build was not current".to_owned())),
        Err(e) => left(e, source),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binding_reads_preserve_decoder_errors_at_both_stages() {
        let mut nonce = [0; 12];
        getrandom::fill(&mut nonce).unwrap();
        let dir = std::env::temp_dir().join(format!("bm-binding-{nonce:x?}"));
        fs::create_dir(&dir).unwrap();
        let dir = dir.canonicalize().unwrap();
        let document = dir.join("test.path");
        let resolved = dir.join("file");
        let beyond_decoder_limit = crate::json::MAX_DEPTH + 2;
        for raw in [
            b"[".repeat(beyond_decoder_limit),
            br#"{"x":"#.repeat(beyond_decoder_limit),
        ] {
            fs::write(&document, &raw).unwrap();
            let error = planned_binding(&dir, "test", &resolved, || {
                panic!("unexpected history read")
            })
            .unwrap_err();
            assert_eq!(error.class(), "RecursionError");
            let locked_error = bind(&dir, "test", &resolved).unwrap_err();
            assert_eq!(locked_error.class(), error.class());
            assert_eq!(locked_error.to_string(), error.to_string());
            assert_eq!(fs::read(&document).unwrap(), raw);
            // Returning from the locked read releases its guard.
            let lock = fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(dir.join("test.path.lock"))
                .unwrap();
            lock.try_lock().unwrap();
        }
        fs::remove_file(&document).unwrap();
        fs::create_dir(&document).unwrap();
        assert!(planned_binding(&dir, "test", &resolved, || panic!(
            "unexpected history read"
        ))
        .unwrap()
        .is_none());
        assert!(!bind(&dir, "test", &resolved).unwrap());
        assert!(document.is_dir());
        fs::remove_dir(&document).unwrap();
        for malformed in [None, Some(b"not json".as_slice())] {
            if let Some(raw) = malformed {
                fs::write(&document, raw).unwrap();
            }
            let mut calls = 0;
            let plan = planned_binding(&dir, "test", &resolved, || {
                calls += 1;
                Ok(())
            })
            .unwrap();
            assert_eq!(calls, 1);
            assert_eq!(plan.as_deref(), Some(resolved.as_path()));
            assert!(bind(&dir, "test", &resolved).unwrap());
            assert_eq!(
                fs::read_to_string(&document).unwrap(),
                BindingPath::resolved(&resolved).json().unwrap()
            );
            fs::remove_file(&document).unwrap();
        }
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_failed_sync_still_writes() {
        let mut nonce = [0; 12];
        getrandom::fill(&mut nonce).unwrap();
        let dir = std::env::temp_dir().join(format!("bm-sync-{nonce:x?}"));
        fs::create_dir(&dir).unwrap();
        let target = dir.join("binding.path");
        fs::write(&target, b"old").unwrap();
        let synced = std::cell::Cell::new(0);
        let out: io::Result<()> = replace_atomic_synced(
            &target,
            b"new",
            |_| {
                synced.set(synced.get() + 1);
                Err(io::Error::from_raw_os_error(5))
            },
            |e, _, _| e,
        );
        assert!(out.is_ok(), "{out:?}");
        assert_eq!(synced.get(), 1);
        assert_eq!(fs::read(&target).unwrap(), b"new");
        assert_eq!(
            fs::read_dir(&dir).unwrap().count(),
            1,
            "no temporary file left"
        );
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn versions_order_as_pep_440() {
        let k = |t: &str| version_key(t).expect(t);
        assert!(k("9.0.0.dev1") < k("9.0.0a1"));
        assert!(k("9.0.0a1") < k("9.0.0b1"));
        assert!(k("9.0.0b1") < k("9.0.0rc1"));
        assert!(k("9.0.0rc1") < k("9.0.0rc19"));
        assert!(k("9.0.0rc19") < k("9.0.0"));
        assert!(k("9.0.0rc1.dev2") < k("9.0.0rc1"));
        assert_eq!(k(" 9.0.0 "), k("9.0.0"));
        assert!(version_key("9.0").is_none());
        assert!(version_key("9.0.0x").is_none());
        assert!(newer_engine(Some("9.0.0rc1"), "9.0.0rc19"));
        assert!(!newer_engine(Some("9.0.0rc19"), "9.0.0rc19"));
        assert!(newer_engine(None, "9.0.0"));
        assert!(newer_engine(Some("junk"), "9.0.0"));
        assert!(!newer_engine(Some("9.0.0"), "junk"));
        assert!(newer_engine(Some("9.0.0+local"), "9.0.1+x"));
    }
}
