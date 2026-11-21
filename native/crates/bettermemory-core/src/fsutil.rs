//! `_fsutil.py`, its first two units: `unlink_retrying` with its Windows
//! retry budget (`_REPLACE_ATTEMPTS`, `_REPLACE_BACKOFF_S`),
//! `bounded_stream_read`, and `flock_excl` with its Windows branch
//! `_flock_windows` (`FLOCK_TIMEOUT_SECONDS`, `_FLOCK_WARNED`).
//!
//! The rest of `_fsutil.py` already lives elsewhere and stays there:
//! `ensure_owner_only_dir`, `fsync`, `atomic_write_bytes` and
//! `replace_atomic` in `store/create.rs`, `store/open.rs` and
//! `store/write.rs`. The store's own lock (`open_lock` in
//! `store/write.rs`) is the same `flock` and is moved onto
//! [`flock_excl`] by a later unit.
//!
//! The Windows branch runs over an [`Msvcrt`] (the open, the one-byte
//! lock, the close, the clock and the pause) so it runs anywhere.
//!
//! The retry runs over a [`Remover`] (the unlink, the class of its error
//! and the pause as one small trait) with the platform an argument, so its
//! Windows branch runs anywhere; [`unlink_retrying`] is the machine's.

use std::io::{ErrorKind, Read};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use crate::fsdata::{Numbered, OsError, PyErr};
use crate::toml::py_repr_str;

/// `_REPLACE_ATTEMPTS` (`_fsutil.py` 329).
pub const REPLACE_ATTEMPTS: u32 = 5;
/// `_REPLACE_BACKOFF_S` (330), in seconds.
pub const REPLACE_BACKOFF_S: f64 = 0.01;

/// What `unlink_retrying` asks of the machine.
pub trait Remover {
    type Error;
    /// `os.unlink(path)`.
    fn unlink(&mut self) -> Result<(), Self::Error>;
    /// Whether Python raises the error as `PermissionError`.
    fn permission(&self, error: &Self::Error) -> bool;
    /// `time.sleep(seconds)`.
    fn sleep(&mut self, seconds: f64);
}

/// `unlink_retrying(path, still)` (386-417) over `remover`; `windows` is
/// `sys.platform == "win32"`. Off Windows one unlink, its error passed
/// through. On Windows up to [`REPLACE_ATTEMPTS`] unlinks; only a
/// permission error is tried again, after a pause of 0.01 s doubling,
/// after which `still` (where given) is asked and a false answer returns
/// without a further attempt. The last failure propagates.
pub fn unlink_retrying_by<R: Remover>(
    remover: &mut R,
    windows: bool,
    mut still: Option<&mut dyn FnMut() -> bool>,
) -> Result<(), R::Error> {
    if !windows {
        return remover.unlink();
    }
    let mut delay = REPLACE_BACKOFF_S;
    let mut attempt = 0;
    loop {
        match remover.unlink() {
            Ok(()) => return Ok(()),
            Err(error) => {
                if !remover.permission(&error) || attempt == REPLACE_ATTEMPTS - 1 {
                    return Err(error);
                }
            }
        }
        remover.sleep(delay);
        delay *= 2.0;
        if let Some(still) = still.as_mut() {
            if !still() {
                return Ok(());
            }
        }
        attempt += 1;
    }
}

/// Whether CPython's `winerror_to_errno` (`PC/errmap.h`) maps a Windows
/// error code to EACCES, the errno `PermissionError` is raised for (no
/// code maps to EPERM): an HRESULT of `FACILITY_WIN32` is unwrapped
/// first; of the Winsock codes only WSAEACCES (10013) gives 13.
pub fn winerror_is_eacces(code: u32) -> bool {
    let code = if code & 0xFFFF_0000 == 0x8007_0000 {
        code & 0xFFFF
    } else {
        code
    };
    matches!(
        code,
        5 // ERROR_ACCESS_DENIED
        | 16 // ERROR_CURRENT_DIRECTORY
        | 19
            ..=36 // ERROR_WRITE_PROTECT to ERROR_SHARING_BUFFER_EXCEEDED,
                  // ERROR_SHARING_VIOLATION 32 and ERROR_LOCK_VIOLATION 33
        | 65 // ERROR_NETWORK_ACCESS_DENIED
        | 82 // ERROR_CANNOT_MAKE
        | 83 // ERROR_FAIL_I24
        | 108 // ERROR_DRIVE_LOCKED
        | 132 // ERROR_SEEK_ON_DEVICE
        | 158 // ERROR_NOT_LOCKED
        | 167 // ERROR_LOCK_FAILED
        | 10013 // WSAEACCES
    )
}

