//! The plain reads of `store_index.py` that the search path and
//! `in_force` call, each over a `&Connection`: `ranked_rows`,
//! `_ranked_batches`, `newest_rows`, `rows_updated_since` and
//! `inbound_links`. The rule, with the
//! reference's lines, is the README's "The plain reads of the index and
//! the records".

use std::collections::HashMap;

use rusqlite::{CachedStatement, Connection, Rows, ToSql};

use super::provenance::BATCH;
use super::record_reads::{col, py_text, rows_of, ReadError};
use crate::datetime::DateTime;
use crate::in_force::Inbound;
use crate::lexicon;
use crate::unicode;

const RANKED: &str = "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? \
                      ORDER BY bm25(memories_fts), rowid";

/// `_ranked_batches(text, batch)` (239-268), in two steps: the statement,
/// taken from the connection's statement cache and held here (none for a blank text or an empty match
/// query, which yield nothing), and [`RankedQuery::batches`], which runs
/// it once and reads `batch` row numbers at a time. Dropping the
/// iterator resets the statement, as the reference's `finally` closes its
/// cursor.
pub struct RankedQuery<'c> {
    statement: Option<(CachedStatement<'c>, String)>,
}

impl<'c> RankedQuery<'c> {
    pub fn new(conn: &'c Connection, text: &[u32]) -> Result<RankedQuery<'c>, ReadError> {
        if text.iter().all(|&c| unicode::is_space(c)) {
            return Ok(RankedQuery { statement: None });
        }
        let match_query = lexicon::fts_match_query(text);
        if match_query.is_empty() {
            return Ok(RankedQuery { statement: None });
        }
        Ok(RankedQuery {
            statement: Some((conn.prepare_cached(RANKED)?, match_query)),
        })
    }

    /// The batches, each a non-empty list. `cursor.fetchmany(batch)` of
    /// CPython 3.13: a negative size raises ValueError, one past
    /// `u32::MAX` OverflowError, both at the first batch; zero ends at
    /// once. As `conn.execute` does, the first batch takes the statement's
    /// first step before the size is checked, so a failing step raises
    /// its SQLite error ahead of the size's.
    pub fn batches(&mut self, batch: i64) -> Batches<'_> {
        let rows = self
            .statement
            .as_mut()
            .map(|(statement, query)| statement.query([query.as_str()]));
        Batches {
            rows,
            batch,
            done: false,
            head: Head::Unstepped,
        }
    }
}

/// The iterator [`RankedQuery::batches`] returns.
pub struct Batches<'s> {
    rows: Option<rusqlite::Result<Rows<'s>>>,
    batch: i64,
    done: bool,
    /// The first step's answer, taken before the first size check.
    head: Head,
}

/// Where [`Batches`] stands with the statement's first step.
enum Head {
    /// Not taken yet.
    Unstepped,
    /// Taken and holding a row not yet handed out.
    Row(i64),
    /// Taken and the query had no row.
    Empty,
    /// Taken and its row handed out.
    Spent,
}

impl Iterator for Batches<'_> {
    type Item = Result<Vec<i64>, ReadError>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.done {
            return None;
        }
        let rows = match self.rows.as_mut() {
            None => {
                self.done = true;
                return None;
            }
            Some(Err(_)) => {
                self.done = true;
                let Some(Err(e)) = self.rows.take() else {
                    return None;
                };
                return Some(Err(e.into()));
            }
            Some(Ok(rows)) => rows,
        };
        let fail = |e: ReadError| Some(Err(e));
        if matches!(self.head, Head::Unstepped) {
            let first = match rows.next() {
                Ok(Some(row)) => row.get::<_, i64>(0).map(Some),
                Ok(None) => Ok(None),
                Err(e) => Err(e),
            };
            match first {
                Ok(Some(v)) => self.head = Head::Row(v),
                Ok(None) => self.head = Head::Empty,
                Err(e) => {
                    self.done = true;
                    return fail(e.into());
                }
            }
        }
        if self.batch < 0 {
            self.done = true;
            return fail(ReadError::raised("ValueError", "value must be positive"));
        }
        if self.batch > i64::from(u32::MAX) {
            self.done = true;
            return fail(ReadError::raised(
                "OverflowError",
                "Python int too large for C uint32_t",
            ));
        }
        let mut out = Vec::new();
        let ended = matches!(self.head, Head::Empty);
        if self.batch > 0 {
            if let Head::Row(v) = self.head {
                out.push(v);
                self.head = Head::Spent;
            }
        }
        while !ended && (out.len() as i64) < self.batch {
            match rows.next() {
                Ok(Some(row)) => match row.get::<_, i64>(0) {
                    Ok(v) => out.push(v),
                    Err(e) => {
                        self.done = true;
                        return Some(Err(e.into()));
                    }
                },
                Ok(None) => break,
                Err(e) => {
                    self.done = true;
                    return Some(Err(e.into()));
                }
            }
        }
        if out.is_empty() {
            self.done = true;
            return None;
        }
        Some(Ok(out))
    }
}

