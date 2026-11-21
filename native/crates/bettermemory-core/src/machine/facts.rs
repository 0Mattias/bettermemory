//! `machine.py`, unit 2: the facts a machine id is kept beside and the two
//! files of the state directory read. The platform's hardware id
//! (`_darwin_hardware`, `_linux_hardware`, `_windows_hardware`,
//! `read_hardware_id` with its memo), `current_hostname`, the id file's
//! object (`_file_object`), `_read_id` and `_read_record`.
//!
//! Every effect is an argument: the platform's answers through
//! [`HardwareSource`], files through [`Fs`], the stat of the id file
//! through [`IdFileStat`], the host name through [`Host`]. The memo is a
//! value the caller holds ([`HardwareMemo`]).
//!
//! Both regular expressions of this unit are matched by hand under rule 3
//! of the regular expression decision (no choice): `_ID_RE` under
//! `fullmatch` ([`is_machine_id`]) and the `ioreg` line under `search`
//! ([`ioreg_uuid`]).

use super::{py_text, ID_HEADER, ID_LENGTH};
use crate::fsdata::{Fs, PyErr};
use crate::location_probe::{Declined, Host};
use crate::pyvalue::PyStr;
use crate::pyvalue::{PyError, PyValue};

/// `_LINUX_MACHINE_ID`: the files `_linux_hardware` reads, in order.
pub const LINUX_MACHINE_ID: [&[u8]; 2] = [b"/etc/machine-id", b"/var/lib/dbus/machine-id"];

/// Whether `os.stat_result` carries `st_birthtime_ns` on this host's
/// CPython 3.13.13 (Windows only; macOS gives the float `st_birthtime`).
/// The platform-gated golden file records what the interpreter showed.
pub const BIRTH_NS: bool = cfg!(windows);

/// The chunk each read of a file asks for.
const CHUNK: usize = 1 << 16;

/// `_ID_RE.fullmatch(value)`: 26 characters of `[0-9A-HJKMNP-TV-Z]`.
pub fn is_machine_id(value: &[u32]) -> bool {
    value.len() == ID_LENGTH
        && value.iter().all(|&c| {
            matches!(c, 0x30..=0x39 | 0x41..=0x48 | 0x4A | 0x4B | 0x4D | 0x4E | 0x50..=0x54 | 0x56..=0x5A)
        })
}

/// `str.strip()`.
fn strip(t: &[u32]) -> &[u32] {
    crate::capture::remote::py_strip(t)
}

/// `str.splitlines()` (no ends kept).
pub fn splitlines(t: &[u32]) -> Vec<&[u32]> {
    let mut out = Vec::new();
    let (mut start, mut i) = (0, 0);
    while i < t.len() {
        let c = t[i];
        if matches!(c, 0x0A..=0x0D | 0x1C..=0x1E | 0x85 | 0x2028 | 0x2029) {
            out.push(&t[start..i]);
            i += if c == 0x0D && t.get(i + 1) == Some(&0x0A) {
                2
            } else {
                1
            };
            start = i;
        } else {
            i += 1;
        }
    }
    if start < t.len() {
        out.push(&t[start..]);
    }
    out
}

/// The whole of a file's bytes: `open(path)` then reads to the end. Err
/// is the class `open` or a read raised.
fn read_bytes<F: Fs + ?Sized>(fs: &F, path: &[u8]) -> Result<Vec<u8>, PyErr> {
    let mut file = fs.open_waiting(path)?;
    let mut out = Vec::new();
    loop {
        let chunk = fs.read(&mut file, CHUNK)?;
        if chunk.is_empty() {
            return Ok(out);
        }
        out.extend_from_slice(&chunk);
    }
}

/// Universal newlines: `\r\n` and a lone `\r` become `\n`.
fn translate(text: impl Iterator<Item = u32>) -> PyStr {
    let mut out = PyStr::new();
    let mut after_cr = false;
    for c in text {
        if c == 0x0A && after_cr {
            after_cr = false;
            continue;
        }
        after_cr = c == 0x0D;
        out.push(if after_cr { 0x0A } else { c });
    }
    out
}

/// Why `Path.read_text` gave no text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadText {
    /// OSError (any subclass).
    Os,
    /// UnicodeDecodeError.
    Decode,
    /// ValueError: a path holding a NUL byte.
    Value,
}

