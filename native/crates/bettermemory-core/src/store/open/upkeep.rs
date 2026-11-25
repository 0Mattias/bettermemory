//! The owner's writing open's upkeep with its key present
//! (`store_open._open_connected`, `store_open.py` 94-108): a foreign tail
//! closed (`Store._close_foreign_tail`, `store.py` 804-854), then the
//! origin anchor settled (`Store._settle_origin` 943-992, with
//! `_record_origin` 927-941 and `_name_origin_key` 994-1022).
//!
//! Each step reads again what the open decided by, as the reference's
//! does, and logs instead of failing the open; what it logs is returned
//! as data ([`Warning`]), never printed. `Store._forget_segments`
//! (920-925) drops a connection's cached segment map and anchor check: the
//! native store keeps neither (a provenance read builds its own), so there
//! is nothing to drop.

use std::fs;
use std::path::Path;

use rusqlite::Connection;

use super::{not_ported, Boundary, NotPorted, OpenError, WritableStore};
use crate::json;
use crate::log::ring::{KeyFiles, RingError};
use crate::log::segments::{chain_segments, ring_keys};
use crate::log::{self, fingerprint, HistoryOrigin, Key, KeyRing, LogRow, LogSource, VerifyError};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::store::create::Provider;
use crate::store::exception::{ExceptionFamily, StoreException};
use crate::store::keys::load_key_ring;
use crate::store::provenance::ConnSource;
use crate::store::restore_rows::Arg as RestoreArg;
use crate::store::write::WriteError;
use crate::store::{log_row, StoreError, LOG_SELECT};
use crate::unicode;

/// The exception a warning names in its parentheses: its class and, for
/// one SQLite raised, its error name (`sqlite_errorname`). Its text is
/// not kept: SQLite and the C library word it, by release and system.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Cause {
    pub class: String,
    pub sqlite: Option<&'static str>,
}

impl Cause {
    fn sqlite(error: &rusqlite::Error) -> Self {
        let sqlite = match error {
            rusqlite::Error::SqliteFailure(e, _) => {
                crate::store::reads::sqlite_error_name(i64::from(e.extended_code))
            }
            _ => None,
        };
        Cause {
            class: crate::store::upgrade::sqlite_class(error).to_owned(),
            sqlite,
        }
    }

    fn named(class: &str) -> Self {
        Cause {
            class: class.to_owned(),
            sqlite: None,
        }
    }

    fn os(error: &std::io::Error) -> Self {
        Self::named(&StoreException::os(error, None, None).exception.class)
    }

    /// What reading a log row raised: an SQLite error, or a text that is
    /// not UTF-8 (`sqlite3`'s OperationalError, raised by the module; a
    /// connection's read returns nothing else).
    pub(super) fn store(error: &StoreError) -> Self {
        match error {
            StoreError::Sqlite(e) => Self::sqlite(e),
            _ => Self::named("OperationalError"),
        }
    }
}

/// One warning the upkeep logged (`log.warning` on `bettermemory.store`),
/// or `restore_rows.settle` after it (on `bettermemory.restore_rows`).
#[derive(Clone, Debug, PartialEq)]
pub enum Warning {
    /// The tail's transaction failed: `named` is the last name read.
    TailNotClosed {
        named: PyStr,
        own: String,
        cause: Cause,
    },
    /// A rekey row at `seq` closed the tail `named` opened.
    TailClosed { named: PyStr, seq: i64, own: String },
    /// The segments could not be read; no anchor.
    OriginUnread(Cause),
    /// The held segment's first row (at `seq`) does not verify.
    OriginUnverified { seq: i64 },
    /// The anchor could not be written.
    OriginNotWritten(Cause),
    /// The anchored row could not be read to name its key.
    KeyUnread(Cause),
    /// The anchor's key could not be named.
    KeyNotNamed(Cause),
    /// `_reclaim`'s transaction failed: meta still names `meta`.
    MetaNotSetBack {
        meta: String,
        own: String,
        cause: Cause,
    },
    /// `_reclaim` kept this host's key `own` over meta's `meta`, with a
    /// rekey row at `seq` when one was due; `foreign` lists the segments
    /// no held key signed.
    Reclaimed {
        meta: String,
        own: String,
        seq: Option<i64>,
        foreign: PyStr,
    },
    /// `_open_rekeyed` wrote the key `new` over `previous`'s segment.
    Rekeyed { previous: PyStr, new: String },
    /// What `restore_rows.settle` logged; its own arguments are
    /// `restore_rows::Warning::args`.
    Restore(crate::store::restore_rows::Warning),
}

