//! The search pool a caller's request ranks, read from a store file
//! without writing to it: `handlers.search.resolve_search_pool` and what
//! it calls (`_admitted_candidates`, `_admit_predicate`,
//! `_corpus_stats_provider`, `Store.ranked_rows`, `newest_rows`,
//! `admitted_share`, `admitted_rows`, `ranked_admitted_rows`,
//! `light_many`, `ranking_statistics`), and the ranking `_rank_pool` makes
//! of it (`search.search` over the pool with the pool's statistics).
//!
//! The caller arrives resolved, as data: an [`Admission`] holds what the
//! reference's `admission.Admission` answers for the rows of one store
//! (membership is not ported). Anchoring is not read: in the reference it
//! reaches only each hit's drift report, never a score, an order, a
//! relevance or a match term.
//!
//! A pool decides its rows, and its statistics price them, through the
//! document frequency memo ([`DocFreqMemo`], the process's own
//! [`docfreq::memo`] unless the reader is given another) as the reference
//! does: the memo's inputs are kept for the process by store and memory
//! head, brought forward from the log where an entry is behind
//! (`_advanced_inputs`), and each term's hits are kept beside them. Inside
//! a transaction no memo is read: each row is decided from its own
//! columns and the statistics are the uncached read, with no average.
//!
//! What differs from the reference, by design of a read-only port:
//!
//! - The memo's inputs come from the digest where it answers. Where the
//!   digest is behind the log (the state any write leaves it in), the
//!   reference's search brings it current (`digest_mark(build=False)`,
//!   `_ingest`: a write) and reads its rows; this port brings the rows
//!   forward in memory over the same log rows (`PoolReader::digest_rows`)
//!   and writes nothing, and an entry kept at an older head is brought
//!   forward from the log only where the digest is current (else it is
//!   read again, to the same inputs, after the log reads the reference's
//!   advance sends before it asks for the digest). Where `digest_mark` answers None (no
//!   digest state, another fingerprint, a log that does not hold the row
//!   the digest saw, a mutation payload or record that does not read) the
//!   inputs are the memory rows', with no average. Not reproduced: where
//!   SQLite itself cannot write the store (a read-only file, a busy lock)
//!   the reference also answers None for a digest that is behind; this
//!   port answers as a reference whose SQLite can write (a `Store.open`
//!   the key ring marks read-only still catches up and writes). Ids are
//!   read through `str()` for scalars; an id that is a list or an object,
//!   and a payload in UTF-16 or UTF-32 bytes, give no average here where
//!   the reference answers.
//! - The digest's fingerprint (`store_digest.fingerprint`, which reads the
//!   supersession rules) is passed by the caller; with none the digest is
//!   never trusted. The memo is keyed as the reference keys it (the
//!   store's path as it was opened, not a private copy's, and its store
//!   id) under the fingerprint this build computes
//!   ([`digest::fingerprint`]); under any other, or none, the key carries
//!   it, so inputs read under one are never handed to another.
//! - The reference reads a pool's records with `seed_tokens` set to
//!   `index_spelled_here()`: where the tokenizer stamp the log names is
//!   this build's, each record's token streams are split from its row's
//!   `body_fts` instead of tokenizing its body. This port reads the stamp
//!   where the reference does (its statements, and what they raise) and
//!   tokenizes the body always. The two agree wherever `body_fts` is
//!   `fts_index_text(body)` under the stamped tokenizer, which every row
//!   the reference writes is; a row whose `body_fts` was spelled otherwise
//!   under a matching stamp would rank differently. Where the stamp names
//!   another tokenizer the reference tokenizes the body too, as here.
//! - A row whose scope list holds anything but strings is refused
//!   (TypeError) where the reference's predicate may answer or raise
//!   another exception; a store the reference writes holds strings only.

use std::cell::{Cell, OnceCell, RefCell};
use std::collections::HashMap;

use rusqlite::types::ValueRef;
use rusqlite::Statement;

