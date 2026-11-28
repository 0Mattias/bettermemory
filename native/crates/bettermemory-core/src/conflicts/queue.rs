//! The contradiction queue (`conflicts.py` 732-986), unit conflicts-2:
//! `ConflictQueue` over the store's `conflicts` table (`load`, `pending`,
//! `file_pair`, `upsert_scan`, `_judged_content_changed`, `resolve`,
//! `_replace_all`), `scan_conflicts`, `split_judgeable` and
//! `conflicts_pending_count`, with `CONFLICTS_FILENAME`.
//!
//! The store is three traits, as the reference duck-types it:
//! [`ConflictRows`] (`list_conflicts`, `count_memories`),
//! [`ConflictWrites`] (`put_conflict`, `delete_conflict`) and
//! [`QueueStore`] (`batch`, whose body runs on a [`ConflictWrites`]). The
//! real store implements them on [`WritableStore`] and [`Transaction`]
//! (a batch inside an open transaction joins it under a savepoint) and on
//! [`Batched`], a writable store with its provider (a batch of its own).
//! The clock (`now`), the warning sink and `before_stamp` are arguments.

use std::collections::{HashMap, HashSet};

use super::{body_hash, find_conflict_candidates, ConflictCandidate, ConflictError};
use crate::datetime::{parse_event_ts, DateTime};
use crate::location_witness::py_eq;
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::records::Memory;
use crate::store::caller::bytes_repr;
use crate::store::create::Provider;
use crate::store::open::WritableStore;
use crate::store::reads::failure_of;
use crate::store::record_reads::{self, ReadError};
use crate::store::tables::{list_conflicts, CONFLICT_COLUMNS};
use crate::store::tx::{Committed, Transaction};
use crate::store::write::WriteError;
use crate::store::{SqlValue, StoreError};
use crate::unicode;

/// `CONFLICTS_FILENAME`: the v8 queue file's name. Nothing here writes it.
pub const CONFLICTS_FILENAME: &str = ".conflicts.jsonl";

const PENDING: &str = "pending";
const DISMISSED: &str = "dismissed";

/// One row as the store lists it: a dict's items.
pub type RawRow = Vec<(PyValue, PyValue)>;

/// `{m.id: m for m in memories}`: a repeated id keeps its last memory.
pub type MemoriesById<'m> = HashMap<&'m [u32], &'m Memory>;

/// What a queue call raised.
#[derive(Debug)]
pub enum QueueError {
    /// What conflicts-1 raises: a `from_dict` error `load` does not skip
    /// (OverflowError, RecursionError), TypeError of a naive `updated`
    /// against the verdict, UnicodeEncodeError of a hashed body.
    Conflict(ConflictError),
    /// A write refused or failed (the store's own refusal, SQLite).
    Write(WriteError),
    /// `list_conflicts` failed (SQLite, a TEXT cell that is not UTF-8).
    Store(StoreError),
    /// `count_memories` failed.
    Read(ReadError),
    /// `resolve`'s ValueError, with the reference's text.
    Value(PyStr),
    /// What a store or a callback raised, by its class.
    Raised(String),
}

/// The class `sqlite3` raises for a SQLite failure, by its result code
/// (`_pysqlite_seterror`, [`failure_of`]); a failure that carries no
/// result code is the DatabaseError the crate's other writers name.
fn sqlite_class(e: &rusqlite::Error) -> String {
    failure_of(e)
        .map_or("DatabaseError", |(class, _)| class)
        .to_owned()
}

