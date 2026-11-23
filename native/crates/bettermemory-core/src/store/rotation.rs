//! `store_keys.py`'s finish of a committed key rotation (unit
//! store_keys-1): `pending_key_path`, `write_key_once`,
//! `_bound_here_or_nowhere`, `_current_named`, `finish_rotation`,
//! `_rename_read_attempts`, `_finish_for_signing`, with the segment
//! signature check (`_signs_a_segment`) and the keys directory's refusal
//! text (`KeysDirUnreadableError`).
//!
//! The key ring is `log::ring::KeyFiles`. The rename is
//! `_fsutil.replace_atomic` ported here with `fsutil`'s retry budget: one
//! rename off Windows, up to [`REPLACE_ATTEMPTS`] on Windows while the
//! rename raises `PermissionError`. Whether to take the Windows branch,
//! the working directory `Path.resolve` reads and the pause are
//! arguments. What the reference logs to `bettermemory.store` is returned
//! as [`LogLine`]s, and an exception's text is spelled as `str(exc)`
//! spells it.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use crate::fsutil::{REPLACE_ATTEMPTS, REPLACE_BACKOFF_S};
use crate::log::ring::{KeyFiles, RingLock};
use crate::log::{fingerprint, row_verifies, Key, LogRow};
use crate::pyvalue::{repr_str, PyError};
use crate::store::create::KEY_BYTES;

/// The level `bettermemory.store` logs a line at.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Info,
    Warning,
}

/// One line the reference logs: its level and `record.getMessage()`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LogLine {
    pub level: Level,
    pub text: String,
}

/// What a finish answered: the key (the reference's `bytes | None`) and
/// what it logged.
#[derive(Debug, Default)]
pub struct Finished {
    pub key: Option<Key>,
    pub log: Vec<LogLine>,
}

/// Where the port does not answer: the reference raised past every
/// `except` (RecursionError from a binding nested past the JSON limit),
/// or the binding must be compared with a resolved path under the
/// Windows flavour, which `fsdata` does not resolve (ENG-124).
#[derive(Debug)]
pub enum Declined {
    Py(PyError),
    NotPorted,
}

/// One segment as `_signs_a_segment` reads it: `Segment.key_fingerprint`
/// and `Segment.first_row`. `log::TrustSegment` carries neither.
#[derive(Clone, Copy, Debug)]
pub struct SegmentStart<'a> {
    pub key_fingerprint: &'a str,
    pub first_row: Option<&'a LogRow>,
}

