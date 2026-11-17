//! A record's provenance, read only: `store_provenance.py`'s
//! `provenance_for`, `_segment_for`, `_origin_broken`, `_pointer_verified`,
//! `trust_rows`, `unaccounted_ids`, `provenance_counts`, `links_with_status`
//! (with `store_index.links_for_many`) and `episode_provenance`, over an
//! open [`StoreFile`] (or a writer's connection, [`ConnSource`]) and its
//! [`KeyRing`]. Nothing is written: every
//! statement is a `SELECT` on the reader's connection.
//!
//! The verify key is the caller's decision (native/README.md, "A record's
//! provenance, read"): a [`Provenance`] that verifies checks pointers under
//! the ring's current key, as the reference's `_verify_key` is the key its
//! open loaded as current; one that does not answers the labels as stored.
//! The state is a fresh open's, no refold having marked an id unaccounted,
//! unless the caller gives the ids its own refold found
//! ([`Provenance::with_unaccounted`]).

use std::cell::RefCell;
use std::collections::HashMap;

use rusqlite::types::ValueRef;

use rusqlite::Connection;

use super::{sql_value, SqlValue, StoreError, StoreFile};
use crate::json;
use crate::log::{self, ChainTrust, KeyRing, LogRow, LogSource, VerifyError};
use crate::pyvalue::{py_str, PyError, PyValue};
use crate::unicode;

/// `store_schema._PROVENANCE_BATCH`.
pub const BATCH: usize = 500;
/// `store_schema.LOCAL`.
pub const LOCAL: &str = "local";
/// `store_schema.UNACCOUNTED`.
pub const UNACCOUNTED: &str = "unaccounted";

/// Why no answer was given.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProvenanceError {
    /// The reference raises here: its exception class. A RecursionError's
    /// text is the interpreter's own and is not carried.
    Raised {
        class: &'static str,
        text: Option<String>,
    },
    /// The reference answers or raises in a way this port does not
    /// reproduce: what was met.
    NotPorted(String),
}

impl std::fmt::Display for ProvenanceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProvenanceError::Raised {
                class,
                text: Some(t),
            } => write!(f, "{class}: {t}"),
            ProvenanceError::Raised { class, text: None } => write!(f, "{class}"),
            ProvenanceError::NotPorted(what) => write!(f, "not ported: {what}"),
        }
    }
}

impl std::error::Error for ProvenanceError {}

fn py(e: PyError) -> ProvenanceError {
    ProvenanceError::Raised {
        class: e.name(),
        text: None,
    }
}

fn store(e: StoreError) -> ProvenanceError {
    match e {
        StoreError::Py(e) => py(e),
        other => ProvenanceError::NotPorted(format!("a store read failed: {other:?}")),
    }
}

impl From<rusqlite::Error> for ProvenanceError {
    fn from(e: rusqlite::Error) -> Self {
        ProvenanceError::NotPorted(format!("a SQLite error: {e}"))
    }
}

fn value(v: ValueRef<'_>) -> Result<SqlValue, ProvenanceError> {
    super::sql_value(v).map_err(store)
}

/// `TrustRow`: the verified label and this host's verification stamp.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrustRow {
    pub label: String,
    pub verified_at: Option<String>,
}

/// One link row as `links_for_many` gives it: `(type, other id, note)`,
/// each value as stored.
pub type Link = (SqlValue, SqlValue, SqlValue);

/// What a reader reads from: the connection its statements run on, the
/// `meta` table and the log rows the segment map needs. A [`StoreFile`]
/// is one; a [`ConnSource`] is a writer's connection inside its own
/// transaction, where the reference's `provenance_for` reads (the relink
/// of a restore, `store_records._put_relinked`).
pub trait Source: LogSource<Error = StoreError> {
    fn connection(&self) -> &Connection;
    /// `meta` as a dict: each key's last value, in first-seen order.
    fn meta(&self) -> Result<Vec<(String, String)>, StoreError>;
}

impl Source for StoreFile {
    fn connection(&self) -> &Connection {
        StoreFile::connection(self)
    }
    fn meta(&self) -> Result<Vec<(String, String)>, StoreError> {
        StoreFile::meta(self)
    }
}

/// A bare connection as a [`Source`]: the same statements a
/// [`StoreFile`] sends, on a connection the caller holds (a write
/// transaction's, which sees its own rows as the reference's does).
#[derive(Clone, Copy)]
pub struct ConnSource<'c>(pub &'c Connection);