use super::digest::{self, DIGEST_KINDS};
use super::docfreq::{self, DocActor, DocFreqError, DocFreqInputs, DocFreqMemo, DocFreqSource};
use super::docfreq::{AdmissionRow, Head, Mask};
use super::docfreq_store::{self, DigestRow, Home, StoreDocFreq};
use super::{index_reads, record_reads, sql_value, SqlValue, StoreError, StoreFile};
use crate::datetime::DateTime;
use crate::pyvalue::{PyError, PyValue};
use crate::records::rows;
use crate::records::RecordError;
use crate::search::{Corpus, CorpusStats, Hit, Mode, Options, Record, SearchError};
use crate::{json, unicode};

/// `handlers.search.POOL_SIZE`: how many admitted candidates one search
/// ranks.
pub const POOL_SIZE: usize = 200;

/// `handlers.search._FIRST_READ`: the rows of the index's ranking, or of
/// the newest, read at a time.
pub const FIRST_READ: usize = 4 * POOL_SIZE;

/// `handlers.search.MAX_SEARCH_RESULTS`.
pub const MAX_SEARCH_RESULTS: i64 = 50;

/// `store_digest.digest_admission_rows`' select, with the record's id.
const DIGEST_ROWS: &str = "SELECT d.rid, d.scopes, h.project, d.client, d.model, d.dl, d.id \
     FROM digest_records d LEFT JOIN homes h ON h.record_id = d.id WHERE d.removed = 0";

/// `handlers.search.clamp_search_width`: a result width narrowed to
/// `[1, MAX_SEARCH_RESULTS]`.
pub fn clamp_search_width(value: i64) -> i64 {
    value.clamp(1, MAX_SEARCH_RESULTS)
}

/// Why a pool could not be drawn or ranked.
#[derive(Debug)]
pub enum PoolError {
    /// A read of the store failed, or a column held what the reference
    /// raises on (`json.loads` of a scope list among them).
    Store(StoreError),
    /// A pool record the model refuses (`light_many`'s `_row_to_memory`).
    Record(RecordError),
    /// The ranking refused (`search.search`'s exceptions).
    Search(SearchError),
    /// The memo or a row's own columns raised: the reference's exception
    /// by class (JSONDecodeError, ValidationError, IndexError, sqlite3's
    /// classes).
    Raised(String),
}

impl From<StoreError> for PoolError {
    fn from(e: StoreError) -> Self {
        PoolError::Store(e)
    }
}

impl From<rusqlite::Error> for PoolError {
    fn from(e: rusqlite::Error) -> Self {
        PoolError::Store(e.into())
    }
}

impl From<PyError> for PoolError {
    fn from(e: PyError) -> Self {
        PoolError::Store(e.into())
    }
}

impl From<DocFreqError> for PoolError {
    fn from(e: DocFreqError) -> Self {
        const PY: [PyError; 6] = [
            PyError::TypeError,
            PyError::ValueError,
            PyError::OverflowError,
            PyError::UnicodeEncodeError,
            PyError::UnicodeDecodeError,
            PyError::RecursionError,
        ];
        match e {
            DocFreqError::Py(p) => p.into(),
            DocFreqError::Source(class) => match PY.into_iter().find(|p| p.name() == class) {
                Some(p) => p.into(),
                None => PoolError::Raised(class),
            },
            DocFreqError::IndexError => PoolError::Raised("IndexError".to_owned()),
        }
    }
}

impl From<record_reads::ReadError> for PoolError {
    fn from(e: record_reads::ReadError) -> Self {
        match e {
            record_reads::ReadError::Sqlite(e) => e.into(),
            record_reads::ReadError::Record(e) => PoolError::Record(e),
            other => PoolError::Raised(other.class()),
        }
    }
}

/// A caller's admission on one surface, as data: what the reference's
/// `Admission.admits_project` answers for each project a store's
/// ``homes`` rows name (``global`` included), and what `admits_row`
/// answers for a global record by its scope list (a global record is
/// decided with the projects its scopes name, which membership reads).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Admission {
    pub projects: HashMap<String, bool>,
    pub global_rows: HashMap<Vec<String>, bool>,
}

