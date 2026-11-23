//! The version decisions of a reader and the diagnostics retained by a writer.
use super::reads::{failure_of, sqlite_error_name};
use super::{meta_get, SCHEMA_REVISION};
use crate::config::{pure_path, Flavour};
use crate::fsutil::fsdecode;
use crate::pyvalue::{repr_str, PyStr};
use crate::unicode::cps;
use std::path::Path;

/// A String-facing API can carry scalar text; exact callers use the code points.
pub(super) fn legacy_text(text: &[u32]) -> String {
    text.iter()
        .map(|&c| char::from_u32(c).unwrap_or(char::REPLACEMENT_CHARACTER))
        .collect()
}

/// The spelling of a supplied filesystem path after Python's Path constructor.
pub fn path_text(path: &Path) -> PyStr {
    let raw = fsdecode(path.as_os_str());
    let flavour = if cfg!(windows) {
        Flavour::Windows
    } else {
        Flavour::Posix
    };
    // PurePath drops separators and dot components, preserving all non-ASCII
    // characters in order. Give each one a placeholder during lexical parsing,
    // then put the original code points (including unpaired surrogates) back.
    if let Some(s) = crate::unicode::to_string(&raw) {
        return cps(&pure_path(&s, flavour));
    }
    let masked: String = raw
        .iter()
        .map(|&c| {
            if c <= 127 {
                c as u8 as char
            } else {
                char::REPLACEMENT_CHARACTER
            }
        })
        .collect();
    let mut opaque = raw.iter().copied().filter(|&c| c > 127);
    pure_path(&masked, flavour)
        .chars()
        .map(|c| {
            if c == char::REPLACEMENT_CHARACTER {
                opaque
                    .next()
                    .expect("PurePath preserves every non-ASCII character")
            } else {
                u32::from(c)
            }
        })
        .collect()
}

/// An actual exception from SQLite, including its optional extended result code.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SqliteDiagnostic {
    pub class: &'static str,
    pub message: PyStr,
    pub code: Option<i64>,
}

impl SqliteDiagnostic {
    pub fn from_error(error: &rusqlite::Error) -> Option<Self> {
        let (class, code) = failure_of(error)?;
        let message = match error {
            rusqlite::Error::SqlInputError { msg, .. } => cps(msg),
            other => cps(&other.to_string()),
        };
        Some(Self {
            class,
            message,
            code,
        })
    }

    pub fn name(&self) -> Option<&'static str> {
        self.code.and_then(sqlite_error_name)
    }
}

/// `store_versions.StoreVersionError`. The message field is its legacy scalar
/// projection of the exact source diagnostic.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoreVersionError {
    pub message: String,
    pub key: Option<&'static str>,
    pub needed: Option<i64>,
}

/// Exact text and cause before projecting an error for the scalar interface.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VersionDiagnostic {
    pub exact_message: PyStr,
    pub key: Option<&'static str>,
    pub needed: Option<i64>,
    pub cause: Option<Box<SqliteDiagnostic>>,
}

impl VersionDiagnostic {
    pub(super) fn new(
        exact_message: PyStr,
        key: Option<&'static str>,
        needed: Option<i64>,
    ) -> Self {
        Self {
            exact_message,
            key,
            needed,
            cause: None,
        }
    }

    pub fn legacy(&self) -> StoreVersionError {
        StoreVersionError {
            message: legacy_text(&self.exact_message),
            key: self.key,
            needed: self.needed,
        }
    }
}

/// The original error category and the precise version exception, if one was raised.
#[derive(Debug)]
pub struct DiagnosticError<E> {
    pub error: E,
    pub version: Option<Box<VersionDiagnostic>>,
}

impl<E> DiagnosticError<E> {
    pub(super) fn version(
        error: VersionDiagnostic,
        wrap: impl FnOnce(StoreVersionError) -> E,
    ) -> Self {
        Self {
            error: wrap(error.legacy()),
            version: Some(Box::new(error)),
        }
    }
}

