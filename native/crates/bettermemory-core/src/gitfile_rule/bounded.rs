//! The bounded reads of recorded paths (`location_reads.py`), with the
//! time bound as data.
//!
//! The reference runs each read on a worker thread and waits on it for a
//! budget; a path that does not answer in time reads `UNANSWERED`, and
//! while that read is in flight every later read of the path answers at
//! once. Here no clock, timer or thread decides: whether a path answers is
//! the file system's to say ([`Answers`]), and a path that does not answer
//! reads [`Read::Unanswered`] for every read of it, as each later read of
//! a stalled path does in the reference. The reference's `what` (one
//! flight per path and reading) only shares a read in flight, so a path's
//! answer is the same for every `what` and it is not taken here.
//!
//! [`StdFs`] answers always: the machine's reads block where the OS
//! blocks, and a later unit that reads recorded paths on the machine
//! bounds them itself (native/README.md). [`MemFs`] answers always too;
//! [`Hung`] names the paths that do not.

use std::collections::BTreeSet;

use crate::fsdata::{MemFs, StdFs};

/// Whether a read of a path answers within its budget.
pub trait Answers {
    /// False where a read of `path` would not answer in time.
    fn answers(&self, path: &[u8]) -> bool;
}

impl Answers for StdFs {
    fn answers(&self, _: &[u8]) -> bool {
        true
    }
}

impl Answers for MemFs {
    fn answers(&self, _: &[u8]) -> bool {
        true
    }
}

/// The paths, by their exact bytes, that do not answer.
#[derive(Clone, Debug, Default)]
pub struct Hung(pub BTreeSet<Vec<u8>>);

impl Answers for Hung {
    fn answers(&self, path: &[u8]) -> bool {
        !self.0.contains(path)
    }
}

/// What one bounded read gives.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Read<T> {
    /// The read's value.
    Value(T),
    /// `UNANSWERED`: the path did not answer within the budget.
    Unanswered,
    /// `NOT_READ`: the read raised (`bounded_reads` without `raises`).
    NotRead,
}

/// `bounded_read(path, what, read, budget)`: the value of `read`, run
/// only where `path` answers; [`Read::Unanswered`] where it does not; an
/// error `read` gives is passed on, as the reference raises it.
pub fn bounded_read<A: Answers + ?Sized, T, E>(
    answers: &A,
    path: &[u8],
    read: impl FnOnce() -> Result<T, E>,
) -> Result<Read<T>, E> {
    if !answers.answers(path) {
        return Ok(Read::Unanswered);
    }
    read().map(Read::Value)
}

/// One job of [`bounded_reads`]: a path and the read of it.
pub type Job<'a, T, E> = (Vec<u8>, Box<dyn FnOnce() -> Result<T, E> + 'a>);

/// `bounded_reads(jobs, budget, raises=...)`: each job's value,
/// [`Read::Unanswered`] where its path does not answer, [`Read::NotRead`]
/// where the read gave an error; with `raises`, the first such error in
/// job order, once every job is read.
pub fn bounded_reads<A: Answers + ?Sized, T, E>(
    answers: &A,
    jobs: Vec<Job<'_, T, E>>,
    raises: bool,
) -> Result<Vec<Read<T>>, E> {
    let mut out = Vec::with_capacity(jobs.len());
    let mut first: Option<E> = None;
    for (path, read) in jobs {
        if !answers.answers(&path) {
            out.push(Read::Unanswered);
            continue;
        }
        match read() {
            Ok(value) => out.push(Read::Value(value)),
            Err(error) => {
                if first.is_none() {
                    first = Some(error);
                }
                out.push(Read::NotRead);
            }
        }
    }
    match first {
        Some(error) if raises => Err(error),
        _ => Ok(out),
    }
}
