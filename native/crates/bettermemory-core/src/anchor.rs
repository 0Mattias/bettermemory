//! External signed-chain anchors, their local files and the read-only verification
//! projection from `anchor.py`.
use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::config::decode::decode_utf8_sig;
use crate::fsutil::{bounded_read, fsdecode, BoundedReadError};
use crate::json::{self, LoadError};
use crate::pyvalue::{self, PyStr, PyValue};
use crate::store::{sql_value, StoreError};
use crate::unicode::{cps, is_space};

pub const ANCHOR_FORMAT: &str = "bettermemory.anchor/1";
pub const MAX_ANCHOR_FILE_BYTES: u64 = 16 * 1024 * 1024;

/// The sequence is a canonical Python integer in decimal. Directly supplied
/// anchors may refer to sequences outside SQLite's range.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Anchor {
    pub store_id: PyStr,
    pub seq: String,
    pub mac: PyStr,
    pub chain_sha256: PyStr,
    pub created_at: PyStr,
}

#[derive(Debug)]
pub enum AnchorError {
    /// The Python exception class and its codepoint-preserving message.
    Raised {
        class: &'static str,
        message: PyStr,
    },
    Store(StoreError),
}

impl From<rusqlite::Error> for AnchorError {
    fn from(err: rusqlite::Error) -> Self {
        Self::Store(err.into())
    }
}
impl From<StoreError> for AnchorError {
    fn from(err: StoreError) -> Self {
        Self::Store(err)
    }
}

fn raised(class: &'static str, message: PyStr) -> AnchorError {
    AnchorError::Raised { class, message }
}

fn file_error(place: &[u32], message: &[u32]) -> AnchorError {
    raised("AnchorFileError", [place, &cps(": "), message].concat())
}

fn dict<'a>(entries: impl IntoIterator<Item = (&'a str, PyValue)>) -> PyValue {
    PyValue::Dict(
        entries
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

impl Anchor {
    /// The insertion order of the Python dataclass's JSON projection.
    pub fn to_dict(&self) -> PyValue {
        dict([
            ("format", PyValue::str(ANCHOR_FORMAT)),
            ("store_id", PyValue::Str(self.store_id.clone())),
            ("seq", PyValue::Int(self.seq.clone())),
            ("mac", PyValue::Str(self.mac.clone())),
            ("chain_sha256", PyValue::Str(self.chain_sha256.clone())),
            ("created_at", PyValue::Str(self.created_at.clone())),
        ])
    }
}

fn field<'a>(raw: &'a PyValue, name: &str) -> Option<&'a PyStr> {
    match raw.get(name) {
        Some(PyValue::Str(text)) => Some(text),
        _ => None,
    }
}
fn hex64(text: &[u32]) -> bool {
    text.len() == 64 && text.iter().all(|c| matches!(*c,0x30..=0x39|0x61..=0x66))
}

pub fn parse_anchor(raw: &PyValue, place: &[u32]) -> Result<Anchor, AnchorError> {
    let refuse = |msg: &str| file_error(place, &cps(msg));
    if !matches!(raw, PyValue::Dict(_)) {
        return Err(refuse("an anchor is a JSON object"));
    }
    if field(raw, "format") != Some(&cps(ANCHOR_FORMAT)) {
        let missing = PyValue::None;
        let value = raw.get("format").unwrap_or(&missing);
        let shown = pyvalue::repr(value).map_err(|e| raised(e.name(), cps(e.name())))?;
        return Err(file_error(
            place,
            &[
                cps("not a bettermemory.anchor/1 anchor (format "),
                shown,
                cps(")"),
            ]
            .concat(),
        ));
    }
    let store_id = field(raw, "store_id")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| refuse("store_id must be non-empty text"))?;
    let seq = match raw.get("seq") {
        Some(PyValue::Int(n)) if n.parse::<i64>().is_ok_and(|v| v > 0) => n,
        _ => {
            return Err(refuse(
                "seq must be a whole number from 1 to 9223372036854775807",
            ))
        }
    };
    let mac = field(raw, "mac")
        .filter(|s| hex64(s))
        .ok_or_else(|| refuse("mac must be 64 lowercase hex characters"))?;
    let chain_sha256 = field(raw, "chain_sha256")
        .filter(|s| hex64(s))
        .ok_or_else(|| refuse("chain_sha256 must be 64 lowercase hex characters"))?;
    let created_at = field(raw, "created_at")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| refuse("created_at must be non-empty text"))?;
    Ok(Anchor {
        store_id: store_id.clone(),
        seq: seq.clone(),
        mac: mac.clone(),
        chain_sha256: chain_sha256.clone(),
        created_at: created_at.clone(),
    })
}