impl Admission {
    /// `Admission.admits_row(scopes, recorded)`: a record with no home is
    /// admitted nowhere, nor is one whose project the data does not name;
    /// a global record whose scope list the data does not name is decided
    /// as ``global``.
    pub fn admits_row(&self, scopes: &[String], recorded: Option<&str>) -> bool {
        let Some(project) = recorded else {
            return false;
        };
        let by_project = self.projects.get(project).copied().unwrap_or(false);
        if project != "global" {
            return by_project;
        }
        self.global_rows.get(scopes).copied().unwrap_or(by_project)
    }
}

/// A request's filters: `scopes`, `excluded_scopes`, the admission and the
/// writer filters, as `resolve_search_pool` takes them.
#[derive(Clone, Copy, Debug, Default)]
pub struct Filters<'a> {
    pub scopes: Option<&'a [String]>,
    pub excluded: &'a [String],
    pub admission: Option<&'a Admission>,
    pub client: Option<&'a str>,
    pub model: Option<&'a str>,
}

impl Filters<'_> {
    /// `search.candidate_admitted` over a row's admission inputs.
    fn admits(
        &self,
        scopes: &[String],
        home: Option<&str>,
        client: Option<&str>,
        model: Option<&str>,
    ) -> bool {
        if !self.excluded.is_empty() && scopes.iter().any(|s| self.excluded.contains(s)) {
            return false;
        }
        if let Some(wanted) = self.scopes {
            // `set(scopes) if scopes else None`: an empty list filters nothing.
            if !wanted.is_empty() && !scopes.iter().any(|s| wanted.contains(s)) {
                return false;
            }
        }
        if let Some(admission) = self.admission {
            if !admission.admits_row(scopes, home) {
                return false;
            }
        }
        // `identity.actor_matches`: no filter passes all; no actor passes none.
        if self.client.is_none() && self.model.is_none() {
            return true;
        }
        if client.is_none() && model.is_none() {
            return false;
        }
        self.client.is_none_or(|c| client == Some(c)) && self.model.is_none_or(|m| model == Some(m))
    }

    /// `_admit_predicate(...)`: the predicate the store's row readers take,
    /// over `(scopes, home project, actor)`. A scope that is not a string
    /// admits nothing and sets `refused` (TypeError, the module's notes);
    /// a home in bytes is read as its `str()`.
    fn predicate<'p>(
        &'p self,
        refused: &'p Cell<bool>,
    ) -> impl FnMut(&[PyValue], Option<Home<'_>>, Option<&DocActor>) -> bool + 'p {
        move |scopes, home, actor| {
            let mut strings = Vec::with_capacity(scopes.len());
            for scope in scopes {
                match scope {
                    PyValue::Str(s) => match unicode::to_string(s) {
                        Some(s) => strings.push(s),
                        None => return refuse(refused),
                    },
                    _ => return refuse(refused),
                }
            }
            let spelled;
            let home = match home {
                None => None,
                Some(Home::Text(t)) => Some(t),
                Some(Home::Bytes(b)) => {
                    spelled = SqlValue::Blob(b.to_vec()).py_str();
                    Some(spelled.as_str())
                }
            };
            let (client, model) = match actor {
                Some(a) => (a.client.as_deref(), a.model.as_deref()),
                None => (None, None),
            };
            self.admits(&strings, home, client, model)
        }
    }
}

fn refuse(refused: &Cell<bool>) -> bool {
    refused.set(true);
    false
}

/// The predicate's refusal, raised after the call that met it.
fn refused_check(refused: &Cell<bool>) -> Result<(), PoolError> {
    if refused.get() {
        return Err(PyError::TypeError.into());
    }
    Ok(())
}

