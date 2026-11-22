//! `origin_dates.py`'s memoised reader of a head's whole history (lines
//! at v9): `_instant` 31-40, `commit_author_timestamps` 43-186 (under
//! `git_read_budget.unread`) and `_remember_dates` 189-202.
//!
//! The memo is the caller's [`TimestampsMemo`], the monotonic clock an
//! argument, git the caller's [`OriginGit`], the files and discovery the
//! caller's [`DateFiles`], the repository's key, guard and cuts the
//! caller's [`MemoLeaves`] (`origin_history.located_key`, `history_guard`,
//! `cuts`), and the hold on the shallow file the caller's [`ShallowHold`]
//! (`_hold_shallow` 205-218, `_cut_held` 221-233, `_shallow_held`
//! 236-248, ported whole by a later unit).

use std::sync::Arc;

use super::{
    extended_author_timestamps, read_author_timestamps, unread_entry, DateFiles, OriginGit,
};
use crate::datetime::DateTime;
use crate::git_read_budget::{unread, Raise, ReadDeadline, Scope};
use crate::origin_history::{
    note_unread, Cut, Dates, HistoryUnread, Key, RunnerNote, TimestampsMemo, ValueError,
};
use crate::pyvalue::PyStr;

/// What `commit_author_timestamps` returns.
#[derive(Clone, Debug, PartialEq)]
pub enum Timestamps {
    /// None: no history applies.
    Absent,
    /// The ascending dates (the stored list itself on a hit).
    Dates(Dates),
    /// Why a history that applies was not read.
    Unread(HistoryUnread),
}

impl From<HistoryUnread> for Timestamps {
    fn from(why: HistoryUnread) -> Self {
        Timestamps::Unread(why)
    }
}

