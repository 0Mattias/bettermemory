//! `store_index._advanced_inputs` over a store connection: a memo entry
//! kept at an older memory head brought to the current one by the record
//! rows the log names since (`_HEAD_KINDS`, `_ADVANCE_ROWS`), read from
//! the digest in chunks of `_PROVENANCE_BATCH`. A new row takes a new
//! position, a changed row its old one under the group its inputs now
//! name; a row the log does not name is not read again.

use std::collections::{HashMap, HashSet};

use super::{
    actor_of, cells, digest_row, home_key, scopes, source, sqlite_class, value, StoreDocFreq,
};
use crate::json::{self, LoadError};
use crate::pyvalue::{self, PyError, PyStr, PyValue};
use crate::store::docfreq::{DocActor, DocFreqError, DocFreqInputs, Group, GroupKey, Head, Mask};
use crate::store::{provenance, SqlValue};
use crate::unicode;

/// `_HEAD_KINDS`: the log rows that move the memo's key.
const HEAD_KINDS: [&str; 5] = [
    "memory_put",
    "memory_delete",
    "home_put",
    "home_delete",
    "index_rebuilt",
];

/// `_ADVANCE_ROWS`: rows an entry follows before it is read again.
const ADVANCE_ROWS: usize = 256;

const MAC_AT: &str = "SELECT mac FROM log WHERE seq = ?";

const SPAN: &str = "SELECT kind, payload FROM log WHERE kind IN (?,?,?,?,?) \
     AND seq > ? AND seq <= ? ORDER BY seq";

fn sqlite(e: rusqlite::Error) -> DocFreqError {
    source(sqlite_class(&e).0)
}

/// What one log row of the span makes of the advance.
enum Named {
    /// The record it names, as `str()` of the payload's id.
    Id(PyStr),
    /// A kind the advance does not follow, or a payload that does not read
    /// (KeyError, TypeError, ValueError, all caught): read again.
    Stop,
}

/// The id a `memory_put`, `home_put` or `home_delete` row names:
/// `json.loads(payload)` (a text, or bytes by their detected encoding),
/// indexed by the kind's keys. RecursionError is not caught.
fn named(kind: &str, payload: &SqlValue) -> Result<Named, DocFreqError> {
    let path: &[&str] = match kind {
        "memory_put" => &["memory", "id"],
        "home_put" => &["home", "record_id"],
        "home_delete" => &["record_id"],
        _ => return Ok(Named::Stop),
    };
    let loaded = match payload {
        SqlValue::Text(t) => json::loads_detailed(&unicode::cps(t)),
        SqlValue::Blob(b) => json::loads_bytes(b),
        _ => return Ok(Named::Stop),
    };
    let mut held = match loaded {
        Ok(v) => v,
        Err(LoadError::Recursion) => return Err(DocFreqError::Py(PyError::RecursionError)),
        Err(_) => return Ok(Named::Stop),
    };
    for key in path {
        let next = match &held {
            PyValue::Dict(_) => held.get(key).cloned(),
            _ => None,
        };
        match next {
            Some(v) => held = v,
            None => return Ok(Named::Stop),
        }
    }
    pyvalue::py_str(&held)
        .map(Named::Id)
        .map_err(DocFreqError::Py)
}

