//! `store_index.admitted_rows`, `_admitter`, `ranked_admitted_rows`,
//! `admitted_share` and `_admitted_rows_read` (680-812): the rows a
//! request admits, decided from the document frequency memo's inputs
//! outside a transaction and from each row's own columns inside one (or
//! for a row the inputs do not hold). The memo's side is any
//! [`DocFreqSource`]; the rows' own columns and the ranking are read on
//! the connection given, each statement through its statement cache.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use rusqlite::Connection;

use super::{
    actor_of, cells, from_store, json_class, source, sqlite_class, with_home, Home, StoreAdmit,
    ADMISSION_INPUTS,
};
use crate::pyvalue::{PyError, PyValue};
use crate::records::rows::load_json;
use crate::store::docfreq::PyInt;
use crate::store::docfreq::{DocActor, DocFreqError, DocFreqMemo, DocFreqSource, Entry, Mask};
use crate::store::index_reads::RankedQuery;
use crate::store::provenance::BATCH;
use crate::store::SqlValue;
use crate::unicode;

fn sqlite(e: rusqlite::Error) -> DocFreqError {
    source(sqlite_class(&e).0)
}

/// `_admitter(self, admit)`: what decides rows for one predicate. Outside
/// a transaction it holds the memo's inputs at the head they were brought
/// to and the mask of the rows `admit` admits (`admitted.to_bytes(...)`);
/// inside one it holds nothing and every row is read.
pub struct Admitter {
    kept: Option<(Arc<Entry>, Mask, usize)>,
}

impl Admitter {
    /// The inputs brought current, once: `_current_inputs`,
    /// `_admitted_mask` and `_keep_doc_freqs` when this call read them.
    pub fn new(
        src: &dyn DocFreqSource,
        memo: &DocFreqMemo,
        admit: &mut StoreAdmit<'_>,
    ) -> Result<Admitter, DocFreqError> {
        if src.in_transaction() {
            return Ok(Admitter { kept: None });
        }
        let mut keyed =
            |s: &[PyValue], h: Option<&str>, a: Option<&DocActor>| with_home(h, |h| admit(s, h, a));
        let (entry, admitted) = memo.admitted_inputs(src, &mut keyed).map_err(json_class)?;
        // `(len(entry.index) + 7) // 8 or 1` little-endian bytes: a mask
        // wider than them is OverflowError.
        let bytes = entry.inputs.index.len().div_ceil(8).max(1);
        if admitted.bit_length() > bytes * 8 {
            return Err(DocFreqError::Py(PyError::OverflowError));
        }
        Ok(Admitter {
            kept: Some((entry, admitted, bytes)),
        })
    }

    /// `decide(wanted)`: those of `wanted` the predicate admits, in their
    /// order. The rows the inputs do not hold are read first, all of them
    /// (`_admitted_rows_read`, one statement per [`BATCH`]), then each row
    /// is decided in turn.
    pub fn decide(
        &self,
        conn: &Connection,
        wanted: &[i64],
        admit: &mut StoreAdmit<'_>,
    ) -> Result<Vec<i64>, DocFreqError> {
        let Some((entry, admitted, bytes)) = &self.kept else {
            return admitted_rows_read(conn, wanted, admit);
        };
        let index = &entry.inputs.index;
        let unknown: Vec<i64> = wanted
            .iter()
            .copied()
            .filter(|rid| !index.contains_key(&PyInt::Small(*rid)))
            .collect();
        let read: HashSet<i64> = if unknown.is_empty() {
            HashSet::new()
        } else {
            admitted_rows_read(conn, &unknown, admit)?
                .into_iter()
                .collect()
        };
        let mut out = Vec::new();
        for &rid in wanted {
            match index.get(&PyInt::Small(rid)) {
                None => {
                    if read.contains(&rid) {
                        out.push(rid);
                    }
                }
                // `bits[position >> 3]`: past the bytes is IndexError.
                Some(&position) if position / 8 >= *bytes => {
                    return Err(DocFreqError::IndexError);
                }
                Some(&position) => {
                    if admitted.get(position) {
                        out.push(rid);
                    }
                }
            }
        }
        Ok(out)
    }
}

/// `admitted_rows(rowids, admit)`: those of `rowids` the predicate
/// admits, in their order; nothing is read for no row.
pub fn admitted_rows(
    src: &dyn DocFreqSource,
    conn: &Connection,
    memo: &DocFreqMemo,
    rowids: &[i64],
    admit: &mut StoreAdmit<'_>,
) -> Result<Vec<i64>, DocFreqError> {
    if rowids.is_empty() {
        return Ok(Vec::new());
    }
    Admitter::new(src, memo, admit)?.decide(conn, rowids, admit)
}

