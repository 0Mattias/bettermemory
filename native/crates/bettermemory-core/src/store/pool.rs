//! The search pool a caller's request ranks, read from a store file
//! without writing to it: `handlers.search.resolve_search_pool` and what
//! it calls (`_admitted_candidates`, `_admit_predicate`,
//! `_corpus_stats_provider`, `Store.ranked_rows`, `newest_rows`,
//! `admitted_rows`, `light_many`, `ranking_statistics`), and the ranking
//! `_rank_pool` makes of it (`search.search` over the pool with the pool's
//! statistics).
//!
//! The caller arrives resolved, as data: an [`Admission`] holds what the
//! reference's `admission.Admission` answers for the rows of one store
//! (membership is not ported). Anchoring is not read: in the reference it
//! reaches only each hit's drift report, never a score, an order, a
//! relevance or a match term.
//!
//! What differs from the reference, by design of a read-only port:
//!
//! - The statistics' average BM25 length comes from the digest. Where the
//!   digest is behind the log (the state any write leaves it in), the
//!   reference's search brings it current (`digest_mark(build=False)`,
//!   `_ingest`: a write) and reads its lengths; this port brings the rows
//!   forward in memory over the same log rows (`PoolReader::digest_rows`)
//!   and writes nothing. Where `digest_mark` answers None (no digest state,
//!   another fingerprint, a log that does not hold the row the digest saw,
//!   a mutation payload or record that does not read) there is no average
//!   here either. Not reproduced: where SQLite itself cannot write the
//!   store (a read-only file, a busy lock) the reference also answers None
//!   for a digest that is behind; this port answers as a reference whose
//!   SQLite can write (a `Store.open` the key ring marks read-only still
//!   catches up and writes). Ids are read through `str()` for scalars; an
//!   id that is a list or an object, and a payload in UTF-16 or UTF-32
//!   bytes, give no average here where the reference answers.
//! - The digest's fingerprint (`store_digest.fingerprint`, which reads the
//!   supersession rules) is not computed here: the caller passes the one
//!   the reference names, and with none the digest is never trusted.
//! - The document frequency memo keeps the rows' admission inputs per
//!   [`PoolReader`], keyed by the memory head (as the reference keys it)
//!   and the digest fingerprint passed, but not each term's hits: each call
//!   reads the index for its terms (the reference's memo answers as the
//!   uncached read does). The reference brings a memo kept at an older
//!   head forward from the log rows since (`_advanced_inputs`); this one
//!   reads the inputs again.
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
//!   A row number or BM25 length past 64 bits is OverflowError here, where
//!   the reference's `int()` would carry it.

use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap};
use std::rc::Rc;

use rusqlite::types::ValueRef;
use rusqlite::Statement;

use super::digest::{self, DIGEST_KINDS};
use super::{sql_value, SqlValue, StoreError, StoreFile};
use crate::datetime::DateTime;
use crate::pyvalue::{PyError, PyValue};
use crate::records::rows;
use crate::records::RecordError;
use crate::search::num::int_true_div;
use crate::search::{Corpus, CorpusStats, Hit, Mode, Options, Record, SearchError};
use crate::unicode;
use crate::{json, lexicon};

/// `handlers.search.POOL_SIZE`: how many admitted candidates one search
/// ranks.
pub const POOL_SIZE: usize = 200;

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
    fn admits(&self, row: &InputRow) -> bool {
        if !self.excluded.is_empty() && row.scopes.iter().any(|s| self.excluded.contains(s)) {
            return false;
        }
        if let Some(wanted) = self.scopes {
            // `set(scopes) if scopes else None`: an empty list filters nothing.
            if !wanted.is_empty() && !row.scopes.iter().any(|s| wanted.contains(s)) {
                return false;
            }
        }
        if let Some(admission) = self.admission {
            if !admission.admits_row(&row.scopes, row.home.as_deref()) {
                return false;
            }
        }
        // `identity.actor_matches`: no filter passes all; no actor passes none.
        if self.client.is_none() && self.model.is_none() {
            return true;
        }
        if row.client.is_none() && row.model.is_none() {
            return false;
        }
        if let Some(c) = self.client {
            if row.client.as_deref() != Some(c) {
                return false;
            }
        }
        if let Some(m) = self.model {
            if row.model.as_deref() != Some(m) {
                return false;
            }
        }
        true
    }
}

