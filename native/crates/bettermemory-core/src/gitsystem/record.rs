//! The answers recorded in the state directory, and whether one stands:
//! `gitsystem.py`'s `_key`, `_developer`, `_current`, `_kept`,
//! `_record_path`, `_entries`, `_text`, `_recorded`, `_recorded_version`
//! and `_record`.
//!
//! Nothing here is ambient. The state directory, the working directory a
//! relative one is resolved against, the clock's `now`, PATH,
//! DEVELOPER_DIR and whether this is macOS are arguments; the record and
//! the git binary are read through [`Fs`]; the record is written with
//! `std::fs`, through the store's `replace_retrying` (`replace_atomic`,
//! with the reference's Windows retry), as `atomic_write_bytes` writes it,
//! after `owner_only_dir`, as `ensure_owner_only_dir(parents=True)`.
//!
//! What the reference raises and does not catch, the port returns as
//! [`Refused::Raises`]: an OverflowError from `int(inf)` in a stamp or
//! `float()` of an integer past a double's range, and a RecursionError
//! from `json.loads` of a record nested 9,999 deep. A stamp the file
//! system cannot give as the reference's is [`Refused::NotPorted`].

use std::path::PathBuf;

use crate::fsdata::{Flavour, Fs};
use crate::gitconfig::legacy::{self, Stamp, UNSTATED};
use crate::gitconfig::paths;
use crate::json::{self, DumpOptions, LoadError};
use crate::pyvalue::{self, PyError, PyStr, PyValue};
use crate::store::caller::json_bytes;
use crate::store::create::owner_only_dir;
use crate::store::write::replace_retrying;
use crate::{config, unicode};

/// `RECORD_FORMAT`.
pub const RECORD_FORMAT: i128 = 5;
/// `RECORD`: the record's file in the state directory.
pub const RECORD: &str = "git-config-sources-v5.json";
/// `REASK_SECONDS`: how long a whole answer stands.
pub const REASK_SECONDS: f64 = 3600.0;
/// `RETRY_SECONDS`: how long the fallback, a partial answer or a kept one
/// stands.
pub const RETRY_SECONDS: f64 = 60.0;
/// `_KEPT`: the entries read and written.
pub const KEPT: usize = 8;
/// `_OWN_MAX`: a build's own files kept of an answer.
pub const OWN_MAX: usize = 8;
/// `_MAX_BYTES`: a record past this is not read.
pub const MAX_BYTES: usize = 1 << 16;
/// `_MAX_PATH`: the longest path `_text` takes, in code points.
pub const MAX_PATH: usize = 4096;
/// The bound of each part of a recorded version (`_recorded_version`).
pub const VERSION_BOUND: u32 = 1 << 16;
/// `XCODE_SELECT_LINK`.
pub const XCODE_SELECT_LINK: &[u8] = b"/var/db/xcode_select_link";

/// A non-negative Python int by its canonical decimal text: ASCII digits,
/// no leading zero but in "0". Only `from_digits` builds one, so a record
/// written holds only what `repr(int)` writes.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Natural(String);

impl Natural {
    /// The int of `digits` (ASCII decimal digits, leading zeros dropped);
    /// None for an empty or non-digit text.
    pub fn from_digits(digits: &str) -> Option<Natural> {
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let kept = digits.trim_start_matches('0');
        Some(Natural(if kept.is_empty() { "0" } else { kept }.to_owned()))
    }

    /// Its canonical decimal text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl From<u32> for Natural {
    fn from(v: u32) -> Natural {
        Natural(v.to_string())
    }
}

/// `gitsystem.Sources`. Text is Python's (code points, a lone surrogate
/// allowed), as a record read back may hold it.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Sources {
    pub own: Vec<PyStr>,
    pub system: Option<PyStr>,
    pub legacy: Option<bool>,
    pub resets: Option<bool>,
    pub prefix: Option<PyStr>,
    pub compat_differ: Option<bool>,
    pub hasconfig: Option<bool>,
    /// Three Python ints: `git version` may name a part of any size
    /// (`gitversion.parse`).
    pub version: Option<[Natural; 3]>,
}