impl std::fmt::Debug for ConnSource<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ConnSource")
    }
}

impl ConnSource<'_> {
    fn log_rows(&self, tail: &str, seq: i64, limit: bool) -> Result<Vec<LogRow>, StoreError> {
        let mut statement = self.0.prepare(&format!("{} {tail}", super::LOG_SELECT))?;
        let mut rows = statement.query([seq])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            out.push(super::log_row(row)?);
            if limit {
                break;
            }
        }
        Ok(out)
    }
}

impl LogSource for ConnSource<'_> {
    type Error = StoreError;
    fn first(&self) -> Result<Option<LogRow>, StoreError> {
        Ok(self
            .log_rows("WHERE seq > ? ORDER BY seq LIMIT 1", 0, true)?
            .into_iter()
            .next())
    }
    fn rekeys(&self) -> Result<Vec<LogRow>, StoreError> {
        Ok(self
            .log_rows("WHERE seq > ? AND kind = 'rekey' ORDER BY seq", 0, false)?
            .into_iter()
            .filter(|r| r.kind == log::REKEY)
            .collect())
    }
    fn at_seq(&self, seq: i64) -> Result<Option<LogRow>, StoreError> {
        if seq <= 0 {
            return Ok(None);
        }
        Ok(self
            .log_rows("WHERE seq = ? ORDER BY seq LIMIT 1", seq, true)?
            .into_iter()
            .next())
    }
    fn each(
        &self,
        visit: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, StoreError> {
        let mut statement = self
            .0
            .prepare(&format!("{} WHERE seq > ? ORDER BY seq", super::LOG_SELECT))?;
        let mut rows = statement.query([0i64])?;
        while let Some(row) = rows.next()? {
            if let Err(e) = visit(&super::log_row(row)?) {
                return Ok(Err(e));
            }
        }
        Ok(Ok(()))
    }
}

impl Source for ConnSource<'_> {
    fn connection(&self) -> &Connection {
        self.0
    }
    fn meta(&self) -> Result<Vec<(String, String)>, StoreError> {
        let mut statement = self.0.prepare("SELECT key, value FROM meta")?;
        let mut rows = statement.query([])?;
        let mut out: Vec<(String, String)> = Vec::new();
        while let Some(row) = rows.next()? {
            let key = sql_value(row.get_ref(0)?)?.py_str();
            let value = sql_value(row.get_ref(1)?)?.py_str();
            match out.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = value,
                None => out.push((key, value)),
            }
        }
        Ok(out)
    }
}

/// `links_with_status`'s answer: outbound links, inbound links and the
/// record's trust row.
pub type LinksWithStatus = (Vec<Link>, Vec<Link>, Option<TrustRow>);

/// The reader: one store, its ring, and what this connection has read
/// (the segment map and the pointer verdicts, as the reference keeps
/// them per connection).
pub struct Provenance<'a> {
    file: &'a dyn Source,
    ring: &'a KeyRing,
    verifies: bool,
    strict: bool,
    chain: RefCell<Option<ChainTrust>>,
    cache: RefCell<HashMap<(String, String), bool>>,
    /// `_unaccounted_memory_ids`: what the caller's last `refold` in this
    /// process found (empty for a fresh open).
    memo: std::collections::HashSet<String>,
}

impl std::fmt::Debug for Provenance<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Provenance")
            .field("verifies", &self.verifies)
            .field("strict", &self.strict)
            .finish()
    }
}

fn dedup(ids: &[&str]) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    ids.iter()
        .filter(|id| seen.insert(**id))
        .map(|id| (*id).to_owned())
        .collect()
}

fn marks(n: usize) -> String {
    vec!["?"; n].join(",")
}

impl<'a> Provenance<'a> {
    /// A reader of `file` under `ring`. `verifies`: the open holds the
    /// ring's current key as its verify key (`Store.can_verify`).
    /// `strict`: `[security] strict_provenance` for this connection.
    pub fn new(
        file: &'a dyn Source,
        ring: &'a KeyRing,
        verifies: bool,
        strict: bool,
    ) -> Result<Self, ProvenanceError> {
        if verifies && ring.current.is_none() {
            return Err(ProvenanceError::NotPorted(
                "a reader that verifies with no current key in the ring: the key \
                 file is missing, unreadable or over the ring's 1 MiB read bound \
                 (keys::MAX_FILE_BYTES; the reference reads any size)"
                    .to_owned(),
            ));
        }
        Ok(Provenance {
            file,
            ring,
            verifies,
            strict,
            chain: RefCell::new(None),
            cache: RefCell::new(HashMap::new()),
            memo: std::collections::HashSet::new(),
        })
    }