/// One argument of a warning, as the logger receives it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Arg<'a> {
    /// The store's path (`self._path`).
    Path,
    Text(PyStr),
    Int(i64),
    Cause(&'a Cause),
}

fn head16(text: &[u32]) -> PyStr {
    text.iter().copied().take(16).collect()
}

impl Warning {
    /// The reference's format string.
    pub fn format(&self) -> &'static str {
        match self {
            Warning::TailNotClosed { .. } => {
                "store %s: the log's last segment is under key %s, not this store's key %s, \
                 and it could not be closed (%s); rows written here will not verify until it is"
            }
            Warning::TailClosed { .. } => {
                "store %s: the log's last segment, opened by a rekey row this host did not \
                 write, is under key %s; its rows read unaccounted and a rekey row at seq %d \
                 returns the chain to this store's key %s. `bettermemory log verify` reports \
                 the segment"
            }
            Warning::OriginUnread(_) => {
                "origin anchor for %s not written: the log could not be read (%s)"
            }
            Warning::OriginUnverified { .. } => {
                "store %s: the first row of the earliest segment this host holds (seq %d) does \
                 not verify under its key; no origin anchor is written. `bettermemory log \
                 verify` reports the row"
            }
            Warning::OriginNotWritten(_) => "origin anchor for %s not written: %s",
            Warning::KeyUnread(_) => {
                "store %s: the origin anchor's key not named: the log could not be read (%s)"
            }
            Warning::KeyNotNamed(_) => "store %s: the origin anchor's key not named: %s",
            Warning::MetaNotSetBack { .. } => {
                "store %s: meta names key %s but this host's key %s signs the log; the store \
                 opens with that key, and meta could not be set back (%s)"
            }
            Warning::Reclaimed { .. } => {
                "store %s: meta named key %s, but this host's key %s signs the log, so it stays \
                 the store's key and no new key is written; %s. Rows no key on this host \
                 signed, after rows it did: %s. They read unaccounted, and `bettermemory log \
                 verify` reports them"
            }
            Warning::Rekeyed { .. } => {
                "store %s was opened without its key (fingerprint %s); a new key %s was written \
                 and the log rekeyed"
            }
            Warning::Restore(w) => w.format(),
        }
    }

    /// The format's arguments, in order (a name cut to 16 code points, as
    /// `[:16]` cuts it).
    pub fn args(&self) -> Vec<Arg<'_>> {
        match self {
            Warning::TailNotClosed { named, own, cause } => vec![
                Arg::Path,
                Arg::Text(head16(named)),
                Arg::Text(head16(&unicode::cps(own))),
                Arg::Cause(cause),
            ],
            Warning::TailClosed { named, seq, own } => vec![
                Arg::Path,
                Arg::Text(head16(named)),
                Arg::Int(*seq),
                Arg::Text(head16(&unicode::cps(own))),
            ],
            Warning::OriginUnverified { seq } => vec![Arg::Path, Arg::Int(*seq)],
            Warning::OriginUnread(cause)
            | Warning::OriginNotWritten(cause)
            | Warning::KeyUnread(cause)
            | Warning::KeyNotNamed(cause) => vec![Arg::Path, Arg::Cause(cause)],
            Warning::MetaNotSetBack { meta, own, cause } => vec![
                Arg::Path,
                Arg::Text(head16(&unicode::cps(meta))),
                Arg::Text(head16(&unicode::cps(own))),
                Arg::Cause(cause),
            ],
            Warning::Reclaimed {
                meta,
                own,
                seq,
                foreign,
            } => vec![
                Arg::Path,
                Arg::Text(head16(&unicode::cps(meta))),
                Arg::Text(head16(&unicode::cps(own))),
                Arg::Text(unicode::cps(&match seq {
                    None => "meta is set back to it".to_owned(),
                    Some(seq) => format!("a rekey row at seq {seq} returns the chain to it"),
                })),
                Arg::Text(if foreign.is_empty() {
                    unicode::cps("none")
                } else {
                    foreign.clone()
                }),
            ],
            Warning::Rekeyed { previous, new } => vec![
                Arg::Path,
                Arg::Text(head16(previous)),
                Arg::Text(head16(&unicode::cps(new))),
            ],
            // The record's path and a value as the text `%s` makes of them.
            Warning::Restore(w) => w
                .args()
                .into_iter()
                .map(|a| match a {
                    RestoreArg::Store => Arg::Path,
                    RestoreArg::Path(p) => Arg::Text(crate::fsutil::fsdecode(p.as_os_str())),
                    RestoreArg::Value(v) => {
                        Arg::Text(crate::pyvalue::py_str(v).unwrap_or_default())
                    }
                    RestoreArg::Text(t) => Arg::Text(unicode::cps(&t)),
                    RestoreArg::Int(n) => Arg::Int(n),
                    RestoreArg::Cause(c) => Arg::Cause(c),
                })
                .collect(),
        }
    }
}