/// A recorded stamp: five Python ints, each by its canonical decimal text
/// (`int()` of what the record holds has no bound).
pub type RecordedStamp = [String; 5];

/// `gitsystem._Answer`.
#[derive(Clone, Debug, PartialEq)]
pub struct Answer {
    pub key: String,
    pub git: Option<PyStr>,
    pub stamp: Option<RecordedStamp>,
    pub asked: f64,
    pub ok: bool,
    pub sources: Sources,
    pub tried: Option<f64>,
}

/// Where the reference does not answer as the port can.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refused {
    /// The reference raises this out of `sources()`.
    Raises(PyError),
    /// A stamp this file system does not give as the reference's.
    NotPorted,
}

impl From<legacy::NotPorted> for Refused {
    fn from(_: legacy::NotPorted) -> Refused {
        Refused::NotPorted
    }
}

/// What [`Place::record`] did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Recorded {
    /// The answer was the fallback and an answer git gave is recorded:
    /// nothing written.
    Kept,
    /// These bytes were written.
    Written(Vec<u8>),
    /// The state directory refused the write: nothing kept.
    Refused(Vec<u8>),
}

/// `os.fsdecode` on POSIX: UTF-8, each byte that is not part of a valid
/// sequence as the lone surrogate U+DC80 + byte (``surrogateescape``).
pub fn surrogateescape(bytes: &[u8]) -> PyStr {
    let mut out = PyStr::with_capacity(bytes.len());
    let mut rest = bytes;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.extend(s.chars().map(u32::from));
                return out;
            }
            Err(e) => {
                let (good, bad) = rest.split_at(e.valid_up_to());
                out.extend(
                    std::str::from_utf8(good)
                        .unwrap_or_default()
                        .chars()
                        .map(u32::from),
                );
                let n = e.error_len().unwrap_or(bad.len());
                out.extend(bad[..n].iter().map(|&b| 0xDC00 + u32::from(b)));
                rest = &bad[n..];
            }
        }
    }
}

/// `text.encode("utf-8", "surrogatepass")`.
pub fn surrogatepass(text: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len());
    for &cp in text {
        match char::from_u32(cp) {
            Some(c) => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
            // A surrogate, in UTF-8's three-byte form.
            None => out.extend_from_slice(&[
                0xE0 | (cp >> 12) as u8,
                0x80 | ((cp >> 6) & 0x3F) as u8,
                0x80 | (cp & 0x3F) as u8,
            ]),
        }
    }
    out
}

/// `_key(path)`: the first 32 hex digits of the SHA-256 of PATH (`"\0"`
/// where it is unset), a NUL and `developer` ([`developer`]).
pub fn key(path: Option<&[u32]>, developer: &[u32]) -> String {
    use sha2::{Digest, Sha256};
    let mut raw: PyStr = path.map_or_else(|| vec![0], <[u32]>::to_vec);
    raw.push(0);
    raw.extend_from_slice(developer);
    let digest = Sha256::digest(surrogatepass(&raw));
    digest[..16].iter().map(|b| format!("{b:02x}")).collect()
}

/// `_developer()`: on macOS, DEVELOPER_DIR (`named`) and the target of
/// `link` ([`XCODE_SELECT_LINK`]), each `"\1"` where there is none,
/// joined by a NUL; elsewhere nothing.
pub fn developer<F: Fs>(fs: &F, macos: bool, named: Option<&[u32]>, link: &[u8]) -> PyStr {
    if !macos {
        return PyStr::new();
    }
    let selected = fs.readlink_result(link).ok().map(|t| surrogateescape(&t));
    let mut out = named.map_or_else(|| vec![1], <[u32]>::to_vec);
    out.push(0);
    out.extend(selected.unwrap_or_else(|| vec![1]));
    out
}

/// The record's place: the state directory, the working directory a
/// relative one is read from, and the file system it is read through.
pub struct Place<'a, F: Fs> {
    pub fs: &'a F,
    pub flavour: Flavour,
    pub state_dir: &'a [u8],
    pub cwd: &'a [u8],
}

/// `entry.get(name)`, None for JSON's null too.
fn field<'v>(entry: &'v PyValue, name: &str) -> Option<&'v PyValue> {
    entry.get(name).filter(|v| !matches!(v, PyValue::None))
}