    /// The reader with `ids` as `_unaccounted_memory_ids`, the ids the
    /// caller's last `refold` in the same process found unaccounted
    /// (`store_events.py` 586-589): `provenance_for` and
    /// `unaccounted_ids` read such an id `unaccounted` without checking its
    /// pointer, as the reference does. A fresh open's set is empty.
    pub fn with_unaccounted(mut self, ids: impl IntoIterator<Item = String>) -> Self {
        self.memo = ids.into_iter().collect();
        self
    }

    /// `Store.can_verify`.
    pub fn can_verify(&self) -> bool {
        self.verifies
    }

    fn key_fingerprint(&self) -> Result<String, ProvenanceError> {
        let meta = self.file.meta().map_err(store)?;
        meta.into_iter()
            .rev()
            .find(|(k, _)| k == "key_fingerprint")
            .map(|(_, v)| v)
            .ok_or(ProvenanceError::Raised {
                class: "KeyError",
                text: Some("'key_fingerprint'".to_owned()),
            })
    }

    /// `_segment_for` and `_origin_broken`: the segment holding `seq`
    /// (its key, whether foreign) and whether the anchor is broken.
    fn segment_for(
        &self,
        seq: i64,
    ) -> Result<Option<(Option<log::Key>, bool, bool)>, ProvenanceError> {
        if self.chain.borrow().is_none() {
            let fp = self.key_fingerprint()?;
            let trust = log::chain_trust(self.file, self.ring, &fp).map_err(|e| match e {
                VerifyError::Py(e) => py(e),
                VerifyError::Source(e) => store(e),
            })?;
            *self.chain.borrow_mut() = Some(trust);
        }
        let chain = self.chain.borrow();
        let Some(chain) = chain.as_ref() else {
            return Ok(None);
        };
        let mut found: Option<&log::TrustSegment> = None;
        for segment in &chain.segments {
            if found.is_some() && seq < segment.first_seq {
                break;
            }
            found = Some(segment);
        }
        Ok(found.map(|s| (s.key.clone(), s.foreign, chain.origin_broken)))
    }

    /// `_pointer_verified`: Some(true) or Some(false), or None when the
    /// pointer cannot be checked here (no verify key, a leading segment).
    pub fn pointer_verified(
        &self,
        record_id: &str,
        kind: &str,
        log_mac: &SqlValue,
    ) -> Result<Option<bool>, ProvenanceError> {
        let Some(own_key) = self.ring.current.as_ref().filter(|_| self.verifies) else {
            return Ok(None);
        };
        let log_mac = match log_mac {
            SqlValue::Text(t) if !t.is_empty() => t,
            _ => return Ok(Some(false)),
        };
        let cache_key = (record_id.to_owned(), log_mac.clone());
        if let Some(cached) = self.cache.borrow().get(&cache_key) {
            return Ok(Some(*cached));
        }
        let conn = self.file.connection();
        let mut statement = conn.prepare(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log WHERE mac = ?",
        )?;
        let mut rows = statement.query([log_mac.as_str()])?;
        let raw = match rows.next()? {
            Some(row) => {
                let mut cells = Vec::with_capacity(7);
                for i in 0..7 {
                    cells.push(value(row.get_ref(i)?)?);
                }
                Some(cells)
            }
            None => None,
        };
        drop(rows);
        let mut verdict = false;
        if let Some(raw) = raw {
            if raw[3].py_str() == kind {
                let named = self.names(&raw[4], kind, record_id)?;
                let seq = match &raw[0] {
                    SqlValue::Integer(i) => *i,
                    other => {
                        return Err(ProvenanceError::NotPorted(format!(
                            "a log seq of {other:?}"
                        )))
                    }
                };
                if named && seq > 0 {
                    let Some((key, foreign, broken)) = self.segment_for(seq)? else {
                        return Ok(None);
                    };
                    let key = match key {
                        Some(k) => k,
                        None => {
                            if !foreign && !broken && !self.strict {
                                return Ok(None);
                            }
                            own_key.clone()
                        }
                    };
                    let session = match &raw[2] {
                        SqlValue::Null => None,
                        other => Some(other.py_str()),
                    };
                    let expected = log::compute_mac(
                        key.bytes(),
                        seq,
                        &raw[1].py_str(),
                        session.as_deref(),
                        &raw[3].py_str(),
                        &raw[4].py_str(),
                        &raw[5].py_str(),
                    );
                    verdict = log::same_mac(&unicode::cps(&expected), &unicode::cps(log_mac));
                }
            }
        }
        self.cache.borrow_mut().insert(cache_key, verdict);
        Ok(Some(verdict))
    }