impl QueueError {
    /// The Python class raised; a native failure is `native:<what>`. A
    /// SQLite failure is named as `sqlite3` names it, by its result code
    /// (OperationalError, DatabaseError for a corrupt page, DataError,
    /// IntegrityError, InterfaceError, InternalError, MemoryError);
    /// `_require_open`'s and `require_current_writer`'s refusals are the
    /// reference's own `sqlite3.OperationalError`.
    pub fn class(&self) -> String {
        match self {
            QueueError::Conflict(e) => e.name().to_owned(),
            QueueError::Write(WriteError::Refused { class, .. }) => (*class).to_owned(),
            QueueError::Write(WriteError::Sqlite(e))
            | QueueError::Store(StoreError::Sqlite(e))
            | QueueError::Read(ReadError::Sqlite(e)) => sqlite_class(e),
            QueueError::Write(WriteError::RolledBack(_) | WriteError::LaterWriter { .. }) => {
                "OperationalError".to_owned()
            }
            // `store_versions.StoreVersionError`: a minimum writer revision
            // that does not read as a number.
            QueueError::Write(WriteError::Version(_)) => "StoreVersionError".to_owned(),
            QueueError::Write(e) => format!("native:{}", e.class()),
            // sqlite3's "Could not decode to UTF-8 column".
            QueueError::Store(StoreError::NotUtf8 { .. }) => "OperationalError".to_owned(),
            QueueError::Store(_) => "native:store".to_owned(),
            QueueError::Read(e) => e.class(),
            QueueError::Value(_) => "ValueError".to_owned(),
            QueueError::Raised(class) => class.clone(),
        }
    }

    /// `str(exc)` where the text is the product's own: `resolve`'s
    /// ValueError and the store's refusals; None for the rest.
    pub fn text(&self) -> Option<PyStr> {
        match self {
            QueueError::Value(text) => Some(text.clone()),
            QueueError::Write(WriteError::Refused {
                text: Some(text), ..
            }) => Some(unicode::cps(text)),
            _ => None,
        }
    }
}

impl From<ConflictError> for QueueError {
    fn from(e: ConflictError) -> Self {
        QueueError::Conflict(e)
    }
}

impl From<WriteError> for QueueError {
    fn from(e: WriteError) -> Self {
        QueueError::Write(e)
    }
}

/// The store's reads the queue makes.
pub trait ConflictRows {
    /// `store.list_conflicts()`: every row, as a dict's items.
    fn list_conflicts(&mut self) -> Result<Vec<RawRow>, QueueError>;
    /// `store.count_memories()`.
    fn count_memories(&mut self) -> Result<i64, QueueError>;
}

/// The store's writes the queue makes, inside a batch.
pub trait ConflictWrites: ConflictRows {
    /// `store.put_conflict(record, session=)`.
    fn put_conflict(&mut self, record: &PyValue, session: Option<&str>) -> Result<(), QueueError>;
    /// `store.delete_conflict(conflict_id, session=)`.
    fn delete_conflict(
        &mut self,
        conflict_id: &[u32],
        session: Option<&str>,
    ) -> Result<(), QueueError>;
}

/// `with store.batch():`, the body's reads and writes committed together
/// or, when it returns an error, rolled back with nothing written.
pub trait QueueStore: ConflictRows {
    /// What the body runs on.
    type Batch<'t>: ConflictWrites;
    /// The batch around `body`.
    fn batch<R>(
        &mut self,
        body: impl FnOnce(&mut Self::Batch<'_>) -> Result<R, QueueError>,
    ) -> Result<R, QueueError>;
}

/// A BLOB cell as `from_dict` reads `bytes`: `str()` is its `repr`, and
/// `float()` reads ASCII bytes as it reads the same text and refuses any
/// byte past ASCII (here a NUL, which `float()` also refuses).
fn blob(column: &str, b: &[u8]) -> PyValue {
    if column != "similarity" {
        return PyValue::str(&bytes_repr(b));
    }
    if b.is_ascii() {
        PyValue::Str(b.iter().map(|&c| u32::from(c)).collect())
    } else {
        PyValue::str("\0")
    }
}

/// A row of [`list_conflicts`] as the queue's `from_dict` reads it.
fn raw_row(cells: Vec<SqlValue>) -> RawRow {
    CONFLICT_COLUMNS
        .iter()
        .zip(cells)
        .map(|(column, cell)| {
            let value = match cell {
                SqlValue::Null => PyValue::None,
                SqlValue::Integer(n) => PyValue::Int(n.to_string()),
                SqlValue::Real(f) => PyValue::Float(f),
                SqlValue::Text(t) => PyValue::str(&t),
                SqlValue::Blob(b) => blob(column, &b),
            };
            (PyValue::str(column), value)
        })
        .collect()
}

fn rows_of(conn: &rusqlite::Connection) -> Result<Vec<RawRow>, QueueError> {
    let rows = list_conflicts(conn).map_err(QueueError::Store)?;
    Ok(rows.into_iter().map(raw_row).collect())
}

fn count_of(conn: &rusqlite::Connection) -> Result<i64, QueueError> {
    record_reads::count_memories(conn).map_err(QueueError::Read)
}

impl ConflictRows for WritableStore {
    fn list_conflicts(&mut self) -> Result<Vec<RawRow>, QueueError> {
        rows_of(self.connection())
    }
    fn count_memories(&mut self) -> Result<i64, QueueError> {
        count_of(self.connection())
    }
}

impl ConflictRows for Transaction<'_> {
    fn list_conflicts(&mut self) -> Result<Vec<RawRow>, QueueError> {
        rows_of(self.connection())
    }
    fn count_memories(&mut self) -> Result<i64, QueueError> {
        count_of(self.connection())
    }
}

