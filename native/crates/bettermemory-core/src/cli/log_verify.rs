//! `cli/log_cmd.py::_cli_log_verify` on the host: the anchors read before
//! the store, the store selected by the configuration the daemon loads and
//! opened as `Store.open(path, read_only=True)` opens it, the chain and the
//! fold in one report, and the report written after the store closed.
//!
//! What the reference writes through `logging` reaches stderr by its
//! handler of last resort, one line per record at WARNING or above; the
//! records are written here in the order they were made, before the
//! command's own last line.
//!
//! Where the reference ends in an exception no caller catches, Python
//! prints a traceback and exits 1. This build prints one line that names
//! the exception and exits 1: the status is the reference's, the text is
//! not.

use std::path::{Path, PathBuf};

use super::common::read_only_path::{PathError, PathGuards, PathWarning, WarningError};
use super::common::{self, ReadOnlyError, ReaderFailure};
use super::log_cmd::VerifyArgs;
use super::log_report::Report;
use super::root::Formatted;
use super::{Exit, UsageError};
use crate::anchor::{self, Anchor, AnchorError};
use crate::argparse::Outcome;
use crate::fsutil::fsdecode;
use crate::gitfile_rule::follow::fsencode;
use crate::lexicon;
use crate::log::KeyRing;
use crate::pyvalue::{PyStr, PyValue};
use crate::recall_prompt::PyException;
use crate::store::exception::StoreException;
use crate::store::key_directory;
use crate::store::key_read::{KeyLog, LogLevel, ReadError, ReaderRing};
use crate::store::key_state::StateError;
use crate::store::keys::load_key_ring;
use crate::store::restore_lock::RESTORE_RUNNING_WHY;
use crate::store::{
    sqlite_text_error, stored_tokenizer, upgrade, OpenCheckError, Stepped, StoreError, StoreFile,
    RESOLVE_STEP,
};
use crate::unicode::{self, cps};

/// The exception classes `open_read_only` catches as "cannot read the
/// store": ValueError, sqlite3.DatabaseError and OSError, by inheritance.
fn caught_at_open(class: &str) -> bool {
    matches!(
        class,
        "ValueError"
            | "UnicodeDecodeError"
            | "UnicodeEncodeError"
            | "UnicodeError"
            | "JSONDecodeError"
            | "TOMLDecodeError"
            | "StoreRefusedError"
            | "LaterStoreError"
            | "StoreVersionError"
            | "DatabaseError"
            | "OperationalError"
            | "IntegrityError"
            | "InternalError"
            | "ProgrammingError"
            | "NotSupportedError"
            | "DataError"
            | "OSError"
            | "FileNotFoundError"
            | "FileExistsError"
            | "PermissionError"
            | "NotADirectoryError"
            | "IsADirectoryError"
            | "InterruptedError"
            | "BlockingIOError"
            | "TimeoutError"
            | "ConnectionError"
            | "BrokenPipeError"
            | "ConnectionAbortedError"
            | "ConnectionRefusedError"
            | "ConnectionResetError"
            | "ProcessLookupError"
            | "ChildProcessError"
            | "KeyUnreadableError"
    )
}

fn exception(class: &str, message: PyStr) -> PyException {
    PyException {
        class: class.into(),
        message,
    }
}

fn classed(error: PyException) -> ReaderFailure<PyException> {
    if caught_at_open(&error.class) {
        ReaderFailure::Unreadable(error)
    } else {
        ReaderFailure::Raised(error)
    }
}