    /// Does `str(json.loads(payload)[kind's first word]["id"])` equal
    /// `record_id`? False for ValueError, KeyError and TypeError.
    fn names(
        &self,
        payload: &SqlValue,
        kind: &str,
        record_id: &str,
    ) -> Result<bool, ProvenanceError> {
        let text: Vec<u32> = match payload {
            SqlValue::Text(t) => unicode::cps(t),
            // `json.loads(bytes)`: decoded in the encoding
            // `json.detect_encoding` names, with `surrogatepass`; a
            // UnicodeDecodeError is a ValueError, read as no id.
            SqlValue::Blob(b) => match decode_json_bytes(b) {
                Some(t) => t,
                None => return Ok(false),
            },
            // An int, a float or None: TypeError.
            _ => return Ok(false),
        };
        let parsed = json::loads(&text);
        drop(text);
        let parsed = match parsed {
            Ok(v) => v,
            Err(PyError::RecursionError) => return Err(py(PyError::RecursionError)),
            Err(_) => return Ok(false),
        };
        let field = kind.split('_').next().unwrap_or(kind);
        let PyValue::Dict(_) = parsed else {
            return Ok(false);
        };
        let Some(record) = parsed.get(field) else {
            return Ok(false);
        };
        let PyValue::Dict(_) = record else {
            // A list or str indexed by "id": TypeError; a number or None
            // is not subscriptable: TypeError.
            return Ok(false);
        };
        let Some(id) = record.get("id") else {
            return Ok(false);
        };
        let wanted = unicode::cps(record_id);
        // A str id is compared in place: no copy of a long text.
        if let PyValue::Str(s) = id {
            return Ok(*s == wanted);
        }
        match py_str(id) {
            Ok(s) => Ok(s == wanted),
            Err(e) => Err(py(e)),
        }
    }

    /// `provenance_for`: `(id, label)` for the active records asked, in
    /// the order SQLite returns them.
    pub fn provenance_for(&self, ids: &[&str]) -> Result<Vec<(String, String)>, ProvenanceError> {
        let ids = dedup(ids);
        let mut out: Vec<(String, String)> = Vec::new();
        for batch in ids.chunks(BATCH) {
            let sql = format!(
                "SELECT id, provenance, log_mac FROM memories WHERE id IN ({})",
                marks(batch.len())
            );
            let rows = self.select(&sql, batch, 3)?;
            for row in rows {
                let memory_id = text_of(&row[0])?;
                let mut label = row[1].py_str();
                if self.memo.contains(&memory_id)
                    || self.pointer_verified(&memory_id, "memory_put", &row[2])? == Some(false)
                {
                    label = UNACCOUNTED.to_owned();
                }
                // The ids are unique in the table and across batches.
                out.push((memory_id, label));
            }
        }
        Ok(out)
    }