/// One memory row's admission inputs: its scopes, the project its
/// ``homes`` row names, its declared writer, its BM25 length (0 where the
/// digest did not answer).
#[derive(Clone, Debug)]
struct InputRow {
    rid: i64,
    scopes: Vec<String>,
    home: Option<String>,
    client: Option<String>,
    model: Option<String>,
    dl: i64,
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

/// `(seq, mac)` of the log's last memory, home or index-rebuild row.
type MemoryHead = (i64, String);

/// The memo: its memory head, the digest fingerprint it was read under,
/// the inputs.
type Memo = (MemoryHead, Option<String>, Rc<Inputs>);

/// What a ranking's statistics calls are shown: the terms and the answer.
pub type StatsSeen<'a> = &'a mut dyn FnMut(&[String], &Option<CorpusStats>);

/// Reads pools from one open store. Its statements are the connection's:
/// each is prepared once and kept in the store's statement cache
/// (preparing one costs about ten microseconds against this schema, more
/// than most of a pool's reads), where the reference's `sqlite3`
/// connection keeps its own, so a pool's reads and the store's other
/// reads share the 128 statements the connection holds. A caller that
/// draws many pools from a store holds one reader, which keeps the
/// admission inputs.
pub struct PoolReader<'s> {
    store: &'s StoreFile,
    /// The document frequency memo's inputs, the memory head they were
    /// read at (`store_index._current_inputs`) and the digest fingerprint
    /// they were read under: read again after the log's last memory, home
    /// or index-rebuild row moved, or under another fingerprint.
    memo: RefCell<Option<Memo>>,
}