/// The ids the log's rows of `HEAD_KINDS` after `seq` and at most `to`
/// name, first seen first; None where one does not follow or the ids and
/// `changed` together pass `ADVANCE_ROWS`. sqlite3 converts a row and
/// steps to the next before it hands the row on, and so does this read.
fn span_ids(
    src: &StoreDocFreq<'_>,
    seq: i64,
    to: i64,
    changed: usize,
) -> Result<Option<Vec<PyStr>>, DocFreqError> {
    let mut statement = src.conn.prepare_cached(SPAN).map_err(sqlite)?;
    let k = HEAD_KINDS;
    let mut rows = statement
        .query(rusqlite::params![k[0], k[1], k[2], k[3], k[4], seq, to])
        .map_err(sqlite)?;
    let (mut ids, mut seen) = (Vec::new(), HashSet::new());
    let mut ahead: Option<(SqlValue, SqlValue)> = None;
    loop {
        let row = rows.next().map_err(sqlite)?;
        if let Some((kind, payload)) = ahead.take() {
            match named(&kind.py_str(), &payload)? {
                Named::Stop => return Ok(None),
                Named::Id(id) => {
                    if seen.insert(id.clone()) {
                        ids.push(id);
                    }
                }
            }
            if ids.len() + changed > ADVANCE_ROWS {
                return Ok(None);
            }
        }
        let Some(row) = row else {
            break;
        };
        let kind = value(row.get_ref(0).map_err(sqlite)?)?;
        let payload = value(row.get_ref(1).map_err(sqlite)?)?;
        ahead = Some((kind, payload));
    }
    Ok(Some(ids))
}

/// The digest's active rows of `ids`, a chunk of `provenance::BATCH` per
/// statement, typed as `digest_admission_rows` hands them on (a number
/// past 64 bits read whole). An SQLite error (a step, a text that is not
/// UTF-8, a table that is not there) is None with no warning; `int()` of
/// a column that does not read and an id that does not encode are
/// raised.
fn digest_rows(
    src: &StoreDocFreq<'_>,
    ids: &[PyStr],
) -> Result<Option<Vec<super::DigestRow>>, DocFreqError> {
    let mut out = Vec::new();
    for chunk in ids.chunks(provenance::BATCH) {
        let marks = vec!["?"; chunk.len()].join(",");
        let sql = format!(
            "SELECT d.rid, d.scopes, h.project, d.client, d.model, d.dl \
             FROM digest_records d LEFT JOIN homes h ON h.record_id = d.id \
             WHERE d.removed = 0 AND d.id IN ({marks})"
        );
        let Ok(mut statement) = src.conn.prepare(&sql) else {
            return Ok(None);
        };
        // sqlite3 binds after it prepares: an id holding a lone surrogate
        // is UnicodeEncodeError, which the reference does not catch.
        let bound = chunk
            .iter()
            .map(|id| unicode::to_string(id).ok_or(DocFreqError::Py(PyError::UnicodeEncodeError)))
            .collect::<Result<Vec<String>, _>>()?;
        let Ok(mut rows) = statement.query(rusqlite::params_from_iter(bound.iter())) else {
            return Ok(None);
        };
        let mut ahead = None;
        loop {
            let Ok(row) = rows.next() else {
                return Ok(None);
            };
            if let Some(c) = ahead.take() {
                out.push(digest_row(c, None)?);
            }
            let Some(row) = row else {
                break;
            };
            match cells(row) {
                Ok(c) => ahead = Some(c),
                Err(_) => return Ok(None),
            }
        }
    }
    Ok(Some(out))
}

