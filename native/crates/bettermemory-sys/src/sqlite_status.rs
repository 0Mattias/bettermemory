//! The outstanding SQLite result on a borrowed connection.

use rusqlite::{ffi, Connection, Error};
use std::ffi::CStr;

/// Copy the connection's current error without preparing or executing SQL.
/// CPython checks this state when a parameter fails to convert during binding.
pub fn current_error(connection: &Connection) -> Option<Error> {
    // SAFETY: the borrow keeps the SQLite connection open. Connection is
    // not Sync, and the handle stays here while we copy its error message.
    // No intervening operation on the connection can invalidate the pointer.
    unsafe {
        let db = connection.handle();
        let code = ffi::sqlite3_extended_errcode(db);
        if code == ffi::SQLITE_OK {
            return None;
        }
        let raw = ffi::sqlite3_errmsg(db);
        let message = (!raw.is_null()).then(|| CStr::from_ptr(raw).to_string_lossy().into_owned());
        Some(Error::SqliteFailure(ffi::Error::new(code), message))
    }
}