/// Why `last_rekey_name` gave no name: a row that does not read, or the
/// reference's own exception, RecursionError from a payload nested past
/// the JSON limit (the only one `_rekey_fingerprints` lets through), with
/// `json.loads`'s text for that payload.
pub(super) enum NameError {
    Store(StoreError),
    Recursion(&'static str),
}

impl From<rusqlite::Error> for NameError {
    fn from(e: rusqlite::Error) -> Self {
        NameError::Store(StoreError::Sqlite(e))
    }
}

/// `log.last_rekey_name`: the name the last `rekey` row (seq 1 on) gives
/// the new key, as code points (any text a payload holds, a lone
/// surrogate among them); the empty text when it names none; None with
/// no `rekey` row.
pub(super) fn last_rekey_name(conn: &Connection) -> Result<Option<PyStr>, NameError> {
    let mut statement = conn.prepare(&format!(
        "{LOG_SELECT} WHERE kind = 'rekey' AND seq > 0 ORDER BY seq DESC LIMIT 1"
    ))?;
    let mut rows = statement.query([])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    let row = log_row(row).map_err(NameError::Store)?;
    match log::rekey_fingerprints(&row) {
        Ok(names) => Ok(Some(names.map_or_else(Vec::new, |(_, new)| new))),
        Err(_) => Err(NameError::Recursion(json::reading_recursion_message(
            &unicode::cps(&row.payload),
        ))),
    }
}

/// The open's RecursionError, worded as `json.loads` words it for `text`;
/// what the upkeep logged before it is joined by the open.
pub(super) fn recursion(text: &'static str) -> OpenError {
    OpenError::Recursion {
        text,
        logged: Vec::new(),
    }
}

/// `read_origin`'s RecursionError: the anchor's text, read again.
pub(super) fn anchor_recursion(keys_dir: &Path, store_id: &str) -> OpenError {
    let text = fs::read(keys_dir.join(format!("{store_id}.origin")))
        .map(|bytes| String::from_utf8_lossy(&bytes).into_owned())
        .unwrap_or_default();
    recursion(json::reading_recursion_message(&unicode::cps(&text)))
}

/// `chain_segments`'s RecursionError: the first rekey payload, in seq
/// order, that nests past the limit (the read that raised).
pub(super) fn segments_recursion(conn: &Connection) -> OpenError {
    let rows = ConnSource(conn).rekeys().unwrap_or_default();
    let nested = rows
        .iter()
        .map(|r| unicode::cps(&r.payload))
        .find(|points| matches!(json::loads(points), Err(PyError::RecursionError)));
    recursion(nested.map_or("maximum recursion depth exceeded", |points| {
        json::reading_recursion_message(&points)
    }))
}

/// Whether the upkeep raises RecursionError, before the steps after it,
/// though the last rekey row reads: the anchor nests past the JSON limit,
/// or, with no anchor, a rekey payload `chain_segments` reads does.
pub(super) fn settle_raises(
    conn: &Connection,
    keys_dir: &Path,
    store_id: &str,
    ring: &KeyRing,
    key: &Key,
) -> bool {
    let Ok(files) = KeyFiles::new(keys_dir, &PyValue::str(store_id)) else {
        return false;
    };
    let own = unicode::cps(&fingerprint(key.bytes()));
    match files.read_origin() {
        Ok(Some(_)) => false,
        Ok(None) => matches!(
            chain_segments(&ConnSource(conn), ring, &own, Some(key)),
            Err(VerifyError::Py(_))
        ),
        Err(_) => true,
    }
}

pub(super) fn files(store: &WritableStore) -> Result<KeyFiles, OpenError> {
    KeyFiles::new(store.keys_dir(), &PyValue::str(store.store_id()))
        .map_err(|_| not_ported(NotPorted::Unreadable("the key ring")))
}

fn ring(store: &WritableStore) -> Result<KeyRing, OpenError> {
    load_key_ring(store.keys_dir(), store.store_id())
        .map_err(|_| not_ported(NotPorted::Unreadable("the key ring")))
}

/// An anchor write's exception: an OSError is logged; RecursionError
/// from the anchor read again under its lock raises out of the open (the
/// anchor's writes read no key).
fn ring_cause(store: &WritableStore, error: RingError) -> Result<Cause, OpenError> {
    match error {
        RingError::Os(e) => Ok(Cause::os(&e)),
        RingError::Py(_) | RingError::KeyUnreadable(_) => {
            Err(anchor_recursion(store.keys_dir(), store.store_id()))
        }
    }
}

/// What the tail's transaction raised that `_close_foreign_tail` logs
/// (`sqlite3.Error`, `OSError`); anything else raises out of the open.
fn tail_cause(error: &WriteError) -> Option<Cause> {
    match error {
        WriteError::Sqlite(e) => Some(Cause::sqlite(e)),
        // The log's last row holds a text that is not UTF-8 (sqlite3's
        // fetch raises it), or meta asks for a later writer
        // (`require_current_writer`): no SQLite error name.
        WriteError::RolledBack(_)
        | WriteError::LaterWriter { .. }
        | WriteError::Refused {
            class: "OperationalError",
            ..
        } => Some(Cause::named("OperationalError")),
        WriteError::Clock(e) => Some(Cause::os(e)),
        WriteError::Callback(e) => match e.family {
            ExceptionFamily::Os | ExceptionFamily::Sqlite | ExceptionFamily::Operational => {
                Some(Cause::named(&e.exception.class))
            }
            _ => None,
        },
        _ => None,
    }
}

/// How the tail's transaction failed: logged (`sqlite3.Error`, `OSError`),
/// or raised out of the open.
pub(super) enum TailFailure {
    Logged(Cause),
    Raised(OpenError),
}

impl From<WriteError> for TailFailure {
    fn from(error: WriteError) -> Self {
        match (tail_cause(&error), error) {
            (Some(cause), _) => TailFailure::Logged(cause),
            // The rekey row's seq past an SQLite INTEGER: sqlite3's text.
            (
                None,
                WriteError::Refused {
                    class: "OverflowError",
                    ..
                },
            ) => TailFailure::Raised(OpenError::Overflow),
            (None, error) => TailFailure::Raised(OpenError::Write(Box::new(error))),
        }
    }
}

/// `Store._close_foreign_tail`: when the last rekey row names another key
/// than this store's, one transaction that reads the name again under the
/// lock and, when it still names another, appends the rekey row from that
/// name to this key (`session` None). With no `provider` (the entries
/// that take the clock shared) a tail is left as it is: the decision
/// refused one, so only another opener's rekey since then is.
pub(super) fn close_foreign_tail(
    store: &mut WritableStore,
    provider: Option<&mut dyn Provider>,
    hook: &mut dyn FnMut(Boundary),
    log: &mut Vec<Warning>,
) -> Result<(), OpenError> {
    let own = fingerprint(store.key.bytes());
    let own_points = unicode::cps(&own);
    // Read again after the engine's and the binding's writes, as the
    // reference reads it there: the decision refused a row that does not
    // read, so only another writer's since then is one.
    let mut named = match last_rekey_name(&store.conn) {
        Ok(named) => named,
        Err(NameError::Recursion(text)) => return Err(recursion(text)),
        Err(NameError::Store(_)) => return Err(not_ported(NotPorted::Unreadable("a rekey row"))),
    };
    if named.as_ref().is_none_or(|n| *n == own_points) {
        return Ok(());
    }
    let Some(provider) = provider else {
        return Ok(());
    };
    hook(Boundary::TailNamed);
    let mut row: Option<LogRow> = None;
    let outcome = (|| -> Result<(), TailFailure> {
        let mut tx = store.begin(provider)?;
        // Another opener may have closed it while this one waited.
        named = match last_rekey_name(tx.connection()) {
            Ok(named) => named,
            Err(NameError::Store(e)) => return Err(TailFailure::Logged(Cause::store(&e))),
            Err(NameError::Recursion(text)) => return Err(TailFailure::Raised(recursion(text))),
        };
        if let Some(name) = named.as_ref().filter(|n| **n != own_points) {
            let payload = PyValue::Dict(vec![
                (PyValue::str("old_fingerprint"), PyValue::Str(name.clone())),
                (PyValue::str("new_fingerprint"), PyValue::str(&own)),
            ]);
            row = Some(tx.append_at(&unicode::cps(log::REKEY), &payload, None, None)?);
        }
        tx.commit()?;
        Ok(())
    })();
    match outcome {
        Ok(()) => {}
        Err(TailFailure::Logged(cause)) => {
            log.push(Warning::TailNotClosed {
                named: named.unwrap_or_default(),
                own,
                cause,
            });
            return Ok(());
        }
        Err(TailFailure::Raised(error)) => return Err(error),
    }
    if let (Some(row), Some(named)) = (row, named) {
        log.push(Warning::TailClosed {
            named,
            seq: row.seq,
            own,
        });
    }
    Ok(())
}

/// `Store._settle_origin`: an anchor that names no key gets one
/// (`_name_origin_key`); with no anchor, one is written on the first row of
/// the earliest segment this host holds a key for, when that row verifies
/// under it.
pub(super) fn settle_origin(
    store: &WritableStore,
    log: &mut Vec<Warning>,
) -> Result<(), OpenError> {
    let files = files(store)?;
    let existing = files
        .read_origin()
        .map_err(|_| anchor_recursion(store.keys_dir(), store.store_id()))?;
    if let Some(existing) = existing {
        if existing.key_fingerprint.is_none() {
            name_origin_key(store, &files, &existing, log)?;
        }
        return Ok(());
    }
    let ring = ring(store)?;
    let own = unicode::cps(&fingerprint(store.key.bytes()));
    let segments = match chain_segments(&ConnSource(&store.conn), &ring, &own, Some(&store.key)) {
        Ok(segments) => segments,
        Err(VerifyError::Source(e)) => {
            log.push(Warning::OriginUnread(Cause::store(&e)));
            return Ok(());
        }
        Err(VerifyError::Py(_)) => return Err(segments_recursion(&store.conn)),
    };
    let Some(held) = segments.iter().find(|s| s.held()) else {
        return Ok(());
    };
    let (Some(key), Some(first)) = (&held.key, &held.first_row) else {
        return Ok(());
    };
    if !log::row_verifies(key.bytes(), first) {
        log.push(Warning::OriginUnverified {
            seq: held.first_seq,
        });
        return Ok(());
    }
    record_origin(store, &files, first, key, log)
}

/// `Store._record_origin`: the anchor on `row`, naming `key`, unless one
/// is there; an OSError is logged and the open goes on.
pub(super) fn record_origin(
    store: &WritableStore,
    files: &KeyFiles,
    row: &LogRow,
    key: &Key,
    log: &mut Vec<Warning>,
) -> Result<(), OpenError> {
    if let Err(error) = files.write_origin(row.seq, &row.mac, Some(&fingerprint(key.bytes()))) {
        log.push(Warning::OriginNotWritten(ring_cause(store, error)?));
    }
    Ok(())
}

/// `Store._name_origin_key`: the anchor checked against its row under the
/// store's key, then every held key (`check_origin`, which reads the
/// anchor again through the same reader); when it holds, the key that
/// verified the row is named on it. Its seq and MAC never move.
fn name_origin_key(
    store: &WritableStore,
    files: &KeyFiles,
    origin: &HistoryOrigin,
    log: &mut Vec<Warning>,
) -> Result<(), OpenError> {
    let ring = ring(store)?;
    let anchor = files
        .read_origin()
        .map_err(|_| anchor_recursion(store.keys_dir(), store.store_id()))?;
    let row = match &anchor {
        Some(a) => match ConnSource(&store.conn).at_seq(a.seq) {
            Ok(row) => row,
            Err(e) => {
                log.push(Warning::KeyUnread(Cause::store(&e)));
                return Ok(());
            }
        },
        None => None,
    };
    let check = log::check_origin_with_keys(
        anchor.as_ref(),
        row.as_ref(),
        ring_keys(&ring, Some(&store.key)),
        log::row_verifies,
    );
    let (true, Some(signer)) = (check.holds(), check.signer) else {
        return Ok(());
    };
    if let Err(error) = files.name_origin_key(origin, &signer) {
        log.push(Warning::KeyNotNamed(ring_cause(store, error)?));
    }
    Ok(())
}