/// `_signs_a_segment` (74-87): is a segment under `key`'s fingerprint
/// whose first row verifies under it.
pub fn signs_a_segment(segments: &[SegmentStart<'_>], key: &[u8]) -> bool {
    let own = fingerprint(key);
    segments.iter().any(|segment| {
        segment.key_fingerprint == own && segment.first_row.is_some_and(|r| row_verifies(key, r))
    })
}

#[cfg(unix)]
const HOST: crate::config::Flavour = crate::config::Flavour::Posix;
#[cfg(not(unix))]
const HOST: crate::config::Flavour = crate::config::Flavour::Windows;

/// `str(Path(path))` on this host: `Path`'s own spelling. A path that is
/// not UTF-8 is spelled lossily.
fn spelled(path: &Path) -> String {
    crate::config::pure_path(&path.to_string_lossy(), HOST)
}

/// `str(Path(path))` as code points: a path that is not UTF-8 is decoded
/// with `surrogateescape` (on POSIX) and left unnormalized.
fn spelled_cps(path: &Path) -> Vec<u32> {
    if let Some(text) = path.to_str() {
        return crate::config::pure_path(text, HOST)
            .chars()
            .map(u32::from)
            .collect();
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        crate::gitsystem::record::surrogateescape(path.as_os_str().as_bytes())
    }
    #[cfg(not(unix))]
    {
        path.to_string_lossy().chars().map(u32::from).collect()
    }
}

/// The supplied store path's Python spelling relative to the supplied cwd.
#[cfg(unix)]
fn host_path(path: &Path, cwd: &[u8]) -> PathBuf {
    use std::ffi::{OsStr, OsString};
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let text = crate::fsutil::fsdecode(path.as_os_str());
    let pure = crate::verify_anchoring::attest::pure_text(crate::fsdata::Flavour::Posix, &text)
        .expect("POSIX lexical paths accept all code points");
    let bytes = crate::response_write::fsencode_as(
        &pure,
        crate::response_write::FsEncoding::SurrogateEscape,
    )
    .expect("a path decoded from OS bytes");
    Path::new(OsStr::from_bytes(cwd)).join(OsString::from_vec(bytes))
}

/// `Path(a) == Path(b)`: the spellings compared, case-folded under the
/// Windows flavour (`_str_normcase`).
fn path_eq(a: &Path, b: &Path) -> bool {
    #[cfg(unix)]
    {
        spelled_cps(a) == spelled_cps(b)
    }
    #[cfg(not(unix))]
    {
        spelled(a).to_lowercase() == spelled(b).to_lowercase()
    }
}

/// `KeysDirUnreadableError.__init__` (102-122): the reason as rewritten
/// for a link on the way to the keys directory, and the message.
pub fn keys_dir_unreadable(keys_dir: &Path, link: &Path, reason: &str) -> (String, String) {
    let reason = if path_eq(link, keys_dir) {
        reason.to_owned()
    } else {
        let rest = reason.strip_prefix("it ").unwrap_or(reason);
        format!("{}, on the way to it, {rest}", spelled(link))
    };
    let message = format!(
        "the keys directory {} cannot be used ({reason}); no store is created until it can \
         keep the new store's key",
        spelled(keys_dir)
    );
    (reason, message)
}

/// `pending_key_path` (880-885): `<keys_dir>/<store_id>.key.pending`.
pub fn pending_key_path<L: RingLock>(ring: &KeyFiles<L>) -> PathBuf {
    ring.keys_dir()
        .join(format!("{}.key.pending", ring.store_id()))
}

/// `write_key_once` (888-905): `path` created holding `key`, mode 0600
/// under the umask, only when nothing is there (false when something is,
/// a dangling link included); the sync's failure is swallowed. The open's
/// error and the write's are both returned as the open's (the reference's
/// `os.write` error carries no file name).
pub fn write_key_once(path: &Path, key: &[u8]) -> io::Result<bool> {
    write_once(path, key, &bettermemory_sys::fsync).map_err(|e| match e {
        WriteFailure::Open(e) | WriteFailure::Write(e) => e,
    })
}

/// Which call of `write_key_once` raised: `os.open`'s error names the
/// file, `os.write`'s does not.
enum WriteFailure {
    Open(io::Error),
    Write(io::Error),
}

/// [`write_key_once`] with its sync (`fsync_file`) an argument.
fn write_once(
    path: &Path,
    key: &[u8],
    sync: &dyn Fn(&fs::File) -> io::Result<()>,
) -> Result<bool, WriteFailure> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = match options.open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => return Ok(false),
        Err(e) => return Err(WriteFailure::Open(e)),
    };
    file.write_all(key).map_err(WriteFailure::Write)?;
    let _ = sync(&file);
    Ok(true)
}

/// Which kind of call raised: one through the C runtime (`open`, `read`,
/// `write`), whose error is an errno, or `os.replace`, which on Windows
/// raises with the system's error code (`[WinError n]`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Call {
    Crt,
    Win32,
}

/// `str(exc)` of the OSError Python raises for `error` on `path` (none for
/// `os.write`), and `to` for a rename: `[Errno n] strerror: 'path'`, on
/// Windows `[WinError n] text: 'src' -> 'dst'` for the rename. None where
/// the text is not ported: an errno the C runtime maps from a Windows
/// error code, whose words the crate does not hold.
fn os_text(
    error: &io::Error,
    call: Call,
    path: Option<&Path>,
    to: Option<&Path>,
) -> Option<String> {
    let Some(code) = error.raw_os_error() else {
        return Some(error.to_string());
    };
    let mut text = errno_words(error, code, call)?;
    if let Some(path) = path {
        text.push_str(&format!(": {}", repr(path)));
        if let Some(to) = to {
            text.push_str(&format!(" -> {}", repr(to)));
        }
    }
    Some(text)
}

