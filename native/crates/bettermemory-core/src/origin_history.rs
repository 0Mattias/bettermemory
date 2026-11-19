//! `origin_history.py`, its first unit: the leaves that read no file and
//! run no git.
//!
//! - the pins every git `origin` runs is given (`AS_STORED_ENV`,
//!   `AS_STORED_CONFIG`, `AS_STORED_DIFF`, `AS_STORED_PATHS`) and the
//!   module's other constants;
//! - [`HistoryUnread`], [`unanswered`] and [`note_unread`], with the
//!   runner's counts and its two notes as data;
//! - [`read_lines`], [`author_instant`], [`author_dates`] and
//!   [`log_stdin`];
//! - [`ReachableWalk`] and its `shas_touching`;
//! - [`TimestampsMemo`]: the module's two ordered dictionaries and their
//!   lock (`recall`, `checked_cut`, `base_for`, `remember`,
//!   `remember_failure`, `_clear_timestamps_memo`) as a value the caller
//!   owns, the monotonic clock an argument;
//! - [`Unwalked`]: the thread-local tally (`unwalked`, `given_up_since`,
//!   `note_unwalked`) as a value the caller owns.
//!
//! Text is a Python `str` as code points ([`PyStr`]), so a lone
//! surrogate is a value like any other.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use crate::datetime::{DateTime, LAST_SECOND};
use crate::pyvalue::PyStr;

/// `os.devnull`.
#[cfg(windows)]
pub const DEVNULL: &str = "nul";
/// `os.devnull`.
#[cfg(not(windows))]
pub const DEVNULL: &str = "/dev/null";

/// `AS_STORED_ENV`, in the dictionary's order.
pub const AS_STORED_ENV: [(&str, &str); 2] =
    [("GIT_NO_REPLACE_OBJECTS", "1"), ("GIT_GRAFT_FILE", DEVNULL)];

/// `AS_STORED_CONFIG`.
pub const AS_STORED_CONFIG: [&str; 12] = [
    "-c",
    "core.useReplaceRefs=false",
    "-c",
    "core.commitGraph=false",
    "-c",
    "log.showRoot=true",
    "-c",
    "log.follow=false",
    "-c",
    "diff.renames=false",
    "-c",
    "i18n.logOutputEncoding=UTF-8",
];

/// `AS_STORED_DIFF`: its last element is `-O` and [`DEVNULL`].
#[cfg(windows)]
pub const AS_STORED_DIFF: [&str; 3] = ["--text", "--ignore-submodules=none", "-Onul"];
/// `AS_STORED_DIFF`: its last element is `-O` and [`DEVNULL`].
#[cfg(not(windows))]
pub const AS_STORED_DIFF: [&str; 3] = ["--text", "--ignore-submodules=none", "-O/dev/null"];

/// `AS_STORED_PATHS`.
pub const AS_STORED_PATHS: [&str; 2] = ["-c", "core.precomposeUnicode=false"];

/// `_TIMESTAMPS_MEMO_CAP`: the most entries, and the most failures, kept.
pub const MEMO_CAP: usize = 16;
/// `_TIMESTAMPS_MEMO_SLOTS`: the most dates the entries hold together.
pub const MEMO_SLOTS: usize = 2_000_000;
/// `_FAILURE_SECONDS`.
pub const FAILURE_SECONDS: f64 = 60.0;
/// `_CUTS_MAX`: the longest `shallow` file `cuts` reads.
pub const CUTS_MAX: usize = 1 << 24;
/// `_HISTORY_ENV`.
pub const HISTORY_ENV: [&str; 4] = [
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_GRAFT_FILE",
    "GIT_SHALLOW_FILE",
];

/// `HistoryUnread`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HistoryUnread {
    Timeout,
    OverBudget,
    Shallow,
    Error,
    UnconfirmedCut,
    CallBudget,
}