/// `_load_json(text, None)` of a scope list: None when it is not a list;
/// the strings of a list, TypeError for any other item.
pub(crate) fn scope_list(text: &str) -> Result<Option<Vec<String>>, StoreError> {
    if text == "[]" {
        return Ok(Some(Vec::new()));
    }
    match &json::loads(&unicode::cps(text))? {
        PyValue::List(items) => items
            .iter()
            .map(|item| match item {
                PyValue::Str(s) => unicode::to_string(s).ok_or(PyError::TypeError.into()),
                _ => Err(PyError::TypeError.into()),
            })
            .collect::<Result<Vec<String>, StoreError>>()
            .map(Some),
        _ => Ok(None),
    }
}

/// What a ranking's statistics calls are shown: the terms and the answer.
pub type StatsSeen<'a> = &'a mut dyn FnMut(&[String], &Option<CorpusStats>);

/// Reads pools from one open store. Its statements are the connection's:
/// each is prepared once and kept in the store's statement cache
/// (preparing one costs about ten microseconds against this schema, more
/// than most of a pool's reads), where the reference's `sqlite3`
/// connection keeps its own, so a pool's reads and the store's other
/// reads share the 128 statements the connection holds. The admission
/// inputs are the memo's, which every reader of the process shares.
pub struct PoolReader<'s> {
    store: &'s StoreFile,
    memo: &'s DocFreqMemo,
    /// Meta's `store_id`, read at the first pool.
    store_id: OnceCell<String>,
}

impl<'s> PoolReader<'s> {
    /// A reader through the process's memo ([`docfreq::memo`]).
    pub fn new(store: &'s StoreFile) -> PoolReader<'s> {
        PoolReader::with_memo(store, docfreq::memo())
    }

    /// A reader through `memo`.
    pub fn with_memo(store: &'s StoreFile, memo: &'s DocFreqMemo) -> PoolReader<'s> {
        PoolReader {
            store,
            memo,
            store_id: OnceCell::new(),
        }
    }

