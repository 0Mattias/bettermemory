//! The write transaction every record writer runs inside: `Store._transaction`
//! (its outermost entry and its nested `_joined` entry), `Store._mutate`, the
//! `returned` marks `Store._flush_returns` settles and appends at the
//! outermost commit (`store_returned.settle`), `Store.release_returned`, and
//! the head's one move after the commit (`Store._move_head`).
//!
//! The rule (README, "The write transaction"): [`WritableStore::begin`]
//! takes the write lock (`BEGIN IMMEDIATE`) and checks the writer;
//! [`Transaction::mutate`] logs and applies one mutation and names the
//! records whose marks it may change; [`Transaction::scope`] runs a body
//! under a savepoint, and on its failure restores what `_joined` restores
//! (the last appended row, the named records) and rolls back to the
//! savepoint while the transaction goes on; [`Transaction::commit`] checks
//! the transaction is still open, settles the named records (the puts
//! first, then the deletions, each set in record-id order), commits once
//! and moves the head once, to the last row kept. A transaction dropped
//! without a commit, or whose commit fails, is rolled back with nothing
//! written.
//!
//! A later writer (`put_memory`, `tombstone`, `restore` and the rest) reads
//! the store through [`Transaction::connection`] inside the lock, appends
//! its rows with [`Transaction::mutate`], names a restored record with
//! [`Transaction::name_returned`] (`_put_restored`'s
//! `_tx_returns.setdefault(id, (id, session))`), and runs its own body in
//! a [`Transaction::scope`], so a typed error it returns rolls back what it
//! wrote and nothing else.

use std::collections::BTreeMap;

use rusqlite::{Connection, OptionalExtension};

use super::create::Provider;
use super::fold::Applier;
use super::open::{require_current_writer, WritableStore};
use super::write::{
    before, candidates, is_active, last_row, load_current, move_head, native_origin, pending_is,
    py_error, HeadMove, WriteBoundary, WriteError, WriteNotPorted, KINDS, SUPERSEDES,
};
use crate::datetime::DateTime;
use crate::log::{fingerprint, next_row, Key, LogRow};
use crate::pyvalue::PyValue;

/// `CHUNK_HOLD_SECONDS` (`store.py:298`): how long a chunk of a long run
/// holds the write lock before it commits.
pub const CHUNK_HOLD_SECONDS: f64 = 0.1;

/// `CHUNK_YIELD_SECONDS` (`store.py:314`): the pause between two chunks,
/// the write lock free.
pub const CHUNK_YIELD_SECONDS: f64 = 0.05;

/// The records a transaction's writes named for `settle`: each with the
/// record whose change named it and the session (`Store._tx_returns`).
/// Ordered by id, as `settle` reads them (`sorted(pending.items())`).
type Pending = BTreeMap<String, (String, Option<String>)>;

/// What a transaction's commit wrote.
#[derive(Debug)]
pub struct Committed {
    /// Every row the commit holds, in log order: the transaction's own,
    /// then its `returned` rows.
    pub rows: Vec<LogRow>,
    /// The head's move to the last row; None when the transaction kept no
    /// row (`_last_appended` is None and `_move_head` does nothing).
    pub head: Option<HeadMove>,
}

/// A write transaction on a [`WritableStore`] (`Store._transaction`'s
/// outermost entry). Begun by [`WritableStore::begin`]; ends by
/// [`Transaction::commit`], or rolled back when dropped.
pub struct Transaction<'s> {
    store: &'s mut WritableStore,
    provider: &'s mut dyn Provider,
    hook: Option<&'s mut dyn FnMut(WriteBoundary)>,
    /// `_tx_depth`: 1 in the outermost scope.
    depth: usize,
    /// `_last_appended`.
    last: Option<LogRow>,
    /// `_tx_returns`.
    returns: Pending,
    /// The rows written so far that a rollback has not taken back.
    rows: Vec<LogRow>,
    /// A write this build does not port was asked for: the reference would
    /// have written it, so the transaction may not commit.
    refused: Option<WriteNotPorted>,
    /// The transaction is this one's to end (BEGIN taken, not ended).
    open: bool,
}

