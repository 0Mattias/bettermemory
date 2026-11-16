//! The one reading of an error as nothing at a path
//! (`_location_probe.py` 40-106): `path_gone` and `path_missing`.
//!
//! An error is described as the reference's exception is: a ValueError
//! (a path the OS cannot be asked about), an OSError of a class, an errno
//! and, where CPython set one, a Windows code, or any other exception.
//! [`PyErr`] names a class but not the errno of a plain `OSError` (ELOOP,
//! ENAMETOOLONG and EIO are all [`PyErr::Os`]), so [`Raised::from`] keeps
//! that errno unknown, which reads as "cannot tell"; a caller that needs
//! ELOOP or ENAMETOOLONG read as gone builds [`Raised::Os`] with it.

use crate::fsdata::PyErr;

/// The errnos the reading tells apart (`GONE_ERRNOS`); any other is
/// [`Errno::Other`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Errno {
    Enoent,
    Enotdir,
    Eloop,
    Enametoolong,
    Other,
}

/// An exception a read of a path raised.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `ValueError` (an embedded NUL).
    Value,
    /// An `OSError`: its class ([`PyErr`]; a class it does not name is
    /// [`PyErr::Os`]), its errno and the Windows code CPython set, if any.
    Os {
        class: PyErr,
        errno: Errno,
        winerror: Option<i64>,
    },
    /// Any other exception.
    Other,
}

impl From<PyErr> for Raised {
    /// An error of the file-system trait: ValueError as itself, an
    /// OSError with the errno its class implies (none for a plain one).
    fn from(error: PyErr) -> Raised {
        match error {
            PyErr::Value => Raised::Value,
            class => Raised::Os {
                class,
                errno: match class {
                    PyErr::FileNotFound => Errno::Enoent,
                    PyErr::NotADirectory => Errno::Enotdir,
                    _ => Errno::Other,
                },
                winerror: None,
            },
        }
    }
}

/// `GONE_WINERRORS`: the Windows codes of a path that names nothing.
pub const GONE_WINERRORS: [i64; 6] = [2, 3, 123, 206, 267, 1921];

/// `path_gone(exc)`: whether the error is the OS's answer that nothing is
/// there. A ValueError is; an error with a Windows code is told by that
/// code alone; any other OSError by its class (FileNotFoundError,
/// NotADirectoryError) or its errno (ENOENT, ENOTDIR, ELOOP,
/// ENAMETOOLONG); any other exception cannot tell.
pub fn path_gone(exc: Raised) -> bool {
    match exc {
        Raised::Value => true,
        Raised::Other => false,
        Raised::Os {
            class,
            errno,
            winerror,
        } => match winerror {
            Some(code) => GONE_WINERRORS.contains(&code),
            None => class.missing() || errno != Errno::Other,
        },
    }
}

/// `path_missing(exc)`: a FileNotFoundError or NotADirectoryError that is
/// the OS's answer that nothing is there ([`path_gone`]).
pub fn path_missing(exc: Raised) -> bool {
    matches!(exc, Raised::Os { class, .. } if class.missing()) && path_gone(exc)
}