/// A store error as the exception the reference raises at the same step.
fn store_exception(error: StoreError, path: &Path, version: &str) -> ReaderFailure<PyException> {
    let name = fsdecode(path.as_os_str());
    match error {
        StoreError::RestoreRunning => ReaderFailure::RestoreRunning(exception(
            "RestoreRunningError",
            [name, cps(" is not opened: "), cps(RESTORE_RUNNING_WHY)].concat(),
        )),
        StoreError::Admission(refusal) => {
            let shown =
                unicode::to_string(&name).unwrap_or_else(|| path.to_string_lossy().into_owned());
            match &refusal.reference_raises {
                Some(message) => {
                    ReaderFailure::Unreadable(exception("OperationalError", cps(message)))
                }
                None => ReaderFailure::Unreadable(exception(
                    "StoreRefusedError",
                    cps(&refusal.message(&shown, version)),
                )),
            }
        }
        StoreError::Sqlite(error) => ReaderFailure::Unreadable(exception(
            upgrade::sqlite_class(&error),
            cps(&sqlite_text(&error)),
        )),
        StoreError::Io(error) => io_exception(&error, path, &name),
        // `connect_read_only` gives up after its last copy
        // (`sqlite3.OperationalError`, `store_schema.py` 1558).
        StoreError::Changed => ReaderFailure::Unreadable(exception(
            "OperationalError",
            [
                name,
                cps(" changed every time it was copied to be read; run the command again"),
            ]
            .concat(),
        )),
        StoreError::Refused(message) => {
            ReaderFailure::Unreadable(exception("ValueError", cps(&message)))
        }
        StoreError::Version(error) => {
            ReaderFailure::Unreadable(exception("StoreVersionError", cps(&error.message)))
        }
        StoreError::NotUtf8 { .. } => ReaderFailure::Unreadable(exception(
            "OperationalError",
            cps(&StoreError::to_string(&error)),
        )),
        // What the reference raises reading a value (TypeError,
        // RecursionError): caught at the open or not by its class.
        StoreError::Py(raised) => classed(exception(raised.name(), Vec::new())),
        other => ReaderFailure::Raised(exception("Exception", cps(&other.to_string()))),
    }
}

/// An I/O failure of the open as the OSError the reference raises there.
/// A step on a named file (the store's `-wal` that cannot be copied) is
/// that file's error, `[Errno N] words: 'file'`; the store file gone
/// between the command's stat and the open is `Store.open`'s "no store
/// at"; any other step keeps its own words, still an OSError the command
/// catches.
fn io_exception(error: &std::io::Error, path: &Path, name: &[u32]) -> ReaderFailure<PyException> {
    let gone = || {
        ReaderFailure::Unreadable(exception(
            "FileNotFoundError",
            [cps("no store at "), name.to_vec()].concat(),
        ))
    };
    let os = |error: &std::io::Error, at: &Path| {
        ReaderFailure::Unreadable(StoreException::os(error, Some(at), None).exception)
    };
    let not_found = error.kind() == std::io::ErrorKind::NotFound;
    let stepped = error
        .get_ref()
        .and_then(|inner| inner.downcast_ref::<Stepped>());
    match stepped {
        Some(step) if step.what == RESOLVE_STEP && not_found => gone(),
        Some(Stepped {
            errno: Some(code),
            path: Some(at),
            ..
        }) => os(&std::io::Error::from_raw_os_error(*code), at),
        Some(_) => ReaderFailure::Unreadable(exception("OSError", cps(&error.to_string()))),
        None if error.raw_os_error().is_some() => os(error, path),
        None if not_found => gone(),
        None => ReaderFailure::Unreadable(exception("OSError", cps(&error.to_string()))),
    }
}

/// `store_index._tell_drift`'s record for an open that does not re-spell
/// the index: the store, the tokenizer its log names, this build's, why.
const TOKENIZER_DRIFT: &str = "store %s: its search index is spelled by tokenizer %s and this \
build's is %s; this open does not re-spell it (%s), so a search may miss memories until the \
owner's next writing open does (the daemon's start, any writing command, or `bettermemory index \
rebuild`)";