/// The exception `os.unlink` raises for an `io::Error` of this machine:
/// `fsdata`'s class, and on Windows the class `PermissionError` exactly
/// where the raw code maps to EACCES.
pub fn unlink_error(error: &std::io::Error) -> OsError {
    let mut found = Numbered::of_io(error).error;
    if let (true, Some(code)) = (cfg!(windows), error.raw_os_error()) {
        let eacces = u32::try_from(code).is_ok_and(winerror_is_eacces);
        if eacces {
            found.class = PyErr::Permission;
            found.errno = Some("EACCES");
        } else if found.class == PyErr::Permission {
            found.class = PyErr::Os;
            found.errno = None;
        }
    }
    found
}

/// What `os.unlink` (`Py_DeleteFileW`, `Modules/posixmodule.c`) does on
/// Windows with a path of these attributes, where `std::fs::remove_file`
/// would answer differently.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowsUnlink {
    /// `DeleteFileW`, which `remove_file` performs as well.
    DeleteFile,
    /// `RemoveDirectoryW`: a directory symbolic link or junction.
    RemoveDirectory,
    /// `DeleteFileW` fails with ERROR_ACCESS_DENIED (5): a read-only file
    /// (which `remove_file` since Rust 1.86 deletes anyway) or a plain
    /// directory.
    AccessDenied,
}

/// `FILE_ATTRIBUTE_READONLY`.
pub const FILE_ATTRIBUTE_READONLY: u32 = 0x1;
/// `FILE_ATTRIBUTE_DIRECTORY`.
pub const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;

/// The choice `Py_DeleteFileW` makes from the path's own attributes (not
/// followed) and whether it is a name-surrogate reparse point (a symbolic
/// link or a junction): a directory link is removed as a directory, any
/// other directory or a read-only entry is refused.
pub fn windows_unlink(attributes: u32, link: bool) -> WindowsUnlink {
    if attributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
        if link {
            WindowsUnlink::RemoveDirectory
        } else {
            WindowsUnlink::AccessDenied
        }
    } else if attributes & FILE_ATTRIBUTE_READONLY != 0 {
        WindowsUnlink::AccessDenied
    } else {
        WindowsUnlink::DeleteFile
    }
}

#[cfg(windows)]
fn remove(path: &Path) -> std::io::Result<()> {
    use std::os::windows::fs::MetadataExt;
    let plan = match std::fs::symlink_metadata(path) {
        Ok(meta) => windows_unlink(meta.file_attributes(), meta.file_type().is_symlink()),
        Err(_) => WindowsUnlink::DeleteFile,
    };
    match plan {
        WindowsUnlink::DeleteFile => std::fs::remove_file(path),
        WindowsUnlink::RemoveDirectory => std::fs::remove_dir(path),
        WindowsUnlink::AccessDenied => Err(std::io::Error::from_raw_os_error(5)),
    }
}

#[cfg(not(windows))]
fn remove(path: &Path) -> std::io::Result<()> {
    std::fs::remove_file(path)
}

/// The machine's [`Remover`] for one path.
pub struct StdRemover<'a> {
    pub path: &'a Path,
}

impl Remover for StdRemover<'_> {
    type Error = OsError;

    fn unlink(&mut self) -> Result<(), OsError> {
        remove(self.path).map_err(|e| unlink_error(&e))
    }

    fn permission(&self, error: &OsError) -> bool {
        error.class == PyErr::Permission
    }

    fn sleep(&mut self, seconds: f64) {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
    }
}

