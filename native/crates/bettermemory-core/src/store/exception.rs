//! Exceptions crossing a store clock or diagnostic callback. The catch
//! family is independent of the displayed class name for Python subclasses.

use std::{any::Any, io, path::Path, sync::Arc};

use crate::pyvalue::repr_str;
use crate::recall_prompt::PyException;
use crate::unicode;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExceptionFamily {
    Os,
    Runtime,
    Sqlite,
    Operational,
    Other,
    /// A direct BaseException, outside the Exception hierarchy.
    Base,
}

#[derive(Debug, Clone)]
pub struct StoreException {
    pub exception: PyException,
    pub family: ExceptionFamily,
    pub sqlite_code: Option<i64>,
    pub errno: Option<i32>,
    pub filename: Option<Box<Path>>,
    pub filename2: Option<Box<Path>>,
    // Clones used by structured warnings still name the original exception.
    identity: Arc<Option<Arc<dyn Any + Send + Sync>>>,
}

// The diagnostic comparison used by existing consumers remains structural.
impl PartialEq for StoreException {
    fn eq(&self, other: &Self) -> bool {
        self.exception == other.exception
            && self.family == other.family
            && self.sqlite_code == other.sqlite_code
            && self.errno == other.errno
            && self.filename == other.filename
            && self.filename2 == other.filename2
    }
}
impl Eq for StoreException {}

impl StoreException {
    pub fn new(exception: PyException, family: ExceptionFamily) -> Self {
        Self {
            exception,
            family,
            sqlite_code: None,
            errno: None,
            filename: None,
            filename2: None,
            identity: Arc::new(None),
        }
    }

    /// Python exception identity, independent of the type spelling and message.
    pub fn is_same(&self, other: &Self) -> bool {
        match (&*self.identity, &*other.identity) {
            (Some(a), Some(b)) => Arc::ptr_eq(a, b),
            _ => Arc::ptr_eq(&self.identity, &other.identity),
        }
    }

    /// Attach the original runtime exception for bindings which can rethrow it.
    pub fn with_handle<T: Any + Send + Sync>(mut self, handle: Arc<T>) -> Self {
        self.identity = Arc::new(Some(handle));
        self
    }

    pub fn handle<T: Any + Send + Sync>(&self) -> Option<Arc<T>> {
        self.identity.as_ref().as_ref()?.clone().downcast().ok()
    }

    pub(crate) fn os(error: &io::Error, filename: Option<&Path>, filename2: Option<&Path>) -> Self {
        let class = os_class(error);
        let mut message = unicode::cps(&super::restore_lock::os_error_text(error));
        if let Some(path) = filename.filter(|_| error.raw_os_error().is_some()) {
            message.extend(unicode::cps(": "));
            message.extend(repr_str(&super::restore_lock::path_text(path)));
        }
        if let Some(path) = filename2 {
            message.extend(unicode::cps(" -> "));
            message.extend(repr_str(&super::restore_lock::path_text(path)));
        }
        let mut raised = Self::new(
            PyException {
                class: class.into(),
                message,
            },
            ExceptionFamily::Os,
        );
        raised.errno = error.raw_os_error();
        raised.filename = filename.map(Box::from);
        raised.filename2 = filename2.map(Box::from);
        raised
    }
}

fn os_class(error: &io::Error) -> &'static str {
    #[cfg(unix)]
    if let Some(code) = error.raw_os_error() {
        return bettermemory_sys::python_os_error_class(code);
    }
    match error.kind() {
        io::ErrorKind::AlreadyExists => "FileExistsError",
        io::ErrorKind::NotFound => "FileNotFoundError",
        io::ErrorKind::PermissionDenied => "PermissionError",
        io::ErrorKind::NotADirectory => "NotADirectoryError",
        io::ErrorKind::IsADirectory => "IsADirectoryError",
        io::ErrorKind::BrokenPipe => "BrokenPipeError",
        io::ErrorKind::ConnectionAborted => "ConnectionAbortedError",
        io::ErrorKind::ConnectionRefused => "ConnectionRefusedError",
        io::ErrorKind::ConnectionReset => "ConnectionResetError",
        io::ErrorKind::WouldBlock => "BlockingIOError",
        io::ErrorKind::Interrupted => "InterruptedError",
        io::ErrorKind::TimedOut => "TimeoutError",
        _ => "OSError",
    }
}

impl From<PyException> for StoreException {
    fn from(exception: PyException) -> Self {
        use ExceptionFamily::*;
        let family = match exception.class.as_str() {
            "OSError"
            | "BlockingIOError"
            | "ChildProcessError"
            | "ConnectionError"
            | "BrokenPipeError"
            | "ConnectionAbortedError"
            | "ConnectionRefusedError"
            | "ConnectionResetError"
            | "FileExistsError"
            | "FileNotFoundError"
            | "InterruptedError"
            | "IsADirectoryError"
            | "NotADirectoryError"
            | "PermissionError"
            | "ProcessLookupError"
            | "TimeoutError" => Os,
            "RuntimeError"
            | "NotImplementedError"
            | "RecursionError"
            | "PythonFinalizationError" => Runtime,
            "OperationalError" => Operational,
            "Error" | "DatabaseError" | "DataError" | "IntegrityError" | "InterfaceError"
            | "InternalError" | "NotSupportedError" | "ProgrammingError" => Sqlite,
            "BaseException" | "KeyboardInterrupt" | "SystemExit" | "GeneratorExit"
            | "BaseExceptionGroup" => Base,
            _ => Other,
        };
        Self::new(exception, family)
    }
}
