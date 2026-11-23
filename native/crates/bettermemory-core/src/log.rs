//! The store's signed log: the row MAC, the key ring, rekey rows and
//! `verify_chain` (`bettermemory.log`).
//!
//! A row's MAC is HMAC-SHA256 over six parts, `str(seq)`, `ts`, `session`,
//! `kind`, the payload text and `prev_mac`: a missing part (only `session`
//! can be missing) as the byte 0x00, a present one as 0x01, its UTF-8
//! length as 8 big-endian bytes, and its UTF-8 bytes. The payload text is
//! stored and signed as written, so verifying re-serialises nothing.
//!
//! The key ring is held in memory ([`KeyRing`]): the bytes of the files a
//! keys directory holds for one store, by the names the reference gives
//! them. Reading the directory is the store's business.

use std::collections::BTreeMap;

use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use zeroize::Zeroizing;

use crate::json::{self, DumpOptions};
use crate::lexicon::hex;
use crate::pyvalue::{py_int, py_str, PyError, PyStr, PyValue};
use crate::unicode;

pub mod ring;

/// The `prev_mac` of the first row.
pub const GENESIS_MAC: &str = "0000000000000000000000000000000000000000000000000000000000000000";
pub const STORE_CREATED: &str = "store_created";
pub const REKEY: &str = "rekey";
/// A retired key file is named by this many leading characters of its
/// fingerprint.
pub const RETIRED_PREFIX_CHARS: usize = 16;

const TAMPER_PROBLEMS: [&str; 6] = [
    "mac_mismatch",
    "chain_break",
    "seq_gap",
    "final_key_mismatch",
    "foreign_segment",
    "origin_mismatch",
];

/// HMAC-SHA256 (RFC 2104) over `sha2`.
pub fn hmac_sha256(key: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    // The key block and both pads are key material: wiped when dropped.
    let mut block = Zeroizing::new([0u8; 64]);
    if key.len() > 64 {
        let mut digest = Zeroizing::new([0u8; 32]);
        digest.copy_from_slice(&Sha256::digest(key));
        block[..32].copy_from_slice(&digest[..]);
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut pad = Zeroizing::new([0u8; 64]);
    for (p, b) in pad.iter_mut().zip(block.iter()) {
        *p = b ^ 0x36;
    }
    let mut inner = Sha256::new();
    inner.update(&pad[..]);
    for part in parts {
        inner.update(part);
    }
    for (p, b) in pad.iter_mut().zip(block.iter()) {
        *p = b ^ 0x5C;
    }
    let mut outer = Sha256::new();
    outer.update(&pad[..]);
    outer.update(inner.finalize());
    outer.finalize().into()
}

/// `log.fingerprint`: the SHA-256 of a key, hex.
pub fn fingerprint(key: &[u8]) -> String {
    hex(&Sha256::digest(key))
}

/// One row of the `log` table.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LogRow {
    pub seq: i64,
    pub ts: String,
    pub session: Option<String>,
    pub kind: String,
    pub payload: String,
    pub prev_mac: String,
    pub mac: String,
}

/// `log.compute_mac`.
pub fn compute_mac(
    key: &[u8],
    seq: i64,
    ts: &str,
    session: Option<&str>,
    kind: &str,
    payload: &str,
    prev_mac: &str,
) -> String {
    let seq = seq.to_string();
    let mut message = Vec::with_capacity(payload.len() + 256);
    for part in [
        Some(seq.as_str()),
        Some(ts),
        session,
        Some(kind),
        Some(payload),
        Some(prev_mac),
    ] {
        match part {
            None => message.push(0),
            Some(text) => {
                message.push(1);
                message.extend_from_slice(&(text.len() as u64).to_be_bytes());
                message.extend_from_slice(text.as_bytes());
            }
        }
    }
    hex(&hmac_sha256(key, &[&message]))
}

/// `log.same_mac`: equal as UTF-8 (surrogates passed through), in
/// constant time for equal lengths.
pub fn same_mac(a: &[u32], b: &[u32]) -> bool {
    let a = wtf8(a);
    let b = wtf8(b);
    a.len() == b.len() && bool::from(a.ct_eq(&b))
}