impl std::fmt::Debug for Transaction<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Transaction")
            .field("depth", &self.depth)
            .field("rows", &self.rows.len())
            .field("named", &self.returns.len())
            .field("refused", &self.refused)
            .finish_non_exhaustive()
    }
}

/// `store_returned.settle`: the `returned` rows the named records leave
/// due as the transaction leaves the store, the puts and the deletions,
/// each with its session. The wall clock is read once, only when a record
/// is named and the table exists, as `settle` reads it.
type Rows = Vec<(PyValue, Option<String>)>;

fn settle(
    conn: &Connection,
    pending: &Pending,
    provider: &mut dyn Provider,
) -> Result<(Rows, Rows), WriteError> {
    if pending.is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }
    let has_table = conn
        .prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'returned'")?
        .exists([])?;
    if !has_table {
        return Ok((Vec::new(), Vec::new()));
    }
    let stamp = provider
        .wall_now()
        .map_err(WriteError::Clock)?
        .isoformat_utc();
    let mut puts = Vec::new();
    let mut deletes = Vec::new();
    for (id, (via, session)) in pending {
        let marked = conn
            .prepare("SELECT 1 FROM returned WHERE record_id = ?")?
            .exists([id])?;
        let active = is_active(conn, id)?;
        let superseded = || -> rusqlite::Result<bool> {
            conn.prepare(
                "SELECT 1 FROM memory_links l JOIN memories m ON m.id = l.source_id \
                 WHERE l.type = ? AND l.target_id = ? AND l.source_id != ? LIMIT 1",
            )?
            .exists([SUPERSEDES, id.as_str(), id.as_str()])
        };
        // `not active or _superseded(...)`: the second query only when active.
        if !active || superseded()? {
            if marked {
                let dropped = if active { "superseded" } else { "removed" };
                deletes.push((
                    dict(&[
                        ("record_id", PyValue::str(id)),
                        ("dropped", PyValue::str(dropped)),
                    ]),
                    session.clone(),
                ));
            }
        } else if !marked {
            let mark = dict(&[
                ("record_id", PyValue::str(id)),
                ("via", PyValue::str(via)),
                ("returned", PyValue::str(&stamp)),
            ]);
            puts.push((dict(&[("returned", mark)]), session.clone()));
        }
    }
    Ok((puts, deletes))
}

fn dict(items: &[(&str, PyValue)]) -> PyValue {
    PyValue::Dict(
        items
            .iter()
            .map(|(k, v)| (PyValue::str(k), v.clone()))
            .collect(),
    )
}

fn rolled_back(store: &WritableStore) -> WriteError {
    WriteError::RolledBack(store.path().display().to_string())
}