impl ConflictWrites for Transaction<'_> {
    fn put_conflict(&mut self, record: &PyValue, session: Option<&str>) -> Result<(), QueueError> {
        Transaction::put_conflict(self, record, session)?;
        Ok(())
    }
    fn delete_conflict(
        &mut self,
        conflict_id: &[u32],
        session: Option<&str>,
    ) -> Result<(), QueueError> {
        // sqlite3 binding a str that holds a lone surrogate.
        let id = unicode::to_string(conflict_id).ok_or(WriteError::Refused {
            class: "UnicodeEncodeError",
            text: None,
        })?;
        Transaction::delete_conflict(self, &id, session)?;
        Ok(())
    }
}

impl<'s> QueueStore for Transaction<'s> {
    type Batch<'t> = Transaction<'s>;
    /// A batch inside the open transaction joins it (`_joined`).
    fn batch<R>(
        &mut self,
        body: impl FnOnce(&mut Self::Batch<'_>) -> Result<R, QueueError>,
    ) -> Result<R, QueueError> {
        self.scope(body)
    }
}

/// A [`WritableStore`] with the provider that stamps its rows: each batch
/// is a transaction of its own (`Store.batch()` at its outermost entry),
/// its commit kept in `committed`.
pub struct Batched<'a> {
    pub store: &'a mut WritableStore,
    pub provider: &'a mut dyn Provider,
    pub committed: Vec<Committed>,
}

impl<'a> Batched<'a> {
    pub fn new(store: &'a mut WritableStore, provider: &'a mut dyn Provider) -> Self {
        Batched {
            store,
            provider,
            committed: Vec::new(),
        }
    }
}

impl ConflictRows for Batched<'_> {
    fn list_conflicts(&mut self) -> Result<Vec<RawRow>, QueueError> {
        rows_of(self.store.connection())
    }
    fn count_memories(&mut self) -> Result<i64, QueueError> {
        count_of(self.store.connection())
    }
}

impl QueueStore for Batched<'_> {
    type Batch<'t> = Transaction<'t>;
    fn batch<R>(
        &mut self,
        body: impl FnOnce(&mut Self::Batch<'_>) -> Result<R, QueueError>,
    ) -> Result<R, QueueError> {
        let (value, committed) = self.store.batch(&mut *self.provider, None, body)?;
        self.committed.push(committed);
        Ok(value)
    }
}

/// `memories_by_id` as `scan_conflicts` builds it.
pub fn memories_by_id(memories: &[Memory]) -> MemoriesById<'_> {
    memories
        .iter()
        .map(|m| (m.fields.id.as_slice(), m))
        .collect()
}

/// Whether `load` skips a row whose `from_dict` raised this: KeyError,
/// TypeError and ValueError (UnicodeEncodeError and UnicodeDecodeError
/// among them).
fn skipped(e: &ConflictError) -> bool {
    matches!(
        e.name(),
        "KeyError" | "TypeError" | "ValueError" | "UnicodeEncodeError" | "UnicodeDecodeError"
    )
}

