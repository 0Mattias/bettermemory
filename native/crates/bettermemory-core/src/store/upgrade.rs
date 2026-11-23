//! `store_upgrade`: `TRANSACTION_WRITER_REVISION` and `_prove_exclusive`,
//! the proof that no other connection holds a store open before a writer
//! older connections cannot gate is introduced. `quiescent`, which takes
//! the restore lock around the proof, is left for a later unit.

use rusqlite::{ffi, Connection};

use super::versions::{path_text, DiagnosticError, SqliteDiagnostic, VersionDiagnostic};
use crate::printable::quoted;
use crate::store::StoreVersionError;
use crate::unicode::cps;
use std::path::Path;

/// `TRANSACTION_WRITER_REVISION`: the first build that rechecks the writer
/// minimum within its write lock.
pub const TRANSACTION_WRITER_REVISION: i64 = 5;

/// What `_prove_exclusive` raises.
#[derive(Debug)]
pub enum UpgradeError {
    /// `StoreVersionError`: an `OperationalError` inside the proof.
    Version(StoreVersionError),
    /// Any other error, with the `sqlite3` class Python gives it.
    Sqlite {
        class: &'static str,
        error: rusqlite::Error,
    },
}

/// The `sqlite3` exception class `_sqlite3` raises for an error
/// (`_pysqlite_seterror`, by the primary result code).
pub fn sqlite_class(error: &rusqlite::Error) -> &'static str {
    let rusqlite::Error::SqliteFailure(e, _) = error else {
        return "ProgrammingError";
    };
    match e.extended_code & 0xff {
        ffi::SQLITE_INTERNAL | ffi::SQLITE_NOTFOUND => "InternalError",
        ffi::SQLITE_NOMEM => "MemoryError",
        ffi::SQLITE_ERROR
        | ffi::SQLITE_PERM
        | ffi::SQLITE_ABORT
        | ffi::SQLITE_BUSY
        | ffi::SQLITE_LOCKED
        | ffi::SQLITE_READONLY
        | ffi::SQLITE_INTERRUPT
        | ffi::SQLITE_IOERR
        | ffi::SQLITE_FULL
        | ffi::SQLITE_CANTOPEN
        | ffi::SQLITE_PROTOCOL
        | ffi::SQLITE_EMPTY
        | ffi::SQLITE_SCHEMA => "OperationalError",
        ffi::SQLITE_TOOBIG => "DataError",
        ffi::SQLITE_CONSTRAINT | ffi::SQLITE_MISMATCH => "IntegrityError",
        ffi::SQLITE_MISUSE | ffi::SQLITE_RANGE => "InterfaceError",
        _ => "DatabaseError",
    }
}

fn raw(error: rusqlite::Error) -> UpgradeError {
    UpgradeError::Sqlite {
        class: sqlite_class(&error),
        error,
    }
}

/// `conn.execute(sql)`: the statement stepped once, as `sqlite3` steps it;
/// the first column of the row it gives, if any.
fn step(conn: &Connection, sql: &str) -> Result<Option<rusqlite::types::Value>, rusqlite::Error> {
    let mut statement = conn.prepare(sql)?;
    let mut rows = statement.query([])?;
    match rows.next()? {
        Some(row) => Ok(Some(row.get(0)?)),
        None => Ok(None),
    }
}

fn int_of(v: Option<rusqlite::types::Value>) -> Result<i64, UpgradeError> {
    match v {
        Some(rusqlite::types::Value::Integer(i)) => Ok(i),
        _ => Err(UpgradeError::Sqlite {
            class: "TypeError",
            error: rusqlite::Error::InvalidQuery,
        }),
    }
}

fn text_of(v: Option<rusqlite::types::Value>) -> Result<String, UpgradeError> {
    match v {
        Some(rusqlite::types::Value::Text(t)) => Ok(t),
        _ => Err(UpgradeError::Sqlite {
            class: "TypeError",
            error: rusqlite::Error::InvalidQuery,
        }),
    }
}

/// The refusal `_prove_exclusive` raises for the store at `path`: the
/// name shown is `str(Path(path))` on this host (doubled and trailing
/// separators and `.` segments dropped, an empty name `.`).
pub fn refusal(path: &str) -> StoreVersionError {
    refusal_at(Path::new(path)).legacy()
}

fn refusal_at(path: &Path) -> VersionDiagnostic {
    let mut message = quoted(&path_text(path));
    message.extend(cps(" needs a newer writer; another connection has it open. Close the daemon and every session using this store, then open it again. No schema step has run."));
    VersionDiagnostic::new(message, None, None)
}

/// `_prove_exclusive(conn, path)`: with no busy wait and the exclusive
/// locking mode, `BEGIN EXCLUSIVE` and `ROLLBACK`; an `OperationalError`
/// among them is the refusal. Then, whatever happened: a transaction
/// still open rolled back, the locking mode and busy timeout put back,
/// and `schema_version` read between them so SQLite drops an exclusive
/// hold. An error there replaces the pending outcome, as a `finally`
/// that raises does.
pub fn prove_exclusive(conn: &Connection, path: &str) -> Result<(), UpgradeError> {
    prove_exclusive_at(conn, Path::new(path)).map_err(|e| e.error)
}

/// The same exclusive proof with the filesystem spelling supplied by its caller.
pub fn prove_exclusive_at(
    conn: &Connection,
    path: &Path,
) -> Result<(), DiagnosticError<UpgradeError>> {
    let timeout = int_of(step(conn, "PRAGMA busy_timeout").map_err(raw)?)?;
    let mode = text_of(step(conn, "PRAGMA locking_mode").map_err(raw)?)?;
    let tried = (|| {
        step(conn, "PRAGMA busy_timeout = 0")?;
        step(conn, "PRAGMA locking_mode = EXCLUSIVE")?;
        step(conn, "BEGIN EXCLUSIVE")?;
        step(conn, "ROLLBACK")?;
        Ok::<(), rusqlite::Error>(())
    })();
    let outcome = match tried {
        Ok(()) => Ok(()),
        Err(e) if sqlite_class(&e) == "OperationalError" => {
            let mut error = refusal_at(path);
            error.cause = SqliteDiagnostic::from_error(&e).map(Box::new);
            Err(DiagnosticError::version(error, UpgradeError::Version))
        }
        Err(e) => Err(raw(e).into()),
    };
    if !conn.is_autocommit() {
        step(conn, "ROLLBACK").map_err(raw)?;
    }
    step(conn, &format!("PRAGMA locking_mode = {mode}")).map_err(raw)?;
    step(conn, "PRAGMA schema_version").map_err(raw)?;
    step(conn, &format!("PRAGMA busy_timeout = {timeout}")).map_err(raw)?;
    outcome
}