/// UTF-8 with surrogates encoded as three bytes (`surrogatepass`).
fn wtf8(text: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len());
    for &cp in text {
        match char::from_u32(cp) {
            Some(c) => {
                let mut buf = [0; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
            None => {
                out.push(0xE0 | (cp >> 12) as u8);
                out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
                out.push(0x80 | (cp & 0x3F) as u8);
            }
        }
    }
    out
}

fn same_mac_str(a: &str, b: &str) -> bool {
    same_mac(&unicode::cps(a), &unicode::cps(b))
}

/// `log.row_verifies`.
pub fn row_verifies(key: &[u8], row: &LogRow) -> bool {
    let expected = compute_mac(
        key,
        row.seq,
        &row.ts,
        row.session.as_deref(),
        &row.kind,
        &row.payload,
        &row.prev_mac,
    );
    same_mac_str(&expected, &row.mac)
}

/// `log.append_row` without the database: the row that follows
/// `previous` (or the first row), its payload in canonical text.
pub fn next_row(
    previous: Option<&LogRow>,
    key: &[u8],
    kind: &str,
    payload: &PyValue,
    session: Option<&str>,
    ts: &str,
) -> Result<LogRow, PyError> {
    // Python's int has no bound; SQLite's INTEGER refuses the row after
    // i64::MAX with OverflowError.
    let seq = match previous {
        None => 1,
        Some(p) => p.seq.checked_add(1).ok_or(PyError::OverflowError)?,
    };
    let prev_mac = previous.map_or(GENESIS_MAC.to_owned(), |p| p.mac.clone());
    let text = json::canonical_payload(payload)?;
    let mac = compute_mac(key, seq, ts, session, kind, &text, &prev_mac);
    Ok(LogRow {
        seq,
        ts: ts.to_owned(),
        session: session.map(str::to_owned),
        kind: kind.to_owned(),
        payload: text,
        prev_mac,
        mac,
    })
}

/// The payload of a `rekey` row from `old_key`'s fingerprint to
/// `new_key`'s (the row is signed with the new key).
pub fn rekey_payload(old_key: &[u8], new_key: &[u8]) -> PyValue {
    PyValue::Dict(vec![
        (
            PyValue::str("old_fingerprint"),
            PyValue::str(&fingerprint(old_key)),
        ),
        (
            PyValue::str("new_fingerprint"),
            PyValue::str(&fingerprint(new_key)),
        ),
    ])
}

/// `log._rekey_fingerprints`: the old and new names a rekey row's payload
/// gives, or None. Err(RecursionError) for a payload nested past
/// [`json::MAX_DEPTH`], which the reference does not catch either.
pub fn rekey_fingerprints(row: &LogRow) -> Result<Option<(PyStr, PyStr)>, PyError> {
    let payload = match json::loads(&unicode::cps(&row.payload)) {
        Ok(payload) => payload,
        Err(PyError::RecursionError) => return Err(PyError::RecursionError),
        Err(_) => return Ok(None),
    };
    if !matches!(payload, PyValue::Dict(_)) {
        return Ok(None);
    }
    Ok(
        match (
            payload.get("old_fingerprint"),
            payload.get("new_fingerprint"),
        ) {
            (Some(PyValue::Str(old)), Some(PyValue::Str(new))) => Some((old.clone(), new.clone())),
            _ => None,
        },
    )
}

/// `log.is_fingerprint`: 64 lowercase hex digits, the only names a key
/// lookup answers.
pub fn is_fingerprint(name: &str) -> bool {
    name.len() == 64
        && name
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Key material: the bytes, wiped when dropped. Its `Debug` shows the
/// length and the fingerprint's first characters, never a byte of the key.
#[derive(Clone, PartialEq, Eq)]
pub struct Key(Zeroizing<Vec<u8>>);

impl Key {
    pub fn new(bytes: Vec<u8>) -> Self {
        Key(Zeroizing::new(bytes))
    }

    pub fn bytes(&self) -> &[u8] {
        &self.0
    }
}

impl From<Vec<u8>> for Key {
    fn from(bytes: Vec<u8>) -> Self {
        Key::new(bytes)
    }
}

impl std::fmt::Debug for Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "Key({} bytes, fingerprint {}…)",
            self.0.len(),
            prefix(&fingerprint(&self.0))
        )
    }
}

/// The head checkpoint (`log.Head`).
#[derive(Clone, Debug, PartialEq)]
pub struct Head {
    pub seq: PyValue,
    pub mac: PyStr,
}

/// The origin anchor (`log.HistoryOrigin`).
#[derive(Clone, Debug, PartialEq)]
pub struct HistoryOrigin {
    pub seq: i64,
    pub mac: String,
    pub key_fingerprint: Option<String>,
}

fn is_hex_mac(text: &[u32]) -> bool {
    text.len() == 64
        && text
            .iter()
            .all(|&c| (0x30..=0x39).contains(&c) || (0x61..=0x66).contains(&c))
}

