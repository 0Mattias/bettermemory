//! The owner's writing open of a store whose key is missing or foreign
//! (`store_open._open_connected`, `store_open.py` 117-143): a key in hand
//! that signs a segment of the log is kept (`Store._reclaim`, `store.py`
//! 856-918); otherwise the key in hand is retired for a new one and the log
//! rekeyed (`store_open._open_rekeyed`, 206-249), and this machine's history
//! anchored at the `rekey` row when no anchor is there
//! (`Store._take_over_origin`, 1024-1042).
//!
//! The dispatch's question is asked when the open decides, with nothing
//! written ([`decide`]); the writes run after the binding, in the
//! reference's order. What is logged is returned as data
//! ([`super::UpkeepWarning`]). The new key's bytes reach only its file.

use rusqlite::types::ValueRef;
use rusqlite::Connection;

use super::upkeep::{self, Cause, NameError, TailFailure, Warning};
use super::{not_ported, Boundary, NotPorted, OpenError, WritableStore};
use crate::log::ring::{KeyFiles, RingError};
use crate::log::segments::{chain_segments, Segment};
use crate::log::{fingerprint, Key, KeyRing, LogRow, VerifyError, REKEY};
use crate::pyvalue::{PyStr, PyValue};
use crate::store::create::Provider;
use crate::store::provenance::ConnSource;
use crate::store::rotation::{signs_a_segment, SegmentStart};
use crate::store::write::WriteError;
use crate::store::{sqlite_text_error, StoreError, LOG_SELECT};
use crate::unicode;

/// What the open does with its key, decided before anything is written.
pub(super) enum Dispatch {
    /// The key meta names (`KEY_PRESENT`): the upkeep (`open/upkeep.rs`).
    Present,
    /// The key in hand signs a segment (`_reclaim`): `tail` is the last
    /// segment's key as the segment map names it (this key's with no
    /// segment), `foreign` the warning's list of foreign segments.
    Reclaim { tail: PyStr, foreign: PyStr },
    /// `_open_rekeyed`: `previous` is the last segment's key as the log
    /// names it, or meta's with no segment.
    Rekey { previous: PyStr },
    /// `chain_segments` raised; nothing there catches it, so it leaves the
    /// open after the binding.
    Raise(OpenError),
}

/// The dispatch (`store_open.py` 117-143), read only: the segments under
/// meta's fingerprint `expected` and the key in hand, then whether that key
/// signs one (`_signs_a_segment`: a segment under its fingerprint whose
/// first row verifies under it; the MAC decides, never the name).
pub(super) fn decide(
    conn: &Connection,
    ring: &KeyRing,
    files: &KeyFiles,
    expected: &str,
    key: Option<&Key>,
) -> Dispatch {
    let segments = match chain_segments(&ConnSource(conn), ring, &unicode::cps(expected), key) {
        Ok(segments) => segments,
        // `check_origin` reads the anchor first, then the log's rows.
        Err(VerifyError::Py(_)) => {
            return Dispatch::Raise(match files.read_origin() {
                Err(_) => upkeep::anchor_recursion(files.keys_dir(), files.store_id()),
                Ok(_) => upkeep::segments_recursion(conn),
            })
        }
        Err(VerifyError::Source(StoreError::Sqlite(e))) => return Dispatch::Raise(e.into()),
        Err(VerifyError::Source(_)) => return Dispatch::Raise(undecodable(conn, files)),
    };
    match key {
        Some(key) if signs(&segments, key) => {
            let own = unicode::cps(&fingerprint(key.bytes()));
            let mut foreign: PyStr = Vec::new();
            for segment in segments.iter().filter(|s| s.foreign) {
                if !foreign.is_empty() {
                    foreign.extend(unicode::cps(", "));
                }
                foreign.extend(unicode::cps(&format!(
                    "seq {} under key ",
                    segment.first_seq
                )));
                foreign.extend(segment.key_fingerprint.iter().take(16));
            }
            Dispatch::Reclaim {
                tail: segments.last().map_or(own, |s| s.key_fingerprint.clone()),
                foreign,
            }
        }
        _ => Dispatch::Rekey {
            previous: segments
                .last()
                .map_or_else(|| unicode::cps(expected), |s| s.key_fingerprint.clone()),
        },
    }
}