/// `unlink_retrying(path, still)` on this machine.
pub fn unlink_retrying(
    path: &Path,
    still: Option<&mut dyn FnMut() -> bool>,
) -> Result<(), OsError> {
    unlink_retrying_by(&mut StdRemover { path }, cfg!(windows), still)
}

/// Why [`bounded_stream_read`] gave no bytes.
#[derive(Debug)]
pub enum StreamReadError {
    /// `ValueError`: the stream carries more than the cap.
    OverCap(i64),
    /// `ValueError` from `BufferedReader.read` of a length below -1.
    BadLength,
    /// `OverflowError`: `max_bytes + 1` does not fit `Py_ssize_t`.
    Overflow,
    /// The stream's own error, passed through.
    Io(std::io::Error),
}

impl StreamReadError {
    /// The exception's class name.
    pub fn class(&self) -> &'static str {
        match self {
            StreamReadError::OverCap(_) | StreamReadError::BadLength => "ValueError",
            StreamReadError::Overflow => "OverflowError",
            StreamReadError::Io(_) => "OSError",
        }
    }

    /// `str(exc)` for the errors this module raises; None for an io error.
    pub fn message(&self) -> Option<String> {
        match self {
            StreamReadError::OverCap(max) => Some(format!("stream input exceeds cap {max} bytes")),
            StreamReadError::BadLength => Some("read length must be non-negative or -1".into()),
            StreamReadError::Overflow => {
                Some("cannot fit 'int' into an index-sized integer".into())
            }
            StreamReadError::Io(_) => None,
        }
    }
}

/// `bounded_stream_read(stream, max_bytes)` (673-688) over a stream read
/// as `io.BufferedReader.read(max_bytes + 1)` reads it: until that many
/// bytes or end of file (-1: to end of file; below -1: refused before any
/// read). More than `max_bytes` bytes is `ValueError`.
pub fn bounded_stream_read<R: Read + ?Sized>(
    stream: &mut R,
    max_bytes: i64,
) -> Result<Vec<u8>, StreamReadError> {
    let want = max_bytes.checked_add(1).ok_or(StreamReadError::Overflow)?;
    if want < -1 {
        return Err(StreamReadError::BadLength);
    }
    let mut raw = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let room = if want == -1 {
            chunk.len()
        } else {
            let left = u64::try_from(want).unwrap_or(0) - raw.len() as u64;
            if left == 0 {
                break;
            }
            usize::try_from(left).map_or(chunk.len(), |l| l.min(chunk.len()))
        };
        match stream.read(&mut chunk[..room]) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) => return Err(StreamReadError::Io(e)),
        }
    }
    if i64::try_from(raw.len()).map_or(true, |len| len > max_bytes) {
        return Err(StreamReadError::OverCap(max_bytes));
    }
    Ok(raw)
}

/// `FLOCK_TIMEOUT_SECONDS` (473): how long the Windows branch of
/// [`flock_excl`] waits for a contended lock.
pub const FLOCK_TIMEOUT_SECONDS: f64 = 30.0;

/// `_FLOCK_WARNED` (467): set once the fallback warning has been given.
pub static FLOCK_WARNED: AtomicBool = AtomicBool::new(false);
/// The warning given in this process, for a caller to read.
static FLOCK_WARNING: Mutex<Option<FlockWarning>> = Mutex::new(None);

/// The one-shot warning of `_flock_windows`'s unlocked fallback.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FlockWarning {
    /// `import msvcrt` failed.
    MsvcrtUnavailable,
    /// `os.open` of the lock file failed; the text is `str(lock_path)`.
    CannotOpen(String),
}

impl FlockWarning {
    /// The logged text, `%s` filled in.
    pub fn message(&self) -> String {
        match self {
            FlockWarning::MsvcrtUnavailable => "flock_excl: msvcrt unavailable on this Windows \
                 interpreter; cross-process locking is disabled. Concurrent writers may corrupt \
                 files. Falling back to in-process-only yield."
                .to_owned(),
            FlockWarning::CannotOpen(path) => format!(
                "flock_excl: cannot open lockfile {path} on Windows; cross-process locking is \
                 disabled. Concurrent writers may corrupt files."
            ),
        }
    }
}