/// `log.parse_origin` of a parsed origin file.
pub fn parse_origin(raw: &PyValue) -> Option<HistoryOrigin> {
    if !matches!(raw, PyValue::Dict(_)) {
        return None;
    }
    let seq = match raw.get("seq")? {
        PyValue::Int(text) => text.parse::<i64>().ok().filter(|s| *s >= 1)?,
        _ => return None,
    };
    let mac = match raw.get("mac")? {
        PyValue::Str(s) if is_hex_mac(s) => unicode::to_string(s)?,
        _ => return None,
    };
    let key_fingerprint = match raw.get("key_fingerprint") {
        Some(PyValue::Str(s)) if is_hex_mac(s) => unicode::to_string(s),
        _ => None,
    };
    Some(HistoryOrigin {
        seq,
        mac,
        key_fingerprint,
    })
}

/// One store's key ring, in memory: the contents of the files a keys
/// directory holds under the store's id.
#[derive(Clone, Debug, Default)]
pub struct KeyRing {
    /// `<store_id>.key`.
    pub current: Option<Key>,
    /// `<store_id>.<prefix>.key`, by the prefix in its name.
    pub retired: BTreeMap<String, Key>,
    /// `<store_id>.head`, its bytes.
    pub head_file: Option<Vec<u8>>,
    /// `<store_id>.origin`, its bytes.
    pub origin_file: Option<Vec<u8>>,
}

fn prefix(fingerprint: &str) -> String {
    fingerprint.chars().take(RETIRED_PREFIX_CHARS).collect()
}

impl KeyRing {
    /// File a key as retired, under its fingerprint's prefix.
    pub fn retire(&mut self, key: Key) {
        self.retired.insert(prefix(&fingerprint(key.bytes())), key);
    }

    /// `KeyRing.key_for_fingerprint`: the current key or the retired key
    /// filed under the name's prefix, when its fingerprint is the name. A
    /// name that is not a fingerprint ([`is_fingerprint`]) matches no key
    /// and names no file.
    pub fn key_for_fingerprint(&self, name: &str) -> Option<Key> {
        if !is_fingerprint(name) {
            return None;
        }
        let candidates = [self.current.as_ref(), self.retired.get(&prefix(name))];
        candidates
            .into_iter()
            .flatten()
            .find(|key| fingerprint(key.bytes()) == name)
            .cloned()
    }

    /// `KeyRing.held_keys`: the current key, then the retired keys in
    /// file-name order, without repeats.
    pub fn held_keys(&self) -> Vec<Key> {
        let mut keys: Vec<Key> = Vec::new();
        for key in self.current.iter().chain(self.retired.values()) {
            if !keys.contains(key) {
                keys.push(key.clone());
            }
        }
        keys
    }

    fn read_json(file: &Option<Vec<u8>>) -> Option<Result<PyValue, PyError>> {
        let bytes = file.as_ref()?;
        let Ok(text) = std::str::from_utf8(bytes) else {
            return Some(Err(PyError::ValueError));
        };
        // `read_text` reads universal newlines.
        let text = text.replace("\r\n", "\n").replace('\r', "\n");
        Some(json::loads(&unicode::cps(&text)))
    }

    /// `KeyRing.read_head`: `int(raw["seq"])` and `str(raw["mac"])`, None
    /// when the file is absent or reads as no head. A `seq` that `int()`
    /// refuses with OverflowError (an infinite float) is not caught by the
    /// reference; it raises out of `verify_chain`, returned here as Err.
    pub fn read_head(&self) -> Result<Option<Head>, PyError> {
        let raw = match Self::read_json(&self.head_file) {
            None => return Ok(None),
            Some(Err(PyError::RecursionError)) => return Err(PyError::RecursionError),
            Some(Err(_)) => return Ok(None),
            Some(Ok(raw)) => raw,
        };
        let PyValue::Dict(_) = raw else {
            return Ok(None);
        };
        let (Some(seq), Some(mac)) = (raw.get("seq"), raw.get("mac")) else {
            return Ok(None);
        };
        let seq = match py_int(seq) {
            Ok(v) => v,
            Err(PyError::OverflowError) => return Err(PyError::OverflowError),
            Err(_) => return Ok(None),
        };
        let mac = match py_str(mac) {
            Ok(mac) => mac,
            Err(PyError::RecursionError) => return Err(PyError::RecursionError),
            Err(_) => return Ok(None),
        };
        Ok(Some(Head { seq, mac }))
    }