    /// Runs `body` on the statement for `sql`, taken from the connection's
    /// statement cache (prepared when it holds none) and put back when
    /// `body` returns.
    fn with<T>(
        &self,
        sql: &'static str,
        body: impl FnOnce(&mut Statement<'s>) -> Result<T, rusqlite::Error>,
    ) -> Result<T, rusqlite::Error> {
        let mut statement = self.store.connection().prepare_cached(sql)?;
        body(&mut statement)
    }

    /// `store_digest._mark`: whether the digest has seen the log's last
    /// record mutation under the rules `fingerprint` names. A read that
    /// fails is not current, as `digest_mark` catches it.
    pub fn digest_current(&self, fingerprint: &str) -> bool {
        matches!(self.digest_built(fingerprint), Ok(Some((seen, head))) if seen == head)
    }

    /// `store_digest._built` and `_head`: the sequence number the digest
    /// has seen, and the log's last record mutation; None when the digest
    /// has to be rebuilt (no state, another fingerprint, a seq that is not
    /// digits, a log whose row at that number is not the one it saw).
    fn digest_built(&self, fingerprint: &str) -> Result<Option<(i64, i64)>, rusqlite::Error> {
        // A store with no digest table: `_state` is None.
        let Ok(state) = self.with("SELECT key, value FROM digest_state", |statement| {
            let mut state: HashMap<String, Option<String>> = HashMap::new();
            let mut rows = statement.query([])?;
            while let Some(row) = rows.next()? {
                let k = sql_value(row.get_ref(0)?).ok().map(|v| v.py_str());
                let v = sql_value(row.get_ref(1)?).ok().map(|v| v.py_str());
                if let Some(k) = k {
                    state.insert(k, v);
                }
            }
            Ok(state)
        }) else {
            return Ok(None);
        };
        let get = |key: &str| -> Option<&str> {
            state.get(key).map(|v| v.as_deref().unwrap_or("\u{fffd}"))
        };
        if get("fingerprint") != Some(fingerprint) {
            return Ok(None);
        }
        let Some(seen) = get("seq") else {
            return Ok(None);
        };
        // `str.isdigit` then `int`: ASCII digits only here (a Unicode digit
        // string is a digest no build writes).
        if seen.is_empty() || !seen.bytes().all(|b| b.is_ascii_digit()) {
            return Ok(None);
        }
        let Ok(seen) = seen.parse::<i64>() else {
            return Ok(None);
        };
        let mac_at = if seen <= 0 {
            Some(String::new())
        } else {
            self.with("SELECT mac FROM log WHERE seq = ?", |statement| {
                let mut rows = statement.query([seen])?;
                Ok(match rows.next()? {
                    None => Some(String::new()),
                    Some(row) => match sql_value(row.get_ref(0)?) {
                        Ok(SqlValue::Null) => Some(String::new()),
                        Ok(v) => Some(v.py_str()),
                        Err(_) => None,
                    },
                })
            })?
        };
        if mac_at.as_deref() != Some(get("mac").unwrap_or("")) {
            return Ok(None);
        }
        // `_head`: the greatest of each kind's last seq, 0 with none.
        let head = self.with(
            "SELECT MAX(seq) FROM log WHERE kind IN (?, ?, ?, ?)",
            |statement| {
                statement.query_row(DIGEST_KINDS, |row| {
                    Ok(match row.get_ref(0)? {
                        ValueRef::Integer(seq) => seq.max(0),
                        _ => 0,
                    })
                })
            },
        )?;
        Ok(Some((seen, head)))
    }

    /// `digest_admission_rows` behind `digest_mark(build=False)`: the
    /// active records as the digest holds them, brought forward in memory
    /// over the record mutations the log holds after the digest's mark,
    /// as `_ingest` writes them (`_mutated_ids`, `_index_record`): each
    /// mutated id's active entry rewritten from its memory row (its row
    /// number, scopes, writer and BM25 length) or dropped when it has
    /// none. None where the reference's `digest_mark` answers None (no
    /// digest to bring forward, a payload or record that does not read)
    /// or its read is caught (an SQLite error, a text that is not UTF-8);
    /// `int()` of a row number or length that does not read raises.
    fn digest_rows(&self, fingerprint: &str) -> Result<Option<Vec<DigestRow>>, DocFreqError> {
        let Some((seen, head)) = self.digest_built(fingerprint).ok().flatten() else {
            return Ok(None);
        };
        let read = self.with(DIGEST_ROWS, |statement| {
            let mut rows = statement.query([])?;
            let mut out = Vec::new();
            while let Some(row) = rows.next()? {
                out.push((sql_value(row.get_ref(6)?), docfreq_store::cells(row)));
            }
            Ok(out)
        });
        let Ok(read) = read else {
            return Ok(None);
        };
        let mut rows: Vec<(String, Vec<SqlValue>)> = Vec::with_capacity(read.len());
        for (id, cells) in read {
            let (Ok(id), Ok(cells)) = (id, cells) else {
                return Ok(None);
            };
            rows.push((id.py_str(), cells));
        }
        if seen != head {
            let Some(forward) = self.brought_forward(rows, seen) else {
                return Ok(None);
            };
            rows = forward;
        }
        let mut out = Vec::with_capacity(rows.len());
        for (_, cells) in rows {
            out.push(docfreq_store::digest_row(cells, None)?);
        }
        Ok(Some(out))
    }

    /// The digest's `rows` brought forward over the log's record
    /// mutations after `seen`; None where `_ingest` would raise.
    fn brought_forward(
        &self,
        mut rows: Vec<(String, Vec<SqlValue>)>,
        seen: i64,
    ) -> Option<Vec<(String, Vec<SqlValue>)>> {
        let (ids, _) = digest::mutated_ids(self.store.connection(), seen).ok()?;
        let mut entries: HashMap<String, Option<Vec<SqlValue>>> = HashMap::new();
        for id in &ids {
            let entry = self.indexed_entry(id)?;
            // `_index_record(conn, True, id)` reads the tombstone through
            // the model too.
            digest::read_entry(self.store.connection(), true, id).ok()?;
            entries.insert(id.clone(), entry);
        }
        rows.retain(|(id, _)| !entries.contains_key(id));
        for id in &ids {
            if let Some(Some(cells)) = entries.remove(id) {
                rows.push((id.clone(), cells));
            }
        }
        Some(rows)
    }

    /// `_index_record(conn, False, id)` in memory ([`digest::read_entry`]):
    /// the digest's columns for `id` once brought forward (None: the
    /// memory row is gone), or None outright where the record does not
    /// read.
    fn indexed_entry(&self, id: &str) -> Option<Option<Vec<SqlValue>>> {
        let Some(entry) = digest::read_entry(self.store.connection(), false, id).ok()? else {
            return Some(None);
        };
        let home = self
            .with(
                "SELECT project FROM homes WHERE record_id = ?",
                |statement| {
                    let mut rows = statement.query([&entry.id])?;
                    Ok(match rows.next()? {
                        None => Ok(SqlValue::Null),
                        Some(row) => sql_value(row.get_ref(0)?),
                    })
                },
            )
            .ok()?
            .ok()?;
        let dl = digest::features(&entry.body).dl;
        // `_put_entry` hashes the body as UTF-8: a lone surrogate raises.
        unicode::to_string(&entry.body)?;
        Some(Some(vec![
            SqlValue::Integer(entry.rid),
            SqlValue::Text(entry.scopes),
            home,
            entry.client,
            entry.model,
            SqlValue::Integer(i64::try_from(dl).ok()?),
        ]))
    }

    /// The memo's source for a pool read under `fingerprint`.
    fn source<'r>(&'r self, fingerprint: Option<&'r str>) -> Result<PoolSource<'r, 's>, PoolError> {
        let store_id = match self.store_id.get() {
            Some(id) => id.clone(),
            None => {
                let meta = self.store.meta()?;
                let id = meta
                    .into_iter()
                    .find(|(k, _)| k == "store_id")
                    .map_or_else(String::new, |(_, v)| v);
                self.store_id.get_or_init(|| id).clone()
            }
        };
        let inner = StoreDocFreq::over(self.store.connection(), self.store.path(), &store_id);
        let (path, id) = inner.store_key();
        // A NUL is in no store id a build writes (`_STORE_ID_PATTERN`).
        let id = match fingerprint {
            Some(f) if f == digest::fingerprint() => id,
            Some(f) => format!("{id}\u{0}rules {f}"),
            None => format!("{id}\u{0}no rules"),
        };
        Ok(PoolSource {
            reader: self,
            inner,
            fingerprint,
            key: (path, id),
        })
    }

