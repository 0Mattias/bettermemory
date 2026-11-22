//! `Pattern.sub`, `subn` and `split` as CPython 3.13.13 answers them (unit
//! pyre-6b): `Modules/_sre/sre.c` `pattern_subx` and `pattern_split`,
//! compared by value with the interpreter. Each runs the scan of
//! `finditer` ([`Regex`]'s `scan`, with its attempt after an empty
//! match), stopped after `count` matches.
//!
//! A replacement is a literal or a function. A literal that holds a
//! backslash is a template in the reference (`re._compile_template`); the
//! engine builds none and refuses it as [`ReplaceError::Template`] before
//! anything else is checked, where `pattern_subx` compiles the template
//! first too.

use std::fmt;

use super::exec::Found;
use super::regex::{CallError, Match, Regex, Subject};

/// Why `sub`, `subn` or `split` answers no value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReplaceError {
    /// The literal holds a backslash: a template, which the engine does
    /// not build.
    Template,
    /// What the scan answers: the type check, `Memory`, `Budget`.
    Call(CallError),
}

impl fmt::Display for ReplaceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ReplaceError::Template => f.write_str("a replacement template is not supported"),
            ReplaceError::Call(e) => e.fmt(f),
        }
    }
}

impl std::error::Error for ReplaceError {}

impl From<CallError> for ReplaceError {
    fn from(e: CallError) -> ReplaceError {
        ReplaceError::Call(e)
    }
}

/// The loop's bound: `while (!count || n < count)`, so 0 is no bound and
/// a negative count finds nothing (after the type check).
fn limit(count: isize) -> usize {
    match count {
        0 => usize::MAX,
        c => usize::try_from(c).unwrap_or(0),
    }
}

/// The plain methods end on an allocation failure as the crate ends.
fn plain<T>(answer: Result<T, ReplaceError>) -> Result<T, ReplaceError> {
    match answer {
        Err(ReplaceError::Call(CallError::Memory)) => {
            std::alloc::handle_alloc_error(std::alloc::Layout::new::<u64>())
        }
        other => other,
    }
}

fn slice<U: Copy>(text: &[U], from: isize, to: isize) -> &[U] {
    text.get(from as usize..to as usize).unwrap_or_default()
}

impl Regex {
    /// `pattern_subx`'s loop: the text between matches kept, each match
    /// replaced by what `with` appends.
    fn replace<U>(
        &self,
        text: &[U],
        count: isize,
        budget: Option<u64>,
        mut with: impl FnMut(&Found, &mut Vec<U>),
    ) -> Result<(Vec<U>, usize), ReplaceError>
    where
        U: Copy,
        [U]: Subject<Piece = Vec<U>>,
    {
        let mut out = Vec::new();
        let mut i = 0isize;
        let mut n = 0usize;
        self.scan(text, 0, isize::MAX, budget, limit(count), |found| {
            out.extend_from_slice(slice(text, i, found.start.max(i)));
            with(&found, &mut out);
            i = found.end;
            n += 1;
        })?;
        let len = isize::try_from(text.len()).map_err(|_| CallError::Memory)?;
        out.extend_from_slice(slice(text, i, len.max(i)));
        Ok((out, n))
    }

    /// `subn(repl, text, count)` for a literal `repl` of the text's type,
    /// with a budget for each run of the matcher.
    pub fn try_subn<U>(
        &self,
        repl: &[U],
        text: &[U],
        count: isize,
        budget: Option<u64>,
    ) -> Result<(Vec<U>, usize), ReplaceError>
    where
        U: Copy + From<u8> + PartialEq,
        [U]: Subject<Piece = Vec<U>>,
    {
        if repl.contains(&U::from(b'\\')) {
            return Err(ReplaceError::Template);
        }
        self.replace(text, count, budget, |_, out| out.extend_from_slice(repl))
    }