/// `_signs_a_segment` over the segment map (a name that is no text, a lone
/// surrogate in it, is no fingerprint).
fn signs(segments: &[Segment], key: &Key) -> bool {
    let names: Vec<Option<String>> = segments
        .iter()
        .map(|s| unicode::to_string(&s.key_fingerprint))
        .collect();
    let starts: Vec<SegmentStart<'_>> = segments
        .iter()
        .zip(&names)
        .filter_map(|(segment, name)| {
            Some(SegmentStart {
                key_fingerprint: name.as_deref()?,
                first_row: segment.first_row.as_ref(),
            })
        })
        .collect();
    signs_a_segment(&starts, key.bytes())
}

/// The OperationalError `sqlite3` raises fetching a row `chain_trust`
/// reads whose text is not UTF-8, worded as the module words it: the
/// anchored row, the first row, then the `rekey` rows in seq order, each
/// column in order.
fn undecodable(conn: &Connection, files: &KeyFiles) -> OpenError {
    const COLUMNS: [&str; 7] = ["seq", "ts", "session", "kind", "payload", "prev_mac", "mac"];
    let anchored = files.read_origin().ok().flatten().map(|o| o.seq);
    let reads = [
        (format!("{LOG_SELECT} WHERE seq = ?1"), anchored),
        (
            format!("{LOG_SELECT} WHERE seq > 0 ORDER BY seq LIMIT 1"),
            None,
        ),
        (
            format!("{LOG_SELECT} WHERE kind = 'rekey' AND seq > 0 ORDER BY seq"),
            None,
        ),
    ];
    for (i, (sql, seq)) in reads.iter().enumerate() {
        if i == 0 && seq.is_none() {
            continue;
        }
        let found = (|| -> rusqlite::Result<Option<String>> {
            let mut statement = conn.prepare(sql)?;
            let mut rows = match seq {
                Some(seq) => statement.query([seq])?,
                None => statement.query([])?,
            };
            while let Some(row) = rows.next()? {
                for (at, column) in COLUMNS.iter().enumerate() {
                    if let ValueRef::Text(raw) = row.get_ref(at)? {
                        if std::str::from_utf8(raw).is_err() {
                            return Ok(Some(sqlite_text_error(column, raw)));
                        }
                    }
                }
            }
            Ok(None)
        })();
        match found {
            Ok(Some(text)) => return OpenError::Decode(text),
            Ok(None) => {}
            Err(e) => return e.into(),
        }
    }
    not_ported(NotPorted::Unreadable("a log row"))
}

fn names(old: &[u32], new: &str) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("old_fingerprint"), PyValue::Str(old.to_vec())),
        (PyValue::str("new_fingerprint"), PyValue::str(new)),
    ])
}

/// `Store._reclaim`: one transaction that sets meta back to the store's
/// key, reads the last rekey name again under the lock (a rekey row another
/// opener appended meanwhile counts) and, while the tail is another key's,
/// appends the `rekey` row from it to this key (no session). meta is set
/// first, so the row is signed with this key. `sqlite3.Error` and `OSError`
/// are logged and the open goes on.
pub(super) fn reclaim(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    meta: &str,
    mut tail: PyStr,
    foreign: PyStr,
    log: &mut Vec<Warning>,
) -> Result<(), OpenError> {
    let own = fingerprint(store.key.bytes());
    let own_points = unicode::cps(&own);
    let mut appended: Option<i64> = None;
    let outcome = (|| -> Result<(), TailFailure> {
        let mut tx = store.begin(provider)?;
        tx.connection()
            .execute(
                "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                [&own],
            )
            .map_err(WriteError::from)?;
        match upkeep::last_rekey_name(tx.connection()) {
            Ok(Some(named)) => tail = named,
            Ok(None) => {}
            Err(NameError::Store(e)) => return Err(TailFailure::Logged(Cause::store(&e))),
            Err(NameError::Recursion(text)) => {
                return Err(TailFailure::Raised(upkeep::recursion(text)))
            }
        }
        if tail != own_points {
            let row = tx.append_at(&unicode::cps(REKEY), &names(&tail, &own), None, None)?;
            appended = Some(row.seq);
        }
        tx.commit()?;
        Ok(())
    })();
    match outcome {
        Ok(()) => log.push(Warning::Reclaimed {
            meta: meta.to_owned(),
            own,
            seq: appended,
            foreign,
        }),
        Err(TailFailure::Logged(cause)) => log.push(Warning::MetaNotSetBack {
            meta: meta.to_owned(),
            own,
            cause,
        }),
        Err(TailFailure::Raised(error)) => return Err(error),
    }
    Ok(())
}