    /// `light_many(rowids)`: the rows read by the store's light read (one
    /// statement per batch, the text `Store.light_many` sends for as many
    /// rows, with no attestation payload), each into the model and then
    /// into the ranker's record, in the order asked; unknown row numbers
    /// skipped.
    fn light_many(&self, rowids: &[i64]) -> Result<Vec<Record>, PoolError> {
        super::record_reads::light_many_with(self.store.connection(), rowids, |row| {
            let memory = rows::memory(row).map_err(PoolError::Record)?;
            memory
                .to_search_record()
                .ok_or(PoolError::Store(StoreError::Py(
                    PyError::UnicodeEncodeError,
                )))
        })
    }

    /// `resolve_search_pool(store, query, ...)` with the pool size `size`
    /// (`_admitted_candidates`): the first `size` rows the request admits,
    /// in the full-text index's order for `query` (`bm25()`, then row
    /// number), or newest first for a query that is empty after
    /// `str.strip`, read as `light_many` reads them. A non-empty query
    /// reads the index's best [`FIRST_READ`] rows where the request admits
    /// enough of the store to fill the pool from them (`admitted_share`),
    /// and the whole ranking a batch at a time where it does not or they
    /// fell short (`ranked_admitted_rows`); an empty one pages the newest
    /// rows [`FIRST_READ`] at a time. `digest_fingerprint` is the digest
    /// fingerprint the reference's rules give
    /// (`store_digest.fingerprint()`); None never trusts the digest.
    pub fn resolve_search_pool<'r>(
        &'r self,
        query: &str,
        filters: &Filters<'r>,
        size: usize,
        digest_fingerprint: Option<&'r str>,
    ) -> Result<SearchPool<'r, 's>, PoolError> {
        let source = self.source(digest_fingerprint)?;
        let conn = self.store.connection();
        let refused = Cell::new(false);
        let mut admit = filters.predicate(&refused);
        let first = FIRST_READ as i64;
        let mut rows: Vec<i64> = Vec::new();
        if query.chars().all(|c| unicode::is_space(c as u32)) {
            let mut offset = 0;
            while rows.len() < size {
                let newest = index_reads::newest_rows(conn, first, offset)?;
                rows.extend(docfreq_store::admitted_rows(
                    &source, conn, self.memo, &newest, &mut admit,
                )?);
                refused_check(&refused)?;
                if newest.len() < FIRST_READ {
                    break;
                }
                offset += newest.len() as i64;
            }
        } else {
            let share = docfreq_store::admitted_share(&source, self.memo, &mut admit)?;
            refused_check(&refused)?;
            // `share[0] * _FIRST_READ >= size * share[1]`, in Python ints.
            let mut wide = share.is_none_or(|(admitted, all)| {
                admitted as u128 * FIRST_READ as u128 >= size as u128 * all as u128
            });
            if wide {
                let ranked = index_reads::ranked_rows(conn, &unicode::cps(query), Some(first))?;
                rows = docfreq_store::admitted_rows(&source, conn, self.memo, &ranked, &mut admit)?;
                refused_check(&refused)?;
                wide = rows.len() >= size || ranked.len() < FIRST_READ;
            }
            if !wide {
                rows = docfreq_store::ranked_admitted_rows(
                    &source, conn, self.memo, query, &mut admit, size, first,
                )?;
                refused_check(&refused)?;
            }
        }
        rows.truncate(size);
        // `index_spelled_here()`, which the reference asks before it reads
        // a pool's records, an empty pool's too: the tokenizer the log
        // names. Its answer is not used here (the module's notes).
        super::fold::stored_fingerprint(conn)?;
        let records = self.light_many(&rows)?;
        Ok(SearchPool {
            source,
            filters: *filters,
            rowids: rows,
            records,
        })
    }
}