/// The warning this process gave, if any: the native form of
/// `logger.warning`, given at most once per process.
pub fn flock_warning() -> Option<FlockWarning> {
    FLOCK_WARNING.lock().map_or(None, |w| w.clone())
}

/// What `_flock_windows` asks of the machine: `os.open`, `msvcrt.locking`
/// of one byte, `os.close`, `time.monotonic` and `time.sleep`. A `false`
/// or `None` answer is the `OSError` the reference catches.
pub trait Msvcrt {
    type Fd;
    /// `os.open(str(lock_path), O_CREAT | O_RDWR)`.
    fn open(&mut self, lock_path: &Path) -> Option<Self::Fd>;
    /// `msvcrt.locking(fd, LK_NBLCK, 1)`.
    fn lock(&mut self, fd: &Self::Fd) -> bool;
    /// `msvcrt.locking(fd, LK_UNLCK, 1)`.
    fn unlock(&mut self, fd: &Self::Fd) -> bool;
    /// `os.close(fd)`.
    fn close(&mut self, fd: Self::Fd) -> bool;
    /// `time.monotonic()`.
    fn monotonic(&mut self) -> f64;
    /// `time.sleep(seconds)`.
    fn sleep(&mut self, seconds: f64);
}

/// `TimeoutError` of the Windows branch; the text is `str(exc)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FlockTimeout(pub String);

/// `_flock_windows`'s hold: dropping it releases the byte (an error
/// ignored) and closes the file (an error ignored). Without a file the
/// hold is the unlocked fallback.
pub struct WindowsHold<'a, M: Msvcrt> {
    msvcrt: Option<&'a mut M>,
    fd: Option<M::Fd>,
}

impl<M: Msvcrt> WindowsHold<'_, M> {
    /// Whether the byte is held (false: the unlocked fallback).
    pub fn locked(&self) -> bool {
        self.fd.is_some()
    }
}

impl<M: Msvcrt> Drop for WindowsHold<'_, M> {
    fn drop(&mut self) {
        if let (Some(m), Some(fd)) = (self.msvcrt.take(), self.fd.take()) {
            let _ = m.unlock(&fd);
            let _ = m.close(fd);
        }
    }
}

/// `{value:.1f}`: the exact binary value rounded half to even.
fn fixed1(value: f64) -> String {
    format!("{value:.1}")
}

fn warn_once(warned: &AtomicBool, warning: FlockWarning, warn: &mut dyn FnMut(FlockWarning)) {
    if !warned.swap(true, Ordering::SeqCst) {
        warn(warning);
    }
}

/// `_flock_windows(lock_path)` (575-670) over `msvcrt` (`None`: the
/// module does not import), with `FLOCK_TIMEOUT_SECONDS` as `timeout`,
/// `_FLOCK_WARNED` as `warned` and the logger as `warn`. Either fallback
/// warns once and holds nothing. Otherwise one byte is tried, then after
/// each failure the clock is read: at or past the deadline the file is
/// closed and `TimeoutError` returned, else a pause of 0.005 s doubling
/// up to 0.1 s and another try.
pub fn flock_windows_by<'a, M: Msvcrt>(
    lock_path: &Path,
    msvcrt: Option<&'a mut M>,
    timeout: f64,
    warned: &AtomicBool,
    warn: &mut dyn FnMut(FlockWarning),
) -> Result<WindowsHold<'a, M>, FlockTimeout> {
    let Some(m) = msvcrt else {
        warn_once(warned, FlockWarning::MsvcrtUnavailable, warn);
        return Ok(WindowsHold {
            msvcrt: None,
            fd: None,
        });
    };
    let Some(fd) = m.open(lock_path) else {
        let shown = lock_path.display().to_string();
        warn_once(warned, FlockWarning::CannotOpen(shown), warn);
        return Ok(WindowsHold {
            msvcrt: None,
            fd: None,
        });
    };
    let deadline = m.monotonic() + timeout;
    let mut backoff = 0.005_f64;
    loop {
        if m.lock(&fd) {
            return Ok(WindowsHold {
                msvcrt: Some(m),
                fd: Some(fd),
            });
        }
        if m.monotonic() >= deadline {
            let _ = m.close(fd);
            return Err(FlockTimeout(format!(
                "flock_excl: could not acquire {} within {}s",
                lock_path.display(),
                fixed1(timeout)
            )));
        }
        m.sleep(backoff);
        backoff = (backoff * 2.0).min(0.1);
    }
}