    /// `KeyRing.read_origin`: Err(RecursionError) for a file nested past
    /// [`json::MAX_DEPTH`], which the reference does not catch.
    pub fn read_origin(&self) -> Result<Option<HistoryOrigin>, PyError> {
        match Self::read_json(&self.origin_file) {
            None => Ok(None),
            Some(Ok(raw)) => Ok(parse_origin(&raw)),
            Some(Err(PyError::RecursionError)) => Err(PyError::RecursionError),
            Some(Err(_)) => Ok(None),
        }
    }
}

/// The text of the head file `KeyRing.write_head` writes.
pub fn head_file_text(seq: i64, mac: &str, ts: &str) -> String {
    let value = PyValue::Dict(vec![
        (PyValue::str("seq"), PyValue::int(i128::from(seq))),
        (PyValue::str("mac"), PyValue::str(mac)),
        (PyValue::str("ts"), PyValue::str(ts)),
    ]);
    // Plain values: dumps cannot fail, and ASCII escaping leaves no
    // surrogate.
    json::dumps(&value, DumpOptions::DEFAULT)
        .map(|text| json::utf8_text(&text))
        .unwrap_or_default()
}

/// A JSON value of a report.
#[derive(Clone, Debug, PartialEq)]
pub enum ReportValue {
    Null,
    Int(i64),
    /// An int too large for i64 (a head file's `seq`), as decimal text.
    BigInt(String),
    Str(PyStr),
}

/// One dict of a report, keys in the order the reference builds them.
pub type ReportDict = Vec<(&'static str, ReportValue)>;

fn s(text: &str) -> ReportValue {
    ReportValue::Str(unicode::cps(text))
}

/// `ChainReport.to_dict()`.
#[derive(Clone, Debug, PartialEq)]
pub struct ChainReport {
    pub status: &'static str,
    pub rows: usize,
    pub segments: Vec<ReportDict>,
    pub problems: Vec<ReportDict>,
    pub head: ReportDict,
    pub origin: ReportDict,
}

#[derive(Clone, Debug)]
struct Segment {
    key_fingerprint: PyStr,
    key: Option<Key>,
    foreign: bool,
}

#[derive(Clone, Debug)]
pub struct OriginCheck {
    pub status: &'static str,
    pub seq: Option<i64>,
    pub reason: Option<&'static str>,
    pub signer: Option<String>,
}

impl OriginCheck {
    pub fn holds(&self) -> bool {
        self.status == "ok"
    }
    pub fn broken(&self) -> bool {
        self.status == "mismatch"
    }

    /// The signer's fingerprint is held privately from the report.
    pub fn to_dict(&self) -> PyValue {
        let mut entries = vec![(PyValue::str("status"), PyValue::str(self.status))];
        if let Some(seq) = self.seq {
            entries.push((PyValue::str("seq"), PyValue::int(i128::from(seq))));
        }
        if let Some(reason) = self.reason {
            entries.push((PyValue::str("reason"), PyValue::str(reason)));
        }
        PyValue::Dict(entries)
    }
}

impl PartialEq for OriginCheck {
    fn eq(&self, other: &Self) -> bool {
        (self.status, self.seq, self.reason) == (other.status, other.seq, other.reason)
    }
}
impl Eq for OriginCheck {}

pub fn check_origin(
    origin: Option<&HistoryOrigin>,
    row: Option<&LogRow>,
    keys: &[Key],
) -> OriginCheck {
    let Some(origin) = origin else {
        return OriginCheck {
            status: "absent",
            seq: None,
            reason: None,
            signer: None,
        };
    };
    let mismatch = |reason| OriginCheck {
        status: "mismatch",
        seq: Some(origin.seq),
        reason: Some(reason),
        signer: None,
    };
    let Some(row) = row else {
        return mismatch("missing");
    };
    if !same_mac_str(&row.mac, &origin.mac) {
        return mismatch("mac");
    }
    let mut signer_held = false;
    for key in keys {
        if row_verifies(key.bytes(), row) {
            return OriginCheck {
                status: "ok",
                seq: Some(origin.seq),
                reason: None,
                signer: Some(fingerprint(key.bytes())),
            };
        }
        if Some(fingerprint(key.bytes())) == origin.key_fingerprint {
            signer_held = true;
        }
    }
    let unverifiable = OriginCheck {
        status: "unverifiable",
        seq: Some(origin.seq),
        reason: None,
        signer: None,
    };
    if keys.is_empty() {
        return unverifiable;
    }
    if origin.key_fingerprint.is_none() || signer_held {
        return mismatch("signature");
    }
    unverifiable
}

/// `log.check_origin`'s status for the anchor `origin` and the log row at
/// its seq (`row`), under the ring's held keys: `absent`, `ok`,
/// `unverifiable` or `mismatch` (`OriginCheck.broken`).
pub fn origin_status(
    origin: Option<&HistoryOrigin>,
    row: Option<&LogRow>,
    ring: &KeyRing,
) -> &'static str {
    check_origin(origin, row, &ring.held_keys()).status
}