/// The memo's source for a pool: the store's own ([`StoreDocFreq`]) for
/// every read but the digest's rows, which come forward in memory
/// ([`PoolReader::digest_rows`]) so that nothing is written, and the
/// advance of a kept entry, which reads the log as the store's does and
/// then is taken only where the digest is current.
struct PoolSource<'r, 's> {
    reader: &'r PoolReader<'s>,
    inner: StoreDocFreq<'s>,
    fingerprint: Option<&'r str>,
    key: (String, String),
}

impl DocFreqSource for PoolSource<'_, '_> {
    fn memory_head(&self) -> Result<Head, DocFreqError> {
        self.inner.memory_head()
    }

    fn store_key(&self) -> (String, String) {
        self.key.clone()
    }

    fn in_transaction(&self) -> bool {
        self.inner.in_transaction()
    }

    fn admission_rows(&self) -> Result<(Vec<AdmissionRow>, bool), DocFreqError> {
        if let Some(fingerprint) = self.fingerprint {
            if let Some(rows) = self.reader.digest_rows(fingerprint)? {
                return Ok((docfreq_store::typed(rows)?, true));
            }
        }
        Ok((self.inner.memory_admission_rows()?, false))
    }

    fn advanced_inputs(
        &self,
        entry: &DocFreqInputs,
        head: &Head,
    ) -> Result<Option<DocFreqInputs>, DocFreqError> {
        // The log row at the entry's head and the log rows since are read
        // first, as the reference reads them before it asks
        // `digest_mark(build=False)`; the digest is then trusted only where
        // it is current under this build's rules, so that the store's own
        // `digest_mark` reads and never writes.
        self.inner
            .advanced_inputs_marked(entry, head, || match self.fingerprint {
                Some(f) if f == digest::fingerprint() && self.reader.digest_current(f) => {
                    Ok(self.inner.digest_mark()?.is_some())
                }
                _ => Ok(false),
            })
    }

