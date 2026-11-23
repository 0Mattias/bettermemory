//! Structured warning arguments, including exceptions from physical file operations.
use super::ReaderRing;
use crate::fsutil::fsdecode;
use crate::log::ring::binding_path::HOST;
use crate::pyvalue::{repr_str, PyStr};
use crate::store::{
    key_state::KeyReadError,
    rotation::{self, RotationFailure, RotationNotice},
};
use crate::unicode::cps;
use std::path::Path;
use std::sync::Arc;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogLevel {
    Debug,
    Info,
    Warning,
}

/// The original failure passed to the logger, including the Python spelling
/// of each filename. No key bytes are part of a diagnostic.
#[derive(Debug)]
pub struct LogCause {
    pub class: String,
    pub message: PyStr,
    pub errno: Option<i32>,
    pub winerror: Option<i32>,
    /// KeyUnreadableError.path, separate from OSError.filename.
    pub path: Option<PyStr>,
    pub filename: Option<PyStr>,
    pub filename2: Option<PyStr>,
    pub reason: Option<PyStr>,
    /// The OSError which caused a KeyUnreadableError, matching Python __cause__.
    pub source: Option<Arc<LogCause>>,
    /// The original OS error when the diagnostic is a direct kernel/CRT error.
    pub os_error: Option<Arc<std::io::Error>>,
}
#[derive(Debug)]
pub struct KeyLog {
    pub level: LogLevel,
    pub format: &'static str,
    pub args: Vec<PyStr>,
    pub cause: Option<Arc<LogCause>>,
}
impl KeyLog {
    pub const LOGGER: &'static str = "bettermemory.store";
    pub fn message(&self) -> PyStr {
        let mut out = Vec::new();
        let mut fields = self.args.iter();
        for (i, part) in self.format.split("%s").enumerate() {
            if i > 0 {
                out.extend_from_slice(fields.next().expect("one argument per field"));
            }
            out.extend(cps(part));
        }
        out
    }
}

fn joined(parts: &[&[u32]]) -> PyStr {
    parts.concat()
}

pub(super) fn os_cause(
    error: std::io::Error,
    path: Option<PyStr>,
    to: Option<PyStr>,
    _call: rotation::Call,
) -> LogCause {
    #[cfg(unix)]
    let (class, mut message, errno, winerror) = {
        let e = crate::store::exception::StoreException::os(&error, None, None);
        (e.exception.class, e.exception.message, e.errno, None)
    };
    #[cfg(windows)]
    let (class, mut message, errno, winerror) = {
        use bettermemory_sys::windows_file_error;
        match error.raw_os_error() {
            Some(code) if _call == rotation::Call::Crt => {
                let (errno, class) = windows_file_error::crt(code);
                let words = windows_file_error::crt_words(errno);
                (
                    class.to_owned(),
                    cps(&format!("[Errno {errno}] {words}")),
                    Some(errno),
                    None,
                )
            }
            Some(code) => {
                let (errno, class) = windows_file_error::win32(code);
                let display = error.to_string();
                let words = rotation::win_words(&display);
                (
                    class.to_owned(),
                    cps(&format!("[WinError {code}] {words}")),
                    Some(errno),
                    Some(code),
                )
            }
            None => ("OSError".to_owned(), cps(&error.to_string()), None, None),
        }
    };
    if let Some(name) = &path {
        message.extend(cps(": "));
        message.extend(repr_str(name));
    }
    if let Some(name) = &to {
        message.extend(cps(" -> "));
        message.extend(repr_str(name));
    }
    LogCause {
        class,
        message,
        errno,
        winerror,
        path: None,
        filename: path,
        filename2: to,
        reason: None,
        source: None,
        os_error: Some(Arc::new(error)),
    }
}

pub(super) fn read_failure(error: &crate::log::ring::CurrentReadError) -> KeyReadError {
    #[cfg(unix)]
    let (errno, reason) = {
        let legacy = error.legacy();
        (legacy.errno, cps(&legacy.reason))
    };
    #[cfg(windows)]
    let (errno, reason) = match error.cause.raw_os_error() {
        Some(code) => {
            let (errno, _) = bettermemory_sys::windows_file_error::crt(code);
            (
                Some(errno),
                cps(&bettermemory_sys::windows_file_error::crt_words(errno)),
            )
        }
        None => (None, cps(&error.cause.to_string())),
    };
    KeyReadError {
        path: error.path.clone(),
        errno,
        reason: error.dangling.clone().unwrap_or(reason),
    }
}

impl ReaderRing {
    pub(super) fn unreadable(&self, error: &KeyReadError, cause: std::io::Error) -> LogCause {
        let path = self.file(".key");
        let message = joined(&[
            &cps("the key file "),
            &path,
            &cps(" is there and cannot be read ("),
            &error.reason,
            &cps("); the store is not rekeyed over it"),
        ]);
        LogCause {
            class: "KeyUnreadableError".into(),
            message,
            errno: error.errno,
            winerror: None,
            source: Some(Arc::new(os_cause(
                cause,
                Some(path.clone()),
                None,
                rotation::Call::Crt,
            ))),
            os_error: None,
            path: Some(path),
            filename: None,
            filename2: None,
            reason: Some(error.reason.clone()),
        }
    }
    fn rotation_cause(&self, failure: RotationFailure) -> LogCause {
        let exact_name =
            |p: &Path| {
                let name = fsdecode(p.file_name().expect("ring file"));
                crate::store::key_state::normalize_spelling(
                    &crate::gitfile_rule::hostpath::text::join(HOST, &self.keys_dir, &[&name]),
                )
            };
        match failure {
            RotationFailure::Read(e) => self.unreadable(&read_failure(&e), e.cause),
            RotationFailure::Os {
                error,
                call,
                path,
                to,
            } => os_cause(
                error,
                path.as_deref().map(exact_name),
                to.as_deref().map(exact_name),
                call,
            ),
            RotationFailure::Occupied(p) => {
                let message = joined(&[
                    &exact_name(&p),
                    &cps(" holds another key under the same prefix"),
                ]);
                LogCause {
                    class: "OSError".into(),
                    message,
                    errno: None,
                    winerror: None,
                    path: None,
                    filename: None,
                    filename2: None,
                    reason: None,
                    source: None,
                    os_error: None,
                }
            }
        }
    }
    pub(super) fn rotation_log(
        &self,
        where_: &[u32],
        recorded: &[u32],
        notice: RotationNotice,
    ) -> KeyLog {
        let mut args = vec![where_.to_vec()];
        let fp = &recorded[..recorded.len().min(16)];
        let (level, format, cause) = match notice {
            RotationNotice::Pending(e) => {
                args.push(self.file(".key.pending"));
                (
                    LogLevel::Warning,
                    "store %s: the pending key %s cannot be read: %s",
                    Some(self.rotation_cause(e)),
                )
            }
            RotationNotice::Failed(e) => {
                args.push(fp.to_vec());
                (LogLevel::Warning,"store %s: the rotated key %s could not be made current (%s); the next open tries again",Some(self.rotation_cause(e)))
            }
            RotationNotice::Finished => {
                args.push(fp.to_vec());
                (
                    LogLevel::Info,
                    "store %s: the key %s a rotation committed is now the current key",
                    None,
                )
            }
        };
        if let Some(e) = &cause {
            args.push(e.message.clone());
        }
        KeyLog {
            level,
            format,
            args,
            cause: cause.map(Arc::new),
        }
    }
}