fn leading_key(ring: &KeyRing, named: &[u32], first_row: &LogRow) -> (PyStr, Option<Key>) {
    let name = unicode::to_string(named);
    let key = match &name {
        Some(n) if !n.is_empty() => ring.key_for_fingerprint(n),
        _ => None,
    };
    if let Some(key) = key {
        return (named.to_vec(), Some(key));
    }
    for candidate in ring.held_keys() {
        if row_verifies(candidate.bytes(), first_row) {
            return (
                unicode::cps(&fingerprint(candidate.bytes())),
                Some(candidate),
            );
        }
    }
    (named.to_vec(), None)
}

/// The segment map by first seq (a later start at the same seq replaces an
/// earlier one, as the reference's dict does).
fn segments_from(
    first_row: Option<&LogRow>,
    rekeys: &[LogRow],
    ring: &KeyRing,
    current_fingerprint: &str,
    origin_seq: Option<i64>,
) -> Result<BTreeMap<i64, Segment>, PyError> {
    let Some(first_row) = first_row else {
        return Ok(BTreeMap::new());
    };
    let mut starts: Vec<(&LogRow, PyStr)> = Vec::new();
    let mut leading_name = unicode::cps(current_fingerprint);
    for (position, row) in rekeys.iter().filter(|r| r.kind == REKEY).enumerate() {
        let names = rekey_fingerprints(row)?;
        if position == 0 {
            if let Some((old, _)) = &names {
                leading_name = old.clone();
            }
        }
        starts.push((row, names.map_or_else(Vec::new, |(_, new)| new)));
    }
    let mut segments: Vec<(i64, Segment)> = Vec::new();
    let mut held_seen = false;
    if starts
        .first()
        .is_none_or(|(row, _)| row.seq != first_row.seq)
    {
        let (name, key) = leading_key(ring, &leading_name, first_row);
        held_seen = key.is_some();
        segments.push((
            first_row.seq,
            Segment {
                key_fingerprint: name,
                key,
                foreign: false,
            },
        ));
    }
    let mut keys: BTreeMap<PyStr, Option<Key>> = BTreeMap::new();
    for (row, name) in starts {
        let key = keys
            .entry(name.clone())
            .or_insert_with(|| match unicode::to_string(&name) {
                Some(n) if !n.is_empty() => ring.key_for_fingerprint(&n),
                _ => None,
            })
            .clone();
        let foreign = key.is_none() && held_seen;
        held_seen = held_seen || key.is_some();
        segments.push((
            row.seq,
            Segment {
                key_fingerprint: name,
                key,
                foreign,
            },
        ));
    }
    if let Some(origin_seq) = origin_seq {
        let count = segments.len();
        for position in 0..count {
            let ends_before = segments
                .get(position + 1)
                .is_some_and(|(first_seq, _)| *first_seq <= origin_seq);
            let segment = &mut segments[position].1;
            segment.foreign = segment.key.is_none() && !ends_before;
        }
    }
    Ok(segments.into_iter().collect())
}

fn row_problem(row: &LogRow, name: &str) -> ReportDict {
    vec![
        ("seq", ReportValue::Int(row.seq)),
        ("kind", s(&row.kind)),
        ("problem", s(name)),
    ]
}

fn int_value(value: &PyValue) -> ReportValue {
    match value {
        PyValue::Int(text) => text
            .parse::<i64>()
            .map_or_else(|_| ReportValue::BigInt(text.clone()), ReportValue::Int),
        other => ReportValue::Str(py_str(other).unwrap_or_default()),
    }
}

fn wide_int(value: i128) -> ReportValue {
    i64::try_from(value).map_or_else(|_| ReportValue::BigInt(value.to_string()), ReportValue::Int)
}

/// Compare a head's `seq` (any int) with a row's.
fn cmp_seq(head: &PyValue, seq: i64) -> std::cmp::Ordering {
    match head {
        PyValue::Int(text) => match text.parse::<i64>() {
            Ok(v) => v.cmp(&seq),
            Err(_) if text.starts_with('-') => std::cmp::Ordering::Less,
            Err(_) => std::cmp::Ordering::Greater,
        },
        _ => std::cmp::Ordering::Greater,
    }
}