#[cfg(unix)]
fn errno_words(error: &io::Error, code: i32, _call: Call) -> Option<String> {
    let words = bettermemory_sys::strerror(code).unwrap_or_else(|| error.to_string());
    Some(format!("[Errno {code}] {words}"))
}

/// Windows: `PyErr_SetExcFromWindowsErr` spells `FormatMessageW`'s text
/// with trailing white space and dots removed.
#[cfg(not(unix))]
fn errno_words(error: &io::Error, code: i32, call: Call) -> Option<String> {
    if call != Call::Win32 {
        return None;
    }
    Some(format!(
        "[WinError {code}] {}",
        win_words(&error.to_string())
    ))
}

/// `FormatMessageW`'s text from the standard library's spelling (`text
/// (os error n)`), trailing white space and dots removed as CPython does.
#[cfg_attr(unix, allow(dead_code))]
pub(crate) fn win_words(display: &str) -> &str {
    let words = match display.rfind(" (os error ") {
        Some(end) => &display[..end],
        None => display,
    };
    words.trim_end_matches(|c: char| c <= ' ' || c == '.')
}

fn repr(path: &Path) -> String {
    let cps = spelled_cps(path);
    repr_str(&cps)
        .into_iter()
        .filter_map(char::from_u32)
        .collect()
}

/// Whether Python raises a failed rename as `PermissionError`.
fn permission(error: &io::Error) -> bool {
    #[cfg(windows)]
    {
        error
            .raw_os_error()
            .is_some_and(|c| crate::fsutil::winerror_is_eacces(c as u32))
    }
    #[cfg(not(windows))]
    {
        error.kind() == io::ErrorKind::PermissionDenied
    }
}

/// `_fsutil.replace_atomic(src, dst)`: one rename off Windows; on Windows
/// a `PermissionError` is tried again after a pause of 0.01 s doubling,
/// up to [`REPLACE_ATTEMPTS`] renames, the last error propagating.
fn replace_atomic(
    src: &Path,
    dst: &Path,
    windows: bool,
    sleep: &mut dyn FnMut(f64),
) -> io::Result<()> {
    if !windows {
        return fs::rename(src, dst);
    }
    let mut delay = REPLACE_BACKOFF_S;
    let mut attempt = 0;
    loop {
        match fs::rename(src, dst) {
            Err(e) if permission(&e) && attempt < REPLACE_ATTEMPTS - 1 => {
                sleep(delay);
                delay *= 2.0;
                attempt += 1;
            }
            other => return other,
        }
    }
}

/// `_bound_here_or_nowhere` (908-920): the ring is bound to no file, or to
/// `store_file` by its resolved spelling or as the same file. A binding
/// that cannot be read is false; RecursionError propagates.
pub fn bound_here_or_nowhere<L: RingLock>(
    ring: &KeyFiles<L>,
    store_file: &Path,
    cwd: &[u8],
) -> Result<bool, Declined> {
    use crate::log::ring::binding_path::BindingError;
    let bound = match ring.read_binding_exact() {
        Ok(Some(bound)) => bound,
        Ok(None) => return Ok(true),
        Err(BindingError::Os(_)) => return Ok(false),
        Err(BindingError::Recursion(_)) => return Err(Declined::Py(PyError::RecursionError)),
        Err(BindingError::Value) => return Err(Declined::Py(PyError::ValueError)),
    };
    #[cfg(unix)]
    {
        use crate::log::ring::{
            binding_path::{same_identity, HOST},
            resolve,
        };
        let Ok(resolved) = resolve(store_file, cwd) else {
            return Ok(false);
        };
        Ok(bound.lexical_eq(&resolved, HOST)
            || bound
                .host_path()
                .is_some_and(|p| same_identity(&p, &host_path(store_file, cwd))))
    }
    #[cfg(not(unix))]
    {
        let _ = (bound, store_file, cwd);
        Err(Declined::NotPorted)
    }
}

/// `_current_named` (923-932): the ring's current key when its
/// fingerprint is `recorded`, else None (an unreadable key included).
pub fn current_named<L: RingLock>(ring: &KeyFiles<L>, recorded: &str) -> Option<Key> {
    match ring.load_current() {
        Ok(Some(current)) if fingerprint(current.bytes()) == recorded => Some(current),
        _ => None,
    }
}