impl HistoryUnread {
    /// The member's value.
    pub fn value(self) -> &'static str {
        match self {
            HistoryUnread::Timeout => "timeout",
            HistoryUnread::OverBudget => "over_budget",
            HistoryUnread::Shallow => "shallow",
            HistoryUnread::Error => "error",
            HistoryUnread::UnconfirmedCut => "unconfirmed_cut",
            HistoryUnread::CallBudget => "call_budget",
        }
    }

    /// `HistoryUnread(value)`; None where no member has it.
    pub fn from_value(value: &str) -> Option<HistoryUnread> {
        [
            HistoryUnread::Timeout,
            HistoryUnread::OverBudget,
            HistoryUnread::Shallow,
            HistoryUnread::Error,
            HistoryUnread::UnconfirmedCut,
            HistoryUnread::CallBudget,
        ]
        .into_iter()
        .find(|m| m.value() == value)
    }
}

/// `unanswered`: `before` and `now` are `git_counts()` (the runner's
/// timeouts and missing calls) before the read and now.
pub fn unanswered(before: (i64, i64), now: (i64, i64), named: bool) -> Option<HistoryUnread> {
    if now.0 != before.0 {
        return Some(HistoryUnread::Timeout);
    }
    if now.1 != before.1 || !named {
        return None;
    }
    Some(HistoryUnread::Error)
}

/// Which of the runner's notes `note_unread` makes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunnerNote {
    /// `_git.note_timed_out`: a failure and a timeout.
    TimedOut,
    /// `_git.note_failed`: a failure.
    Failed,
}

/// `note_unread`: the note the runner is given for `why`.
pub fn note_unread(why: HistoryUnread) -> RunnerNote {
    if why == HistoryUnread::Timeout {
        RunnerNote::TimedOut
    } else {
        RunnerNote::Failed
    }
}

/// `read_lines`: `read` of each record of `raw` split on line feeds
/// alone, a final empty record dropped; None where any reads as None.
pub fn read_lines<T>(raw: &[u32], mut read: impl FnMut(&[u32]) -> Option<T>) -> Option<Vec<T>> {
    let mut lines: Vec<&[u32]> = raw.split(|&c| c == 0x0A).collect();
    if lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    let mut out = Vec::with_capacity(lines.len());
    for line in lines {
        out.push(read(line)?);
    }
    Some(out)
}

/// The first second a datetime holds, counted from the epoch.
const FIRST_SECOND: i128 = -62_135_596_800;

/// `datetime.max` in UTC: `_LATEST`.
pub const LATEST: DateTime = DateTime {
    year: 9999,
    month: 12,
    day: 31,
    hour: 23,
    minute: 59,
    second: 59,
    microsecond: 999_999,
    offset_us: Some(0),
};

/// `datetime.min` in UTC: `_EARLIEST`.
pub const EARLIEST: DateTime = DateTime {
    year: 1,
    month: 1,
    day: 1,
    hour: 0,
    minute: 0,
    second: 0,
    microsecond: 0,
    offset_us: Some(0),
};

/// `author_instant`: the epoch plus `value` seconds in UTC; past the range
/// a datetime holds, `_LATEST` for a positive value and `_EARLIEST`
/// otherwise. A Python int beyond `i128` reads as the nearest `i128`,
/// which answers the same.
pub fn author_instant(value: i128) -> DateTime {
    if !(FIRST_SECOND..=i128::from(LAST_SECOND)).contains(&value) {
        return if value > 0 { LATEST } else { EARLIEST };
    }
    // In range, so the microseconds fit an i64 and the date is valid.
    DateTime::from_unix_micros_utc((value * 1_000_000) as i64).unwrap_or(EARLIEST)
}

/// `author_dates`: `values` sorted, each through [`author_instant`].
pub fn author_dates(values: &[i128]) -> Vec<DateTime> {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    sorted.into_iter().map(author_instant).collect()
}