/// Why `path.with_suffix(path.suffix + lock_suffix)` refused (the
/// `ValueError` text of CPython 3.13).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockPathError(pub String);

/// `path.with_suffix(path.suffix + lock_suffix)` as CPython 3.13's
/// pathlib answers it: the last component with `lock_suffix` appended (a
/// stem and its suffix put back together are the name), refused when the
/// path has no name, when a path without a suffix meets a `lock_suffix`
/// that is not `.` and more, or when the new name holds a separator
/// (`/`, and `\` too where `windows`).
pub fn lock_path(path: &Path, lock_suffix: &str, windows: bool) -> Result<PathBuf, LockPathError> {
    // pathlib drops every `.` component (a leading `./` too) and, on
    // POSIX, keeps a leading `//` (exactly two slashes) as it is.
    let mut comps: Vec<Component<'_>> = path
        .components()
        .filter(|c| !matches!(c, Component::CurDir))
        .collect();
    let bytes = path.as_os_str().as_encoded_bytes();
    let double_root = !windows && bytes.starts_with(b"//") && !bytes.starts_with(b"///");
    let rebuild = |comps: &[Component<'_>]| -> PathBuf {
        let mut out = PathBuf::new();
        for c in comps {
            if double_root && matches!(c, Component::RootDir) {
                out.push("//");
            } else {
                out.push(c);
            }
        }
        out
    };
    let name = match comps.last() {
        Some(Component::Normal(name)) => name.to_os_string(),
        Some(Component::ParentDir) => "..".into(),
        _ => {
            let kind = if windows { "WindowsPath" } else { "PosixPath" };
            let mut shown = rebuild(&comps);
            if shown.as_os_str().is_empty() {
                shown = PathBuf::from(".");
            }
            let mut text = py_text(shown.as_os_str());
            for cp in &mut text {
                if *cp == u32::from(b'\\') {
                    *cp = u32::from(b'/');
                }
            }
            return Err(LockPathError(format!(
                "{kind}({}) has an empty name",
                py_repr_text(&text)
            )));
        }
    };
    let bytes = name.as_encoded_bytes();
    let has_suffix = bytes
        .iter()
        .rposition(|&b| b == b'.')
        .is_some_and(|i| i > 0 && i + 1 < bytes.len());
    let valid = lock_suffix.is_empty() || (lock_suffix.starts_with('.') && lock_suffix.len() > 1);
    if !has_suffix && !valid {
        return Err(LockPathError(format!(
            "Invalid suffix {}",
            py_repr_str(lock_suffix)
        )));
    }
    let mut new = name;
    new.push(lock_suffix);
    if lock_suffix.contains('/') || (windows && lock_suffix.contains('\\')) {
        return Err(LockPathError(format!(
            "Invalid name {}",
            py_repr_text(&py_text(&new))
        )));
    }
    comps.pop();
    let mut out = rebuild(&comps);
    out.push(new);
    Ok(out)
}

/// The `str` Python holds for a path's text: `os.fsdecode` on POSIX
/// (`surrogateescape`, a byte outside UTF-8 as U+DC80 + byte), the UTF-16
/// units on Windows (a lone surrogate kept as itself).
fn py_text(text: &std::ffi::OsStr) -> crate::pyvalue::PyStr {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        char::decode_utf16(text.encode_wide())
            .map(|r| r.map_or_else(|e| u32::from(e.unpaired_surrogate()), u32::from))
            .collect()
    }
    #[cfg(not(windows))]
    {
        crate::gitsystem::record::surrogateescape(text.as_encoded_bytes())
    }
}