/// `Path(path).read_text(encoding=...)`, UTF-8 (strict) or ASCII.
pub fn read_text<F: Fs + ?Sized>(fs: &F, path: &[u8], ascii: bool) -> Result<PyStr, ReadText> {
    let bytes = read_bytes(fs, path).map_err(|e| match e {
        PyErr::Value => ReadText::Value,
        _ => ReadText::Os,
    })?;
    if ascii {
        if !bytes.is_ascii() {
            return Err(ReadText::Decode);
        }
        return Ok(translate(bytes.iter().map(|&b| u32::from(b))));
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| ReadText::Decode)?;
    Ok(translate(text.chars().map(u32::from)))
}

/// `_read_id(path)`: the id on the line after the header, or None. A path
/// holding a NUL raises ValueError, which the reference does not catch.
pub fn read_id<F: Fs + ?Sized>(fs: &F, path: &[u8]) -> Result<Option<String>, PyError> {
    let text = match read_text(fs, path, false) {
        Ok(text) => text,
        Err(ReadText::Value) => return Err(PyError::ValueError),
        Err(_) => return Ok(None),
    };
    let lines = splitlines(&text);
    let header: Vec<u32> = ID_HEADER.chars().map(u32::from).collect();
    if lines.len() < 2 || strip(lines[0]) != header.as_slice() {
        return Ok(None);
    }
    let value = strip(lines[1]);
    if !is_machine_id(value) {
        return Ok(None);
    }
    Ok(Some(
        value.iter().filter_map(|&c| char::from_u32(c)).collect(),
    ))
}

/// `_read_record(path)`: the file's JSON object, or None. RecursionError
/// from `json.loads` is not caught by the reference and is returned.
pub fn read_record<F: Fs + ?Sized>(fs: &F, path: &[u8]) -> Result<Option<PyValue>, PyError> {
    let Ok(text) = read_text(fs, path, false) else {
        return Ok(None);
    };
    match crate::json::loads(&text) {
        Ok(value @ PyValue::Dict(_)) => Ok(Some(value)),
        Ok(_) | Err(PyError::ValueError) => Ok(None),
        Err(e) => Err(e),
    }
}

/// `str(uuid.UUID(bytes=raw)).upper()`.
pub fn uuid_text(raw: &[u8; 16]) -> String {
    let mut out = String::with_capacity(36);
    for (i, b) in raw.iter().enumerate() {
        if matches!(i, 4 | 6 | 8 | 10) {
            out.push('-');
        }
        out.push_str(&format!("{b:02X}"));
    }
    out
}

/// `re.search(r'"IOPlatformUUID" = "([^"]+)"', out).group(1)`.
pub fn ioreg_uuid(out: &[u32]) -> Option<PyStr> {
    let prefix: Vec<u32> = "\"IOPlatformUUID\" = \"".chars().map(u32::from).collect();
    let quote = u32::from(b'"');
    for at in 0..out.len() {
        if !out[at..].starts_with(&prefix) {
            continue;
        }
        let from = at + prefix.len();
        if let Some(len) = out[from..].iter().position(|&c| c == quote) {
            if len > 0 {
                return Some(out[from..from + len].to_vec());
            }
        }
    }
    None
}

/// `_darwin_hardware()` on darwin: `gethostuuid`'s 16 bytes (None where
/// the call fails or raises), else `ioreg`'s output, asked only then
/// (None where the spawn raises; Err where the output does not decode,
/// which the reference does not catch).
pub fn darwin_hardware(
    gethostuuid: Option<[u8; 16]>,
    ioreg: impl FnOnce() -> Result<Option<PyStr>, PyError>,
) -> Result<Option<PyStr>, PyError> {
    if let Some(raw) = gethostuuid {
        return Ok(Some(uuid_text(&raw).chars().map(u32::from).collect()));
    }
    Ok(ioreg()?.and_then(|out| ioreg_uuid(&out)))
}

/// `_linux_hardware()` over `paths`: the first file that reads as ASCII
/// and is not blank after `strip()`. A path holding a NUL raises
/// ValueError, which the reference does not catch.
pub fn linux_hardware<F: Fs + ?Sized>(fs: &F, paths: &[&[u8]]) -> Result<Option<PyStr>, PyError> {
    for path in paths {
        match read_text(fs, path, true) {
            Ok(text) => {
                let value = strip(&text);
                if !value.is_empty() {
                    return Ok(Some(value.to_vec()));
                }
            }
            Err(ReadText::Value) => return Err(PyError::ValueError),
            Err(_) => {}
        }
    }
    Ok(None)
}