    fn select(
        &self,
        sql: &str,
        params: &[String],
        width: usize,
    ) -> Result<Vec<Vec<SqlValue>>, ProvenanceError> {
        let conn = self.file.connection();
        let mut statement = conn.prepare(sql)?;
        let mut rows = statement.query(rusqlite::params_from_iter(params.iter()))?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let mut cells = Vec::with_capacity(width);
            for i in 0..width {
                cells.push(value(row.get_ref(i)?)?);
            }
            out.push(cells);
        }
        Ok(out)
    }

    /// `trust_rows`: None when the reader does not verify.
    pub fn trust_rows(
        &self,
        ids: &[&str],
    ) -> Result<Option<Vec<(String, TrustRow)>>, ProvenanceError> {
        if !self.verifies {
            return Ok(None);
        }
        let labels = self.provenance_for(ids)?;
        if labels.is_empty() {
            return Ok(Some(Vec::new()));
        }
        let mut stamps: HashMap<String, String> = HashMap::new();
        let wanted: Vec<String> = labels.iter().map(|(id, _)| id.clone()).collect();
        for batch in wanted.chunks(BATCH) {
            let sql = format!(
                "SELECT memory_id, verified_at FROM verifications \
                 WHERE machine_id IS NULL AND memory_id IN ({})",
                marks(batch.len())
            );
            for row in self.select(&sql, batch, 2)? {
                stamps.insert(row[0].py_str(), row[1].py_str());
            }
        }
        Ok(Some(
            labels
                .into_iter()
                .map(|(id, label)| {
                    let verified_at = stamps.get(&id).cloned();
                    (id, TrustRow { label, verified_at })
                })
                .collect(),
        ))
    }

    /// `unaccounted_ids`: newest first.
    pub fn unaccounted_ids(&self) -> Result<Vec<String>, ProvenanceError> {
        let rows = self.select(
            "SELECT id, created, log_mac FROM memories ORDER BY created DESC, id",
            &[],
            3,
        )?;
        let mut out = Vec::new();
        for row in rows {
            let memory_id = text_of(&row[0])?;
            if self.memo.contains(&memory_id)
                || self.pointer_verified(&memory_id, "memory_put", &row[2])? == Some(false)
            {
                out.push(memory_id);
            }
        }
        Ok(out)
    }

    /// `provenance_counts`: `(label, count)` in first-seen order.
    pub fn provenance_counts(&self) -> Result<Vec<(String, i64)>, ProvenanceError> {
        let unaccounted: std::collections::HashSet<String> =
            self.unaccounted_ids()?.into_iter().collect();
        let mut counts: Vec<(String, i64)> = Vec::new();
        let mut at: HashMap<String, usize> = HashMap::new();
        for row in self.select("SELECT id, provenance FROM memories", &[], 2)? {
            let label = if unaccounted.contains(&text_of(&row[0])?) {
                UNACCOUNTED.to_owned()
            } else {
                row[1].py_str()
            };
            match at.get(&label) {
                Some(&i) => counts[i].1 += 1,
                None => {
                    at.insert(label.clone(), counts.len());
                    counts.push((label, 1));
                }
            }
        }
        Ok(counts)
    }

    /// `links_for_many([id])[id]`: outbound and inbound link rows.
    pub fn links_for(&self, memory_id: &str) -> Result<(Vec<Link>, Vec<Link>), ProvenanceError> {
        let id = [memory_id.to_owned()];
        let link = |row: Vec<SqlValue>| -> Link {
            let mut it = row.into_iter();
            let mut next = || it.next().unwrap_or(SqlValue::Null);
            let (_, a, b, c) = (next(), next(), next(), next());
            (a, b, c)
        };
        let outbound = self
            .select(
                "SELECT source_id, type, target_id, note FROM memory_links \
                 WHERE source_id IN (?) ORDER BY source_id, type, target_id",
                &id,
                4,
            )?
            .into_iter()
            .map(link)
            .collect();
        let inbound = self
            .select(
                "SELECT target_id, type, source_id, note FROM memory_links \
                 WHERE target_id IN (?) ORDER BY target_id, type, source_id",
                &id,
                4,
            )?
            .into_iter()
            .map(link)
            .collect();
        Ok((outbound, inbound))
    }

    /// `links_with_status`: the links and the record's trust row.
    pub fn links_with_status(&self, memory_id: &str) -> Result<LinksWithStatus, ProvenanceError> {
        let (outbound, inbound) = self.links_for(memory_id)?;
        let trust = self.trust_rows(&[memory_id])?;
        let row = trust.and_then(|rows| {
            rows.into_iter()
                .find(|(id, _)| id == memory_id)
                .map(|(_, row)| row)
        });
        Ok((outbound, inbound, row))
    }

    /// `episode_provenance`: `local` or `unaccounted` per episode found.
    pub fn episode_provenance(
        &self,
        ids: &[&str],
    ) -> Result<Vec<(String, String)>, ProvenanceError> {
        let ids = dedup(ids);
        let mut out: Vec<(String, String)> = Vec::new();
        for batch in ids.chunks(BATCH) {
            let sql = format!(
                "SELECT id, log_mac FROM episodes WHERE id IN ({})",
                marks(batch.len())
            );
            for row in self.select(&sql, batch, 2)? {
                let episode_id = text_of(&row[0])?;
                let verified = self.pointer_verified(&episode_id, "episode_put", &row[1])?;
                let label = if verified == Some(false) {
                    UNACCOUNTED
                } else {
                    LOCAL
                };
                out.push((episode_id, label.to_owned()));
            }
        }
        Ok(out)
    }
}