/// `log_stdin`: each revision on a line, a `--` line, each pathspec on a
/// line, encoded UTF-8 with `surrogateescape`; None for an empty element,
/// one holding a LF or a CR, a revision starting with a dash, or a
/// surrogate outside U+DC80 to U+DCFF.
pub fn log_stdin(revisions: &[PyStr], pathspecs: &[PyStr]) -> Option<Vec<u8>> {
    let dashes: PyStr = vec![0x2D, 0x2D];
    let lines: Vec<&[u32]> = revisions
        .iter()
        .map(Vec::as_slice)
        .chain(std::iter::once(dashes.as_slice()))
        .chain(pathspecs.iter().map(Vec::as_slice))
        .collect();
    if lines
        .iter()
        .any(|l| l.is_empty() || l.contains(&0x0A) || l.contains(&0x0D))
    {
        return None;
    }
    if revisions.iter().any(|r| r.first() == Some(&0x2D)) {
        return None;
    }
    let mut out = Vec::new();
    for line in lines {
        for &cp in line {
            if (0xDC80..=0xDCFF).contains(&cp) {
                out.push((cp - 0xDC00) as u8);
            } else {
                let c = char::from_u32(cp)?;
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
        out.push(b'\n');
    }
    Some(out)
}

/// `ReachableWalk`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReachableWalk {
    pub anchor: PyStr,
    pub head: PyStr,
    pub commits: Vec<PyStr>,
    /// Sorted by code point, as `_sorted_paths` sorts them.
    pub touched: BTreeMap<PyStr, BTreeSet<PyStr>>,
    pub listed: bool,
    pub timed_out: bool,
}

impl ReachableWalk {
    /// `shas_touching`: every commit for a walk that did not list its
    /// names; else the commits of each spec's exact path and of every
    /// path under `spec.rstrip("/") + "/"`.
    pub fn shas_touching(&self, pathspecs: &[PyStr]) -> BTreeSet<PyStr> {
        if !self.listed {
            return self.commits.iter().cloned().collect();
        }
        let mut out = BTreeSet::new();
        for spec in pathspecs {
            if let Some(exact) = self.touched.get(spec) {
                out.extend(exact.iter().cloned());
            }
            let end = spec.iter().rposition(|&c| c != 0x2F).map_or(0, |i| i + 1);
            let mut prefix = spec[..end].to_vec();
            prefix.push(0x2F);
            for (path, commits) in self.touched.range(prefix.clone()..) {
                if !path.starts_with(&prefix) {
                    break;
                }
                out.extend(commits.iter().cloned());
            }
        }
        out
    }
}

/// `Key`: the root as the caller resolved it, and the head's hash.
pub type Key = (PyStr, PyStr);
/// A set of commits a `shallow` file listed (`cuts`).
pub type Cut = BTreeSet<PyStr>;
/// A kept list of dates: shared, as the reference returns the stored list.
pub type Dates = Arc<Vec<DateTime>>;

/// `_Entry`.
#[derive(Clone, Debug)]
pub struct Entry<G> {
    pub dates: Dates,
    pub guard: Option<G>,
    pub cut: Cut,
}

/// A remembered failure: when (the monotonic clock), under which guard,
/// and why.
#[derive(Clone, Debug)]
pub struct Failure<G> {
    pub at: f64,
    pub guard: Option<G>,
    pub why: HistoryUnread,
}

/// `_TIMESTAMPS_MEMO` and `_TIMESTAMPS_FAILED`, oldest first, as one value
/// the caller owns (and locks, where it shares it). `G` is the guard
/// `history_guard` reads, compared with `==` as Python compares tuples.
#[derive(Clone, Debug)]
pub struct TimestampsMemo<G> {
    entries: Vec<(Key, Entry<G>)>,
    failed: Vec<(Key, Failure<G>)>,
}

impl<G> Default for TimestampsMemo<G> {
    fn default() -> Self {
        TimestampsMemo {
            entries: Vec::new(),
            failed: Vec::new(),
        }
    }
}

fn place<V>(list: &[(Key, V)], key: &Key) -> Option<usize> {
    list.iter().position(|(k, _)| k == key)
}

/// `OrderedDict[key] = value` then `move_to_end(key)`.
fn put_last<V>(list: &mut Vec<(Key, V)>, key: &Key, value: V) {
    if let Some(i) = place(list, key) {
        list.remove(i);
    }
    list.push((key.clone(), value));
}

impl<G: PartialEq + Clone> TimestampsMemo<G> {
    pub fn new() -> Self {
        Self::default()
    }

    /// `_clear_timestamps_memo`.
    pub fn clear(&mut self) {
        self.entries.clear();
        self.failed.clear();
    }

    /// The entries, oldest first.
    pub fn entries(&self) -> &[(Key, Entry<G>)] {
        &self.entries
    }

    /// The failures, oldest first.
    pub fn failures(&self) -> &[(Key, Failure<G>)] {
        &self.failed
    }

    /// `recall`, `now` being `time.monotonic()`.
    pub fn recall(
        &mut self,
        key: &Key,
        guard: Option<&G>,
        cut: Option<&Cut>,
        now: f64,
    ) -> (Option<Dates>, Option<HistoryUnread>) {
        if let (Some(i), Some(cut)) = (place(&self.entries, key), cut) {
            if cut.is_subset(&self.entries[i].1.cut) {
                let item = self.entries.remove(i);
                let dates = Arc::clone(&item.1.dates);
                self.entries.push(item);
                return (Some(dates), None);
            }
        }
        if let Some(i) = place(&self.failed, key) {
            let failure = &self.failed[i].1;
            let since = now - failure.at;
            if failure.guard.as_ref() == guard && (0.0..FAILURE_SECONDS).contains(&since) {
                return (None, Some(failure.why));
            }
            self.failed.remove(i);
        }
        (None, None)
    }

    /// `checked_cut`, the entry's place untouched.
    pub fn checked_cut(&self, key: &Key) -> Option<Cut> {
        place(&self.entries, key).map(|i| self.entries[i].1.cut.clone())
    }

    /// `base_for`: the most recently used entry under `root` read under
    /// `guard` and checked against `cut`, as `(head, dates, cut)`.
    pub fn base_for(
        &self,
        root: &[u32],
        guard: Option<&G>,
        cut: &Cut,
    ) -> Option<(PyStr, Dates, Cut)> {
        self.entries
            .iter()
            .rev()
            .find(|((r, _), e)| r.as_slice() == root && e.guard.as_ref() == guard && &e.cut == cut)
            .map(|((_, head), e)| (head.clone(), Arc::clone(&e.dates), e.cut.clone()))
    }

    /// `remember`: False, and nothing kept, for more dates than
    /// [`MEMO_SLOTS`].
    pub fn remember(&mut self, key: &Key, dates: Dates, guard: Option<G>, cut: Cut) -> bool {
        if dates.len() > MEMO_SLOTS {
            return false;
        }
        if let Some(i) = place(&self.failed, key) {
            self.failed.remove(i);
        }
        put_last(&mut self.entries, key, Entry { dates, guard, cut });
        let mut held: usize = self.entries.iter().map(|(_, e)| e.dates.len()).sum();
        let mut gone = 0;
        while self.entries.len() - gone > MEMO_CAP || held > MEMO_SLOTS {
            held -= self.entries[gone].1.dates.len();
            gone += 1;
        }
        self.entries.drain(..gone);
        true
    }

    /// `remember_failure`, `now` being `time.monotonic()`; a call budget
    /// is never remembered.
    pub fn remember_failure(&mut self, key: &Key, guard: Option<G>, why: HistoryUnread, now: f64) {
        if why == HistoryUnread::CallBudget {
            return;
        }
        put_last(
            &mut self.failed,
            key,
            Failure {
                at: now,
                guard,
                why,
            },
        );
        if self.failed.len() > MEMO_CAP {
            let over = self.failed.len() - MEMO_CAP;
            self.failed.drain(..over);
        }
    }
}

/// `_Unwalked`: the walks given up on and why the last was, as a value
/// the caller owns (one per thread in the reference).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Unwalked {
    count: u64,
    why: HistoryUnread,
}

impl Default for Unwalked {
    fn default() -> Self {
        Unwalked {
            count: 0,
            why: HistoryUnread::Timeout,
        }
    }
}

impl Unwalked {
    pub fn new() -> Self {
        Self::default()
    }

    /// `unwalked`.
    pub fn count(&self) -> u64 {
        self.count
    }

    /// `given_up_since`.
    pub fn given_up_since(&self, before: u64) -> Option<HistoryUnread> {
        (self.count != before).then_some(self.why)
    }

    /// `note_unwalked`.
    pub fn note(&mut self, why: HistoryUnread) {
        self.count = self.count.wrapping_add(1);
        self.why = why;
    }
}
