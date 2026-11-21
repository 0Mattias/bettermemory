//! One record mutation on a store open for writing: `Store._mutate`
//! inside `Store._transaction`, and the head checkpoint's move after the
//! commit (`Store._move_head`, `KeyRing.write_head`).
//!
//! The order is the reference's (README, "Writing a record mutation"):
//! `BEGIN IMMEDIATE` (the connection's busy timeout is the wait), the
//! writer check (`require_current_writer`), `store_returned.before`, the
//! signing key (`_signing_key`), the last row, the clock, the row
//! (`log.append_row`: seq, ts, session, kind, canonical payload,
//! prev_mac, MAC), the mutation applied to the tables with the fold's
//! applier (`store_rows._apply_mutation`; the FTS5 index follows through
//! the schema's triggers), `_flush_returns`, `COMMIT`, then the head
//! moved forward under its lock.
//!
//! A mutation is a transaction of one (`store::tx`), whose commit appends
//! the `returned` rows `_flush_returns` settles. What this unit does not
//! port it refuses before the commit, with the transaction rolled back and
//! nothing written: a kind outside the record kinds (the membership kinds,
//! `returned`, `home` and `standing` among them), an origin
//! `prepare_mutation` would move into `native_origin`, and a meta that
//! names a key other than the one the open read (a rekey or a rotation
//! another connection committed, which `_signing_key` would follow).

use std::fs;
use std::io;
use std::path::Path;

use rusqlite::{Connection, OptionalExtension};
use zeroize::Zeroizing;

use super::create::{with_suffix, Provider};
use super::open::{replace_atomic, WritableStore};
use crate::log::{fingerprint, head_file_text, Key, KeyRing, LogRow};
use crate::pyvalue::{PyError, PyValue};
use crate::unicode;

/// The kinds `mutate` writes: every record mutation kind of
/// `MUTATION_KINDS` outside the membership tables
/// (`RECORD_MUTATION_KINDS`). On none of them does `_mutate` do more than
/// the row and its table effects, and name the records whose `returned`
/// marks a `memory_put` or `memory_delete` may change.
pub const KINDS: [&str; 14] = [
    "memory_put",
    "memory_delete",
    "tombstone_put",
    "tombstone_delete",
    "episode_put",
    "episode_delete",
    "verification_put",
    "verification_delete",
    "conflict_put",
    "conflict_delete",
    "import_put",
    "import_delete",
    "quarantine_put",
    "quarantine_delete",
];

/// What the reference would do that this unit does not port.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WriteNotPorted {
    /// A kind outside [`KINDS`]: a membership kind (`home`, `standing`,
    /// `returned`, `native_member` and the rest, each with rules of its
    /// own), or no mutation kind.
    Kind(String),
    /// A record whose origin holds a surrogate: `prepare_mutation` moves
    /// it into the payload's `native_origin`, which needs a revision-5
    /// writer check.
    NativeOrigin,
    /// meta names another key than the one the open read: a rekey or a
    /// rotation another connection committed since (`_signing_key` would
    /// follow it, finishing the rotation's rename).
    Rekeyed,
    /// A membership row or key holding a lone surrogate: the reference
    /// writes it as a `native_member` row beside its legacy projection
    /// (`store_native`), left to a later unit.
    NativeMember,
    /// A value of a membership read this build does not carry the way the
    /// reference does (named): `bytes` from a BLOB column, or a sort over a
    /// value outside None, bool, int, float and surrogate-free str.
    Value(&'static str),
}

impl WriteNotPorted {
    pub fn name(&self) -> &'static str {
        match self {
            WriteNotPorted::Kind(_) => "kind",
            WriteNotPorted::NativeOrigin => "native_origin",
            WriteNotPorted::Rekeyed => "rekeyed",
            WriteNotPorted::NativeMember => "native_member",
            WriteNotPorted::Value(_) => "value",
        }
    }
}

/// Why a write wrote nothing.
#[derive(Debug)]
pub enum WriteError {
    /// [`WriteNotPorted`]: the reference would have written; this build
    /// leaves the store as it was.
    NotPorted(WriteNotPorted),
    /// The reference's own refusal: the exception `_mutate` raises (its
    /// class, and `str(exc)` where the class is one the fold reports
    /// as `payload_invalid` or `UnknownFieldsError`; a pydantic
    /// ValidationError's text is its first line). The transaction is
    /// rolled back.
    Refused {
        class: &'static str,
        text: Option<String>,
    },
    /// The payload is not an object (the reference's API takes a mapping).
    NotAnObject,
    /// meta's minimum writer revision is above this build's
    /// (`require_current_writer`): the connection is held to reading.
    LaterWriter,
    /// SQLite refused (busy past the timeout, a full disk, a schema that
    /// no longer fits the statements); rolled back.
    Sqlite(rusqlite::Error),
    /// The clock could not be read; rolled back.
    Clock(io::Error),
    /// `_require_open`: SQLite ended the transaction after an error inside
    /// it (sqlite3.OperationalError in the reference); nothing it wrote was
    /// kept. Holds the store's path.
    RolledBack(String),
}