/// `_open_rekeyed`'s key: the key in hand retired (`retire_current`), then
/// a new one drawn and written (`create_key`). Either raises out of the
/// open, after which no key is retired twice: the next open reads what
/// was left.
pub(super) fn new_key(
    keys_dir: &std::path::Path,
    store_id: &str,
    retire: bool,
    provider: &mut dyn Provider,
    hook: &mut dyn FnMut(Boundary),
) -> Result<Key, OpenError> {
    let io = |step: &'static str| move |error: std::io::Error| OpenError::Io { step, error };
    let files = KeyFiles::new(keys_dir, &PyValue::str(store_id))
        .map_err(|_| not_ported(NotPorted::Unreadable("the key ring")))?;
    if retire {
        match files.retire_current() {
            Ok(_) => {}
            Err(RingError::KeyUnreadable(k)) => {
                return Err(OpenError::KeyUnreadable {
                    path: k.path,
                    error: std::io::Error::other(k.reason),
                })
            }
            Err(RingError::Os(error)) => return Err(io("retire the key")(error)),
            Err(RingError::Py(_)) => return Err(not_ported(NotPorted::Unreadable("the key"))),
        }
        hook(Boundary::KeyRetired);
    }
    // `create_key`: the directory, then the draw, then the write.
    files.ensure_dir().map_err(io("create the key"))?;
    let key = provider.key().map_err(io("draw a key"))?;
    files.create_key(&key).map_err(io("create the key"))?;
    hook(Boundary::KeyCreated);
    Ok(key)
}

/// What the rekey's transaction raised: nothing there catches it.
fn raised(error: WriteError) -> OpenError {
    match error {
        WriteError::Sqlite(e) => OpenError::Sqlite(e),
        WriteError::Clock(error) => OpenError::Io {
            step: "read the clock",
            error,
        },
        WriteError::Refused {
            class: "OverflowError",
            ..
        } => OpenError::Overflow,
        other => OpenError::Write(Box::new(other)),
    }
}

/// `_open_rekeyed` from the bind on, the store holding the new key: one
/// transaction that names it in meta and appends the `rekey` row from
/// `previous` to it (signed with it: meta names it at the append), the
/// warning, then `_take_over_origin`. The transaction's errors leave the
/// open, after the key was retired and the new one written.
pub(super) fn rekeyed(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    previous: PyStr,
    hook: &mut dyn FnMut(Boundary),
    log: &mut Vec<Warning>,
) -> Result<(), OpenError> {
    let new = fingerprint(store.key.bytes());
    let row = (|| -> Result<LogRow, WriteError> {
        let mut tx = store.begin(provider)?;
        tx.connection().execute(
            "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
            [&new],
        )?;
        let row = tx.append_at(&unicode::cps(REKEY), &names(&previous, &new), None, None)?;
        tx.commit()?;
        Ok(row)
    })()
    .map_err(raised)?;
    hook(Boundary::Rekeyed);
    log.push(Warning::Rekeyed { previous, new });
    take_over_origin(store, &row, log)?;
    hook(Boundary::OriginTaken);
    Ok(())
}

/// `Store._take_over_origin`: with no anchor, one on `row` naming the new
/// key (`_record_origin`); an anchor that is there stays where it is, and
/// `_settle_origin` names its key when it names none.
fn take_over_origin(
    store: &WritableStore,
    row: &LogRow,
    log: &mut Vec<Warning>,
) -> Result<(), OpenError> {
    let files = upkeep::files(store)?;
    match files.read_origin() {
        Err(_) => Err(upkeep::anchor_recursion(store.keys_dir(), store.store_id())),
        Ok(None) => upkeep::record_origin(store, &files, row, &store.key, log),
        Ok(Some(_)) => upkeep::settle_origin(store, log),
    }
}