/// `_windows_hardware()` on win32: `value` is MachineGuid's value, None
/// where the registry raised OSError; `str(value) if value else None`.
pub fn windows_hardware(value: Option<&PyValue>) -> Result<Option<PyStr>, PyError> {
    match value {
        Some(v) if super::truthy(v) => py_text(v).map(Some),
        _ => Ok(None),
    }
}

/// The platform's answers `read_hardware_id` asks for.
pub trait HardwareSource {
    /// `gethostuuid(3)`'s bytes, None where it fails or raises.
    fn gethostuuid(&self) -> Option<[u8; 16]>;
    /// `ioreg -rd1 -c IOPlatformExpertDevice`'s output as
    /// `subprocess.run(..., text=True)` gives it: decoded in the locale's
    /// encoding with universal newlines (`\r\n` and `\r` read as `\n`).
    /// `Ok(None)` where the spawn raises OSError or SubprocessError, and
    /// `Err(UnicodeDecodeError)` where the output does not decode, which
    /// the reference does not catch.
    fn ioreg(&self) -> Result<Option<PyStr>, PyError>;
    /// The registry's MachineGuid, None where reading it raises OSError.
    fn machine_guid(&self) -> Option<PyValue>;
}

/// `_HARDWARE`: the hardware id read once, kept by the caller.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct HardwareMemo(Option<Option<PyStr>>);

impl HardwareMemo {
    /// The value kept, or `fill`'s, kept from then on.
    pub fn get_or_fill<E>(
        &mut self,
        fill: impl FnOnce() -> Result<Option<PyStr>, E>,
    ) -> Result<Option<PyStr>, E> {
        if let Some(value) = &self.0 {
            return Ok(value.clone());
        }
        let value = fill()?;
        self.0 = Some(value.clone());
        Ok(value)
    }

    /// `_clear_hardware()`.
    pub fn clear(&mut self) {
        self.0 = None;
    }
}

/// `read_hardware_id()` under `sys.platform == platform`, the Linux files
/// at `linux_paths` (`LINUX_MACHINE_ID` in use).
pub fn read_hardware_id<F: Fs + ?Sized, S: HardwareSource + ?Sized>(
    memo: &mut HardwareMemo,
    platform: &str,
    source: &S,
    fs: &F,
    linux_paths: &[&[u8]],
) -> Result<Option<PyStr>, PyError> {
    memo.get_or_fill(|| {
        if platform == "darwin" {
            darwin_hardware(source.gethostuuid(), || source.ioreg())
        } else if platform == "win32" {
            windows_hardware(source.machine_guid().as_ref())
        } else if platform.starts_with("linux") {
            linux_hardware(fs, linux_paths)
        } else {
            Ok(None)
        }
    })
}

/// `current_hostname()`: the name `os.fsdecode` spells, None where the
/// call raises or the name is empty.
pub fn current_hostname<H: Host + ?Sized>(host: &H) -> Option<PyStr> {
    let name = host.hostname()?;
    if name.is_empty() {
        return None;
    }
    Some(crate::gitsystem::record::surrogateescape(&name))
}

/// What `os.stat` gives `_file_object`: the device, the inode and the
/// birth time as (seconds, nanoseconds), None where the stat has none.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IdStat {
    pub dev: u64,
    pub ino: u64,
    pub birth: Option<(i64, u32)>,
}

/// Why `os.stat` gave no answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IdStatError {
    /// OSError.
    Os,
    /// ValueError: a path holding a NUL byte.
    Value,
    /// Not ported here (the Windows flavour).
    NotPorted,
}

/// `os.stat(path)` as `_file_object` reads it.
pub trait IdFileStat {
    fn id_stat(&self, path: &[u8]) -> Result<IdStat, IdStatError>;
}

/// The machine's `os.stat`: `MetadataExt` and `Metadata::created`. The
/// birth time is kept only where CPython exposes one (macOS); Linux's
/// `os.stat_result` has none, though `statx` knows it.
#[derive(Clone, Copy, Debug, Default)]
pub struct StdIdFileStat;

impl IdFileStat for StdIdFileStat {
    #[cfg(unix)]
    fn id_stat(&self, path: &[u8]) -> Result<IdStat, IdStatError> {
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::MetadataExt;
        if path.contains(&0) {
            return Err(IdStatError::Value);
        }
        let md =
            std::fs::metadata(std::ffi::OsStr::from_bytes(path)).map_err(|_| IdStatError::Os)?;
        let birth = if cfg!(any(target_os = "macos", target_os = "ios")) {
            md.created().ok().map(seconds_of_time)
        } else {
            None
        };
        Ok(IdStat {
            dev: md.dev(),
            ino: md.ino(),
            birth,
        })
    }