/// pysqlite's OperationalError for the text `stored_fingerprint` fetched
/// that is not UTF-8, in the order it reads: the newest `index_rebuilt`
/// row's payload, the `store_created` row's, meta's copy.
fn stamp_text_error(conn: &rusqlite::Connection) -> Option<String> {
    use rusqlite::types::ValueRef;
    let undecodable = |raw: ValueRef<'_>, column: &str| match raw {
        ValueRef::Text(raw) if std::str::from_utf8(raw).is_err() => {
            Some(sqlite_text_error(column, raw))
        }
        _ => None,
    };
    for kind in ["index_rebuilt", "store_created"] {
        let mut statement = conn
            .prepare("SELECT seq, payload FROM log WHERE kind = ? ORDER BY seq DESC LIMIT 1")
            .ok()?;
        let mut rows = statement.query([kind]).ok()?;
        if let Ok(Some(row)) = rows.next() {
            if let Some(message) = undecodable(row.get_ref(1).ok()?, "payload") {
                return Some(message);
            }
        }
    }
    let mut statement = conn.prepare("SELECT value FROM meta WHERE key = ?").ok()?;
    let mut rows = statement.query(["tokenizer_fingerprint"]).ok()?;
    match rows.next() {
        Ok(Some(row)) => undecodable(row.get_ref(0).ok()?, "value"),
        _ => None,
    }
}

/// pysqlite's OperationalError for the first text of the meta table that
/// is not UTF-8, in the order `read_store_meta` fetches it.
fn meta_text_error(conn: &rusqlite::Connection) -> Option<String> {
    let mut statement = conn.prepare("SELECT key, value FROM meta").ok()?;
    let mut rows = statement.query([]).ok()?;
    while let Ok(Some(row)) = rows.next() {
        for (index, column) in ["key", "value"].into_iter().enumerate() {
            if let Ok(rusqlite::types::ValueRef::Text(raw)) = row.get_ref(index) {
                if std::str::from_utf8(raw).is_err() {
                    return Some(sqlite_text_error(column, raw));
                }
            }
        }
    }
    None
}

/// `str(exc)` of a sqlite3 exception: SQLite's message alone.
fn sqlite_text(error: &rusqlite::Error) -> String {
    match error {
        // rusqlite adds the name it opened to SQLite's words.
        rusqlite::Error::SqliteFailure(code, Some(_))
            if code.code == rusqlite::ffi::ErrorCode::CannotOpen =>
        {
            "unable to open database file".to_owned()
        }
        rusqlite::Error::SqliteFailure(_, Some(message)) => message.clone(),
        rusqlite::Error::SqliteFailure(code, None) => code.to_string(),
        rusqlite::Error::SqlInputError { msg, .. } => msg.clone(),
        other => other.to_string(),
    }
}

/// The store as `_open_connected` leaves a read-only open: admitted, its
/// meta checked, its key ring found and its key state read once.
struct Reader {
    store: StoreFile,
    /// `str(store._path)`.
    shown: PyStr,
    store_id: String,
    keys_dir: PathBuf,
}

fn meta_value<'a>(meta: &'a [(String, String)], key: &str) -> &'a str {
    meta.iter()
        .find(|(k, _)| k == key)
        .map_or("", |(_, v)| v.as_str())
}