fn load_rows<S: ConflictRows + ?Sized>(
    store: &mut S,
) -> Result<Vec<ConflictCandidate>, QueueError> {
    let mut out = Vec::new();
    for raw in store.list_conflicts()? {
        match ConflictCandidate::from_dict(&raw) {
            Ok(row) => out.push(row),
            Err(e) if skipped(&e) => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(out)
}

/// `upsert_scan`'s counters, written in the reference's key order by
/// [`Upserted::items`].
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Upserted {
    pub added: usize,
    pub resurrected: usize,
    pub refreshed: usize,
    pub dropped: usize,
    pub gc_deferred: usize,
    pub pending_rows_on_disk: usize,
}

impl Upserted {
    pub fn items(&self) -> [(&'static str, usize); 6] {
        [
            ("added", self.added),
            ("resurrected", self.resurrected),
            ("refreshed", self.refreshed),
            ("dropped", self.dropped),
            ("gc_deferred", self.gc_deferred),
            ("pending_rows_on_disk", self.pending_rows_on_disk),
        ]
    }
}

fn is(text: &[u32], word: &str) -> bool {
    text.iter().copied().eq(word.chars().map(u32::from))
}

/// `ConflictQueue(store)`.
pub struct ConflictQueue<'s, S: ?Sized> {
    pub store: &'s mut S,
}

impl<'s, S: ConflictRows + ?Sized> ConflictQueue<'s, S> {
    pub fn new(store: &'s mut S) -> Self {
        ConflictQueue { store }
    }

    /// `load()`: every listed row through `from_dict`, in the listed
    /// order; a row it refuses with KeyError, TypeError or ValueError is
    /// skipped, anything else raised (and the listing's own error) goes on.
    pub fn load(&mut self) -> Result<Vec<ConflictCandidate>, QueueError> {
        load_rows(self.store)
    }

    /// `pending()`: the loaded rows whose status is `pending`, in order.
    pub fn pending(&mut self) -> Result<Vec<ConflictCandidate>, QueueError> {
        let mut rows = self.load()?;
        rows.retain(|c| is(&c.status, PENDING));
        Ok(rows)
    }
}

impl<'s, S: QueueStore> ConflictQueue<'s, S> {
    /// `file_pair(cand, session=)` in one batch: the status of the first
    /// loaded row of the pair's id, else `cand` put and its own status.
    pub fn file_pair(
        &mut self,
        cand: &ConflictCandidate,
        session: Option<&str>,
    ) -> Result<PyStr, QueueError> {
        self.store.batch(|tx| {
            if let Some(row) = load_rows(tx)?.into_iter().find(|r| r.id == cand.id) {
                return Ok(row.status);
            }
            tx.put_conflict(&cand.to_dict(), session)?;
            Ok(cand.status.clone())
        })
    }

    /// `upsert_scan(fresh, {m.id: m for m in memories}, session=)` in one
    /// batch: new pairs added, pending rows refreshed (the detector kept
    /// unless empty), dismissed rows resurrected when a judged body
    /// changed, and, when the snapshot holds at least the store's count of
    /// memories, rows with a side outside it dropped; else `warn` hears the
    /// deferral. The table is then made to hold what was kept, writing only
    /// what changed.
    pub fn upsert_scan(
        &mut self,
        fresh: &[ConflictCandidate],
        memories: &[Memory],
        session: Option<&str>,
        warn: &mut dyn FnMut(&str),
    ) -> Result<Upserted, QueueError> {
        let by_id = memories_by_id(memories);
        self.store.batch(|tx| {
            // `before` and `by_id`: a repeated id keeps its first place and
            // its last row, as a dict's assignment does.
            let mut rows: Vec<ConflictCandidate> = Vec::new();
            let mut before: Vec<PyValue> = Vec::new();
            let mut index: HashMap<PyStr, usize> = HashMap::new();
            for c in load_rows(tx)? {
                match index.get(&c.id) {
                    Some(&i) => {
                        before[i] = c.to_dict();
                        rows[i] = c;
                    }
                    None => {
                        index.insert(c.id.clone(), rows.len());
                        before.push(c.to_dict());
                        rows.push(c);
                    }
                }
            }
            let loaded = rows.len();
            let mut out = Upserted::default();
            for cand in fresh {
                let Some(&i) = index.get(&cand.id) else {
                    index.insert(cand.id.clone(), rows.len());
                    rows.push(cand.clone());
                    out.added += 1;
                    continue;
                };
                let existing = &mut rows[i];
                if is(&existing.status, PENDING) {
                    existing.summary_a.clone_from(&cand.summary_a);
                    existing.summary_b.clone_from(&cand.summary_b);
                    existing.similarity = cand.similarity;
                    if existing.detector.is_empty() {
                        existing.detector.clone_from(&cand.detector);
                    }
                    out.refreshed += 1;
                } else if is(&existing.status, DISMISSED)
                    && judged_content_changed(existing, &by_id)?
                {
                    existing.status = unicode::cps(PENDING);
                    existing.verdict_ts = None;
                    existing.note = None;
                    existing.verdict_hash_a = None;
                    existing.verdict_hash_b = None;
                    existing.summary_a.clone_from(&cand.summary_a);
                    existing.summary_b.clone_from(&cand.summary_b);
                    existing.similarity = cand.similarity;
                    existing.created.clone_from(&cand.created);
                    out.resurrected += 1;
                }
            }
            let snapshot = by_id.len();
            let collectable = i64::try_from(snapshot).unwrap_or(i64::MAX) >= tx.count_memories()?;
            if !collectable {
                let held = tx.count_memories()?;
                warn(&format!(
                    "conflict-queue GC deferred: the caller's snapshot has {snapshot} \
                     memories but the store holds {held}"
                ));
            }
            let kept: Vec<&ConflictCandidate> = rows
                .iter()
                .filter(|c| {
                    !collectable
                        || (by_id.contains_key(c.a_id.as_slice())
                            && by_id.contains_key(c.b_id.as_slice()))
                })
                .collect();
            out.dropped = rows.len() - kept.len();
            let before: Vec<(&[u32], &PyValue)> = rows[..loaded]
                .iter()
                .map(|c| c.id.as_slice())
                .zip(before.iter())
                .collect();
            replace_all(tx, &kept, &before, session)?;
            out.gc_deferred = usize::from(!collectable);
            out.pending_rows_on_disk = kept.iter().filter(|c| is(&c.status, PENDING)).count();
            Ok(out)
        })
    }

    /// `resolve(candidate_id, status=, note=, member_bodies=,
    /// before_stamp=, session=)` with `utcnow()` as `now`: a status other
    /// than `confirmed` or `dismissed` is ValueError before the batch;
    /// inside it, the first loaded pending row of the id (else None), then
    /// `before_stamp` on the batch, then the stamp (`now.isoformat()`, the
    /// note, each side's body hash where its body is given) and one put.
    /// Returns the stamped row.
    #[allow(clippy::too_many_arguments)]
    pub fn resolve<F>(
        &mut self,
        candidate_id: &[u32],
        status: &[u32],
        note: Option<&[u32]>,
        member_bodies: &HashMap<PyStr, PyStr>,
        before_stamp: Option<F>,
        session: Option<&str>,
        now: &DateTime,
    ) -> Result<Option<ConflictCandidate>, QueueError>
    where
        F: for<'t> FnOnce(&mut S::Batch<'t>) -> Result<(), QueueError>,
    {
        if !is(status, "confirmed") && !is(status, DISMISSED) {
            let mut text = unicode::cps("verdict status must be 'confirmed' or 'dismissed', got ");
            text.extend(repr_str(status));
            return Err(QueueError::Value(text));
        }
        self.store.batch(|tx| {
            let hit = load_rows(tx)?
                .into_iter()
                .find(|c| c.id == candidate_id && is(&c.status, PENDING));
            let Some(mut hit) = hit else {
                return Ok(None);
            };
            if let Some(stamp) = before_stamp {
                stamp(tx)?;
            }
            hit.status = status.to_vec();
            hit.verdict_ts = Some(unicode::cps(&now.isoformat()));
            hit.note = note.map(<[u32]>::to_vec);
            let hash = |id: &PyStr| -> Result<Option<PyStr>, ConflictError> {
                member_bodies
                    .get(id)
                    .map(|body| body_hash(body).map(|h| unicode::cps(&h)))
                    .transpose()
            };
            hit.verdict_hash_a = hash(&hit.a_id)?;
            hit.verdict_hash_b = hash(&hit.b_id)?;
            tx.put_conflict(&hit.to_dict(), session)?;
            Ok(Some(hit))
        })
    }
}

/// `_judged_content_changed(cand, memories_by_id)`: per side in (a, b)
/// order, a memory not in the snapshot is passed over; a stored body hash
/// decides by `_body_hash(body)`; without one, no parseable verdict time,
/// or `updated` after it, says changed. A naive `updated` is TypeError.
pub fn judged_content_changed(
    cand: &ConflictCandidate,
    by_id: &MemoriesById<'_>,
) -> Result<bool, ConflictError> {
    let verdict = cand.verdict_ts.as_deref().and_then(parse_event_ts);
    for (mid, judged) in [
        (&cand.a_id, &cand.verdict_hash_a),
        (&cand.b_id, &cand.verdict_hash_b),
    ] {
        let Some(m) = by_id.get(mid.as_slice()) else {
            continue;
        };
        if let Some(judged) = judged {
            if !is(judged, &body_hash(&m.fields.body)?) {
                return Ok(true);
            }
            continue;
        }
        let Some(verdict) = &verdict else {
            return Ok(true);
        };
        // `parse_event_ts` answers an aware time; a naive `updated` is
        // TypeError against it.
        match (m.fields.updated.instant_us(), verdict.instant_us()) {
            (Some(u), Some(v)) if u > v => return Ok(true),
            (Some(_), Some(_)) => {}
            _ => return Err(ConflictError::NaiveAware),
        }
    }
    Ok(false)
}

/// `_replace_all(rows, before, session=)`: the ids of `before` (in its
/// order) not kept are deleted, then every kept row whose `to_dict` is not
/// `==` its `before` (Python's dict equality: key order ignored, a NaN
/// equal to nothing) is put, in `rows`' order. `rows` hold distinct ids.
fn replace_all<W: ConflictWrites + ?Sized>(
    tx: &mut W,
    rows: &[&ConflictCandidate],
    before: &[(&[u32], &PyValue)],
    session: Option<&str>,
) -> Result<(), QueueError> {
    let keep: HashSet<&[u32]> = rows.iter().map(|c| c.id.as_slice()).collect();
    for (id, _) in before {
        if !keep.contains(id) {
            tx.delete_conflict(id, session)?;
        }
    }
    let was: HashMap<&[u32], &PyValue> = before.iter().copied().collect();
    for c in rows {
        let record = c.to_dict();
        if was.get(c.id.as_slice()).is_none_or(|b| !py_eq(b, &record)) {
            tx.put_conflict(&record, session)?;
        }
    }
    Ok(())
}

/// `scan_conflicts(store, memories, threshold=, session=)` with
/// `utcnow()` as `now`: the candidates found, folded into the queue.
pub fn scan_conflicts<S: QueueStore>(
    store: &mut S,
    memories: &[Memory],
    threshold: Option<f64>,
    session: Option<&str>,
    now: &DateTime,
    warn: &mut dyn FnMut(&str),
) -> Result<Upserted, QueueError> {
    let fresh = find_conflict_candidates(memories, threshold, now)?;
    ConflictQueue::new(store).upsert_scan(&fresh, memories, session, warn)
}

/// `split_judgeable(pending, is_active)`: the rows whose two members are
/// active and the count of the rest. `is_active` is asked left to right
/// and short-circuits; the order is kept and the whole input consumed. An
/// error of `is_active` ends it.
pub fn split_judgeable<E>(
    pending: impl IntoIterator<Item = ConflictCandidate>,
    mut is_active: impl FnMut(&[u32]) -> Result<bool, E>,
) -> Result<(Vec<ConflictCandidate>, usize), E> {
    let mut judgeable = Vec::new();
    let mut omitted = 0;
    for cand in pending {
        if is_active(&cand.a_id)? && is_active(&cand.b_id)? {
            judgeable.push(cand);
        } else {
            omitted += 1;
        }
    }
    Ok((judgeable, omitted))
}

/// `conflicts_pending_count(store)`: the count of pending rows, or 0 when
/// anything raised, with `warn` hearing `conflict queue unreadable in
/// <path>` (`path` is `str(store.path)`).
pub fn conflicts_pending_count<S: ConflictRows + ?Sized>(
    store: &mut S,
    path: &str,
    warn: &mut dyn FnMut(&str),
) -> usize {
    match ConflictQueue::new(store).pending() {
        Ok(rows) => rows.len(),
        Err(_) => {
            warn(&format!("conflict queue unreadable in {path}"));
            0
        }
    }
}