impl WritableStore {
    /// `with store._transaction() as tx:` at its outermost entry: a
    /// transaction no call owns is rolled back first (`_clear_residue`),
    /// `BEGIN IMMEDIATE` (the busy timeout is the wait), the writer check.
    /// `provider`'s clocks stamp the rows (`now`), the marks (`wall_now`)
    /// and the head (`now`).
    pub fn begin<'s>(
        &'s mut self,
        provider: &'s mut dyn Provider,
    ) -> Result<Transaction<'s>, WriteError> {
        self.begin_hooked(provider, None)
    }

    /// [`WritableStore::begin`] with `hook` shown each [`WriteBoundary`].
    #[doc(hidden)]
    pub fn begin_with<'s>(
        &'s mut self,
        provider: &'s mut dyn Provider,
        hook: &'s mut dyn FnMut(WriteBoundary),
    ) -> Result<Transaction<'s>, WriteError> {
        self.begin_hooked(provider, Some(hook))
    }

    fn begin_hooked<'s>(
        &'s mut self,
        provider: &'s mut dyn Provider,
        hook: Option<&'s mut dyn FnMut(WriteBoundary)>,
    ) -> Result<Transaction<'s>, WriteError> {
        if !self.conn.is_autocommit() {
            self.conn.execute_batch("ROLLBACK")?;
        }
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        let mut tx = Transaction {
            store: self,
            provider,
            hook,
            depth: 1,
            last: None,
            returns: Pending::new(),
            rows: Vec::new(),
            refused: None,
            open: true,
        };
        tx.passed(WriteBoundary::Begun);
        if require_current_writer(&tx.store.conn).is_err() {
            // Dropped: rolled back.
            return Err(WriteError::LaterWriter);
        }
        Ok(tx)
    }

    /// `Store.batch()` (`store.py` 1314-1321): one transaction around
    /// `body`, committed when it returns Ok, rolled back with nothing
    /// written when it returns an error. A batch inside an open
    /// transaction is [`Transaction::scope`] (`batch` joins it).
    pub fn batch<T, E: From<WriteError>>(
        &mut self,
        provider: &mut dyn Provider,
        hook: Option<&mut dyn FnMut(WriteBoundary)>,
        body: impl FnOnce(&mut Transaction<'_>) -> Result<T, E>,
    ) -> Result<(T, Committed), E> {
        let mut hook = hook;
        let mut relay = |b: WriteBoundary| {
            if let Some(h) = hook.as_mut() {
                h(b);
            }
        };
        let mut tx = self.begin_hooked(provider, Some(&mut relay))?;
        let value = body(&mut tx)?;
        Ok((value, tx.commit()?))
    }

    /// `Store._in_chunks(items, apply)` (`store.py` 1324-1354) with no
    /// transaction open: `apply` runs on each item in order inside write
    /// transactions of their own (chunks). A chunk begins, reads the
    /// monotonic clock, and after each item reads it again, closing when
    /// [`CHUNK_HOLD_SECONDS`] have passed or the items run out; it
    /// commits (its returned marks settled, the head moved), and while
    /// items remain the run pauses [`CHUNK_YIELD_SECONDS`] with the lock
    /// free. No items: nothing begun. An item's error rolls its chunk back
    /// and ends the run: the chunks before it stay committed, the items
    /// after it are not tried. Returns each chunk's commit in order.
    pub fn in_chunks<I, E: From<WriteError>>(
        &mut self,
        provider: &mut dyn Provider,
        mut hook: Option<&mut dyn FnMut(WriteBoundary)>,
        items: &[I],
        mut apply: impl FnMut(&mut Transaction<'_>, &I) -> Result<(), E>,
    ) -> Result<Vec<Committed>, E> {
        let mut done = 0;
        let mut chunks = Vec::new();
        while done < items.len() {
            let mut relay = |b: WriteBoundary| {
                if let Some(h) = hook.as_mut() {
                    h(b);
                }
            };
            let mut tx = self.begin_hooked(&mut *provider, Some(&mut relay))?;
            let started = tx.monotonic()?;
            while done < items.len() {
                apply(&mut tx, &items[done])?;
                done += 1;
                if tx.monotonic()? - started >= CHUNK_HOLD_SECONDS {
                    break;
                }
            }
            chunks.push(tx.commit()?);
            if done < items.len() {
                provider.pause(CHUNK_YIELD_SECONDS);
            }
        }
        Ok(chunks)
    }

    /// `Store.release_returned(record_id, session=)` in a transaction of
    /// its own: whether a mark was there (and is now deleted).
    pub fn release_returned(
        &mut self,
        record_id: &str,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<(bool, Committed), WriteError> {
        let mut tx = self.begin(provider)?;
        let released = tx.release_returned(record_id, session)?;
        Ok((released, tx.commit()?))
    }
}

impl Transaction<'_> {
    fn passed(&mut self, b: WriteBoundary) {
        if let Some(hook) = self.hook.as_mut() {
            hook(b);
        }
    }

    /// The wall clock, read once (`datetime.now` as a writer reads it,
    /// `tombstone`'s removal time among them).
    pub fn wall_now(&mut self) -> Result<DateTime, WriteError> {
        self.provider.wall_now().map_err(WriteError::Clock)
    }

    /// `time.monotonic()` as `_in_chunks` reads it.
    pub fn monotonic(&mut self) -> Result<f64, WriteError> {
        self.provider.monotonic().map_err(WriteError::Clock)
    }

    /// `_in_chunks` inside an open transaction (`store.py` 1340-1343):
    /// every item into this one, no clock read, no pause.
    pub fn in_chunks<I, E: From<WriteError>>(
        &mut self,
        items: &[I],
        mut apply: impl FnMut(&mut Self, &I) -> Result<(), E>,
    ) -> Result<(), E> {
        for item in items {
            apply(self, item)?;
        }
        Ok(())
    }

    /// The connection, to read the store inside the write lock.
    pub fn connection(&self) -> &Connection {
        &self.store.conn
    }

    /// The rows written so far that no rollback has taken back.
    pub fn rows(&self) -> &[LogRow] {
        &self.rows
    }

    /// `_require_open`: SQLite may have ended the transaction (a full disk,
    /// an I/O error).
    fn require_open(&self) -> Result<(), WriteError> {
        if self.store.conn.is_autocommit() {
            return Err(rolled_back(self.store));
        }
        Ok(())
    }

    /// `_append`: the signing key (`_signing_key`, meta read again under
    /// the lock), the last row, the log clock, the row inserted.
    fn append(
        &mut self,
        kind: &str,
        payload: &PyValue,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        let conn = &self.store.conn;
        // `str(row[0])`: any stored type, NULL read as "None".
        let recorded: Option<String> = conn
            .query_row(
                "SELECT value FROM meta WHERE key = 'key_fingerprint'",
                [],
                |r| Ok(super::sql_value(r.get_ref(0)?).map(|v| v.py_str())),
            )
            .optional()?
            .map(|v| {
                v.map_err(|_| WriteError::Refused {
                    class: "UnicodeDecodeError",
                    text: None,
                })
            })
            .transpose()?;
        if let Some(recorded) = recorded.filter(|r| *r != fingerprint(self.store.key.bytes())) {
            // The ring's current key when meta names it (a rekey in this
            // ring); a pending key that is meta's, which `finish_rotation`
            // would rename over the current one, is not ported; else still
            // the open's key (meta is not taken at its word).
            let current: Option<Key> = load_current(self.store.keys_dir(), self.store.store_id());
            match current {
                Some(key) if fingerprint(key.bytes()) == recorded => {
                    // `_append` keeps the key it signs with.
                    self.store.key = key;
                }
                _ if pending_is(self.store.keys_dir(), self.store.store_id(), &recorded) => {
                    return Err(self.refuse(WriteNotPorted::Rekeyed));
                }
                _ => {}
            }
        }
        let conn = &self.store.conn;
        let previous = last_row(conn)?;
        let ts = self
            .provider
            .now()
            .map_err(WriteError::Clock)?
            .isoformat_utc();
        let row = next_row(
            previous.as_ref(),
            self.store.key.bytes(),
            kind,
            payload,
            session,
            &ts,
        )
        .map_err(py_error)?;
        conn.prepare(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) \
             VALUES (?, ?, ?, ?, ?, ?, ?)",
        )?
        .execute(rusqlite::params![
            row.seq,
            row.ts,
            row.session,
            row.kind,
            row.payload,
            row.prev_mac,
            row.mac
        ])?;
        self.last = Some(row.clone());
        self.rows.push(row.clone());
        self.passed(WriteBoundary::Appended);
        Ok(row)
    }

    /// The row appended and applied (`_append`, `_apply_mutation`), for any
    /// kind the applier knows.
    fn write_row(
        &mut self,
        kind: &str,
        payload: &PyValue,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        let row = self.append(kind, payload, session)?;
        // The caller's payload, as `_mutate` applies its mapping; the row
        // holds its canonical text, which `refold` replays.
        Applier::live(&self.store.conn)
            .apply_live(kind, payload, &row.mac)
            .map_err(|(class, text)| WriteError::Refused { class, text })?;
        self.passed(WriteBoundary::Applied);
        Ok(row)
    }

    fn refuse(&mut self, why: WriteNotPorted) -> WriteError {
        if self.refused.is_none() {
            self.refused = Some(why.clone());
        }
        WriteError::NotPorted(why)
    }

    /// `Store._mutate(tx, kind, payload, session=)`: `before`, the row, the
    /// apply, the records it names (`candidates`, each kept with the first
    /// write that named it). A mutation that fails takes back what it
    /// wrote (under a savepoint of its own) and returns the error, which a
    /// writer passes out of its scope. A kind or origin this build does not
    /// write is refused with nothing written, and the transaction can then
    /// only be rolled back.
    pub fn mutate(
        &mut self,
        kind: &str,
        payload: &PyValue,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        if !KINDS.contains(&kind) {
            return Err(self.refuse(WriteNotPorted::Kind(kind.to_owned())));
        }
        if !matches!(payload, PyValue::Dict(_)) {
            return Err(WriteError::NotAnObject);
        }
        if native_origin(kind, payload) {
            return Err(self.refuse(WriteNotPorted::NativeOrigin));
        }
        self.step(|tx| {
            let state = before(&tx.store.conn, kind, payload)?;
            let row = tx.write_row(kind, payload, session)?;
            if let Some(state) = state {
                for id in candidates(&tx.store.conn, kind, &state)? {
                    tx.returns
                        .entry(id)
                        .or_insert_with(|| (state.record_id.clone(), session.map(str::to_owned)));
                }
            }
            Ok(row)
        })
    }

    /// `_mutate` for `home_put` and `standing_put`, the rows only
    /// [`super::home`]'s functions append (a direct [`Transaction::mutate`]
    /// refuses them as not ported): the row appended and applied under a
    /// savepoint of its own. Neither kind names a record for `settle`.
    pub(crate) fn mutate_home_or_standing(
        &mut self,
        kind: &'static str,
        payload: &PyValue,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        debug_assert!(matches!(kind, "home_put" | "standing_put"));
        self.step(|tx| tx.write_row(kind, payload, session))
    }

    /// `body` under a savepoint of this build's own: on failure the rows,
    /// the last row and the named records are put back as they were.
    fn step<T>(
        &mut self,
        body: impl FnOnce(&mut Self) -> Result<T, WriteError>,
    ) -> Result<T, WriteError> {
        self.savepoint("native_step", body)
    }

    fn savepoint<T, E: From<WriteError>>(
        &mut self,
        name: &str,
        body: impl FnOnce(&mut Self) -> Result<T, E>,
    ) -> Result<T, E> {
        self.require_open()?;
        let (last, returns, kept) = (self.last.clone(), self.returns.clone(), self.rows.len());
        self.store
            .conn
            .execute_batch(&format!("SAVEPOINT {name}"))
            .map_err(WriteError::from)?;
        self.depth += 1;
        let out = body(self).and_then(|v| {
            self.store
                .conn
                .execute_batch(&format!("RELEASE {name}"))
                .map_err(|e| E::from(WriteError::from(e)))?;
            Ok(v)
        });
        self.depth -= 1;
        if out.is_err() {
            self.last = last;
            self.returns = returns;
            self.rows.truncate(kept);
            if !self.store.conn.is_autocommit() {
                let back = self
                    .store
                    .conn
                    .execute_batch(&format!("ROLLBACK TO {name}; RELEASE {name}"));
                if back.is_err() {
                    // The owner's transaction is rolled back with it; the
                    // owner's commit then finds it ended (`_require_open`).
                    let _ = self.store.conn.execute_batch("ROLLBACK");
                    self.rows.clear();
                    self.last = None;
                }
            }
        }
        out
    }

    /// A nested entry of `_transaction` (`_joined`): `body` under the
    /// savepoint `nested_<depth>`. On success what it wrote commits with the
    /// transaction; on failure the last row and the named records are put
    /// back as they stood at its entry, it is rolled back to the savepoint,
    /// and the error is returned while the transaction goes on.
    pub fn scope<T, E: From<WriteError>>(
        &mut self,
        body: impl FnOnce(&mut Self) -> Result<T, E>,
    ) -> Result<T, E> {
        let name = format!("nested_{}", self.depth);
        self.savepoint(&name, body)
    }

    /// Name `record_id` for the commit's `settle`, with the record whose
    /// change named it and the session, unless a write in this transaction
    /// named it first (`_tx_returns.setdefault`, as `_put_restored` does).
    pub fn name_returned(&mut self, record_id: &str, via: &str, session: Option<&str>) {
        self.returns
            .entry(record_id.to_owned())
            .or_insert_with(|| (via.to_owned(), session.map(str::to_owned)));
    }

    /// `Store.release_returned(record_id, session=)` inside this
    /// transaction (a nested entry): whether `record_id` had a mark, which
    /// a `returned_delete {"record_id"}` row then deletes. A store whose
    /// file lacks the table has no mark.
    pub fn release_returned(
        &mut self,
        record_id: &str,
        session: Option<&str>,
    ) -> Result<bool, WriteError> {
        self.scope(|tx| {
            // `store_returned.returned_rows(store, [record_id])`.
            let read = tx
                .store
                .conn
                .prepare("SELECT record_id, via, returned FROM returned WHERE record_id IN (?)")
                .and_then(|mut s| s.exists([record_id]));
            let marked = match read {
                Ok(marked) => marked,
                Err(e) if e.to_string().contains("no such table") => false,
                Err(e) => return Err(WriteError::Sqlite(e)),
            };
            if !marked {
                return Ok(false);
            }
            let payload = dict(&[("record_id", PyValue::str(record_id))]);
            tx.step(|tx| tx.write_row("returned_delete", &payload, session))?;
            Ok(true)
        })
    }

    /// The outermost entry's end: `_require_open`, `_flush_returns`
    /// (`settle`; the `returned_put` rows, then the `returned_delete`
    /// rows), `COMMIT`, then the head moved once to the last row. Any
    /// failure before the commit returns rolls everything back.
    pub fn commit(mut self) -> Result<Committed, WriteError> {
        if let Some(why) = self.refused.clone() {
            return Err(WriteError::NotPorted(why));
        }
        self.require_open()?;
        let pending = std::mem::take(&mut self.returns);
        let (puts, deletes) = settle(&self.store.conn, &pending, self.provider)?;
        for (payload, session) in &puts {
            self.write_row("returned_put", payload, session.as_deref())?;
        }
        for (payload, session) in &deletes {
            self.write_row("returned_delete", payload, session.as_deref())?;
        }
        self.store.conn.execute_batch("COMMIT")?;
        self.open = false;
        self.passed(WriteBoundary::Committed);
        let head = match self.last.take() {
            None => None,
            Some(row) => {
                let path = self
                    .store
                    .keys_dir()
                    .join(format!("{}.head", self.store.store_id()));
                let hook = &mut self.hook;
                let moved = move_head(&path, &row, self.provider, &mut |b| {
                    if let Some(h) = hook.as_mut() {
                        h(b);
                    }
                });
                self.passed(WriteBoundary::HeadMoved);
                self.store.head = Some((row.seq, row.mac.clone()));
                Some(moved)
            }
        };
        Ok(Committed {
            rows: std::mem::take(&mut self.rows),
            head,
        })
    }
}

impl Drop for Transaction<'_> {
    /// `_roll_back`: a transaction not committed is rolled back while the
    /// connection is still in it; a rollback that fails leaves a residue
    /// the next `begin` rolls back first.
    fn drop(&mut self) {
        if self.open && !self.store.conn.is_autocommit() {
            let _ = self.store.conn.execute_batch("ROLLBACK");
        }
    }
}