fn open_reader(
    path: &Path,
    version: &str,
    logged: &mut PyStr,
) -> Result<Reader, ReaderFailure<PyException>> {
    let shown = fsdecode(path.as_os_str());
    let store = StoreFile::open(path).map_err(|e| store_exception(e, path, version))?;
    let meta = store.meta().map_err(|e| match e {
        StoreError::NotUtf8 { .. } => match meta_text_error(store.connection()) {
            Some(message) => {
                ReaderFailure::Unreadable(exception("OperationalError", cps(&message)))
            }
            None => store_exception(e, path, version),
        },
        e => store_exception(e, path, version),
    })?;
    let checked = store
        .check_opened_as(&meta, &shown, version)
        .map_err(|error| match error {
            OpenCheckError::ValueError(message) => {
                ReaderFailure::Unreadable(exception("ValueError", message))
            }
            OpenCheckError::Version(detail) => {
                ReaderFailure::Unreadable(exception("StoreVersionError", detail.exact_message))
            }
            OpenCheckError::Sqlite(error) => {
                store_exception(StoreError::Sqlite(error), path, version)
            }
        })?;
    // `check_versions` tells a store a later writer owns once a process:
    // `log.warning("%s", reason)`.
    if let Some(reason) = &checked.read_only_reason {
        logged.extend_from_slice(reason);
        logged.push(u32::from(b'\n'));
    }
    let store_id = checked.opened.store_id.clone();
    let expected = cps(&checked.opened.key_fingerprint);
    let keys_text = key_directory::resolve_keys_dir(None)
        .map_err(|e| classed(exception(e.class, e.message)))?;
    let ring = ReaderRing::new(&keys_text, &PyValue::str(&store_id))
        .map_err(|_| classed(exception("ValueError", cps("not a store id"))))?;
    let emit = |record: KeyLog| -> Result<(), std::convert::Infallible> {
        if record.level == LogLevel::Warning {
            logged.extend(record.message());
            logged.push(u32::from(b'\n'));
        }
        Ok(())
    };
    ring.key_state_at(store.connection(), path, &shown, &expected, true, emit)
        .map_err(|error| match error {
            ReadError::Path(e) => classed(exception(e.class(), e.message())),
            ReadError::State(StateError::Raised { class, message }) => {
                classed(exception(class, message))
            }
            ReadError::State(StateError::Read(e)) => store_exception(e, path, version),
            ReadError::Logger(never) => match never {},
        })?;
    // `Store.open` is `reconciles_tokenizer`'s: once the open returned a
    // store, the tokenizer the log names is compared with this build's
    // (`settle_tokenizer`). A reader re-spells nothing and says so once.
    // What the read raises fails the open, after the store closed.
    let stored = stored_tokenizer(store.connection()).map_err(|e| match e {
        StoreError::NotUtf8 { .. } => match stamp_text_error(store.connection()) {
            Some(message) => {
                ReaderFailure::Unreadable(exception("OperationalError", cps(&message)))
            }
            None => store_exception(e, path, version),
        },
        e => store_exception(e, path, version),
    })?;
    let live = cps(&lexicon::python_tokenizer_fingerprint());
    if stored.as_deref() != Some(live.as_slice()) {
        let named = match &stored {
            Some(text) if !text.is_empty() => text.clone(),
            _ => cps("none"),
        };
        let lead = |text: &[u32]| text[..text.len().min(16)].to_vec();
        logged.extend(percent(
            TOKENIZER_DRIFT,
            &[
                shown.clone(),
                lead(&named),
                lead(&live),
                cps("it was opened read-only"),
            ],
        ));
        logged.push(u32::from(b'\n'));
    }
    let keys_dir = key_directory::os_path(&keys_text).map_err(|_| {
        ReaderFailure::Unreadable(exception(
            "UnicodeEncodeError",
            cps("the keys directory has no name on this host"),
        ))
    })?;
    Ok(Reader {
        store,
        shown,
        store_id,
        keys_dir,
    })
}

/// The reference's message of a logging record with `%s` fields.
fn percent(format: &str, fields: &[PyStr]) -> PyStr {
    let mut out = Vec::new();
    let mut fields = fields.iter();
    for (index, part) in format.split("%s").enumerate() {
        if index > 0 {
            if let Some(field) = fields.next() {
                out.extend_from_slice(field);
            }
        }
        out.extend(cps(part));
    }
    out
}

/// The one line of an ending the reference leaves to a traceback: the
/// exception's class and message, or what stopped the read.
fn uncaught(mut logged: PyStr, class: &str, message: &[u32]) -> Outcome {
    logged.extend(cps("bettermemory: log verify stopped: "));
    logged.extend(cps(class));
    if !message.is_empty() {
        logged.extend(cps(": "));
        logged.extend_from_slice(message);
    }
    logged.push(u32::from(b'\n'));
    Outcome {
        code: 1,
        stdout: Vec::new(),
        stderr: logged,
    }
}

/// [`uncaught`] for a read of the store that failed after it opened.
fn unread(logged: PyStr, error: &dyn std::fmt::Display) -> Outcome {
    uncaught(
        logged,
        "the store's log could not be read",
        &cps(&error.to_string()),
    )
}

fn exited(mut logged: PyStr, exit: Exit) -> Outcome {
    logged.extend(exit.stderr);
    Outcome {
        code: exit.code,
        stdout: Vec::new(),
        stderr: logged,
    }
}