impl WriteError {
    pub fn class(&self) -> &'static str {
        match self {
            WriteError::NotPorted(_) => "not_ported",
            WriteError::Refused { .. } => "refused",
            WriteError::NotAnObject => "not_an_object",
            WriteError::LaterWriter => "later_writer",
            WriteError::Sqlite(_) => "sqlite",
            WriteError::Clock(_) => "clock",
            WriteError::RolledBack(_) => "rolled_back",
        }
    }
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WriteError::NotPorted(WriteNotPorted::Kind(kind)) => {
                write!(f, "the native build does not write {kind:?} rows")
            }
            WriteError::NotPorted(WriteNotPorted::NativeOrigin) => {
                f.write_str("the native build does not write a native-path origin")
            }
            WriteError::NotPorted(WriteNotPorted::Rekeyed) => f.write_str(
                "the store was rekeyed since it was opened; the native build does not follow",
            ),
            WriteError::NotPorted(WriteNotPorted::NativeMember) => {
                f.write_str("the native build does not write a native-path membership row")
            }
            WriteError::NotPorted(WriteNotPorted::Value(what)) => {
                write!(
                    f,
                    "the native build does not read {what} in a membership row"
                )
            }
            WriteError::Refused {
                class,
                text: Some(text),
            } => write!(f, "{class}: {text}"),
            WriteError::Refused { class, text: None } => f.write_str(class),
            WriteError::NotAnObject => f.write_str("a mutation's payload is an object"),
            WriteError::LaterWriter => {
                f.write_str("the store now needs a later writer; it is held to reading")
            }
            WriteError::Sqlite(e) => write!(f, "{e}"),
            WriteError::Clock(e) => write!(f, "the clock could not be read: {e}"),
            WriteError::RolledBack(path) => write!(
                f,
                "the write transaction on store {path} was rolled back by an error inside it; \
                 nothing it wrote was kept"
            ),
        }
    }
}

impl std::error::Error for WriteError {}

impl From<rusqlite::Error> for WriteError {
    fn from(e: rusqlite::Error) -> Self {
        WriteError::Sqlite(e)
    }
}

/// What became of the head checkpoint after the commit.
#[derive(Debug)]
pub enum HeadMove {
    /// Written: the head names the row.
    Moved,
    /// Left: it named this row or a later one already (only forward).
    Ahead,
    /// Not written; the commit stands (`_move_head` logs the OSError and
    /// the call succeeds). The head is behind, which verifies ok.
    Failed(io::Error),
}

/// A committed mutation.
#[derive(Debug)]
pub struct Written {
    /// The mutation's row.
    pub row: LogRow,
    /// The `returned` rows its commit appended after it, puts then
    /// deletions.
    pub returned: Vec<LogRow>,
    pub head: HeadMove,
}

/// A point between two of a write's steps (the kill test stops the
/// process there).
#[doc(hidden)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WriteBoundary {
    /// `BEGIN IMMEDIATE` taken; nothing written.
    Begun,
    /// The log row inserted, in the open transaction.
    Appended,
    /// The tables and the index changed, in the open transaction.
    Applied,
    /// Committed; the head not yet moved.
    Committed,
    /// The head's lock held, the head not yet replaced.
    HeadLocked,
    /// The head moved (or left).
    HeadMoved,
}

impl WriteBoundary {
    pub const ALL: [WriteBoundary; 6] = [
        WriteBoundary::Begun,
        WriteBoundary::Appended,
        WriteBoundary::Applied,
        WriteBoundary::Committed,
        WriteBoundary::HeadLocked,
        WriteBoundary::HeadMoved,
    ];
}

pub(super) const SUPERSEDES: &str = "supersedes";

/// `store_returned.Before`.
pub(super) struct Before {
    pub(super) record_id: String,
    active: bool,
    superseded: Vec<String>,
}