/// `recorded[:16]`.
fn prefix(recorded: &str) -> String {
    recorded.chars().take(16).collect()
}

pub(super) enum RotationFailure {
    Read(crate::log::ring::CurrentReadError),
    Os {
        error: io::Error,
        call: Call,
        path: Option<PathBuf>,
        to: Option<PathBuf>,
    },
    Occupied(PathBuf),
}
impl RotationFailure {
    fn legacy_text(&self) -> Option<String> {
        match self {
            Self::Read(e) => Some(e.legacy().to_string()),
            Self::Os {
                error,
                call,
                path,
                to,
            } => os_text(error, *call, path.as_deref(), to.as_deref()),
            Self::Occupied(p) => Some(format!(
                "{} holds another key under the same prefix",
                spelled(p)
            )),
        }
    }
    fn crt(error: io::Error, path: Option<&Path>) -> Self {
        Self::Os {
            error,
            call: Call::Crt,
            path: path.map(Path::to_owned),
            to: None,
        }
    }
}

pub(super) enum RotationNotice {
    Pending(RotationFailure),
    Failed(RotationFailure),
    Finished,
}

/// The `try` of `finish_rotation`: the current key kept under its
/// fingerprint, then the pending key renamed over it; the error's text.
fn make_current<L: RingLock>(
    ring: &KeyFiles<L>,
    pending: &Path,
    recorded: &str,
    windows: bool,
    sleep: &mut dyn FnMut(f64),
) -> Result<(), RotationFailure> {
    let current = ring.load_current_exact().map_err(RotationFailure::Read)?;
    if let Some(current) = current {
        let named = fingerprint(current.bytes());
        if named != recorded {
            let retired = ring.retired_path(&named);
            let wrote = write_once(&retired, current.bytes(), &bettermemory_sys::fsync).map_err(
                |e| match e {
                    WriteFailure::Open(e) => RotationFailure::crt(e, Some(&retired)),
                    WriteFailure::Write(e) => RotationFailure::crt(e, None),
                },
            )?;
            if !wrote {
                let there =
                    fs::read(&retired).map_err(|e| RotationFailure::crt(e, Some(&retired)))?;
                if there != current.bytes() {
                    return Err(RotationFailure::Occupied(retired));
                }
            }
        }
    }
    let target = ring.current_path();
    replace_atomic(pending, &target, windows, sleep).map_err(|e| RotationFailure::Os {
        error: e,
        call: Call::Win32,
        path: Some(pending.to_owned()),
        to: Some(target),
    })
}

/// `finish_rotation` (935-988) for the store file `store_file` whose meta
/// names the key `recorded`. `cwd` is `os.getcwd()`, `windows` is
/// `sys.platform == "win32"` for the rename, `sleep` its pause.
pub fn finish_rotation<L: RingLock>(
    ring: &KeyFiles<L>,
    store_file: &Path,
    recorded: &str,
    cwd: &[u8],
    windows: bool,
    sleep: &mut dyn FnMut(f64),
) -> Result<Finished, Declined> {
    let mut log = Vec::new();
    let key = finish_with(
        ring,
        recorded,
        windows,
        sleep,
        || bound_here_or_nowhere(ring, store_file, cwd),
        |notice| {
            let store = spelled(store_file);
            let (level,text) = match notice {
                RotationNotice::Pending(error) => (Level::Warning,format!(
                    "store {store}: the pending key {} cannot be read: {}",
                    spelled(&pending_key_path(ring)), error.legacy_text().ok_or(Declined::NotPorted)?)),
                RotationNotice::Failed(error) => (Level::Warning,format!(
                    "store {store}: the rotated key {} could not be made current ({}); the next open tries again",
                    prefix(recorded),error.legacy_text().ok_or(Declined::NotPorted)?)),
                RotationNotice::Finished => (Level::Info,format!(
                    "store {store}: the key {} a rotation committed is now the current key", prefix(recorded))),
            };
            log.push(LogLine { level, text });
            Ok(())
        },
    )?;
    Ok(Finished { key, log })
}