/// `_text(raw)`.
fn text(raw: Option<&PyValue>) -> Option<PyStr> {
    match raw {
        Some(PyValue::Str(s)) if !s.is_empty() && s.len() <= MAX_PATH && !s.contains(&0) => {
            Some(s.clone())
        }
        _ => None,
    }
}

/// `float(value)` for a value JSON parsing yields.
fn py_float(value: &PyValue) -> Result<f64, PyError> {
    match value {
        PyValue::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        PyValue::Int(t) => match t.parse::<f64>() {
            Ok(f) if f.is_finite() => Ok(f),
            _ => Err(PyError::OverflowError),
        },
        PyValue::Float(f) => Ok(*f),
        PyValue::Str(s) => float_from_text(s).ok_or(PyError::ValueError),
        _ => Err(PyError::TypeError),
    }
}

/// `float(text)`: each non-ASCII space read as a space, then Python's
/// ASCII whitespace stripped (`Py_ISSPACE`, which takes neither
/// U+001C..U+001F nor anything past ASCII), then the number.
fn float_from_text(s: &[u32]) -> Option<f64> {
    let mapped: PyStr = s
        .iter()
        .map(|&c| {
            if c > 0x7F && unicode::is_space(c) {
                0x20
            } else {
                c
            }
        })
        .collect();
    let space = |c: &u32| matches!(c, 0x09..=0x0D | 0x20);
    let start = mapped.iter().position(|c| !space(c))?;
    let end = mapped.iter().rposition(|c| !space(c))? + 1;
    let body = unicode::to_string(&mapped[start..end])?;
    config::py_float_from_str(&body)
}

/// `_recorded_version(raw)`: None for none, `Err` (ValueError) for
/// anything but three ints (no bool) in `0..2**16`.
fn recorded_version(raw: Option<&PyValue>) -> Result<Option<[Natural; 3]>, ()> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    let PyValue::List(parts) = raw else {
        return Err(());
    };
    if parts.len() != 3 {
        return Err(());
    }
    let mut out = [0u32; 3];
    for (slot, part) in out.iter_mut().zip(parts) {
        let PyValue::Int(t) = part else {
            return Err(());
        };
        match t.parse::<u32>() {
            Ok(v) if t.len() <= 5 && v < VERSION_BOUND => *slot = v,
            _ => return Err(()),
        }
    }
    Ok(Some(out.map(Natural::from)))
}

/// One entry of the PATH `key` names, read as `_recorded` reads it.
fn answer_of(entry: &PyValue, key: &str) -> Result<Option<Answer>, PyError> {
    let git = field(entry, "git");
    let stamp = field(entry, "stamp");
    let own = field(entry, "own");
    let system = field(entry, "system");
    let Some(PyValue::List(own)) = own else {
        return Ok(None);
    };
    if own.len() > OWN_MAX {
        return Ok(None);
    }
    let files: Vec<Option<PyStr>> = own.iter().map(|p| text(Some(p))).collect();
    let git_text = text(git);
    let system_text = text(system);
    if (git.is_some() && git_text.is_none())
        || (system.is_some() && system_text.is_none())
        || files.iter().any(Option::is_none)
    {
        return Ok(None);
    }
    let mut stamped: Option<RecordedStamp> = None;
    if let Some(stamp) = stamp {
        let PyValue::List(parts) = stamp else {
            return Ok(None);
        };
        if parts.len() != 5 {
            return Ok(None);
        }
        let mut out: Vec<String> = Vec::with_capacity(5);
        for part in parts {
            match &pyvalue::py_int(part) {
                Ok(PyValue::Int(t)) => out.push(t.clone()),
                Err(PyError::OverflowError) => return Err(PyError::OverflowError),
                _ => return Ok(None),
            }
        }
        stamped = <[String; 5]>::try_from(out).ok();
    }
    let tried = field(entry, "tried");
    let mut flags = [None; 4];
    for (slot, name) in flags
        .iter_mut()
        .zip(["legacy", "resets", "compat_differ", "hasconfig"])
    {
        match field(entry, name) {
            None => {}
            Some(PyValue::Bool(b)) => *slot = Some(*b),
            Some(_) => return Ok(None),
        }
    }
    let [legacy, resets, compat_differ, hasconfig] = flags;
    let prefix = match field(entry, "prefix") {
        None => None,
        Some(PyValue::Str(s)) if !s.contains(&0) => Some(s.clone()),
        Some(_) => return Ok(None),
    };
    let Ok(version) = recorded_version(field(entry, "version")) else {
        return Ok(None);
    };
    let Some(asked) = entry.get("asked") else {
        return Ok(None);
    };
    let asked = match py_float(asked) {
        Ok(f) => f,
        Err(PyError::OverflowError) => return Err(PyError::OverflowError),
        Err(_) => return Ok(None),
    };
    let ok = matches!(entry.get("ok"), Some(PyValue::Bool(true)))
        && git_text.is_some()
        && stamped.is_some();
    let tried = match tried.map(py_float) {
        None => None,
        Some(Ok(f)) => Some(f),
        Some(Err(PyError::OverflowError)) => return Err(PyError::OverflowError),
        Some(Err(_)) => return Ok(None),
    };
    Ok(Some(Answer {
        key: key.to_owned(),
        git: git_text,
        stamp: stamped,
        asked,
        ok,
        sources: Sources {
            own: files.into_iter().flatten().collect(),
            system: system_text,
            legacy,
            resets,
            prefix,
            compat_differ,
            hasconfig,
            version,
        },
        tried,
    }))
}