/// `str(row["id"])`.
fn text_of(v: &SqlValue) -> Result<String, ProvenanceError> {
    Ok(v.py_str())
}

/// `json.detect_encoding(b)` and `b.decode(that, "surrogatepass")`, as
/// `json.loads` reads bytes (CPython 3.13's `json/__init__.py`): the code
/// points, or None for a UnicodeDecodeError.
pub fn decode_json_bytes(b: &[u8]) -> Option<Vec<u32>> {
    const BOM8: &[u8] = b"\xef\xbb\xbf";
    if b.starts_with(b"\x00\x00\xfe\xff") {
        return utf32(&b[4..], true);
    }
    if b.starts_with(b"\xff\xfe\x00\x00") {
        return utf32(&b[4..], false);
    }
    if b.starts_with(b"\xfe\xff") {
        return utf16(&b[2..], true);
    }
    if b.starts_with(b"\xff\xfe") {
        return utf16(&b[2..], false);
    }
    if let Some(rest) = b.strip_prefix(BOM8) {
        return utf8(rest);
    }
    if b.len() >= 4 {
        if b[0] == 0 {
            return if b[1] != 0 {
                utf16(b, true)
            } else {
                utf32(b, true)
            };
        }
        if b[1] == 0 {
            return if b[2] != 0 || b[3] != 0 {
                utf16(b, false)
            } else {
                utf32(b, false)
            };
        }
    } else if b.len() == 2 {
        if b[0] == 0 {
            return utf16(b, true);
        }
        if b[1] == 0 {
            return utf16(b, false);
        }
    }
    utf8(b)
}

/// UTF-8 with `surrogatepass`: an encoded surrogate (ED A0..BF xx) is
/// read as itself, never paired with the next.
fn utf8(b: &[u8]) -> Option<Vec<u32>> {
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        let (len, min, init) = match c {
            0x00..=0x7f => {
                out.push(u32::from(c));
                i += 1;
                continue;
            }
            0xc2..=0xdf => (2, 0x80, u32::from(c & 0x1f)),
            0xe0..=0xef => (3, 0x800, u32::from(c & 0x0f)),
            0xf0..=0xf4 => (4, 0x10000, u32::from(c & 0x07)),
            _ => return None,
        };
        if i + len > b.len() {
            return None;
        }
        let mut cp = init;
        for &k in &b[i + 1..i + len] {
            if k & 0xc0 != 0x80 {
                return None;
            }
            cp = (cp << 6) | u32::from(k & 0x3f);
        }
        if cp < min || cp > 0x10ffff {
            return None;
        }
        out.push(cp);
        i += len;
    }
    Some(out)
}

/// UTF-16 with `surrogatepass`: a pair joins, a lone surrogate is itself,
/// an odd trailing byte is an error.
fn utf16(b: &[u8], big: bool) -> Option<Vec<u32>> {
    if !b.len().is_multiple_of(2) {
        return None;
    }
    let units: Vec<u32> = b
        .chunks(2)
        .map(|p| {
            if big {
                u32::from(u16::from_be_bytes([p[0], p[1]]))
            } else {
                u32::from(u16::from_le_bytes([p[0], p[1]]))
            }
        })
        .collect();
    let mut out = Vec::with_capacity(units.len());
    let mut i = 0;
    while i < units.len() {
        let u = units[i];
        if (0xd800..0xdc00).contains(&u) {
            if let Some(&low) = units.get(i + 1) {
                if (0xdc00..0xe000).contains(&low) {
                    out.push(0x10000 + ((u - 0xd800) << 10) + (low - 0xdc00));
                    i += 2;
                    continue;
                }
            }
        }
        out.push(u);
        i += 1;
    }
    Some(out)
}

/// UTF-32 with `surrogatepass`: anything past U+10FFFF or a length that is
/// not a multiple of four is an error.
fn utf32(b: &[u8], big: bool) -> Option<Vec<u32>> {
    if !b.len().is_multiple_of(4) {
        return None;
    }
    b.chunks(4)
        .map(|q| {
            let a = [q[0], q[1], q[2], q[3]];
            let cp = if big {
                u32::from_be_bytes(a)
            } else {
                u32::from_le_bytes(a)
            };
            (cp <= 0x10ffff).then_some(cp)
        })
        .collect()
}