fn recursion(text: &[u32]) -> AnchorError {
    let message = json::DecodeFailure::Recursion.message(text);
    raised("RecursionError", cps(&message))
}

/// Decode a regular file's bytes, accepting either a single JSON object or JSON Lines.
pub fn decode_anchors(raw: &[u8], place: &[u32]) -> Result<Vec<Anchor>, AnchorError> {
    let text = cps(&decode_utf8_sig(raw)
        .map_err(|msg| file_error(place, &cps(&format!("cannot read an anchor file: {msg}"))))?);
    match json::loads_detailed(&text) {
        Ok(value) => return Ok(vec![parse_anchor(&value, place)?]),
        Err(LoadError::Recursion) => return Err(recursion(&text)),
        Err(_) => {}
    }
    let mut anchors = Vec::new();
    for (i, line) in crate::difflib::splitline_slices(&text).enumerate() {
        if line.iter().all(|&c| is_space(c)) {
            continue;
        }
        let where_ = [place, &cps(&format!(":{}", i + 1))].concat();
        let value = json::loads_detailed(line).map_err(|err| {
            if err == LoadError::Recursion {
                return recursion(line);
            }
            let message = json::decode_failure(line)
                .expect("a failed JSON decode has a diagnostic")
                .message(line);
            file_error(&where_, &cps(&format!("not JSON ({message})")))
        })?;
        anchors.push(parse_anchor(&value, &where_)?);
    }
    if anchors.is_empty() {
        return Err(file_error(place, &cps("holds no anchor")));
    }
    Ok(anchors)
}