/// Whether `stamp` (a file's now) is the recorded one.
fn same_stamp(now: Option<Stamp>, recorded: Option<&RecordedStamp>) -> bool {
    match (now, recorded) {
        (None, None) => true,
        (Some(a), Some(b)) => a.iter().zip(b).all(|(x, y)| x.to_string() == *y),
        _ => false,
    }
}

/// The record as `_record` writes it for `answer` over `others`.
fn record_bytes(answer: &Answer, others: Vec<PyValue>) -> Result<Vec<u8>, PyError> {
    let opt_str = |v: &Option<PyStr>| v.clone().map_or(PyValue::None, PyValue::Str);
    let opt_bool = |v: Option<bool>| v.map_or(PyValue::None, PyValue::Bool);
    let s = &answer.sources;
    let entry = PyValue::Dict(vec![
        (PyValue::str("path"), PyValue::str(&answer.key)),
        (PyValue::str("git"), opt_str(&answer.git)),
        (
            PyValue::str("stamp"),
            answer.stamp.as_ref().map_or(PyValue::None, |st| {
                PyValue::List(st.iter().map(|t| PyValue::Int(t.clone())).collect())
            }),
        ),
        (PyValue::str("asked"), PyValue::Float(answer.asked)),
        (PyValue::str("ok"), PyValue::Bool(answer.ok)),
        (
            PyValue::str("own"),
            PyValue::List(s.own.iter().cloned().map(PyValue::Str).collect()),
        ),
        (PyValue::str("system"), opt_str(&s.system)),
        (PyValue::str("legacy"), opt_bool(s.legacy)),
        (PyValue::str("resets"), opt_bool(s.resets)),
        (PyValue::str("prefix"), opt_str(&s.prefix)),
        (PyValue::str("compat_differ"), opt_bool(s.compat_differ)),
        (PyValue::str("hasconfig"), opt_bool(s.hasconfig)),
        (
            PyValue::str("version"),
            s.version.as_ref().map_or(PyValue::None, |v| {
                PyValue::List(
                    v.iter()
                        .map(|p| PyValue::Int(p.as_str().to_owned()))
                        .collect(),
                )
            }),
        ),
        (
            PyValue::str("tried"),
            answer.tried.map_or(PyValue::None, PyValue::Float),
        ),
    ]);
    let mut entries = vec![entry];
    entries.extend(others);
    entries.truncate(KEPT);
    let body = PyValue::Dict(vec![
        (PyValue::str("format"), PyValue::int(RECORD_FORMAT)),
        (PyValue::str("entries"), PyValue::List(entries)),
    ]);
    let mut text = json::dumps(&body, DumpOptions::DEFAULT)?;
    text.push(u32::from(b'\n'));
    // ASCII throughout: `ensure_ascii` escapes the rest.
    Ok(text.iter().map(|&c| c as u8).collect())
}