impl<E> From<E> for DiagnosticError<E> {
    fn from(error: E) -> Self {
        Self {
            error,
            version: None,
        }
    }
}

/// `store_versions.VersionVerdict` for a reader's open.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Verdict {
    pub read_only: Option<String>,
    pub revision: i64,
    pub min_reader: i64,
    pub min_writer: i64,
}

/// The reader verdict together with the unquoted source reason.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VersionVerdict {
    pub verdict: Verdict,
    pub read_only_points: Option<PyStr>,
}

pub(super) fn shown(text: &str) -> String {
    let repr = repr_str(&cps(text));
    let inner = &repr[1..repr.len() - 1];
    let mut out = inner[..inner.len().min(64)].to_vec();
    if inner.len() > 64 {
        out.truncate(61);
        out.extend(cps("..."));
    }
    crate::json::utf8_text(&out)
}

/// No decoding or formatting of the path on the normal write-transaction path.
pub(super) fn stored_revision(
    raw: Option<&str>,
    key: &'static str,
    path: impl FnOnce() -> PyStr,
) -> Result<i64, VersionDiagnostic> {
    let Some(raw) = raw else { return Ok(0) };
    if (1..=9).contains(&raw.len()) && raw.bytes().all(|c| c.is_ascii_digit()) {
        return Ok(raw.parse::<i64>().expect("at most nine ASCII digits"));
    }
    let mut message = path();
    let detail = shown(raw);
    message.extend(cps(&format!(
        " carries a {key} that is not a whole number ('{detail}'); the store is not opened: \
         set meta's {key} back to a whole number, or restore the store from a backup taken before the edit"
    )));
    Err(VersionDiagnostic::new(message, Some(key), None))
}

/// Existing scalar reader interface. `path` is the already displayed Path.
pub fn check_versions(
    meta: &[(String, String)],
    path: &str,
    version: &str,
) -> Result<Verdict, StoreVersionError> {
    check_versions_points(meta, &cps(path), version)
        .map(|v| v.verdict)
        .map_err(|e| e.legacy())
}

/// The same decisions and key order, with Python's exact text for the path.
pub fn check_versions_points(
    meta: &[(String, String)],
    path: &[u32],
    version: &str,
) -> Result<VersionVerdict, VersionDiagnostic> {
    let revision = |key| stored_revision(meta_get(meta, key), key, || path.to_vec());
    let revision_number = revision("schema_revision")?;
    let min_reader = revision("min_reader_revision")?;
    let min_writer = revision("min_writer_revision")?;
    let newest = || {
        meta_get(meta, "engine_version")
            .filter(|v| !v.is_empty())
            .map_or_else(String::new, |engine| {
                format!(" (its newest writer was bettermemory {})", shown(engine))
            })
    };
    if min_reader > SCHEMA_REVISION {
        let mut message = path.to_vec();
        let newest = newest();
        message.extend(cps(&format!(
            " needs a reader at schema revision {min_reader} or later (meta's min_reader_revision)\
             {newest}; this build, bettermemory {version}, reads revision {SCHEMA_REVISION}: \
             upgrade bettermemory to open it"
        )));
        return Err(VersionDiagnostic::new(
            message,
            Some("min_reader_revision"),
            Some(min_reader),
        ));
    }
    let read_only_points = (min_writer > SCHEMA_REVISION).then(|| {
        let mut message = path.to_vec();
        let newest = newest();
        message.extend(cps(&format!(
            " is open for reading only: it needs a writer at schema revision {min_writer} or later\
             {newest}, and this build, bettermemory {version}, writes revision {SCHEMA_REVISION}: \
             upgrade bettermemory to write to it"
        )));
        message
    });
    Ok(VersionVerdict {
        verdict: Verdict {
            read_only: read_only_points.as_deref().map(legacy_text),
            revision: revision_number,
            min_reader,
            min_writer,
        },
        read_only_points,
    })
}
