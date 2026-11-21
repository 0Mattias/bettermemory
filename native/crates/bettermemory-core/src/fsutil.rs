//! `_fsutil.py`, its first unit: `unlink_retrying` with its Windows retry
//! budget (`_REPLACE_ATTEMPTS`, `_REPLACE_BACKOFF_S`), and
//! `bounded_stream_read`.
//!
//! The rest of `_fsutil.py` already lives elsewhere and stays there:
//! `ensure_owner_only_dir`, `fsync`, `atomic_write_bytes` and
//! `replace_atomic` in `store/create.rs`, `store/open.rs` and
//! `store/write.rs`; `flock_excl`'s POSIX lock in `store/write.rs`
//! (`open_lock` and its callers). `_flock_windows` and a native
//! `flock_excl` are the next unit.
//!
//! The retry runs over a [`Remover`] (the unlink, the class of its error
//! and the pause as one small trait) with the platform an argument, so its
//! Windows branch runs anywhere; [`unlink_retrying`] is the machine's.

use std::io::{ErrorKind, Read};
use std::path::Path;

use crate::fsdata::{Numbered, OsError, PyErr};

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