impl<'s> PoolReader<'s> {
    pub fn new(store: &'s StoreFile) -> PoolReader<'s> {
        PoolReader {
            store,
            memo: RefCell::new(None),
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
    /// none. None where the reference's `digest_mark` answers None: no
    /// digest to bring forward, a payload or record that does not read,
    /// an SQLite error.
    fn digest_rows(&self, fingerprint: &str) -> Option<Vec<RawInput>> {
        let (seen, head) = self.digest_built(fingerprint).ok()??;
        let mut rows: Vec<(String, RawInput)> = self
            .with(DIGEST_ROWS, |statement| {
                let mut rows = statement.query([])?;
                let mut out = Vec::new();
                while let Some(row) = rows.next()? {
                    let id = sql_value(row.get_ref(6)?).map(|v| v.py_str());
                    out.push((id, raw_input(row)?));
                }
                Ok(out)
            })
            .ok()?
            .into_iter()
            .map(|(id, raw)| id.map(|id| (id, raw)))
            .collect::<Result<_, _>>()
            .ok()?;
        if seen == head {
            return Some(rows.into_iter().map(|(_, raw)| raw).collect());
        }
        let (ids, _) = digest::mutated_ids(self.store.connection(), seen).ok()?;
        let mut entries: HashMap<String, Option<RawInput>> = HashMap::new();
        for id in &ids {
            let entry = self.indexed_entry(id)?;
            // `_index_record(conn, True, id)` reads the tombstone through
            // the model too.
            digest::read_entry(self.store.connection(), true, id).ok()?;
            entries.insert(id.clone(), entry);
        }
        rows.retain(|(id, _)| !entries.contains_key(id));
        let mut out: Vec<RawInput> = rows.into_iter().map(|(_, raw)| raw).collect();
        for id in &ids {
            if let Some(Some(raw)) = entries.remove(id) {
                out.push(raw);
            }
        }
        Some(out)
    }

    /// `_index_record(conn, False, id)` in memory ([`digest::read_entry`]):
    /// the active entry the digest holds for `id` once brought forward
    /// (None: the memory row is gone), or None outright where the record
    /// does not read.
    fn indexed_entry(&self, id: &str) -> Option<Option<RawInput>> {
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
        Some(Some(RawInput {
            rid: rusqlite::types::Value::Integer(entry.rid),
            scopes: rusqlite::types::Value::Text(entry.scopes),
            home: to_value(home),
            client: to_value(entry.client),
            model: to_value(entry.model),
            dl: rusqlite::types::Value::Integer(i64::try_from(dl).ok()?),
        }))
    }

    /// `store_index._memory_head`: the log's last memory, home or
    /// index-rebuild row, `(seq, mac)`; `(0, "")` with none.
    fn memory_head(&self) -> Result<MemoryHead, StoreError> {
        let head = self.with(
            "SELECT seq, mac FROM log WHERE seq = (SELECT MAX(s) FROM (\
             SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_put' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_delete' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_put' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_delete' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'index_rebuilt'))",
            |statement| {
                let mut rows = statement.query([])?;
                Ok(match rows.next()? {
                    None => None,
                    Some(row) => Some((
                        match row.get_ref(0)? {
                            ValueRef::Integer(i) => Some(i),
                            _ => None,
                        },
                        sql_value(row.get_ref(1)?),
                    )),
                })
            },
        )?;
        match head {
            None => Ok((0, String::new())),
            Some((Some(seq), Ok(mac))) => Ok((seq, mac.py_str())),
            Some((None, _)) => Err(PyError::TypeError.into()),
            Some((_, Err(e))) => Err(e),
        }
    }

    /// The memo's inputs at the store's memory head (`_current_inputs`),
    /// read when the head moved since they were.
    fn current_inputs(&self, fingerprint: Option<&str>) -> Result<Rc<Inputs>, StoreError> {
        let head = self.memory_head()?;
        if let Some((seen, under, inputs)) = self.memo.borrow().as_ref() {
            if *seen == head && under.as_deref() == fingerprint {
                return Ok(Rc::clone(inputs));
            }
        }
        let inputs = Rc::new(self.read_inputs(fingerprint)?);
        *self.memo.borrow_mut() = Some((head, fingerprint.map(str::to_owned), Rc::clone(&inputs)));
        Ok(inputs)
    }

    /// Every memory row's admission inputs (`Store._read_doc_freq_inputs`):
    /// from the digest, brought forward, with each row's BM25 length where
    /// it answers (`digest_rows`); else from the memory rows, with none. A
    /// value `int()` refuses, or a scope list that does not parse, raises
    /// as there.
    fn read_inputs(&self, fingerprint: Option<&str>) -> Result<Inputs, StoreError> {
        if let Some(raw) = fingerprint.and_then(|f| self.digest_rows(f)) {
            return Ok(index(parse_rows(raw)?, true));
        }
        Ok(index(
            parse_rows(self.raw_rows(ADMISSION_INPUTS_SELECT)?)?,
            false,
        ))
    }

    fn raw_rows(&self, sql: &'static str) -> Result<Vec<RawInput>, rusqlite::Error> {
        self.with(sql, |statement| {
            let mut rows = statement.query([])?;
            let mut out = Vec::new();
            while let Some(row) = rows.next()? {
                out.push(raw_input(row)?);
            }
            Ok(out)
        })
    }

    /// `ADMISSION_INPUTS_SELECT`'s row for `rid`, as `_admitted_rows_read`
    /// reads a row the inputs do not hold.
    fn own_input(&self, rid: i64) -> Result<Option<InputRow>, StoreError> {
        let raw = self.with(ADMISSION_INPUT_OF_ROW, |statement| {
            let mut rows = statement.query([rid])?;
            match rows.next()? {
                Some(row) => Ok(Some(raw_input(row)?)),
                None => Ok(None),
            }
        })?;
        Ok(match raw {
            Some(raw) => parse_rows(vec![raw])?.pop(),
            None => None,
        })
    }

    /// `ranked_rows(query)`: the rows the query matches, by `bm25()` then
    /// row number; none for a query with no token.
    fn ranked_rows(&self, query: &str) -> Result<Vec<i64>, StoreError> {
        let match_query = lexicon::fts_match_query(&unicode::cps(query));
        if match_query.is_empty() {
            return Ok(Vec::new());
        }
        Ok(self.with(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? \
             ORDER BY bm25(memories_fts), rowid",
            |statement| row_numbers(statement, [match_query.as_str()]),
        )?)
    }

    /// `newest_rows` read whole: newest ``updated`` first, the greater id
    /// first among equals.
    fn newest_rows(&self) -> Result<Vec<i64>, StoreError> {
        Ok(self.with(
            "SELECT rowid FROM memories ORDER BY updated DESC, id DESC",
            |statement| row_numbers(statement, []),
        )?)
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

    /// The rows of the inputs `match_expr` matches, as a mask over
    /// positions.
    fn matching(&self, inputs: &Inputs, match_expr: &str) -> Result<Vec<bool>, StoreError> {
        let rids = self.with(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
            |statement| row_numbers(statement, [match_expr]),
        )?;
        let mut mask = vec![false; inputs.rows.len()];
        for rid in rids {
            if let Some(&p) = inputs.index.get(&rid) {
                mask[p] = true;
            }
        }
        Ok(mask)
    }

    /// `resolve_search_pool(store, query, ...)` with the pool size `size`:
    /// the first `size` rows the request admits, in the full-text index's
    /// order for `query` (`ranked_rows`: `bm25()`, then row number), or
    /// newest first for a query that is empty after `str.strip`, read as
    /// `light_many` reads them. `digest_fingerprint` is the digest
    /// fingerprint the reference's rules give (`store_digest.fingerprint()`);
    /// None never trusts the digest.
    pub fn resolve_search_pool(
        &self,
        query: &str,
        filters: &Filters<'_>,
        size: usize,
        digest_fingerprint: Option<&str>,
    ) -> Result<SearchPool<'_, 's>, PoolError> {
        let inputs = self.current_inputs(digest_fingerprint)?;
        let admitted: Vec<bool> = inputs.rows.iter().map(|r| filters.admits(r)).collect();
        let candidates = if query.chars().all(|c| unicode::is_space(c as u32)) {
            self.newest_rows()?
        } else {
            self.ranked_rows(query)?
        };
        let mut rowids = Vec::new();
        for rid in candidates {
            if rowids.len() >= size {
                break;
            }
            let ok = match inputs.index.get(&rid) {
                Some(&p) => admitted[p],
                None => self.own_input(rid)?.is_some_and(|r| filters.admits(&r)),
            };
            if ok {
                rowids.push(rid);
            }
        }
        // `index_spelled_here()`, which the reference asks before it reads
        // a pool's records, an empty pool's too: the tokenizer the log
        // names. Its answer is not used here (the module's notes).
        super::fold::stored_fingerprint(self.store.connection())?;
        let records = self.light_many(&rowids)?;
        Ok(SearchPool {
            reader: self,
            rowids,
            records,
            inputs,
            admitted,
        })
    }
}

/// `ADMISSION_INPUTS_SELECT` for one row.
const ADMISSION_INPUT_OF_ROW: &str = "SELECT m.rowid, m.scopes_json, h.project, m.actor_client, \
     m.actor_model, 0 FROM memories m LEFT JOIN homes h ON h.record_id = m.id WHERE m.rowid = ?";

fn row_numbers<P: rusqlite::Params>(
    statement: &mut Statement<'_>,
    params: P,
) -> Result<Vec<i64>, rusqlite::Error> {
    let mut rows = statement.query(params)?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        if let ValueRef::Integer(i) = row.get_ref(0)? {
            out.push(i);
        }
    }
    Ok(out)
}

/// One admission-input row as SQLite gives it.
struct RawInput {
    rid: rusqlite::types::Value,
    scopes: rusqlite::types::Value,
    home: rusqlite::types::Value,
    client: rusqlite::types::Value,
    model: rusqlite::types::Value,
    dl: rusqlite::types::Value,
}

fn raw_input(row: &rusqlite::Row<'_>) -> Result<RawInput, rusqlite::Error> {
    Ok(RawInput {
        rid: row.get(0)?,
        scopes: row.get(1)?,
        home: row.get(2)?,
        client: row.get(3)?,
        model: row.get(4)?,
        dl: row.get(5)?,
    })
}

fn opt_value(v: &rusqlite::types::Value) -> Result<Option<String>, StoreError> {
    Ok(match sql_value(ValueRef::from(v))? {
        SqlValue::Null => None,
        other => Some(other.py_str()),
    })
}

/// The rows whose scopes are a list (the others are left out, as the
/// reference leaves them out), parsed: first every row's number and
/// length through `int()` (the reference's list comprehension), then each
/// row's scopes (its loop).
fn parse_rows(raw: Vec<RawInput>) -> Result<Vec<InputRow>, StoreError> {
    let mut numbers = Vec::with_capacity(raw.len());
    for r in &raw {
        numbers.push((py_int(&r.rid)?, py_int(&r.dl)?));
    }
    let mut out = Vec::with_capacity(raw.len());
    for (r, (rid, dl)) in raw.into_iter().zip(numbers) {
        let text = sql_value(ValueRef::from(&r.scopes))?.py_str();
        let Some(scopes) = scope_list(&text)? else {
            continue;
        };
        out.push(InputRow {
            rid,
            scopes,
            home: opt_value(&r.home)?,
            client: opt_value(&r.client)?,
            model: opt_value(&r.model)?,
            dl,
        });
    }
    Ok(out)
}

/// Python's `int()` of a value sqlite3 gives: an integer as it is, a real
/// truncated toward zero, text or bytes read as an integer's spelling;
/// ValueError, OverflowError or TypeError where `int()` raises them (a
/// value past 64 bits is OverflowError here; the reference would carry it).
fn py_int(v: &rusqlite::types::Value) -> Result<i64, StoreError> {
    use rusqlite::types::Value;
    let text = match v {
        Value::Integer(i) => return Ok(*i),
        Value::Real(f) => {
            if f.is_nan() {
                return Err(PyError::ValueError.into());
            }
            let t = f.trunc();
            if !(-9.223_372_036_854_776e18..9.223_372_036_854_776e18).contains(&t) {
                return Err(PyError::OverflowError.into());
            }
            return Ok(t as i64);
        }
        Value::Null => return Err(PyError::TypeError.into()),
        Value::Text(t) => unicode::cps(t),
        Value::Blob(b) => match std::str::from_utf8(b) {
            Ok(t) if t.is_ascii() => unicode::cps(t),
            _ => return Err(PyError::ValueError.into()),
        },
    };
    match &crate::pyvalue::int_from_str(&text)? {
        PyValue::Int(decimal) => decimal
            .parse::<i64>()
            .map_err(|_| PyError::OverflowError.into()),
        _ => Err(PyError::ValueError.into()),
    }
}

fn to_value(v: SqlValue) -> rusqlite::types::Value {
    use rusqlite::types::Value;
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(i) => Value::Integer(i),
        SqlValue::Real(f) => Value::Real(f),
        SqlValue::Text(t) => Value::Text(t),
        SqlValue::Blob(b) => Value::Blob(b),
    }
}

/// Every memory row's admission inputs, by position, and whether they
/// carry BM25 lengths.
struct Inputs {
    rows: Vec<InputRow>,
    index: HashMap<i64, usize>,
    lengths: bool,
}

/// `store_index._ADMISSION_INPUTS`, its columns in the digest select's
/// order.
const ADMISSION_INPUTS_SELECT: &str = "SELECT m.rowid, m.scopes_json, h.project, m.actor_client, \
     m.actor_model, 0 FROM memories m LEFT JOIN homes h ON h.record_id = m.id";

fn index(rows: Vec<InputRow>, lengths: bool) -> Inputs {
    let index = rows.iter().enumerate().map(|(i, r)| (r.rid, i)).collect();
    Inputs {
        rows,
        index,
        lengths,
    }
}

/// A drawn pool: its records in pool order, and what prices them.
pub struct SearchPool<'r, 's> {
    reader: &'r PoolReader<'s>,
    /// The pool's row numbers, in pool order.
    pub rowids: Vec<i64>,
    /// The pool's records as a ranking reads them, in pool order.
    pub records: Vec<Record>,
    inputs: Rc<Inputs>,
    /// Per position in `inputs`: admitted by the request.
    admitted: Vec<bool>,
}

impl SearchPool<'_, '_> {
    /// `Store.ranking_statistics(terms, admit=...)`: the admitted
    /// collection's size, each term's document frequency in the body and
    /// in the body or scopes, and the admitted rows' average BM25 length
    /// where the digest answered. None for no term or nothing admitted.
    pub fn statistics(&self, terms: &[String]) -> Result<Option<CorpusStats>, StoreError> {
        let unique: BTreeSet<&String> = terms.iter().filter(|t| !t.is_empty()).collect();
        if unique.is_empty() {
            return Ok(None);
        }
        let size = self.admitted.iter().filter(|a| **a).count() as i64;
        if size == 0 {
            return Ok(None);
        }
        let mut body_df = HashMap::new();
        let mut scope_df = HashMap::new();
        for term in unique {
            let quoted = format!("\"{}\"", term.replace('"', "\"\""));
            let body = self
                .reader
                .matching(&self.inputs, &format!("body_fts : {quoted}"))?;
            let scopes = self
                .reader
                .matching(&self.inputs, &format!("scopes_fts : {quoted}"))?;
            let mut either = body.clone();
            for (i, s) in scopes.iter().enumerate() {
                either[i] |= *s;
            }
            let count = |mask: &[bool]| {
                mask.iter()
                    .zip(&self.admitted)
                    .filter(|(m, a)| **m && **a)
                    .count() as i64
            };
            let (b, e) = (count(&body), count(&either));
            if b > 0 {
                body_df.insert(term.clone(), b);
            }
            if e > 0 {
                scope_df.insert(term.clone(), e);
            }
        }
        let avgdl = self.inputs.lengths.then(|| {
            let length: i64 = self
                .inputs
                .rows
                .iter()
                .zip(&self.admitted)
                .filter(|(_, a)| **a)
                .map(|(r, _)| r.dl)
                .sum();
            int_true_div(i128::from(length), i128::from(size))
        });
        Ok(Some(CorpusStats {
            size,
            body_df,
            scope_df,
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
        let failed: RefCell<Option<StoreError>> = RefCell::new(None);
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
            return Err(PoolError::Store(e));
        }
        hits.map_err(PoolError::Search)
    }
}
