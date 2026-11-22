//! `re.Pattern` and `re.Match` as CPython 3.13.13 builds and answers them
//! (unit pyre-6a): a [`Regex`] built from a pattern's text and flags as
//! `re.compile` builds it (`Lib/re/__init__.py` `_compile`, then
//! `_compiler.compile`), and the methods `match`, `fullmatch`, `search`,
//! `finditer` and `findall` with `pos` and `endpos` (`Modules/_sre/sre.c`
//! `pattern_new_match`, `scanner_search`, `pattern_findall`, compared by
//! value with the interpreter). `sub`, `subn` and `split` are unit
//! pyre-6b, in [`super::replace`].
//!
//! A pattern goes through [`parse`] (with the engine's refusals), [`lower`]
//! and [`supports`]; each distinct class is compiled once when the pattern
//! is built. A failure at any stage is a [`RegexError`], never a panic, so
//! a pattern held as a static is a `LazyLock<Result<Regex, RegexError>>`
//! built from the reference's own text.
//!
//! The scans reproduce `_sre`'s `must_advance` with a second copy of the
//! program ([`advance`]), since [`exec`] has no such rule.

use std::collections::hash_map::Entry;
use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

use super::charset::{compile_class, Charset};
use super::exec::{exec, supports, ClassTest, Found, MatchError, Mode, Text};
use super::lower::{lower, Class, Inst, LowerError, Op, Program, Unit};
use super::parse::parse;
use super::tree::{BuildError, Feature, Member};

/// Why a pattern is not built.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RegexError {
    /// `parse`: `re.error`, the OverflowError and ValueError `re` raises,
    /// or the engine's refusal of the tree.
    Build(BuildError),
    /// `lower`: a refusal that depends on the flags, or `_compile`'s
    /// lookbehind errors.
    Lower(LowerError),
    /// A class the class compiler refuses.
    Class(Feature),
    /// A node the matcher does not run yet, by its [`supports`] name.
    Exec(String),
    /// `_sre.compile`'s OverflowError "Python int too large to convert to
    /// C int": the flags, read as a C `int`, past `i32::MAX`. It is raised
    /// after `parse` and `lower` (`_code`), so their errors come first.
    FlagsOverflow,
}

/// Why a call answers no value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CallError {
    /// `_sre`'s TypeError: a `str` pattern on bytes or the reverse.
    Type { str_pattern: bool },
    /// A buffer could not grow (`try_*` only).
    Memory,
    /// The steps of one run passed the budget (`try_*` only).
    Budget,
}

impl fmt::Display for CallError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CallError::Type { str_pattern: true } => {
                f.write_str("cannot use a string pattern on a bytes-like object")
            }
            CallError::Type { str_pattern: false } => {
                f.write_str("cannot use a bytes pattern on a string-like object")
            }
            CallError::Memory => f.write_str("out of memory"),
            CallError::Budget => f.write_str("step budget exceeded"),
        }
    }
}

impl std::error::Error for CallError {}

impl From<MatchError> for CallError {
    fn from(e: MatchError) -> CallError {
        match e {
            MatchError::Memory => CallError::Memory,
            MatchError::Budget => CallError::Budget,
        }
    }
}

/// A text a pattern of its type reads: the code points of a `str`, or
/// bytes. `Piece` is what `findall` hands back for a slice of it.
pub trait Subject: Text {
    const IS_STR: bool;
    type Piece;
    fn piece(&self, from: usize, to: usize) -> Self::Piece;
}

macro_rules! subject {
    ($t:ty, $is_str:expr) => {
        impl Subject for [$t] {
            const IS_STR: bool = $is_str;
            type Piece = Vec<$t>;
            fn piece(&self, from: usize, to: usize) -> Vec<$t> {
                self.get(from..to).unwrap_or_default().to_vec()
            }
        }
    };
}

subject!(u32, true);
subject!(u8, false);

/// `Pattern.pattern`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Source {
    Str(Vec<u32>),
    Bytes(Vec<u8>),
}

/// Every class of a program, compiled once and found by the address of
/// its members (unrolled copies of one set share them) and its flags.
#[derive(Clone, Debug, Default)]
struct Classes(HashMap<(usize, u32), Charset>);

fn key(class: &Class) -> (usize, u32) {
    (
        Arc::as_ptr(&class.members).cast::<Member>() as usize,
        class.flags,
    )
}

impl ClassTest for Classes {
    fn contains(&self, class: &Class, c: u32) -> bool {
        self.0.get(&key(class)).is_some_and(|set| set.contains(c))
    }
}