    #[cfg(not(unix))]
    fn id_stat(&self, _path: &[u8]) -> Result<IdStat, IdStatError> {
        Err(IdStatError::NotPorted)
    }
}

/// A time as `st_birthtimespec` holds it: whole seconds since 1970
/// rounded toward minus infinity and the nanoseconds past them, so half a
/// second before 1970 is (-1, 500000000).
pub fn seconds_of_time(t: std::time::SystemTime) -> (i64, u32) {
    match t.duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => (d.as_secs() as i64, d.subsec_nanos()),
        Err(e) => {
            let d = e.duration();
            let (s, n) = (d.as_secs() as i64, d.subsec_nanos());
            if n == 0 {
                (-s, 0)
            } else {
                (-s - 1, 1_000_000_000 - n)
            }
        }
    }
}

/// `st_birthtime` as macOS's CPython makes it: `bsec + 1e-9 * bnsec`
/// with `bsec` an `unsigned long`, so a time before 1970 wraps.
pub fn birth_seconds(sec: i64, nsec: u32) -> f64 {
    (sec as u64) as f64 + 1e-9 * f64::from(nsec)
}

/// `int(seconds * 1_000_000_000)` as text: the product truncated toward
/// zero and written exactly, however large. `int` raises ValueError for
/// NaN and OverflowError for an infinity.
pub fn ns_of_seconds(seconds: f64) -> Result<String, PyError> {
    let v = (seconds * 1_000_000_000.0).trunc();
    if v.is_nan() {
        return Err(PyError::ValueError);
    }
    if v.is_infinite() {
        return Err(PyError::OverflowError);
    }
    if v.abs() < 1e38 {
        return Ok(format!("{}", v as i128));
    }
    // An integral double past `i128`: mantissa times a power of two, in
    // base 10^9 digits, least significant first.
    let bits = v.to_bits();
    let exp = ((bits >> 52) & 0x7FF) as i32 - 1075;
    let mantissa = (bits & ((1 << 52) - 1)) | (1 << 52);
    let mut digits = vec![mantissa % 1_000_000_000, mantissa / 1_000_000_000];
    for _ in 0..exp {
        let mut carry = 0;
        for d in digits.iter_mut() {
            let x = *d * 2 + carry;
            *d = x % 1_000_000_000;
            carry = x / 1_000_000_000;
        }
        if carry > 0 {
            digits.push(carry);
        }
    }
    while digits.len() > 1 && digits.last() == Some(&0) {
        digits.pop();
    }
    let mut out = String::from(if v < 0.0 { "-" } else { "" });
    let mut parts = digits.iter().rev();
    if let Some(top) = parts.next() {
        out.push_str(&top.to_string());
    }
    for d in parts {
        out.push_str(&format!("{d:09}"));
    }
    Ok(out)
}

/// The birth time's text: `st_birthtime_ns` where the stat has it
/// (`has_ns`), else from the float `st_birthtime`.
pub fn birth_text(birth: Option<(i64, u32)>, has_ns: bool) -> Option<String> {
    let (sec, nsec) = birth?;
    Some(if has_ns {
        (i128::from(sec) * 1_000_000_000 + i128::from(nsec)).to_string()
    } else {
        // `birth_seconds` is finite for every (sec, nsec).
        ns_of_seconds(birth_seconds(sec, nsec)).unwrap_or_default()
    })
}

/// `_file_object(path)`: (device, inode, birth time in ns or None) as
/// texts, None where the stat raises OSError. ValueError (a NUL in the
/// path) is raised, as the reference does not catch it.
pub fn file_object<S: IdFileStat + ?Sized>(
    stat: &S,
    path: &[u8],
) -> Result<Option<(String, String, Option<String>)>, FileObjectError> {
    match stat.id_stat(path) {
        Ok(st) => Ok(Some((
            st.dev.to_string(),
            st.ino.to_string(),
            birth_text(st.birth, BIRTH_NS),
        ))),
        Err(IdStatError::Os) => Ok(None),
        Err(IdStatError::Value) => Err(FileObjectError::Raised(PyError::ValueError)),
        Err(IdStatError::NotPorted) => Err(FileObjectError::Declined(Declined::NotPorted)),
    }
}

/// Why `file_object` gives no answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FileObjectError {
    Raised(PyError),
    Declined(Declined),
}
