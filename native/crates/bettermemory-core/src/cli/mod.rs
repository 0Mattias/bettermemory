//! The command line (`cli/`), its first unit: the shared leaves of
//! `cli/_common.py` ([`common`]), and the root parser and `main`'s
//! dispatch of `cli/__init__.py` ([`root`]).

use crate::pyvalue::PyStr;

pub mod common;
pub mod root;

/// `SystemExit(code)` with what the command wrote to stderr before it:
/// the one way the CLI's ported code ends a command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Exit {
    pub code: i32,
    pub stderr: PyStr,
}

/// What `_refuse` needs of an `argparse.ArgumentParser`: its `prog` and
/// `error` (the usage line and the message on stderr, exit 2).
pub trait UsageError {
    fn prog(&self) -> &PyStr;
    fn error(&self, message: &PyStr) -> Exit;
}