/// A path the leaves are asked about: `cwd` as given, or `Path(key[0])`,
/// the key's root text (the leaf reads it as `Path` does).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Place<'a> {
    Cwd(&'a [u8]),
    Root(&'a [u32]),
}

/// The leaves of `origin_history` the memoised read asks.
pub trait MemoLeaves<G> {
    /// `origin_history.located_key(cwd)`.
    fn located_key(&mut self, cwd: &[u8]) -> Option<Key>;
    /// `origin_history.history_guard(Path(root))`.
    fn history_guard(&mut self, root: &[u32]) -> Option<G>;
    /// `origin_history.cuts(start, walk=walk)`; the ValueError it raises.
    fn cuts(&mut self, start: Place<'_>, walk: bool) -> Result<Option<Cut>, ValueError>;
}

/// The hold on the shallow file across a read (`_hold_shallow`,
/// `_cut_held`, `_shallow_held`).
pub trait ShallowHold {
    /// The held stamps (`dict[str, tuple[int, int]]`).
    type Held;
    /// `_hold_shallow(start, walk=walk)`: None where a stamp is unread.
    fn hold(&mut self, start: Place<'_>, walk: bool) -> Option<Self::Held>;
    /// `_shallow_held(start, cut, held, walk=walk)`.
    fn shallow_held(
        &mut self,
        start: Place<'_>,
        cut: &Cut,
        held: Option<&Self::Held>,
        walk: bool,
    ) -> bool;
    /// `_cut_held(cwd, cut, dates, held)`.
    fn cut_held(
        &mut self,
        git: &mut dyn OriginGit,
        cwd: &[u8],
        cut: &Cut,
        dates: Timestamps,
        held: Option<&Self::Held>,
    ) -> Timestamps {
        if matches!(dates, Timestamps::Dates(_))
            && !self.shallow_held(Place::Cwd(cwd), cut, held, true)
        {
            git.note_failed();
            return Timestamps::Unread(HistoryUnread::Shallow);
        }
        dates
    }
}

/// `_instant(stamp)`: `stamp.timestamp()` of an aware date, the true
/// quotient of its microseconds since the epoch by a million, rounded
/// once as Python's int division rounds. None for a naive date, whose
/// timestamp follows the host's zone.
pub fn instant(stamp: &DateTime) -> Option<f64> {
    const EPOCH_US: i128 = 62_135_596_800_000_000;
    let us = stamp.instant_us()? - EPOCH_US;
    // Below 2**53 both operands are exact and IEEE division rounds once.
    // Above it the truncated whole seconds and the remainder share a sign
    // and are both exact; the one rounding of their sum is the correctly
    // rounded quotient, since no remainder over a million lies within an
    // f64's rounding of a coarser midpoint.
    #[allow(clippy::cast_precision_loss)]
    if us.unsigned_abs() <= 1 << 53 {
        return Some(us as f64 / 1e6);
    }
    let (q, rem) = (us / 1_000_000, us % 1_000_000);
    #[allow(clippy::cast_precision_loss)]
    Some(q as f64 + rem as f64 / 1e6)
}

fn of(dates: super::Dates) -> Timestamps {
    match dates {
        Ok(d) => Timestamps::Dates(Arc::new(d)),
        Err(Some(why)) => Timestamps::Unread(why),
        Err(None) => Timestamps::Absent,
    }
}

fn note(git: &mut dyn OriginGit, n: RunnerNote) {
    match n {
        RunnerNote::TimedOut => git.note_timed_out(),
        RunnerNote::Failed => git.note_failed(),
    }
}

fn raised<E>(git: &dyn OriginGit) -> Result<(), Raise<E>> {
    if git.deadline() {
        Err(Raise::Deadline(ReadDeadline))
    } else {
        Ok(())
    }
}

/// The effects `commit_author_timestamps` reads through.
pub struct Reader<'a, G, H: ShallowHold + ?Sized> {
    pub git: &'a mut dyn OriginGit,
    pub files: &'a mut dyn DateFiles,
    pub leaves: &'a mut dyn MemoLeaves<G>,
    pub hold: &'a mut H,
    pub memo: &'a mut TimestampsMemo<G>,
    /// `time.monotonic`.
    pub clock: &'a dyn Fn() -> f64,
}

/// `commit_author_timestamps(cwd, located=located)`: `located` is the
/// root (as `str(root)`) and head the caller resolved. The ValueError a
/// cut raises is the error.
pub fn commit_author_timestamps<G: PartialEq + Clone, H: ShallowHold + ?Sized>(
    scope: &mut Scope,
    r: &mut Reader<'_, G, H>,
    cwd: Option<&[u8]>,
    located: Option<Key>,
) -> Result<Timestamps, ValueError> {
    unread(scope, None, |_| body(r, cwd, located))
}

fn body<G: PartialEq + Clone, H: ShallowHold + ?Sized>(
    r: &mut Reader<'_, G, H>,
    cwd: Option<&[u8]>,
    located: Option<Key>,
) -> Result<Timestamps, Raise<ValueError>> {
    let Some(cwd) = cwd else {
        return Ok(Timestamps::Absent);
    };
    let located = match located {
        Some(l) => Some(l),
        None => r.git.toplevel_and_head_from_files(cwd),
    };
    let key = match located {
        Some(l) => Some(l),
        None => r.leaves.located_key(cwd),
    };
    let Some(key) = key else {
        let cut = r.leaves.cuts(Place::Cwd(cwd), true).map_err(Raise::Other)?;
        let Some(cut) = cut else {
            r.git.note_failed();
            if r.files.gitdir_at(cwd).is_some() || r.files.locate(cwd).is_some() {
                return Ok(Timestamps::Unread(HistoryUnread::Shallow));
            }
            return Ok(unread_entry(r.files, cwd).map_or(Timestamps::Absent, Timestamps::Unread));
        };
        let held = r.hold.hold(Place::Cwd(cwd), true);
        let head: PyStr = "HEAD".chars().map(u32::from).collect();
        let dates = of(read_author_timestamps(r.git, r.files, cwd, &head, &cut));
        raised(r.git)?;
        return Ok(r.hold.cut_held(r.git, cwd, &cut, dates, held.as_ref()));
    };
    let root = key.0.clone();
    let guard = r.leaves.history_guard(&root);
    let cut = r
        .leaves
        .cuts(Place::Root(&root), false)
        .map_err(Raise::Other)?;
    let (stored, failure) = r
        .memo
        .recall(&key, guard.as_ref(), cut.as_ref(), (r.clock)());
    if let Some(stored) = stored {
        return Ok(Timestamps::Dates(stored));
    }
    let (Some(mut cut), None) = (cut, failure) else {
        let why = match failure {
            Some(why) => why,
            None => {
                let why = HistoryUnread::Shallow;
                r.memo.remember_failure(&key, guard, why, (r.clock)());
                why
            }
        };
        note(r.git, note_unread(why));
        return Ok(Timestamps::Unread(why));
    };
    let held = r.hold.hold(Place::Root(&root), false);
    let base = guard
        .as_ref()
        .and_then(|g| r.memo.base_for(&root, Some(g), &cut));
    let mut dates = None;
    if let Some((base_head, base_dates, base_cut)) = base {
        cut = base_cut;
        let extended =
            extended_author_timestamps(r.git, cwd, &base_head, &base_dates, &key.1, &cut);
        raised(r.git)?;
        dates = match extended {
            Ok(None) => None,
            Ok(Some(d)) => Some(Timestamps::Dates(Arc::new(d))),
            Err(why) => Some(Timestamps::Unread(why)),
        };
    }
    let dates = match dates {
        Some(d) => d,
        None => {
            let d = of(read_author_timestamps(r.git, r.files, cwd, &key.1, &cut));
            raised(r.git)?;
            d
        }
    };
    if matches!(dates, Timestamps::Dates(_))
        && !r
            .hold
            .shallow_held(Place::Root(&root), &cut, held.as_ref(), false)
    {
        r.git.note_failed();
        return Ok(Timestamps::Unread(HistoryUnread::Shallow));
    }
    Ok(remember_dates(
        r.git, r.memo, r.clock, &key, guard, cut, dates,
    ))
}

/// `_remember_dates(key, guard, cut, dates)`.
pub fn remember_dates<G: PartialEq + Clone>(
    git: &mut dyn OriginGit,
    memo: &mut TimestampsMemo<G>,
    clock: &dyn Fn() -> f64,
    key: &Key,
    guard: Option<G>,
    cut: Cut,
    dates: Timestamps,
) -> Timestamps {
    match dates {
        Timestamps::Unread(why) => {
            memo.remember_failure(key, guard, why, clock());
            Timestamps::Unread(why)
        }
        Timestamps::Dates(d) => {
            if memo.remember(key, Arc::clone(&d), guard.clone(), cut) {
                return Timestamps::Dates(d);
            }
            let why = HistoryUnread::OverBudget;
            memo.remember_failure(key, guard, why, clock());
            git.note_failed();
            Timestamps::Unread(why)
        }
        Timestamps::Absent => Timestamps::Absent,
    }
}