pub(super) fn targets(conn: &Connection, source: &str) -> rusqlite::Result<Vec<String>> {
    let mut statement =
        conn.prepare("SELECT target_id FROM memory_links WHERE source_id = ? AND type = ?")?;
    let mut out: Vec<String> = statement
        .query_map([source, SUPERSEDES], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<_>>()?;
    out.sort();
    out.dedup();
    Ok(out)
}

pub(super) fn is_active(conn: &Connection, id: &str) -> rusqlite::Result<bool> {
    conn.prepare("SELECT 1 FROM memories WHERE id = ?")?
        .exists([id])
}

/// `store_returned.before`.
pub(super) fn before(
    conn: &Connection,
    kind: &str,
    payload: &PyValue,
) -> Result<Option<Before>, WriteError> {
    let id = match kind {
        "memory_put" => payload.get("memory").and_then(|m| match m {
            PyValue::Dict(_) => m.get("id"),
            _ => None,
        }),
        "memory_delete" => payload.get("id"),
        _ => None,
    };
    // A str that SQLite cannot take (a surrogate) fails the apply after it.
    let Some(PyValue::Str(id)) = id else {
        return Ok(None);
    };
    // `_is_active` binds it: a surrogate raises before anything is written.
    let Some(id) = unicode::to_string(id) else {
        return Err(WriteError::Refused {
            class: "UnicodeEncodeError",
            text: None,
        });
    };
    let active = is_active(conn, &id)?;
    let superseded = if active {
        targets(conn, &id)?
    } else {
        Vec::new()
    };
    Ok(Some(Before {
        record_id: id,
        active,
        superseded,
    }))
}

/// `store_returned.candidates`.
pub(super) fn candidates(
    conn: &Connection,
    kind: &str,
    state: &Before,
) -> rusqlite::Result<Vec<String>> {
    if kind == "memory_delete" {
        if !state.active {
            return Ok(Vec::new());
        }
        let mut out = state.superseded.clone();
        out.push(state.record_id.clone());
        return Ok(out);
    }
    let after = targets(conn, &state.record_id)?;
    let mut out: Vec<String> = state
        .superseded
        .iter()
        .filter(|t| !after.contains(t))
        .chain(after.iter().filter(|t| !state.superseded.contains(t)))
        .cloned()
        .collect();
    out.sort();
    Ok(out)
}

pub(super) fn has_surrogates(value: &PyValue) -> bool {
    match value {
        // `native_text.has_surrogates` asks `isinstance(value, str)`: a
        // `PurePosixPath` is not one.
        PyValue::Str(s) => s.iter().any(|c| (0xD800..=0xDFFF).contains(c)),
        PyValue::Dict(items) => items
            .iter()
            .any(|(k, v)| has_surrogates(k) || has_surrogates(v)),
        PyValue::List(items) | PyValue::Tuple(items) => items.iter().any(has_surrogates),
        _ => false,
    }
}

/// `native_text.mutation_payload`'s question: whether it would project
/// the record's origin into `native_origin`.
pub(super) fn native_origin(kind: &str, payload: &PyValue) -> bool {
    let field = match kind {
        "memory_put" => "memory",
        "tombstone_put" => "tombstone",
        "episode_put" => "episode",
        _ => return false,
    };
    match payload.get(field) {
        Some(record @ PyValue::Dict(_)) => match record.get("origin") {
            Some(origin @ PyValue::Dict(_)) => has_surrogates(origin),
            _ => false,
        },
        _ => false,
    }
}

pub(super) fn last_row(conn: &Connection) -> rusqlite::Result<Option<LogRow>> {
    conn.prepare(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log \
         ORDER BY seq DESC LIMIT 1",
    )?
    .query_row([], |r| {
        Ok(LogRow {
            seq: r.get(0)?,
            ts: r.get(1)?,
            session: r.get(2)?,
            kind: r.get(3)?,
            payload: r.get(4)?,
            prev_mac: r.get(5)?,
            mac: r.get(6)?,
        })
    })
    .optional()
}

/// `KeyRing.load_current` as `_signing_key` reads it: None when there is
/// no key file or it cannot be read (`KeyUnreadableError`).
pub(super) fn load_current(keys_dir: &Path, store_id: &str) -> Option<Key> {
    let bytes = Zeroizing::new(fs::read(keys_dir.join(format!("{store_id}.key"))).ok()?);
    Some(Key::new(bytes.to_vec()))
}

/// Whether `<id>.key.pending` holds the key meta names (32 bytes whose
/// fingerprint is `recorded`): what `finish_rotation` would rename. A
/// pending key meta does not name is a rotation that did not commit, and
/// the reference signs with the open's key past it.
pub(super) fn pending_is(keys_dir: &Path, store_id: &str, recorded: &str) -> bool {
    fs::read(keys_dir.join(format!("{store_id}.key.pending")))
        .map(Zeroizing::new)
        .is_ok_and(|b| b.len() == 32 && fingerprint(&b) == recorded)
}

pub(super) fn py_error(e: PyError) -> WriteError {
    WriteError::Refused {
        class: e.name(),
        text: None,
    }
}

impl WritableStore {
    /// `Store._mutate(tx, kind, payload, session=session)` in a
    /// transaction of its own (`with store._transaction() as tx`), with
    /// the `returned` rows its commit settles, then the head's move: a
    /// [`super::tx::Transaction`] of one mutation. `provider`'s log clock
    /// stamps the rows and the head, its wall clock the marks.
    pub fn mutate(
        &mut self,
        kind: &str,
        payload: &PyValue,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Written, WriteError> {
        self.mutate_with(kind, payload, session, provider, &mut |_| {})
    }

    /// [`WritableStore::mutate`] with `hook` shown each
    /// [`WriteBoundary`] as it is passed.
    #[doc(hidden)]
    pub fn mutate_with(
        &mut self,
        kind: &str,
        payload: &PyValue,
        session: Option<&str>,
        provider: &mut dyn Provider,
        hook: &mut dyn FnMut(WriteBoundary),
    ) -> Result<Written, WriteError> {
        // Refused before the transaction is begun, as before it existed.
        if !KINDS.contains(&kind) {
            return Err(WriteError::NotPorted(WriteNotPorted::Kind(kind.to_owned())));
        }
        if !matches!(payload, PyValue::Dict(_)) {
            return Err(WriteError::NotAnObject);
        }
        if native_origin(kind, payload) {
            return Err(WriteError::NotPorted(WriteNotPorted::NativeOrigin));
        }
        let mut tx = self.begin_with(provider, hook)?;
        let row = tx.mutate(kind, payload, session)?;
        let committed = tx.commit()?;
        let returned = committed.rows.into_iter().skip(1).collect();
        // A transaction that kept a row moves the head: always Some here.
        let head = committed.head.unwrap_or(HeadMove::Ahead);
        Ok(Written {
            row,
            returned,
            head,
        })
    }
}

/// `KeyRing.write_head(seq=, mac=)` as `_move_head` calls it: under the
/// head's lock (`<id>.head.lock`, kept), only forward, the clock read
/// only when it writes; an OSError leaves the head where it was and the
/// commit stands.
pub(super) fn move_head(
    path: &Path,
    row: &LogRow,
    provider: &mut dyn Provider,
    hook: &mut dyn FnMut(WriteBoundary),
) -> HeadMove {
    let lock = match open_lock(&with_suffix(path, ".lock")) {
        Ok(lock) => lock,
        Err(e) => return HeadMove::Failed(e),
    };
    if let Err(e) = lock.lock() {
        return HeadMove::Failed(e);
    }
    hook(WriteBoundary::HeadLocked);
    let ring = KeyRing {
        head_file: fs::read(path).ok(),
        ..KeyRing::default()
    };
    // `read_head` catches OSError, ValueError, KeyError and TypeError: a
    // head that does not read is written over. OverflowError and
    // RecursionError escape it and `_move_head`, raising out of the
    // committed call; native leaves the head and reports it.
    match ring.read_head() {
        Ok(Some(current)) => {
            let ahead = match &current.seq {
                PyValue::Int(text) => text
                    .parse::<i128>()
                    .map_or(!text.starts_with('-'), |s| s >= i128::from(row.seq)),
                _ => false,
            };
            if ahead {
                return HeadMove::Ahead;
            }
        }
        Ok(None) => {}
        Err(e) => {
            return HeadMove::Failed(io::Error::other(format!(
                "the head did not read ({})",
                e.name()
            )))
        }
    }
    let ts = match provider.now() {
        Ok(now) => now.isoformat_utc(),
        Err(e) => return HeadMove::Failed(e),
    };
    let text = head_file_text(row.seq, &row.mac, &ts);
    let out = replace_retrying(path, text.as_bytes());
    drop(lock);
    match out {
        Ok(()) => HeadMove::Moved,
        Err(e) => HeadMove::Failed(e),
    }
}

/// `_fsutil.flock_excl`'s lock file: created 0600 or opened, kept.
fn open_lock(path: &Path) -> io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    options.open(path)
}

/// `atomic_write_bytes`: the temporary file written and renamed into
/// place; `replace_atomic` retries only the rename, on Windows.
pub(crate) fn replace_retrying(path: &Path, data: &[u8]) -> io::Result<()> {
    replace_atomic(path, data)
}