/// `ranked_admitted_rows(text, admit, size=, batch=)`: the first `size`
/// rows of the ranking the predicate admits, the admitter made before the
/// ranking's one statement opens and the statement read `batch` rows at a
/// time until `size` are found.
pub fn ranked_admitted_rows(
    src: &dyn DocFreqSource,
    conn: &Connection,
    memo: &DocFreqMemo,
    text: &str,
    admit: &mut StoreAdmit<'_>,
    size: usize,
    batch: i64,
) -> Result<Vec<i64>, DocFreqError> {
    let decide = Admitter::new(src, memo, admit)?;
    let class = |e: crate::store::record_reads::ReadError| source(&e.class());
    let mut query = RankedQuery::new(conn, &unicode::cps(text)).map_err(class)?;
    let mut out = Vec::new();
    for rows in query.batches(batch) {
        out.extend(decide.decide(conn, &rows.map_err(class)?, admit)?);
        if out.len() >= size {
            break;
        }
    }
    out.truncate(size);
    Ok(out)
}

/// `admitted_share(admit)`: how many of the memo's rows the predicate
/// admits, and how many it holds; None inside a transaction.
pub fn admitted_share(
    src: &dyn DocFreqSource,
    memo: &DocFreqMemo,
    admit: &mut StoreAdmit<'_>,
) -> Result<Option<(usize, usize)>, DocFreqError> {
    if src.in_transaction() {
        return Ok(None);
    }
    let mut keyed =
        |s: &[PyValue], h: Option<&str>, a: Option<&DocActor>| with_home(h, |h| admit(s, h, a));
    let (entry, admitted) = memo.admitted_inputs(src, &mut keyed).map_err(json_class)?;
    Ok(Some((admitted.count(), entry.inputs.index.len())))
}

/// `_admitted_rows_read(wanted, admit)`: the rows decided from their own
/// columns, `_ADMISSION_INPUTS WHERE m.rowid IN (?,...)` per [`BATCH`] of
/// `wanted`. A row the table does not hold, and one whose scopes are not
/// a list, is admitted nowhere; a scope list that does not load and an
/// actor pydantic refuses raise.
fn admitted_rows_read(
    conn: &Connection,
    wanted: &[i64],
    admit: &mut StoreAdmit<'_>,
) -> Result<Vec<i64>, DocFreqError> {
    let mut out = Vec::new();
    for chunk in wanted.chunks(BATCH) {
        let sql = format!(
            "{ADMISSION_INPUTS} WHERE m.rowid IN ({})",
            vec!["?"; chunk.len()].join(",")
        );
        // `{int(row["rid"]): row for row in ...}`: every row fetched (each
        // column decoded) before any is decided, a later one replacing an
        // earlier one of the same number.
        let mut found: HashMap<i64, Vec<SqlValue>> = HashMap::new();
        {
            let mut statement = conn.prepare_cached(&sql).map_err(sqlite)?;
            let mut rows = statement
                .query(rusqlite::params_from_iter(chunk))
                .map_err(sqlite)?;
            while let Some(row) = rows.next().map_err(sqlite)? {
                let c = cells(row).map_err(from_store)?;
                // `m.rowid` is an integer always.
                if let SqlValue::Integer(rid) = c[0] {
                    found.insert(rid, c);
                }
            }
        }
        for &rid in chunk {
            let Some(c) = found.get(&rid) else {
                continue;
            };
            let scopes = match &mut load_json(&c[2], PyValue::None) {
                Ok(PyValue::List(items)) => std::mem::take(items),
                Ok(_) => continue,
                Err(e) => return Err(source(e.class())),
            };
            let actor = match (&c[4], &c[5]) {
                (SqlValue::Null, SqlValue::Null) => None,
                (client, model) => match (actor_of(client), actor_of(model)) {
                    (Some(client), Some(model)) => Some(DocActor { client, model }),
                    _ => return Err(source("ValidationError")),
                },
            };
            let spelled = c[3].py_str();
            let home = match &c[3] {
                SqlValue::Null => None,
                SqlValue::Blob(b) => Some(Home::Bytes(b)),
                SqlValue::Text(t) => Some(Home::Text(t)),
                _ => Some(Home::Text(&spelled)),
            };
            if admit(&scopes, home, actor.as_ref()) {
                out.push(rid);
            }
        }
    }
    Ok(out)
}