fn python_path(path: &Path) -> PathBuf {
    let mut result = PathBuf::new();
    for part in path.components() {
        if part != Component::CurDir {
            result.push(part.as_os_str());
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::{OsStrExt, OsStringExt};
        let raw = path.as_os_str().as_bytes();
        if raw.starts_with(b"//") && !raw.starts_with(b"///") {
            let mut double = vec![b'/'];
            double.extend_from_slice(result.as_os_str().as_bytes());
            return PathBuf::from(std::ffi::OsString::from_vec(double));
        }
    }
    if result.as_os_str().is_empty() {
        result.push(".");
    }
    result
}

fn io_text(error: &std::io::Error) -> String {
    #[cfg(windows)]
    if let Some((number, words)) = match error.kind() {
        std::io::ErrorKind::NotFound => Some((2, "No such file or directory")),
        std::io::ErrorKind::PermissionDenied => Some((13, "Permission denied")),
        std::io::ErrorKind::NotADirectory => Some((2, "No such file or directory")),
        _ => None,
    } {
        return format!("[Errno {number}] {words}");
    }
    if let Some(code) = error.raw_os_error() {
        let words = bettermemory_sys::strerror(code).unwrap_or_else(|| error.to_string());
        format!("[Errno {code}] {words}")
    } else {
        error.to_string()
    }
}

pub fn load_anchors(path: &Path) -> Result<Vec<Anchor>, AnchorError> {
    let path = python_path(path);
    let label = fsdecode(path.as_os_str());
    let raw = bounded_read(&path, MAX_ANCHOR_FILE_BYTES).map_err(|err| {
        let cause = match err {
            BoundedReadError::Open(e) => {
                [cps(&io_text(&e)), cps(": "), pyvalue::repr_str(&label)].concat()
            }
            BoundedReadError::Io(e) => cps(&io_text(&e)),
            BoundedReadError::Directory(fd) => cps(&format!(
                "[Errno 21] {}: {fd}",
                bettermemory_sys::strerror(21).unwrap_or_else(|| "Is a directory".to_owned())
            )),
            BoundedReadError::NonRegular => {
                [label.clone(), cps(": not a regular file; not read")].concat()
            }
            BoundedReadError::TooLarge { size, limit } => [
                label.clone(),
                cps(&format!(": file size {size} exceeds cap {limit} bytes")),
            ]
            .concat(),
            BoundedReadError::NullPath => cps("open: embedded null character in path"),
        };
        file_error(
            &label,
            &[cps("cannot read an anchor file: "), cause].concat(),
        )
    })?;
    decode_anchors(&raw, &label)
}

fn bind_seq(n: &str) -> Result<i64, AnchorError> {
    n.parse().map_err(|_| {
        raised(
            "OverflowError",
            cps("Python int too large to convert to SQLite INTEGER"),
        )
    })
}

/// Compute the digest at each requested integer, even in a gap between log rows.
pub fn chain_digests(
    conn: &Connection,
    seqs: &[String],
) -> Result<Vec<(String, PyStr)>, AnchorError> {
    let mut wanted: Vec<_> = seqs.iter().map(String::as_str).collect();
    wanted.sort_unstable_by(|a, b| pyvalue::cmp_int_text(a, b));
    wanted.dedup();
    let mut out = Vec::with_capacity(wanted.len());
    let Some(last) = wanted.last() else {
        return Ok(out);
    };
    let mut query = conn.prepare("SELECT seq, mac FROM log WHERE seq <= ? ORDER BY seq")?;
    let mut rows = query.query([bind_seq(last)?])?;
    let mut checkpoints = wanted.iter().peekable();
    let mut digest = Sha256::new();
    let save = |out: &mut Vec<(String, PyStr)>, seq: &str, d: &Sha256| {
        out.push((
            seq.to_owned(),
            cps(&crate::lexicon::hex(&d.clone().finalize())),
        ));
    };
    while let Some(row) = rows.next()? {
        let seq = sql_int(sql_value(row.get_ref(0)?)?)?;
        let PyValue::Int(ref n) = seq else {
            unreachable!("int() returns an integer")
        };
        while let Some(v) = checkpoints.next_if(|v| pyvalue::cmp_int_text(v, n).is_lt()) {
            save(&mut out, v, &digest);
        }
        digest.update(sql_value(row.get_ref(1)?)?.py_str().as_bytes());
        digest.update(b"\n");
    }
    for v in checkpoints {
        save(&mut out, v, &digest);
    }
    Ok(out)
}

fn as_int(v: &PyValue) -> Result<PyValue, AnchorError> {
    pyvalue::py_int(v).map_err(|e| {
        use pyvalue::PyError::*;
        let msg = match (e, v) {
            (ValueError, PyValue::Str(t)) => [
                cps("invalid literal for int() with base 10: "),
                pyvalue::repr_str(t),
            ]
            .concat(),
            (ValueError, _) => cps("cannot convert float NaN to integer"),
            (OverflowError, _) => cps("cannot convert float infinity to integer"),
            _ => cps(&format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                crate::membership_lines::type_name(v)
            )),
        };
        raised(e.name(), msg)
    })
}

fn sql_int(v: crate::store::SqlValue) -> Result<PyValue, AnchorError> {
    use crate::store::SqlValue::*;
    match v {
        Integer(n) => Ok(PyValue::int(n.into())),
        Real(n) => as_int(&PyValue::Float(n)),
        Text(s) => as_int(&PyValue::str(&s)),
        Null => as_int(&PyValue::None),
        Blob(ref bytes) => {
            if bytes.is_ascii() {
                if let Ok(val) =
                    pyvalue::int_from_str(&cps(std::str::from_utf8(bytes).expect("ASCII")))
                {
                    return Ok(val);
                }
            }
            Err(raised(
                "ValueError",
                cps(&format!(
                    "invalid literal for int() with base 10: {}",
                    v.py_str()
                )),
            ))
        }
    }
}