    fn term_masks(&self, term: &str, inputs: &DocFreqInputs) -> Result<(Mask, Mask), DocFreqError> {
        self.inner.term_masks(term, inputs)
    }

    fn fts_rowids(&self, expr: &str, low: i64, limit: usize) -> Result<Vec<i64>, DocFreqError> {
        self.inner.fts_rowids(expr, low, limit)
    }
}

/// A drawn pool: its records in pool order, and what prices them.
pub struct SearchPool<'r, 's> {
    source: PoolSource<'r, 's>,
    filters: Filters<'r>,
    /// The pool's row numbers, in pool order.
    pub rowids: Vec<i64>,
    /// The pool's records as a ranking reads them, in pool order.
    pub records: Vec<Record>,
}

impl SearchPool<'_, '_> {
    /// `Store.ranking_statistics(terms, admit=...)`, read when called: the
    /// admitted collection's size, each term's document frequency in the
    /// body and in the body or scopes, and the admitted rows' average BM25
    /// length where the memo's inputs carry lengths. Through the memo
    /// outside a transaction; inside one the uncached read, with no
    /// average. None for no term or nothing admitted.
    pub fn statistics(&self, terms: &[String]) -> Result<Option<CorpusStats>, PoolError> {
        let refused = Cell::new(false);
        let mut admit = self.filters.predicate(&refused);
        let found = docfreq_store::ranking_statistics_from(
            &self.source,
            &self.source.inner,
            self.source.reader.memo,
            terms,
            &mut admit,
        )?;
        refused_check(&refused)?;
        let counts = |dfs: Vec<(String, usize)>| -> HashMap<String, i64> {
            dfs.into_iter().map(|(t, n)| (t, n as i64)).collect()
        };
        Ok(found.map(|(size, body, scope, avgdl)| CorpusStats {
            size: size as i64,
            body_df: counts(body),
            scope_df: counts(scope),
            avgdl,
        }))
    }

    /// The pool's record ids, in pool order.
    pub fn ids(&self) -> Vec<&str> {
        self.records.iter().map(|r| r.id.as_str()).collect()
    }

    /// `_rank_pool`'s `search.search` over this pool, priced with its
    /// statistics: `mode`, `max_results`, the configured half-life and
    /// lane (`BehaviorConfig`: 30 days, on; constants here because
    /// `config.FIXED_SETTINGS` fixes both since 9.0.0rc20, so no config
    /// changes them), no rescue leg, no empty-query
    /// browse. The request's filters are not applied again: the pool holds
    /// only rows they admit, and the reference's second pass runs the same
    /// predicate. `stats_seen` is given each statistics call's terms and
    /// answer, in order.
    pub fn rank(
        &self,
        query: &str,
        now: &DateTime,
        mode: Mode,
        max_results: i64,
        stats_seen: StatsSeen<'_>,
    ) -> Result<Vec<Hit>, PoolError> {
        let corpus = Corpus::new(&self.records).map_err(PoolError::Search)?;
        let failed: RefCell<Option<PoolError>> = RefCell::new(None);
        let seen: RefCell<StatsSeen<'_>> = RefCell::new(stats_seen);
        let provider = |terms: &[String]| -> Option<CorpusStats> {
            match self.statistics(terms) {
                Ok(found) => {
                    (seen.borrow_mut())(terms, &found);
                    found
                }
                Err(e) => {
                    failed.borrow_mut().get_or_insert(e);
                    None
                }
            }
        };
        let opts = Options {
            mode,
            max_results,
            half_life_days: 30.0,
            conversational: true,
            rescue_expansion: false,
            allow_empty_query: false,
            corpus_stats_provider: Some(&provider),
            ..Options::default()
        };
        let hits = corpus.search(&unicode::cps(query), now, &opts);
        if let Some(e) = failed.into_inner() {
            return Err(e);
        }
        hits.map_err(PoolError::Search)
    }
}
