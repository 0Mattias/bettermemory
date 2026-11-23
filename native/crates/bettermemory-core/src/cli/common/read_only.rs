//! The supplied-path policy of `cli/_common.py:open_read_only`.

use std::path::Path;

use super::{path_text, refuse, s};
use crate::cli::{Exit, UsageError};
use crate::pyvalue::PyStr;
use crate::recall_prompt::PyException;
use crate::store::restore_lock::RESTORE_RUNNING_STATUS;
use crate::store_refusal;

/// The exceptions of `path.stat()` in Python catch order.
pub enum StatFailure<E> {
    /// FileNotFoundError or NotADirectoryError. The original message is unused.
    Missing,
    Os(E),
    Raised(E),
}

/// Classify the original opener exception by inheritance at the binding.
pub enum ReaderFailure<E> {
    RestoreRunning(E),
    /// ValueError, sqlite3.DatabaseError or OSError.
    Unreadable(E),
    Raised(E),
}

/// Python's `str(exc)` can itself raise another exception.
pub trait Diagnostic: Sized {
    /// Explicit `str(exc)` in the restore branch.
    fn message(&self) -> Result<PyStr, Self>;
    /// Empty-spec f-string formatting of an ordinary stat or open failure.
    fn format_message(&self) -> Result<PyStr, Self> {
        self.message()
    }
}

impl Diagnostic for PyException {
    fn message(&self) -> Result<PyStr, Self> {
        Ok(self.message.clone())
    }
}

/// An ordinary CLI refusal, or the unchanged exception from the indicated phase.
#[derive(Debug, PartialEq, Eq)]
pub enum ReadOnlyError<S, B, E> {
    Exit(Exit),
    Stat(S),
    Bundle(B),
    Open(E),
}

fn cannot_read(parser: &impl UsageError, path: &Path, reason: PyStr, exit_code: i32) -> Exit {
    let mut message = s("cannot read the store at ");
    message.extend(path_text(path));
    message.extend(s(": "));
    message.extend(reason);
    refuse(parser, &message, exit_code)
}

/// Run the three reference operations in order. A successful open returns its owner
/// unchanged, including any shared lock that it holds.
pub fn open_read_only_with<T, S: Diagnostic, B, E: Diagnostic>(
    parser: &impl UsageError,
    path: &Path,
    exit_code: i32,
    stat: impl FnOnce(&Path) -> Result<bool, StatFailure<S>>,
    bundle: impl FnOnce(&Path) -> Result<Option<PyStr>, B>,
    open: impl FnOnce(&Path) -> Result<T, ReaderFailure<E>>,
) -> Result<T, ReadOnlyError<S, B, E>> {
    match stat(path) {
        Ok(true) => {}
        Ok(false) | Err(StatFailure::Missing) => {
            let mut message = s("no bettermemory 9 store at ");
            message.extend(path_text(path));
            return Err(ReadOnlyError::Exit(parser.error(&message)));
        }
        Err(StatFailure::Os(error)) => {
            let reason = error.format_message().map_err(ReadOnlyError::Stat)?;
            return Err(ReadOnlyError::Exit(cannot_read(
                parser, path, reason, exit_code,
            )));
        }
        Err(StatFailure::Raised(error)) => return Err(ReadOnlyError::Stat(error)),
    }
    if let Some(reason) = bundle(path).map_err(ReadOnlyError::Bundle)? {
        return Err(ReadOnlyError::Exit(refuse(parser, &reason, exit_code)));
    }
    match open(path) {
        Ok(owner) => Ok(owner),
        Err(ReaderFailure::RestoreRunning(error)) => {
            let reason = error.message().map_err(ReadOnlyError::Open)?;
            Err(ReadOnlyError::Exit(refuse(
                parser,
                &reason,
                RESTORE_RUNNING_STATUS,
            )))
        }
        Err(ReaderFailure::Unreadable(error)) => {
            let reason = error.format_message().map_err(ReadOnlyError::Open)?;
            Err(ReadOnlyError::Exit(cannot_read(
                parser, path, reason, exit_code,
            )))
        }
        Err(ReaderFailure::Raised(error)) => Err(ReadOnlyError::Open(error)),
    }
}

fn regular_file(path: &Path) -> Result<bool, StatFailure<PyException>> {
    if path.as_os_str().as_encoded_bytes().contains(&0) {
        return Err(StatFailure::Raised(PyException {
            class: "ValueError".into(),
            message: s("stat: embedded null character in path"),
        }));
    }
    std::fs::metadata(path)
        .map(|metadata| metadata.is_file())
        .map_err(|error| match error.kind() {
            std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory => {
                StatFailure::Missing
            }
            _ => StatFailure::Os(stat_os_error(&error, path)),
        })
}

#[cfg(not(windows))]
fn stat_os_error(error: &std::io::Error, path: &Path) -> PyException {
    crate::store::exception::StoreException::os(error, Some(path), None).exception
}

#[cfg(windows)]
fn stat_os_error(error: &std::io::Error, path: &Path) -> PyException {
    let description = error.to_string();
    let message = if let Some(code) = error.raw_os_error() {
        let suffix = format!(" (os error {code})");
        let text = description.strip_suffix(&suffix).unwrap_or(&description);
        let mut message = s(&format!(
            "[WinError {code}] {}: ",
            text.trim_end_matches(['.', '\r', '\n', ' '])
        ));
        message.extend(crate::pyvalue::repr_str(&path_text(path)));
        message
    } else {
        s(&description)
    };
    PyException {
        class: "OSError".into(),
        message,
    }
}

/// A host path has not necessarily passed through Python's Path constructor.
#[cfg(unix)]
fn supplied_path(path: &Path) -> std::path::PathBuf {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let spelling = crate::githead::discovery::pure(path.as_os_str().as_bytes());
    std::ffi::OsString::from_vec(spelling).into()
}

#[cfg(windows)]
fn supplied_path(path: &Path) -> std::path::PathBuf {
    use std::os::windows::ffi::OsStringExt;
    let text = path_text(path);
    // PureWindowsPath rewrites only ASCII path syntax. One scalar stands in
    // for each non-ASCII code point; restore them in their original order.
    let spelling: String = text
        .iter()
        .map(|&c| if c < 128 { c as u8 as char } else { '\u{e000}' })
        .collect();
    let pure = crate::config::pure_path(&spelling, crate::config::Flavour::Windows);
    let mut originals = text.into_iter().filter(|&c| c >= 128);
    let mut wide = Vec::new();
    for ch in pure.chars() {
        let cp = if ch == '\u{e000}' {
            originals.next().expect("path component preserved")
        } else {
            u32::from(ch)
        };
        if let Some(scalar) = char::from_u32(cp) {
            wide.extend_from_slice(scalar.encode_utf16(&mut [0; 2]));
        } else {
            wide.push(cp as u16);
        }
    }
    std::ffi::OsString::from_wide(&wide).into()
}

/// `open_read_only(parser, path, exit_code=...)` for an already selected path.
/// Supply the read-only opener of the eventual owner. Python's default exit code is 2.
pub fn open_read_only<T, E: Diagnostic>(
    parser: &impl UsageError,
    path: &Path,
    exit_code: i32,
    open: impl FnOnce(&Path) -> Result<T, ReaderFailure<E>>,
) -> Result<T, ReadOnlyError<PyException, store_refusal::Failure, E>> {
    let supplied = supplied_path(path);
    open_read_only_with(
        parser,
        &supplied,
        exit_code,
        regular_file,
        store_refusal::bundle_refusal,
        open,
    )
}