/// Check all supplied sources in one snapshot, retaining a caller's open transaction.
pub fn check_anchors(
    conn: &Connection,
    store_id: &[u32],
    anchors: &[(PyStr, Anchor)],
) -> Result<Vec<PyValue>, AnchorError> {
    crate::history::read_snapshot(conn, || {
        let mine = anchors
            .iter()
            .filter(|(_, a)| a.store_id == store_id)
            .map(|(_, a)| a.seq.clone())
            .collect::<Vec<_>>();
        let digests: HashMap<_, _> = chain_digests(conn, &mine)?.into_iter().collect();
        let mut out = Vec::with_capacity(anchors.len());
        for (source, a) in anchors {
            let mut fields = vec![
                ("file", PyValue::Str(source.clone())),
                ("store_id", PyValue::Str(a.store_id.clone())),
                ("seq", PyValue::Int(a.seq.clone())),
                ("created_at", PyValue::Str(a.created_at.clone())),
            ];
            if a.store_id != store_id {
                fields.push(("status", PyValue::str("other_store")));
            } else {
                let mut query = conn.prepare("SELECT mac FROM log WHERE seq = ?")?;
                let mut rows = query.query([bind_seq(&a.seq)?])?;
                let reason = match rows.next()? {
                    None => Some("missing"),
                    Some(row)
                        if !crate::log::same_mac(
                            &a.mac,
                            &cps(&sql_value(row.get_ref(0)?)?.py_str()),
                        ) =>
                    {
                        Some("mac")
                    }
                    Some(_) if !crate::log::same_mac(&digests[&a.seq], &a.chain_sha256) => {
                        Some("digest")
                    }
                    Some(_) => None,
                };
                fields.push((
                    "status",
                    PyValue::str(if reason.is_some() { "mismatch" } else { "ok" }),
                ));
                if let Some(r) = reason {
                    fields.push(("reason", PyValue::str(r)));
                }
            }
            out.push(dict(fields));
        }
        Ok(out)
    })?
}

fn required<'a>(value: &'a PyValue, key: &str) -> Result<&'a PyValue, AnchorError> {
    value
        .get(key)
        .ok_or_else(|| raised("KeyError", pyvalue::repr_str(&cps(key))))
}

fn set(value: &mut PyValue, key: &str, item: PyValue) -> Result<(), AnchorError> {
    let PyValue::Dict(fields) = value else {
        return Err(raised("TypeError", cps("report must be a dict")));
    };
    let key = PyValue::str(key);
    if let Some((_, old)) = fields.iter_mut().find(|(k, _)| *k == key) {
        *old = item;
    } else {
        fields.push((key, item));
    }
    Ok(())
}

/// Attach the ordered results first, then replace problems only after all sort keys succeed.
pub fn merge_into_report(report: &mut PyValue, results: Vec<PyValue>) -> Result<(), AnchorError> {
    set(report, "anchors", PyValue::List(results))?;
    let problems = required(report, "problems")?;
    let mut combined = crate::membership_lines::items(problems)
        .map_err(|err| raised(err.class, cps(&err.message)))?;
    let Some(PyValue::List(entries)) = report.get("anchors") else {
        unreachable!()
    };
    let mut mismatch = false;
    for e in entries {
        if required(e, "status")? == &PyValue::str("mismatch") {
            combined.push(dict([
                ("seq", required(e, "seq")?.clone()),
                ("kind", PyValue::str("anchor")),
                ("problem", PyValue::str("anchor_mismatch")),
                ("reason", required(e, "reason")?.clone()),
                ("anchor", required(e, "file")?.clone()),
            ]));
            mismatch = true;
        }
    }
    let mut keyed = Vec::with_capacity(combined.len());
    for p in combined {
        let seq = required(&p, "seq")?;
        let n = as_int(seq)?;
        let name = pyvalue::py_str(required(&p, "problem")?)
            .map_err(|e| raised(e.name(), cps(e.name())))?;
        keyed.push((n, name, p));
    }
    keyed.sort_by(|a, b| {
        pyvalue::cmp_keys(&a.0, &b.0)
            .unwrap()
            .then_with(|| a.1.cmp(&b.1))
    });
    set(
        report,
        "problems",
        PyValue::List(keyed.into_iter().map(|(_, _, p)| p).collect()),
    )?;
    if mismatch {
        set(report, "status", PyValue::str("tampered"))?;
    }
    Ok(())
}