/// `_advanced_inputs(self, entry, head)`: `entry` brought to `head`, or
/// None where the inputs have to be read from the store. The entry's
/// terms are the memo's to copy.
pub(super) fn advanced_inputs(
    src: &StoreDocFreq<'_>,
    entry: &DocFreqInputs,
    head: &Head,
) -> Result<Option<DocFreqInputs>, DocFreqError> {
    let (Some(lengths), Some(group_of), Some(length_of)) =
        (&entry.lengths, &entry.group_of, &entry.length_of)
    else {
        return Ok(None);
    };
    let (seq, mac) = (entry.head.0, &entry.head.1);
    if head.0 <= seq {
        return Ok(None);
    }
    let at = {
        let mut statement = src.conn.prepare_cached(MAC_AT).map_err(sqlite)?;
        let mut rows = statement.query([seq]).map_err(sqlite)?;
        match rows.next().map_err(sqlite)? {
            Some(row) => Some(value(row.get_ref(0).map_err(sqlite)?)?.py_str()),
            None => None,
        }
    };
    if at.as_deref() != Some(mac.as_str()) {
        return Ok(None);
    }
    let Some(ids) = span_ids(src, seq, head.0, entry.changed.len())? else {
        return Ok(None);
    };
    if src.digest_mark()?.is_none() {
        return Ok(None);
    }
    let Some(read) = digest_rows(src, &ids)? else {
        return Ok(None);
    };
    let mut index = entry.index.clone();
    let mut group_of = group_of.clone();
    let mut length_of = length_of.clone();
    let mut groups = entry.groups.clone();
    let mut lengths = lengths.clone();
    let mut keys = entry.keys.clone();
    let mut number_of: HashMap<GroupKey, usize> = keys
        .iter()
        .enumerate()
        .map(|(n, k)| (k.clone(), n))
        .collect();
    // The reference's version is a Python int; one past `u64::MAX` (2**64
    // advances, out of reach) is not held here: the entry is read again,
    // whose answers the advanced one's equal.
    let (version, past) = entry.version.overflowing_add(1);
    let mut changed = entry.changed.clone();
    // Each row as the reference's loop meets it, its failures in that
    // order: a scopes text that is not JSON, an index past an array of
    // the entry (IndexError, an entry whose arrays disagree), an actor
    // pydantic refuses on a new group, a length past 64 bits that
    // `array('q')` refuses (OverflowError).
    let index_error = || DocFreqError::IndexError;
    let overflow = || DocFreqError::Py(PyError::OverflowError);
    for (rid, scopes_text, home, client, model, length) in read {
        let scopes = scopes(&scopes_text)?;
        let position = match index.get(&rid).copied() {
            None => {
                if scopes.is_none() {
                    continue;
                }
                let position = index.len();
                index.insert(rid.clone(), position);
                group_of.push(-1);
                length_of.push(0);
                position
            }
            Some(position) => {
                let was = *group_of.get(position).ok_or_else(index_error)?;
                if let Ok(was) = usize::try_from(was) {
                    let group = groups.get_mut(was).ok_or_else(index_error)?;
                    group.mask.clear(position);
                    let total = lengths.get_mut(was).ok_or_else(index_error)?;
                    *total -= i128::from(*length_of.get(position).ok_or_else(index_error)?);
                }
                *group_of.get_mut(position).ok_or_else(index_error)? = -1;
                *length_of.get_mut(position).ok_or_else(index_error)? = 0;
                position
            }
        };
        changed.push((version, rid, position));
        let Some(scopes) = scopes else {
            continue;
        };
        // A key's client or model is the column's text (bytes decoded as
        // UTF-8: the store-2 difference); a column pydantic refuses names
        // no key the entry holds, so its group is new and `Actor` raises.
        let (Some(client), Some(model)) = (actor_of(&client), actor_of(&model)) else {
            return Err(source("ValidationError"));
        };
        let key: GroupKey = (scopes_text, home_key(&home), client, model);
        let number = match number_of.get(&key) {
            Some(&n) => n,
            None => {
                let n = groups.len();
                groups.push(Group {
                    scopes,
                    home_project: key.1.clone(),
                    actor: DocActor::of(&key.2, &key.3),
                    mask: Mask::default(),
                });
                lengths.push(0);
                number_of.insert(key.clone(), n);
                keys.push(key);
                n
            }
        };
        groups
            .get_mut(number)
            .ok_or_else(index_error)?
            .mask
            .set(position);
        let total = lengths.get_mut(number).ok_or_else(index_error)?;
        if let Some(n) = length.small() {
            *total += i128::from(n);
        }
        let slot = group_of.get_mut(position).ok_or_else(index_error)?;
        *slot = i32::try_from(number).map_err(|_| overflow())?;
        let slot = length_of.get_mut(position).ok_or_else(index_error)?;
        *slot = length.small().ok_or_else(overflow)?;
    }
    if past {
        return Ok(None);
    }
    Ok(Some(DocFreqInputs {
        head: head.clone(),
        groups,
        index,
        lengths: Some(lengths),
        keys,
        group_of: Some(group_of),
        length_of: Some(length_of),
        version,
        changed,
    }))
}