/// `_cli_log_verify(json_out=, parser=, anchor_files=)` (374-419) for the
/// build `version`. `parser` is the `log` parser.
pub fn verify(args: &VerifyArgs, parser: &Formatted<'_>, version: &str) -> Outcome {
    let mut logged: PyStr = Vec::new();
    // Every anchor before the store: a file that is not one is a usage
    // error, reported before any work and never as a verdict.
    let mut anchors: Vec<(PyStr, Anchor)> = Vec::new();
    for name in &args.anchor_files {
        let Some(bytes) = fsencode(name) else {
            return uncaught(logged, "UnicodeEncodeError", &[]);
        };
        let path = {
            use std::os::unix::ffi::OsStringExt;
            PathBuf::from(std::ffi::OsString::from_vec(bytes))
        };
        match anchor::load_anchors(&path) {
            Ok(found) => anchors.extend(found.into_iter().map(|a| (name.clone(), a))),
            Err(AnchorError::Raised {
                class: "AnchorFileError",
                message,
            }) => return exited(logged, parser.error(&message)),
            Err(AnchorError::Raised { class, message }) => {
                return uncaught(logged, class, &message)
            }
            Err(AnchorError::Store(error)) => {
                return uncaught(logged, "Exception", &cps(&error.to_string()))
            }
        }
    }
    // A config or store file that cannot be read is a log that cannot be
    // verified: exit 1 with the reason.
    let mut guards = PathGuards::default();
    let selected = {
        let mut warn = |warning: PathWarning<'_>| -> Result<(), WarningError<()>> {
            match warning {
                PathWarning::Config(message) => logged.extend_from_slice(message),
                PathWarning::System {
                    source,
                    directory,
                    prefix,
                } => logged.extend(percent(
                    common::read_only_path::SYSTEM_WARNING,
                    &[
                        cps(source),
                        fsdecode(directory.as_os_str()),
                        fsdecode(prefix.as_os_str()),
                    ],
                )),
            }
            logged.push(u32::from(b'\n'));
            Ok(())
        };
        common::read_only_store_path(parser, Some(1), &mut guards, &mut warn)
    };
    let path = match selected {
        Ok(path) => path,
        Err(PathError::Exit(exit)) => return exited(logged, exit),
        Err(PathError::Raised(error)) => return uncaught(logged, error.class, &error.message),
        Err(PathError::Logger(_)) => return uncaught(logged, "Exception", &[]),
    };
    let opened = common::open_read_only(parser, &path, 1, |supplied| {
        open_reader(supplied, version, &mut logged)
    });
    let reader = match opened {
        Ok(reader) => reader,
        Err(ReadOnlyError::Exit(exit)) => return exited(logged, exit),
        Err(ReadOnlyError::Stat(error) | ReadOnlyError::Open(error)) => {
            return uncaught(logged, &error.class, &error.message)
        }
        Err(ReadOnlyError::Bundle(_)) => return uncaught(logged, "Exception", &[]),
    };
    let Reader {
        store,
        shown,
        store_id,
        keys_dir,
    } = reader;
    let mut load = |s: &StoreFile| -> Result<(KeyRing, String), StoreError> {
        let fingerprint = meta_value(&s.meta()?, "key_fingerprint").to_owned();
        let ring = load_key_ring(&keys_dir, &store_id)
            .map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, fingerprint))
    };
    let verified = match store.log_verify_loading(&mut load) {
        Ok(verified) => verified,
        Err(error) => return unread(logged, &error),
    };
    let mut report = Report::from_verify(&verified, &shown);
    match store.history_replaced() {
        Ok(history) => report.add_history(history),
        Err(error) => return unread(logged, &error),
    }
    if !args.anchor_files.is_empty() {
        let merged = anchor::check_anchors(store.connection(), &cps(&store_id), &anchors)
            .and_then(|results| anchor::merge_into_report(&mut report.document, results));
        match merged {
            Ok(()) => {}
            Err(AnchorError::Raised { class, message }) => {
                return uncaught(logged, class, &message)
            }
            Err(AnchorError::Store(error)) => return unread(logged, &error),
        }
    }
    drop(store);
    match report.output(args.json_out) {
        Ok((code, text)) => Outcome {
            code,
            stdout: cps(&text),
            stderr: logged,
        },
        Err(error) => uncaught(logged, "TypeError", &cps(&error.to_string())),
    }
}