/// `ranked_rows(text, limit=)` (215-236): the row numbers `text`
/// matches, by `bm25()` then row number, at most `limit` when given (the
/// text then ends in ` LIMIT ?`); none for a blank text or an empty match
/// query, with no statement. Each text through the statement cache.
pub fn ranked_rows(
    conn: &Connection,
    text: &[u32],
    limit: Option<i64>,
) -> Result<Vec<i64>, ReadError> {
    if text.iter().all(|&c| unicode::is_space(c)) {
        return Ok(Vec::new());
    }
    let match_query = lexicon::fts_match_query(text);
    if match_query.is_empty() {
        return Ok(Vec::new());
    }
    let out = match limit {
        None => conn
            .prepare_cached(RANKED)?
            .query_map([match_query], |r| r.get::<_, i64>(0))?
            .collect::<Result<Vec<_>, _>>()?,
        Some(limit) => conn
            .prepare_cached(RANKED_LIMITED)?
            .query_map(rusqlite::params![match_query, limit], |r| {
                r.get::<_, i64>(0)
            })?
            .collect::<Result<Vec<_>, _>>()?,
    };
    Ok(out)
}

const RANKED_LIMITED: &str = "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? \
                              ORDER BY bm25(memories_fts), rowid LIMIT ?";

/// `newest_rows(limit=, offset=)` (271-282): `limit` row numbers from the
/// `offset`-th newest, newest `updated` first and the greater id first
/// among equals, through the statement cache.
pub fn newest_rows(conn: &Connection, limit: i64, offset: i64) -> Result<Vec<i64>, ReadError> {
    let out = conn
        .prepare_cached(
            "SELECT rowid FROM memories ORDER BY updated DESC, id DESC LIMIT ? OFFSET ?",
        )?
        .query_map([limit, offset], |r| r.get::<_, i64>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(out)
}

/// `rows_updated_since(since)` (285-298): the row numbers whose `updated`
/// is at or after the first nineteen characters of `since` in UTC,
/// newest first. A naive `since` is read by the reference as the
/// machine's local time, which this build does not ask (NotPorted).
pub fn rows_updated_since(conn: &Connection, since: &DateTime) -> Result<Vec<i64>, ReadError> {
    let utc = match since.to_utc() {
        None => return Err(ReadError::NotPorted("a naive datetime's local time")),
        Some(Err(_)) => {
            return Err(ReadError::raised(
                "OverflowError",
                "date value out of range",
            ))
        }
        Some(Ok(utc)) => utc,
    };
    let cut: String = utc.isoformat().chars().take(19).collect();
    let mut statement = conn
        .prepare("SELECT rowid FROM memories WHERE updated >= ? ORDER BY updated DESC, id DESC")?;
    let out = statement
        .query_map([cut], |r| r.get::<_, i64>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(out)
}

/// `inbound_links(target_ids, types)` (339-366): for each target, the
/// links of `types` into it as `(type, source, source updated)`, oldest
/// source first, the source id breaking a tie; only links whose source
/// has an active row. Targets are read `provenance::BATCH` at a time.
pub fn inbound_links(
    conn: &Connection,
    target_ids: &[String],
    types: &[String],
) -> Result<HashMap<String, Vec<Inbound>>, ReadError> {
    let mut ids: Vec<&String> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for id in target_ids {
        if seen.insert(id) {
            ids.push(id);
        }
    }
    let mut kinds: Vec<&String> = types.iter().collect();
    kinds.sort();
    kinds.dedup();
    let mut out: HashMap<String, Vec<Inbound>> = HashMap::new();
    if ids.is_empty() || kinds.is_empty() {
        return Ok(out);
    }
    let marks = vec!["?"; kinds.len()].join(",");
    for batch in ids.chunks(BATCH) {
        let sql = format!(
            "SELECT l.target_id AS target_id, l.type AS type, \
             l.source_id AS source_id, m.updated AS updated \
             FROM memory_links l JOIN memories m ON m.id = l.source_id \
             WHERE l.type IN ({marks}) AND l.target_id IN ({}) \
             ORDER BY l.target_id, m.updated, l.source_id",
            vec!["?"; batch.len()].join(",")
        );
        let params: Vec<&dyn ToSql> = kinds
            .iter()
            .chain(batch.iter())
            .map(|s| *s as &dyn ToSql)
            .collect();
        for row in rows_of(conn, &sql, &params)? {
            let entry = (
                py_text(col(&row, "type"))?,
                py_text(col(&row, "source_id"))?,
                py_text(col(&row, "updated"))?,
            );
            out.entry(py_text(col(&row, "target_id"))?)
                .or_default()
                .push(entry);
        }
    }
    Ok(out)
}