/// `repr(s)` of a `str` that may hold a lone surrogate.
fn py_repr_text(text: &[u32]) -> String {
    crate::unicode::to_string(&crate::pyvalue::repr_str(text)).expect("a repr is text")
}

/// Why [`flock_excl`] gave no lock.
#[derive(Debug)]
pub enum FlockError {
    /// `ValueError` of the lock path's name.
    Path(LockPathError),
    /// `OSError` of the directory, the open or the lock.
    Io(std::io::Error),
    /// `TimeoutError` of the Windows branch.
    Timeout(FlockTimeout),
}

/// The machine's [`Msvcrt`]: std's whole-file `try_lock`, which on
/// Windows covers byte 0 and so excludes the reference's lock.
pub struct StdMsvcrt;

impl Msvcrt for StdMsvcrt {
    type Fd = std::fs::File;

    fn open(&mut self, lock_path: &Path) -> Option<std::fs::File> {
        std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(lock_path)
            .ok()
    }

    fn lock(&mut self, fd: &std::fs::File) -> bool {
        fd.try_lock().is_ok()
    }

    fn unlock(&mut self, fd: &std::fs::File) -> bool {
        fd.unlock().is_ok()
    }

    fn close(&mut self, fd: std::fs::File) -> bool {
        drop(fd);
        true
    }

    fn monotonic(&mut self) -> f64 {
        static START: OnceLock<std::time::Instant> = OnceLock::new();
        START
            .get_or_init(std::time::Instant::now)
            .elapsed()
            .as_secs_f64()
    }

    fn sleep(&mut self, seconds: f64) {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
    }
}

/// `flock_excl(path, lock_suffix=...)`'s hold; dropping it releases.
pub struct FlockGuard {
    _file: Option<std::fs::File>,
    #[cfg(windows)]
    _hold: Option<WindowsHold<'static, StdMsvcrt>>,
}

#[cfg(not(windows))]
impl Drop for FlockGuard {
    fn drop(&mut self) {
        if let Some(file) = &self._file {
            let _ = file.unlock();
        }
    }
}

/// `flock_excl(path, lock_suffix=lock_suffix)` (477-572) on this machine:
/// the parent made, then the lock file `<name><lock_suffix>`. On POSIX it
/// is opened (created 0600) and held with a blocking `flock(LOCK_EX)`, no
/// deadline; on Windows `_flock_windows`, warning through
/// [`flock_warning`].
pub fn flock_excl(path: &Path, lock_suffix: &str) -> Result<FlockGuard, FlockError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(FlockError::Io)?;
    }
    let lock = lock_path(path, lock_suffix, cfg!(windows)).map_err(FlockError::Path)?;
    #[cfg(windows)]
    {
        // A unit struct: leaking it allocates nothing.
        let msvcrt: &'static mut StdMsvcrt = Box::leak(Box::new(StdMsvcrt));
        let mut record = |w: FlockWarning| {
            if let Ok(mut slot) = FLOCK_WARNING.lock() {
                *slot = Some(w);
            }
        };
        let hold = flock_windows_by(
            &lock,
            Some(msvcrt),
            FLOCK_TIMEOUT_SECONDS,
            &FLOCK_WARNED,
            &mut record,
        )
        .map_err(FlockError::Timeout)?;
        Ok(FlockGuard {
            _file: None,
            _hold: Some(hold),
        })
    }
    #[cfg(not(windows))]
    {
        let mut options = std::fs::OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        let file = options.open(&lock).map_err(FlockError::Io)?;
        lock_retrying_eintr(|| file.lock()).map_err(FlockError::Io)?;
        Ok(FlockGuard { _file: Some(file) })
    }
}

/// Calls `lock` until it answers anything but `Interrupted`, as
/// `fcntl.flock` tries again on EINTR (PEP 475); std's `lock` does not.
pub fn lock_retrying_eintr(mut lock: impl FnMut() -> std::io::Result<()>) -> std::io::Result<()> {
    loop {
        match lock() {
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            other => return other,
        }
    }
}