/// `re.Pattern`.
#[derive(Clone, Debug)]
pub struct Regex {
    source: Source,
    flags: u32,
    groups: usize,
    /// `indexgroup`: a group's name by its number, ascending.
    names: Vec<(usize, Vec<u32>)>,
    prog: Program,
    advance: Program,
    classes: Classes,
}

/// A span `(start, end)` in code units.
pub type Span = (isize, isize);

/// `re.Match`: the clamped window, the span of the match and of each group
/// (`(-1, -1)` for a group that took no part), `lastindex`, `lastgroup`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Match {
    pub pos: isize,
    pub endpos: isize,
    pub regs: Vec<(isize, isize)>,
    pub lastindex: Option<usize>,
    pub lastgroup: Option<Vec<u32>>,
}

impl Match {
    /// `span(g)`; `None` past the last group (`re`'s IndexError).
    pub fn span(&self, g: usize) -> Option<(isize, isize)> {
        self.regs.get(g).copied()
    }

    /// The span of group `g` when it took part: `group(g)` is not `None`.
    pub fn group(&self, g: usize) -> Option<(isize, isize)> {
        self.span(g).filter(|&(a, _)| a >= 0)
    }

    /// `groups(default)`: for each group from 1, its span when it took
    /// part, else `default` (`re`'s default is `None`).
    pub fn groups(&self, default: Option<(isize, isize)>) -> Vec<Option<(isize, isize)>> {
        (1..self.regs.len())
            .map(|g| self.group(g).or(default))
            .collect()
    }

    /// `groupdict(default)` over the pattern's `names`
    /// ([`Regex::names`]): each name, in the order of its group number
    /// as `groupindex` holds them, with what `groups(default)` answers
    /// for its group.
    pub fn groupdict(
        &self,
        names: &[(usize, Vec<u32>)],
        default: Option<(isize, isize)>,
    ) -> Vec<(Vec<u32>, Option<Span>)> {
        names
            .iter()
            .map(|(g, name)| (name.clone(), self.group(*g).or(default)))
            .collect()
    }
}

/// One item of `findall`: the whole match or the one group, or the tuple
/// of every group; a group that took no part is an empty piece.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Item<P> {
    One(P),
    Tuple(Vec<P>),
}

/// The program that answers the attempt after an empty match: copy B is
/// `prog` as it is, copy A (offset by `n`) is `prog` before anything is
/// consumed. In A, `MATCH` fails, a unit leads into B, and a repeat of
/// minimum 0 that consumes is split, in its own order of trial, into the
/// counts from 1 (into B) and the count 0 (staying in A). A lookaround's
/// body stays in B: what it reads is not consumed.
pub fn advance(prog: &Program) -> Program {
    let n = prog.insts.len();
    let mut insts = prog.insts.clone();
    let mut extra = Vec::new();
    for inst in &prog.insts {
        let next = inst.next;
        let (op, next) = match &inst.op {
            Op::Unit(_) => (inst.op.clone(), next),
            Op::Rep1 {
                unit,
                min: 0,
                max,
                greedy,
            } if *max > 0 => {
                let some = 2 * n + extra.len();
                extra.push(Inst {
                    op: Op::Rep1 {
                        unit: unit.clone(),
                        min: 1,
                        max: *max,
                        greedy: *greedy,
                    },
                    next,
                });
                if *greedy {
                    (Op::Split(next + n), some)
                } else {
                    (Op::Split(some), next + n)
                }
            }
            Op::Rep1 { min, .. } if *min > 0 => (inst.op.clone(), next),
            Op::Rep1 { .. } | Op::At(_) | Op::Mark(_) | Op::Look { .. } => {
                (inst.op.clone(), next + n)
            }
            Op::Split(second) => (Op::Split(second + n), next + n),
            Op::Match => (Op::Fail, next),
            Op::Fail | Op::LookEnd => (inst.op.clone(), next),
        };
        insts.push(Inst { op, next });
    }
    insts.extend(extra);
    Program {
        insts,
        start: prog.start + n,
        trace: prog.trace.clone(),
    }
}

/// The plain methods end as every other allocation failure ends.
fn plain<T>(answer: Result<T, CallError>) -> Result<T, CallError> {
    match answer {
        Err(CallError::Memory) => std::alloc::handle_alloc_error(std::alloc::Layout::new::<u64>()),
        other => other,
    }
}