/// Where `verify_chain` reads the log's rows from: the rows in memory
/// (a slice) or a store's log table (`store::StoreFile`). Every row is
/// visited once, in seq order, by [`LogSource::each`]; the few rows the
/// report needs out of order are asked for by seq or by kind, so a source
/// on disk never holds the whole log.
pub trait LogSource {
    /// What reading a row can fail with (none for rows in memory).
    type Error;
    /// The first row in seq order.
    fn first(&self) -> Result<Option<LogRow>, Self::Error>;
    /// The `rekey` rows, in seq order.
    fn rekeys(&self) -> Result<Vec<LogRow>, Self::Error>;
    /// The first row in seq order whose seq is `seq`.
    fn at_seq(&self, seq: i64) -> Result<Option<LogRow>, Self::Error>;
    /// Visit every row in seq order; `visit`'s error stops the walk.
    fn each(
        &self,
        visit: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, Self::Error>;
}

impl LogSource for [LogRow] {
    type Error = std::convert::Infallible;
    fn first(&self) -> Result<Option<LogRow>, Self::Error> {
        Ok(<[LogRow]>::first(self).cloned())
    }
    fn rekeys(&self) -> Result<Vec<LogRow>, Self::Error> {
        Ok(self.iter().filter(|r| r.kind == REKEY).cloned().collect())
    }
    fn at_seq(&self, seq: i64) -> Result<Option<LogRow>, Self::Error> {
        Ok(self.iter().find(|r| r.seq == seq).cloned())
    }
    fn each(
        &self,
        visit: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, Self::Error> {
        for row in self {
            if let Err(e) = visit(row) {
                return Ok(Err(e));
            }
        }
        Ok(Ok(()))
    }
}

/// Why `verify_chain_from` gave no report: the reference's exception, or
/// the source's own failure.
#[derive(Debug)]
pub enum VerifyError<E> {
    Py(PyError),
    Source(E),
}

impl<E> From<PyError> for VerifyError<E> {
    fn from(e: PyError) -> Self {
        VerifyError::Py(e)
    }
}

/// `log.verify_chain` over the rows the log holds from seq 1 on, in seq
/// order. Err only where the reference raises: OverflowError for a head
/// file whose `seq` is an infinite float, RecursionError for an origin
/// file, rekey payload or head file nested past [`json::MAX_DEPTH`].
pub fn verify_chain(
    rows: &[LogRow],
    ring: &KeyRing,
    current_fingerprint: &str,
) -> Result<ChainReport, PyError> {
    verify_chain_from(rows, ring, current_fingerprint).map_err(|e| match e {
        VerifyError::Py(e) => e,
        VerifyError::Source(never) => match never {},
    })
}

/// [`verify_chain`] over any [`LogSource`]: the same report, the rows read
/// in one pass plus the first row, the rekey rows and the rows the origin
/// anchor and the head name.
pub fn verify_chain_from<S: LogSource + ?Sized>(
    source: &S,
    ring: &KeyRing,
    current_fingerprint: &str,
) -> Result<ChainReport, VerifyError<S::Error>> {
    let mut problems: Vec<ReportDict> = Vec::new();
    let anchor = ring.read_origin()?;
    let anchored_row = match &anchor {
        Some(a) => source.at_seq(a.seq).map_err(VerifyError::Source)?,
        None => None,
    };
    let origin = check_origin(anchor.as_ref(), anchored_row.as_ref(), &ring.held_keys());
    let mut origin_dict: ReportDict = vec![("status", s(origin.status))];
    if let Some(seq) = origin.seq {
        origin_dict.push(("seq", ReportValue::Int(seq)));
    }
    if let Some(reason) = origin.reason {
        origin_dict.push(("reason", s(reason)));
    }
    if origin.status == "mismatch" {
        problems.push(vec![
            (
                "seq",
                origin.seq.map_or(ReportValue::Null, ReportValue::Int),
            ),
            ("kind", s("origin")),
            ("problem", s("origin_mismatch")),
            ("reason", origin.reason.map_or(ReportValue::Null, s)),
        ]);
    }
    let origin_seq = if origin.status == "ok" {
        origin.seq
    } else {
        None
    };
    let first_row = source.first().map_err(VerifyError::Source)?;
    let rekeys = if first_row.is_some() {
        source.rekeys().map_err(VerifyError::Source)?
    } else {
        Vec::new()
    };
    let segment_map = segments_from(
        first_row.as_ref(),
        &rekeys,
        ring,
        current_fingerprint,
        origin_seq,
    )?;
    drop(rekeys);

    struct Open {
        first_seq: i64,
        last_seq: i64,
        key_fingerprint: PyStr,
        status: &'static str,
        rows: i64,
        foreign: bool,
    }
    let mut segments: Vec<Open> = Vec::new();
    let mut key: Option<Key> = None;
    // Python's ints: a seq of i64::MAX expects one past it.
    let mut expected_seq: i128 = 1;
    let mut previous_mac: Option<String> = None;
    let mut last: Option<LogRow> = None;
    let mut count: usize = 0;
    let mut visit = |row: &LogRow| -> Result<(), PyError> {
        count += 1;
        if row.kind == REKEY && rekey_fingerprints(row)?.is_none() {
            problems.push(row_problem(row, "payload_invalid"));
        }
        if let Some(named) = segment_map.get(&row.seq) {
            key = named.key.clone();
            segments.push(Open {
                first_seq: row.seq,
                last_seq: row.seq,
                key_fingerprint: named.key_fingerprint.clone(),
                status: if key.is_some() { "ok" } else { "unverifiable" },
                rows: 0,
                foreign: named.foreign,
            });
        }
        // The first row always opens a segment (it is the leading
        // segment's or a rekey's); a row before any would be skipped.
        let Some(segment) = segments.last_mut() else {
            last = Some(row.clone());
            return Ok(());
        };
        segment.last_seq = row.seq;
        segment.rows += 1;
        if i128::from(row.seq) != expected_seq {
            let mut p = row_problem(row, "seq_gap");
            p.push(("expected", wide_int(expected_seq)));
            problems.push(p);
            expected_seq = i128::from(row.seq);
        }
        expected_seq += 1;
        let linked_to = previous_mac.as_deref().unwrap_or(GENESIS_MAC);
        if row.prev_mac != linked_to {
            problems.push(row_problem(row, "chain_break"));
        }
        if let Some(k) = &key {
            if !row_verifies(k.bytes(), row) {
                problems.push(row_problem(row, "mac_mismatch"));
                segment.status = "tampered";
            }
        }
        previous_mac = Some(row.mac.clone());
        last = Some(row.clone());
        Ok(())
    };
    source.each(&mut visit).map_err(VerifyError::Source)??;
    for stray in segments.iter().filter(|s| s.foreign) {
        problems.push(vec![
            ("seq", ReportValue::Int(stray.first_seq)),
            ("kind", s(REKEY)),
            ("problem", s("foreign_segment")),
            ("first_seq", ReportValue::Int(stray.first_seq)),
            ("last_seq", ReportValue::Int(stray.last_seq)),
            (
                "key_fingerprint",
                ReportValue::Str(stray.key_fingerprint.clone()),
            ),
        ]);
    }
    if let Some(last) = segments.last() {
        if last.key_fingerprint != unicode::cps(current_fingerprint) {
            problems.push(vec![
                ("seq", ReportValue::Int(last.last_seq)),
                ("kind", s(REKEY)),
                ("problem", s("final_key_mismatch")),
                ("expected", s(current_fingerprint)),
                ("found", ReportValue::Str(last.key_fingerprint.clone())),
            ]);
        }
    }

    let head = ring.read_head()?;
    let last = last.as_ref();
    let head_dict: ReportDict = match (&head, last) {
        (None, _) => vec![("status", s("absent"))],
        (Some(h), last) => {
            let status = match last {
                None => "tail_deleted",
                Some(last) => match cmp_seq(&h.seq, last.seq) {
                    std::cmp::Ordering::Greater => "tail_deleted",
                    std::cmp::Ordering::Equal => {
                        if same_mac(&h.mac, &unicode::cps(&last.mac)) {
                            "ok"
                        } else {
                            "mismatch"
                        }
                    }
                    std::cmp::Ordering::Less => {
                        // A head seq below the last row's is an i64.
                        let named = match &h.seq {
                            PyValue::Int(text) => match text.parse::<i64>() {
                                Ok(seq) => source.at_seq(seq).map_err(VerifyError::Source)?,
                                Err(_) => None,
                            },
                            _ => None,
                        };
                        if named.is_some_and(|r| same_mac(&h.mac, &unicode::cps(&r.mac))) {
                            "stale"
                        } else {
                            "mismatch"
                        }
                    }
                },
            };
            vec![
                ("status", s(status)),
                ("seq", int_value(&h.seq)),
                ("mac", ReportValue::Str(h.mac.clone())),
            ]
        }
    };
    let head_status = match head_dict.first() {
        Some((_, ReportValue::Str(text))) => unicode::to_string(text).unwrap_or_default(),
        _ => String::new(),
    };
    let tampered = problems.iter().any(|p| {
        p.iter().any(|(k, v)| {
            *k == "problem"
                && matches!(v, ReportValue::Str(t) if TAMPER_PROBLEMS.iter().any(|n| unicode::cps(n) == *t))
        })
    }) || head_status == "tail_deleted"
        || head_status == "mismatch";
    let unverifiable = head_status == "absent"
        || origin.status == "unverifiable"
        || segments.iter().any(|s| s.status == "unverifiable");
    let status = if tampered {
        "tampered"
    } else if unverifiable {
        "unverifiable"
    } else {
        "ok"
    };
    Ok(ChainReport {
        status,
        rows: count,
        segments: segments
            .iter()
            .map(|s| {
                vec![
                    ("first_seq", ReportValue::Int(s.first_seq)),
                    ("last_seq", ReportValue::Int(s.last_seq)),
                    (
                        "key_fingerprint",
                        ReportValue::Str(s.key_fingerprint.clone()),
                    ),
                    ("status", self::s(s.status)),
                    ("rows", ReportValue::Int(s.rows)),
                ]
            })
            .collect(),
        problems,
        head: head_dict,
        origin: origin_dict,
    })
}

/// One segment of [`chain_trust`]'s map: where it starts, the key that
/// verifies it when the ring holds one, and whether it is foreign
/// (`log.Segment`).
#[derive(Clone, Debug)]
pub struct TrustSegment {
    pub first_seq: i64,
    pub key: Option<Key>,
    pub foreign: bool,
}

/// `log.ChainTrust`: the segments in seq order and whether the origin
/// anchor they were read under no longer matches the log
/// (`OriginCheck.broken`).
#[derive(Clone, Debug)]
pub struct ChainTrust {
    pub segments: Vec<TrustSegment>,
    pub origin_broken: bool,
}

/// `log.chain_trust(conn, keyring=ring, current_fingerprint=...,
/// current_key=ring.current)`: the origin check and the segment map
/// `verify_chain_from` reads, from the first row, the rekey rows and the
/// anchored row. With the ring's current key as `current_key` the keys
/// are tried in `held_keys`' order, the current key first, which is the
/// reference's order (`_ring_keys`, `_leading_key`); a `current_key` the
/// ring does not hold as current is not expressible here.
pub fn chain_trust<S: LogSource + ?Sized>(
    source: &S,
    ring: &KeyRing,
    current_fingerprint: &str,
) -> Result<ChainTrust, VerifyError<S::Error>> {
    let anchor = ring.read_origin()?;
    let anchored_row = match &anchor {
        Some(a) => source.at_seq(a.seq).map_err(VerifyError::Source)?,
        None => None,
    };
    let origin = check_origin(anchor.as_ref(), anchored_row.as_ref(), &ring.held_keys());
    let origin_seq = if origin.status == "ok" {
        origin.seq
    } else {
        None
    };
    let first_row = source.first().map_err(VerifyError::Source)?;
    let rekeys = if first_row.is_some() {
        source.rekeys().map_err(VerifyError::Source)?
    } else {
        Vec::new()
    };
    let map = segments_from(
        first_row.as_ref(),
        &rekeys,
        ring,
        current_fingerprint,
        origin_seq,
    )?;
    Ok(ChainTrust {
        segments: map
            .into_iter()
            .map(|(first_seq, s)| TrustSegment {
                first_seq,
                key: s.key,
                foreign: s.foreign,
            })
            .collect(),
        origin_broken: origin.status == "mismatch",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unhex(text: &str) -> Vec<u8> {
        (0..text.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
            .collect()
    }

    /// RFC 4231's HMAC-SHA256 test cases 1 to 7 (case 5 truncated to 128
    /// bits, as the RFC gives it).
    #[test]
    fn rfc4231() {
        let long_key = vec![0xaa; 131];
        let cases: [(Vec<u8>, Vec<u8>, &str); 7] = [
            (vec![0x0b; 20], b"Hi There".to_vec(), "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"),
            (b"Jefe".to_vec(), b"what do ya want for nothing?".to_vec(), "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"),
            (vec![0xaa; 20], vec![0xdd; 50], "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe"),
            (unhex("0102030405060708090a0b0c0d0e0f10111213141516171819"), vec![0xcd; 50], "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b"),
            (vec![0x0c; 20], b"Test With Truncation".to_vec(), "a3b6167473100ee06e0c796c2955552b"),
            (long_key.clone(), b"Test Using Larger Than Block-Size Key - Hash Key First".to_vec(), "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"),
            (long_key, b"This is a test using a larger than block-size key and a larger than block-size data. The key needs to be hashed before being used by the HMAC algorithm.".to_vec(), "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2"),
        ];
        for (key, data, expected) in cases {
            let mac = hex(&hmac_sha256(&key, &[&data]));
            assert_eq!(&mac[..expected.len()], expected);
        }
    }
}