/// Bytes as the OS path they name.
pub(super) fn os_path(bytes: &[u8]) -> Option<PathBuf> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        Some(PathBuf::from(std::ffi::OsStr::from_bytes(bytes)))
    }
    #[cfg(not(unix))]
    {
        std::str::from_utf8(bytes).ok().map(PathBuf::from)
    }
}

impl<F: Fs> Place<'_, F> {
    /// `_record_path()`: `RECORD` in the state directory.
    pub fn record_path(&self) -> Vec<u8> {
        paths::join(self.flavour, self.state_dir, RECORD.as_bytes())
    }

    /// The record's bytes, at most `MAX_BYTES + 1` of them, as
    /// `f.read(_MAX_BYTES + 1)` reads them; None where it cannot be read.
    fn read(&self) -> Option<Vec<u8>> {
        let path = paths::opened(self.flavour, &self.record_path(), self.cwd);
        let mut file = self.fs.open(&path).ok()?;
        let mut data = Vec::new();
        while data.len() <= MAX_BYTES {
            let got = self.fs.read(&mut file, MAX_BYTES + 1 - data.len()).ok()?;
            if got.is_empty() {
                break;
            }
            data.extend_from_slice(&got);
        }
        Some(data)
    }

    /// `_entries()`: the record's entries, each a dict; none where there
    /// is no record, it is past `MAX_BYTES` or is not one.
    pub fn entries(&self) -> Result<Vec<PyValue>, PyError> {
        let Some(data) = self.read() else {
            return Ok(Vec::new());
        };
        if data.len() > MAX_BYTES {
            return Ok(Vec::new());
        }
        let Some(text) = json_bytes(&data) else {
            return Ok(Vec::new());
        };
        let raw = match json::loads_detailed(&text) {
            Ok(raw) => raw,
            Err(LoadError::Recursion) => return Err(PyError::RecursionError),
            Err(_) => return Ok(Vec::new()),
        };
        if !matches!(raw, PyValue::Dict(_)) {
            return Ok(Vec::new());
        }
        // `raw.get("format") != RECORD_FORMAT`: 5.0 is 5 to Python.
        let format_ok = match raw.get("format") {
            Some(PyValue::Int(t)) => *t == RECORD_FORMAT.to_string(),
            Some(PyValue::Float(f)) => *f == RECORD_FORMAT as f64,
            _ => false,
        };
        if !format_ok {
            return Ok(Vec::new());
        }
        let Some(PyValue::List(entries)) = raw.get("entries") else {
            return Ok(Vec::new());
        };
        Ok(entries
            .iter()
            .take(KEPT)
            .filter(|e| matches!(e, PyValue::Dict(_)))
            .cloned()
            .collect())
    }

    /// `_recorded(key)`: the answer recorded for the PATH `key` names;
    /// None where none is, or the first entry for it is not one.
    pub fn recorded(&self, key: &str) -> Result<Option<Answer>, Refused> {
        let want = unicode::cps(key);
        for entry in self.entries().map_err(Refused::Raises)? {
            if !matches!(entry.get("path"), Some(PyValue::Str(p)) if *p == want) {
                continue;
            }
            return answer_of(&entry, key).map_err(Refused::Raises);
        }
        Ok(None)
    }

    /// `file_stamp(git)` for a recorded git path.
    fn git_stamp(&self, git: &[u32]) -> Result<Option<Stamp>, Refused> {
        let mut bytes = Vec::with_capacity(git.len());
        for &cp in git {
            match cp {
                0xDC80..=0xDCFF if self.flavour == Flavour::Posix => {
                    bytes.push((cp - 0xDC00) as u8)
                }
                // `os.fsencode` refuses it: a ValueError, `_UNSTATED`.
                0xD800..=0xDFFF if self.flavour == Flavour::Posix => return Ok(Some(UNSTATED)),
                0xD800..=0xDFFF => return Err(Refused::NotPorted),
                _ => bytes.extend(surrogatepass(&[cp])),
            }
        }
        let path = paths::opened(self.flavour, &bytes, self.cwd);
        Ok(legacy::file_stamp(self.fs, &path)?)
    }

    /// `_current(answer, now, stamp=stamp)`.
    pub fn current(&self, answer: &Answer, now: f64, stamp: bool) -> Result<bool, Refused> {
        if let Some(tried) = answer.tried {
            let since = now - tried;
            return Ok((0.0..RETRY_SECONDS).contains(&since));
        }
        let age = now - answer.asked;
        let s = &answer.sources;
        let whole = answer.ok
            && s.legacy.is_some()
            && s.prefix.is_some()
            && s.compat_differ.is_some()
            && s.hasconfig.is_some()
            && s.version.is_some();
        let life = if whole { REASK_SECONDS } else { RETRY_SECONDS };
        if !(0.0..life).contains(&age) {
            return Ok(false);
        }
        let Some(git) = answer.git.as_deref().filter(|_| stamp) else {
            return Ok(true);
        };
        Ok(same_stamp(self.git_stamp(git)?, answer.stamp.as_ref()))
    }

    /// `_kept(key, now, *seen)`: where an ask failed, the record's answer
    /// if one that stands was recorded meanwhile, else the newest answer
    /// git gave of the record's and `seen`, kept from `now`.
    pub fn kept(
        &self,
        key: &str,
        now: f64,
        seen: &[Option<&Answer>],
    ) -> Result<Option<Answer>, Refused> {
        let again = self.recorded(key)?;
        if let Some(a) = &again {
            if a.ok && self.current(a, now, true)? {
                return Ok(again);
            }
        }
        let mut best: Option<&Answer> = None;
        for a in again.iter().chain(seen.iter().flatten().copied()) {
            if !a.ok {
                continue;
            }
            // `max` keeps the first of equals, and NaN replaces nothing.
            if best.is_none_or(|b| a.asked > b.asked) {
                best = Some(a);
            }
        }
        Ok(best.map(|b| Answer {
            tried: Some(now),
            ..b.clone()
        }))
    }

    /// `_record(answer)`: `answer` first, then the other PATHs' entries,
    /// written whole over the record; nothing where `answer` is the
    /// fallback and an answer git gave is recorded for its PATH.
    pub fn record(&self, answer: &Answer) -> Result<Recorded, Refused> {
        if !answer.ok {
            if let Some(prior) = self.recorded(&answer.key)? {
                if prior.ok {
                    return Ok(Recorded::Kept);
                }
            }
        }
        let want = unicode::cps(&answer.key);
        let others: Vec<PyValue> = self
            .entries()
            .map_err(Refused::Raises)?
            .into_iter()
            .filter(|e| !matches!(e.get("path"), Some(PyValue::Str(p)) if *p == want))
            .collect();
        let data = record_bytes(answer, others).map_err(Refused::Raises)?;
        let target = paths::opened(self.flavour, &self.record_path(), self.cwd);
        let Some(path) = os_path(&target) else {
            return Ok(Recorded::Refused(data));
        };
        let parent = match path.parent() {
            Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
            _ => PathBuf::from("."),
        };
        if owner_only_dir(&parent).is_err() || replace_retrying(&path, &data).is_err() {
            return Ok(Recorded::Refused(data));
        }
        // `mode_before_rename=0o600`, whatever the umask, and its
        // post-rename chmod.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        }
        Ok(Recorded::Written(data))
    }
}

#[cfg(test)]
mod tests {
    use super::Natural;

    #[test]
    fn a_natural_is_only_canonical_decimal_text() {
        let text = |d: &str| Natural::from_digits(d).map(|n| n.as_str().to_owned());
        assert_eq!(text("007").as_deref(), Some("7"));
        assert_eq!(text("000").as_deref(), Some("0"));
        assert_eq!(text("0").as_deref(), Some("0"));
        assert_eq!(text("120").as_deref(), Some("120"));
        for bad in ["", "-1", "+1", "1_0", " 1", "\u{0662}", "1.0"] {
            assert_eq!(text(bad), None, "{bad:?}");
        }
        assert_eq!(Natural::from(0).as_str(), "0");
    }
}