/// The three calls of one attempt or one scan over starts, each as `try_*`
/// with a budget and as the plain method without one.
macro_rules! calls {
    ($($(#[$doc:meta])* $plain:ident, $tried:ident, $mode:expr;)*) => {$(
        $(#[$doc])*
        pub fn $tried<T: Subject + ?Sized>(
            &self,
            text: &T,
            pos: isize,
            endpos: isize,
            budget: Option<u64>,
        ) -> Result<Option<Match>, CallError> {
            self.one(text, pos, endpos, $mode, budget)
        }

        $(#[$doc])*
        pub fn $plain<T: Subject + ?Sized>(
            &self,
            text: &T,
            pos: isize,
            endpos: isize,
        ) -> Result<Option<Match>, CallError> {
            plain(self.$tried(text, pos, endpos, None))
        }
    )*};
}

impl Regex {
    /// `re.compile(pattern, flags)` for a `str` pattern of these code
    /// points.
    pub fn new(pattern: &[u32], flags: u32) -> Result<Regex, RegexError> {
        Regex::build(Source::Str(pattern.to_vec()), pattern, true, flags)
    }

    /// [`Regex::new`] for a pattern written as Rust text.
    pub fn new_str(pattern: &str, flags: u32) -> Result<Regex, RegexError> {
        let codes: Vec<u32> = pattern.chars().map(u32::from).collect();
        Regex::new(&codes, flags)
    }

    /// `re.compile(pattern, flags)` for a bytes pattern.
    pub fn new_bytes(pattern: &[u8], flags: u32) -> Result<Regex, RegexError> {
        let codes: Vec<u32> = pattern.iter().map(|&b| u32::from(b)).collect();
        Regex::build(Source::Bytes(pattern.to_vec()), &codes, false, flags)
    }

    fn build(source: Source, codes: &[u32], istext: bool, flags: u32) -> Result<Regex, RegexError> {
        let (tree, state) = parse(codes, istext, flags).map_err(RegexError::Build)?;
        let prog = lower(&tree, state.flags, !istext).map_err(RegexError::Lower)?;
        if i32::try_from(state.flags).is_err() {
            return Err(RegexError::FlagsOverflow);
        }
        supports(&prog).map_err(|e| RegexError::Exec(format!("{e:?}")))?;
        let mut classes = HashMap::new();
        for inst in &prog.insts {
            let class = match &inst.op {
                Op::Unit(Unit::Class(class)) => class,
                Op::Rep1 {
                    unit: Unit::Class(class),
                    ..
                } => class,
                _ => continue,
            };
            if let Entry::Vacant(free) = classes.entry(key(class)) {
                free.insert(compile_class(&class.members, class.flags).map_err(RegexError::Class)?);
            }
        }
        let mut names: Vec<(usize, Vec<u32>)> = state
            .groupdict
            .iter()
            .map(|(name, &gid)| (gid, name.clone()))
            .collect();
        names.sort();
        Ok(Regex {
            source,
            flags: state.flags,
            groups: state.groups() - 1,
            names,
            advance: advance(&prog),
            prog,
            classes: Classes(classes),
        })
    }

    /// `Pattern.pattern`.
    pub fn pattern(&self) -> &Source {
        &self.source
    }

    /// `Pattern.flags`: the flags as given, with UNICODE for a `str`
    /// pattern and the pattern's leading inline flags.
    pub fn flags(&self) -> u32 {
        self.flags
    }

    /// `Pattern.groups`.
    pub fn groups(&self) -> usize {
        self.groups
    }

    /// `Pattern.groupindex` as `(number, name)`, by number.
    pub fn names(&self) -> &[(usize, Vec<u32>)] {
        &self.names
    }

    fn check<T: Subject + ?Sized>(&self) -> Result<(), CallError> {
        let str_pattern = matches!(self.source, Source::Str(_));
        if T::IS_STR != str_pattern {
            return Err(CallError::Type { str_pattern });
        }
        Ok(())
    }

    /// `pattern_new_match`.
    pub(super) fn wrap(&self, found: &Found, len: isize, pos: isize, endpos: isize) -> Match {
        let mut regs = vec![(found.start, found.end)];
        for g in 0..self.groups {
            let a = found.marks.get(2 * g).copied().unwrap_or(-1);
            let b = found.marks.get(2 * g + 1).copied().unwrap_or(-1);
            regs.push(if a >= 0 && b >= 0 { (a, b) } else { (-1, -1) });
        }
        let lastindex = usize::try_from(found.lastindex).ok();
        let lastgroup = lastindex.and_then(|i| {
            self.names
                .iter()
                .find(|(gid, _)| *gid == i)
                .map(|(_, name)| name.clone())
        });
        Match {
            pos: pos.clamp(0, len),
            endpos: endpos.clamp(0, len),
            regs,
            lastindex,
            lastgroup,
        }
    }

    fn one<T: Subject + ?Sized>(
        &self,
        text: &T,
        pos: isize,
        endpos: isize,
        mode: Mode,
        budget: Option<u64>,
    ) -> Result<Option<Match>, CallError> {
        self.check::<T>()?;
        let len = isize::try_from(text.len()).map_err(|_| CallError::Memory)?;
        let found = exec(&self.prog, &self.classes, text, pos, endpos, mode, budget)?;
        Ok(found.map(|f| self.wrap(&f, len, pos, endpos)))
    }

    /// The matches of `scanner_search` and `pattern_findall`, in order,
    /// each handed to `each` as it is found, at most `limit` of them: the
    /// loop of `pattern_subx` and `pattern_split` checks its count before
    /// each search, so no search runs once `limit` matches are found.
    pub(super) fn scan<T: Subject + ?Sized>(
        &self,
        text: &T,
        pos: isize,
        endpos: isize,
        budget: Option<u64>,
        limit: usize,
        mut each: impl FnMut(Found),
    ) -> Result<(), CallError> {
        self.check::<T>()?;
        let len = isize::try_from(text.len()).map_err(|_| CallError::Memory)?;
        let end = endpos.clamp(0, len);
        let mut start = pos.clamp(0, len);
        let mut must_advance = false;
        let mut found_so_far = 0usize;
        let run = |prog: &Program, at: isize, mode: Mode| {
            exec(prog, &self.classes, text, at, endpos, mode, budget)
        };
        while found_so_far < limit && start <= end {
            let found = if must_advance {
                match run(&self.advance, start, Mode::Match)? {
                    None if start < end => run(&self.prog, start + 1, Mode::Search)?,
                    other => other,
                }
            } else {
                run(&self.prog, start, Mode::Search)?
            };
            let Some(found) = found else {
                break;
            };
            must_advance = found.end == found.start;
            start = found.end;
            found_so_far += 1;
            each(found);
        }
        Ok(())
    }

    calls! {
        /// `Pattern.match(text, pos, endpos)`. `endpos` has no default:
        /// a caller passes `isize::MAX` for `re`'s default of
        /// `sys.maxsize`.
        r#match, try_match, Mode::Match;
        /// `Pattern.fullmatch(text, pos, endpos)`.
        fullmatch, try_fullmatch, Mode::Fullmatch;
        /// `Pattern.search(text, pos, endpos)`.
        search, try_search, Mode::Search;
    }

    /// `finditer`, collected. The budget holds for each run of the
    /// matcher, not for the whole scan.
    pub fn try_finditer<T: Subject + ?Sized>(
        &self,
        text: &T,
        pos: isize,
        endpos: isize,
        budget: Option<u64>,
    ) -> Result<Vec<Match>, CallError> {
        let len = isize::try_from(text.len()).map_err(|_| CallError::Memory)?;
        let mut out = Vec::new();
        self.scan(text, pos, endpos, budget, usize::MAX, |f| {
            out.push(self.wrap(&f, len, pos, endpos));
        })?;
        Ok(out)
    }

    /// `findall`: the whole match without groups, the group with one, the
    /// tuple of groups with more; a group that took no part is empty.
    pub fn try_findall<T: Subject + ?Sized>(
        &self,
        text: &T,
        pos: isize,
        endpos: isize,
        budget: Option<u64>,
    ) -> Result<Vec<Item<T::Piece>>, CallError> {
        let mut out = Vec::new();
        let piece = |found: &Found, g: usize| -> T::Piece {
            let (a, b) = (
                found.marks.get(2 * g).copied().unwrap_or(-1),
                found.marks.get(2 * g + 1).copied().unwrap_or(-1),
            );
            if a >= 0 && b >= 0 {
                text.piece(a as usize, b as usize)
            } else {
                text.piece(0, 0)
            }
        };
        let groups = self.groups;
        self.scan(text, pos, endpos, budget, usize::MAX, |f| {
            out.push(match groups {
                0 => Item::One(text.piece(f.start as usize, f.end as usize)),
                1 => Item::One(piece(&f, 0)),
                _ => Item::Tuple((0..groups).map(|g| piece(&f, g)).collect()),
            });
        })?;
        Ok(out)
    }

    pub fn finditer<T: Subject + ?Sized>(
        &self,
        text: &T,
        pos: isize,
        endpos: isize,
    ) -> Result<Vec<Match>, CallError> {
        plain(self.try_finditer(text, pos, endpos, None))
    }

    pub fn findall<T: Subject + ?Sized>(
        &self,
        text: &T,
        pos: isize,
        endpos: isize,
    ) -> Result<Vec<Item<T::Piece>>, CallError> {
        plain(self.try_findall(text, pos, endpos, None))
    }
}