pub(super) fn finish_with<L: RingLock, E>(
    ring: &KeyFiles<L>,
    recorded: &str,
    windows: bool,
    sleep: &mut dyn FnMut(f64),
    bound_here: impl FnOnce() -> Result<bool, E>,
    mut emit: impl FnMut(RotationNotice) -> Result<(), E>,
) -> Result<Option<Key>, E> {
    let pending = pending_key_path(ring);
    let key = match fs::read(&pending) {
        Ok(bytes) => Key::new(bytes),
        Err(e) if crate::log::ring::key_not_found(&e) => return Ok(current_named(ring, recorded)),
        Err(e) => {
            emit(RotationNotice::Pending(RotationFailure::crt(
                e,
                Some(&pending),
            )))?;
            return Ok(None);
        }
    };
    if key.bytes().len() != KEY_BYTES || fingerprint(key.bytes()) != recorded || !bound_here()? {
        return Ok(None);
    }
    if let Err(error) = make_current(ring, &pending, recorded, windows, sleep) {
        if let Some(now) = current_named(ring, recorded) {
            return Ok(Some(now));
        }
        emit(RotationNotice::Failed(error))?;
        return Ok(None);
    }
    emit(RotationNotice::Finished)?;
    Ok(Some(key))
}

/// `_rename_read_attempts` (991-996): `_REPLACE_ATTEMPTS` on Windows, else 1.
pub fn rename_read_attempts(windows: bool) -> u32 {
    if windows {
        REPLACE_ATTEMPTS
    } else {
        1
    }
}

/// `_finish_for_signing` (999-1013): [`finish_rotation`] up to `attempts`
/// times (`rename_read_attempts`), with a pause of 0.01 s doubling
/// between, while it answers None and the pending key file is there
/// (`os.path.lexists`). The log lines of every attempt, in order.
pub fn finish_for_signing<L: RingLock>(
    ring: &KeyFiles<L>,
    store_file: &Path,
    recorded: &str,
    cwd: &[u8],
    windows: bool,
    attempts: u32,
    sleep: &mut dyn FnMut(f64),
) -> Result<Finished, Declined> {
    let pending = pending_key_path(ring);
    let mut delay = REPLACE_BACKOFF_S;
    let mut log = Vec::new();
    for attempt in 0..attempts {
        let finished = finish_rotation(ring, store_file, recorded, cwd, windows, sleep)?;
        log.extend(finished.log);
        if finished.key.is_some() {
            return Ok(Finished {
                key: finished.key,
                log,
            });
        }
        if attempt == attempts - 1 || fs::symlink_metadata(&pending).is_err() {
            break;
        }
        sleep(delay);
        delay *= 2.0;
    }
    Ok(Finished { key: None, log })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `os.write`'s OSError carries no file name: `str(exc)` is
    /// `[Errno 28] No space left on device` (POSIX words).
    #[cfg(unix)]
    #[test]
    fn a_write_error_names_no_file() {
        let error = io::Error::from_raw_os_error(28);
        let words = bettermemory_sys::strerror(28).unwrap();
        assert_eq!(
            os_text(&error, Call::Crt, None, None).unwrap(),
            format!("[Errno 28] {words}")
        );
        assert_eq!(
            os_text(&error, Call::Crt, Some(Path::new("/a/./b/")), None).unwrap(),
            format!("[Errno 28] {words}: '/a/b'")
        );
    }

    /// CPython removes trailing white space and dots from
    /// `FormatMessageW`'s text: `[WinError 5] Access is denied`.
    #[test]
    fn windows_words_lose_the_trailing_dot() {
        assert_eq!(
            win_words("Access is denied. (os error 5)"),
            "Access is denied"
        );
        assert_eq!(
            win_words("The file is in use.\r\n (os error 32)"),
            "The file is in use"
        );
        assert_eq!(win_words("plain"), "plain");
    }

    /// The sync's failure is swallowed, as `fsync_file` suppresses
    /// OSError.
    #[test]
    fn a_failed_sync_still_writes() {
        let dir = std::env::temp_dir().join(format!("bm-rot-sync-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("k");
        let failing = |_: &fs::File| Err(io::Error::from_raw_os_error(5));
        assert!(matches!(write_once(&path, b"key", &failing), Ok(true)));
        assert_eq!(fs::read(&path).unwrap(), b"key");
        let _ = fs::remove_dir_all(&dir);
    }
}