    /// `subn(function, text, count)`: each match handed to `function` as a
    /// [`Match`] over the whole text, its answer appended, `None` as
    /// nothing (`pattern_subx` skips `None`).
    pub fn try_subn_with<U, F>(
        &self,
        mut function: F,
        text: &[U],
        count: isize,
        budget: Option<u64>,
    ) -> Result<(Vec<U>, usize), ReplaceError>
    where
        U: Copy,
        [U]: Subject<Piece = Vec<U>>,
        F: FnMut(&Match) -> Option<Vec<U>>,
    {
        let len = isize::try_from(text.len()).map_err(|_| CallError::Memory)?;
        self.replace(text, count, budget, |found, out| {
            if let Some(piece) = function(&self.wrap(found, len, 0, isize::MAX)) {
                out.extend(piece);
            }
        })
    }

    /// `split(text, maxsplit)`, with a budget: the text before each match,
    /// then each group (`None` where it took no part), then the rest.
    pub fn try_split<U>(
        &self,
        text: &[U],
        maxsplit: isize,
        budget: Option<u64>,
    ) -> Result<Vec<Option<Vec<U>>>, ReplaceError>
    where
        U: Copy,
        [U]: Subject<Piece = Vec<U>>,
    {
        let mut out = Vec::new();
        let mut last = 0isize;
        let groups = self.groups();
        self.scan(text, 0, isize::MAX, budget, limit(maxsplit), |found| {
            out.push(Some(slice(text, last, found.start.max(last)).to_vec()));
            for g in 0..groups {
                let a = found.marks.get(2 * g).copied().unwrap_or(-1);
                let b = found.marks.get(2 * g + 1).copied().unwrap_or(-1);
                out.push((a >= 0 && b >= 0).then(|| slice(text, a, b.max(a)).to_vec()));
            }
            last = found.end;
        })?;
        let len = isize::try_from(text.len()).map_err(|_| CallError::Memory)?;
        out.push(Some(slice(text, last, len.max(last)).to_vec()));
        Ok(out)
    }

    /// `subn` with a literal, no budget.
    pub fn subn<U>(
        &self,
        repl: &[U],
        text: &[U],
        count: isize,
    ) -> Result<(Vec<U>, usize), ReplaceError>
    where
        U: Copy + From<u8> + PartialEq,
        [U]: Subject<Piece = Vec<U>>,
    {
        plain(self.try_subn(repl, text, count, None))
    }

    /// `sub` with a literal: `subn` without the count.
    pub fn sub<U>(&self, repl: &[U], text: &[U], count: isize) -> Result<Vec<U>, ReplaceError>
    where
        U: Copy + From<u8> + PartialEq,
        [U]: Subject<Piece = Vec<U>>,
    {
        self.subn(repl, text, count).map(|(out, _)| out)
    }

    /// `subn` with a function, no budget.
    pub fn subn_with<U, F>(
        &self,
        function: F,
        text: &[U],
        count: isize,
    ) -> Result<(Vec<U>, usize), ReplaceError>
    where
        U: Copy,
        [U]: Subject<Piece = Vec<U>>,
        F: FnMut(&Match) -> Option<Vec<U>>,
    {
        plain(self.try_subn_with(function, text, count, None))
    }

    /// `sub` with a function.
    pub fn sub_with<U, F>(
        &self,
        function: F,
        text: &[U],
        count: isize,
    ) -> Result<Vec<U>, ReplaceError>
    where
        U: Copy,
        [U]: Subject<Piece = Vec<U>>,
        F: FnMut(&Match) -> Option<Vec<U>>,
    {
        self.subn_with(function, text, count).map(|(out, _)| out)
    }

    /// `split` with no budget.
    pub fn split<U>(&self, text: &[U], maxsplit: isize) -> Result<Vec<Option<Vec<U>>>, ReplaceError>
    where
        U: Copy,
        [U]: Subject<Piece = Vec<U>>,
    {
        plain(self.try_split(text, maxsplit, None))
    }
}
