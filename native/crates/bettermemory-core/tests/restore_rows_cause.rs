//! `RestoreError::cause`: what `settle`'s `except (sqlite3.Error,
//! OSError, RuntimeError, ValueError)` catches, as the class it logs, and
//! what it lets raise. Run on every target: the Windows lock's
//! `TimeoutError` (`_flock_windows`' 30 s wait) is reached only there,
//! and the reference's `settle` logs it, TimeoutError being an OSError.

use bettermemory_core::fsutil::{FlockError, FlockTimeout, LockPathError};
use bettermemory_core::pyvalue::PyError;
use bettermemory_core::store::restore_rows::RestoreError;
use bettermemory_core::store::write::WriteError;

fn logged(error: &RestoreError) -> Option<String> {
    error.cause().map(|c| c.class)
}

#[test]
fn the_lock_errors_are_logged_by_their_class() {
    let timeout = RestoreError::Lock(FlockError::Timeout(FlockTimeout("lock".to_owned())));
    assert_eq!(logged(&timeout).as_deref(), Some("TimeoutError"));
    let path = RestoreError::Lock(FlockError::Path(LockPathError("name".to_owned())));
    assert_eq!(logged(&path).as_deref(), Some("ValueError"));
    let denied = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
    let io = RestoreError::Lock(FlockError::Io(denied));
    assert_eq!(logged(&io).as_deref(), Some("PermissionError"));
}

#[test]
fn every_class_of_the_four_bases_is_logged_and_no_other() {
    let caught = [
        // sqlite3.Error and its subclasses.
        "Error",
        "DatabaseError",
        "OperationalError",
        "IntegrityError",
        "ProgrammingError",
        "InterfaceError",
        "DataError",
        "NotSupportedError",
        "InternalError",
        // OSError and its subclasses a write can raise.
        "OSError",
        "FileNotFoundError",
        "FileExistsError",
        "PermissionError",
        "IsADirectoryError",
        "NotADirectoryError",
        "BlockingIOError",
        "InterruptedError",
        "TimeoutError",
        "KeyUnreadableError",
        "UnsupportedOperation",
        // RuntimeError and its subclasses.
        "RuntimeError",
        "RecursionError",
        "NotImplementedError",
        "PythonFinalizationError",
        // ValueError and its subclasses.
        "ValueError",
        "UnicodeError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "JSONDecodeError",
        "StoreVersionError",
    ];
    for class in caught {
        let refused = RestoreError::Write(WriteError::Refused { class, text: None });
        assert_eq!(logged(&refused).as_deref(), Some(class), "{class}");
    }
    for class in [
        "TypeError",
        "KeyError",
        "OverflowError",
        "AttributeError",
        "MemoryError",
    ] {
        let refused = RestoreError::Write(WriteError::Refused { class, text: None });
        assert_eq!(logged(&refused), None, "{class}");
    }
}

#[test]
fn the_python_errors_raise_or_are_logged_as_the_reference_does() {
    for (error, class) in [
        (PyError::RecursionError, Some("RecursionError")),
        (PyError::ValueError, Some("ValueError")),
        (PyError::UnicodeDecodeError, Some("UnicodeDecodeError")),
        (PyError::OverflowError, None),
        (PyError::TypeError, None),
    ] {
        assert_eq!(
            logged(&RestoreError::Py(error)).as_deref(),
            class,
            "{error:?}"
        );
    }
    assert_eq!(
        logged(&RestoreError::NotUtf8).as_deref(),
        Some("OperationalError")
    );
}
