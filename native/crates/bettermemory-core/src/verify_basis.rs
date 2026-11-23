//! `verify_basis.py` whole (lines at v9): the two bases a commit-drift
//! count is measured on (42-43), `CommitDriftUnread` (57), the unread
//! recommendations (60-100), `dates_since` (103-113),
//! `date_count_uncertainty` (116-127) and `_count_since_verify`
//! (130-195).
//!
//! The two reads of `origin` the count takes (`repo_toplevel_and_head`
//! and `commits_since_anchor`) are the caller's [`AnchorWalk`], the walks
//! given up on are the caller's [`Unwalked`], and the whole-history
//! author dates (`origin_dates.commit_author_timestamps`) are a closure
//! called only where the date count is taken.

use std::cmp::Ordering;

use crate::datetime::DateTime;
use crate::origin_history::{HistoryUnread, ReachableWalk, Unwalked};
use crate::pyvalue::PyStr;

/// `BASIS_REACHABILITY`.
pub const BASIS_REACHABILITY: &str = "reachability";
/// `BASIS_AUTHOR_DATE`.
pub const BASIS_AUTHOR_DATE: &str = "author-date";

/// `CommitDriftUnread`.
pub type CommitDriftUnread = HistoryUnread;

/// `_UNREAD_RECOMMENDATIONS`, in the dict's order.
pub const UNREAD_RECOMMENDATIONS: [(HistoryUnread, &str); 6] = [
    (
        HistoryUnread::CallBudget,
        "This call reached its shared Git-read deadline before the memory's \
         commit history was checked. Retry the read; later calls give \
         deferred home roots a turn. Spot-check the claim before relying on it.",
    ),
    (
        HistoryUnread::UnconfirmedCut,
        "No usable verification commit anchors this memory's history. \
         Author dates cannot rule out older commits integrated after \
         verification. Spot-check the claim and verify it again in its \
         home checkout to record the current commit.",
    ),
    (
        HistoryUnread::Timeout,
        "The repository's commit history could not be read in time, so the \
         commits since this memory was verified were not counted. Spot-check \
         a claim before relying on the memory.",
    ),
    (
        HistoryUnread::OverBudget,
        "The repository's commit history is longer than bettermemory reads, \
         so the commits since this memory was verified were not counted. \
         Spot-check a claim before relying on the memory.",
    ),
    (
        HistoryUnread::Shallow,
        "The checkout is a shallow clone, whose history is cut, so the \
         commits since this memory was verified were not counted. Fetch the \
         missing history (git fetch --unshallow), or spot-check a claim \
         before relying on the memory.",
    ),
    (
        HistoryUnread::Error,
        "git failed reading the repository's commit history, so the commits \
         since this memory was verified were not counted. Spot-check a \
         claim before relying on the memory.",
    ),
];

/// `unread_recommendation`.
pub fn unread_recommendation(why: HistoryUnread) -> &'static str {
    UNREAD_RECOMMENDATIONS
        .iter()
        .find(|(k, _)| *k == why)
        .map(|(_, v)| *v)
        .unwrap_or_default()
}

/// The TypeError Python raises ordering a naive datetime against an
/// aware one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NaiveAware;

/// `a < b` as Python orders two datetimes: by instant when both are
/// aware, by wall clock when both are naive, a TypeError otherwise.
fn less(a: &DateTime, b: &DateTime) -> Result<bool, NaiveAware> {
    let ord = match (a.instant_us(), b.instant_us()) {
        (Some(x), Some(y)) => x.cmp(&y),
        (None, None) => a.wall_micros().cmp(&b.wall_micros()),
        _ => return Err(NaiveAware),
    };
    Ok(ord == Ordering::Less)
}

/// `bisect.bisect_right(a, x)`, probing the same elements in the same
/// order (so a TypeError is raised exactly where the reference's is).
fn bisect_right(a: &[DateTime], x: &DateTime) -> Result<usize, NaiveAware> {
    let (mut lo, mut hi) = (0usize, a.len());
    while lo < hi {
        let mid = (lo + hi) / 2;
        if less(x, &a[mid])? {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }
    Ok(lo)
}

/// What a count over a history answers: None where none applies, why
/// it was not read, or the count.
pub type Counted = Option<Result<usize, HistoryUnread>>;

/// `dates_since`.
pub fn dates_since(
    history: Option<Result<&[DateTime], HistoryUnread>>,
    since: &DateTime,
) -> Result<Counted, NaiveAware> {
    match history {
        None => Ok(None),
        Some(Err(why)) => Ok(Some(Err(why))),
        Some(Ok(dates)) => Ok(Some(Ok(dates.len() - bisect_right(dates, since)?))),
    }
}

/// `date_count_uncertainty`.
pub fn date_count_uncertainty(count: i64, basis: &str) -> Option<HistoryUnread> {
    (count == 0 && basis == BASIS_AUTHOR_DATE).then_some(HistoryUnread::UnconfirmedCut)
}

/// The two reads of `origin` the count takes.
pub trait AnchorWalk {
    /// `repo_toplevel_and_head(cwd)`: the root and HEAD, or None.
    fn toplevel_and_head(&mut self, cwd: &[u32]) -> Option<(PyStr, PyStr)>;
    /// `commits_since_anchor(cwd, verified_head, toplevel=, head=)`; a walk
    /// given up on is noted on `unwalked`.
    fn commits_since_anchor(
        &mut self,
        cwd: &[u32],
        verified_head: &[u32],
        toplevel: &[u32],
        head: &[u32],
        unwalked: &mut Unwalked,
    ) -> Option<ReachableWalk>;
}

/// The tuple `_count_since_verify` answers.
#[derive(Clone, Debug, PartialEq)]
pub struct SinceVerify {
    pub count: usize,
    pub basis: &'static str,
    pub walk: Option<ReachableWalk>,
    pub toplevel: Option<PyStr>,
}

/// `_count_since_verify`: Ok(None) where none applies, Ok(Some(Err(why)))
/// where the history was not read; `Err` is the TypeError of a naive
/// stamp ordered against aware dates.
pub fn count_since_verify(
    origin: &mut dyn AnchorWalk,
    unwalked: &mut Unwalked,
    cwd: &[u32],
    last_verified_at: &DateTime,
    verified_head: Option<&[u32]>,
    dates: impl FnOnce() -> Option<Result<Vec<DateTime>, HistoryUnread>>,
) -> Result<Option<Result<SinceVerify, HistoryUnread>>, NaiveAware> {
    count_since_verify_until(
        origin,
        unwalked,
        cwd,
        last_verified_at,
        verified_head,
        dates,
        || false,
    )
}

/// Stop at the read that raised the shared deadline, before any fallback read.
pub(crate) fn count_since_verify_until(
    origin: &mut dyn AnchorWalk,
    unwalked: &mut Unwalked,
    cwd: &[u32],
    last_verified_at: &DateTime,
    verified_head: Option<&[u32]>,
    dates: impl FnOnce() -> Option<Result<Vec<DateTime>, HistoryUnread>>,
    deadline: impl Fn() -> bool,
) -> Result<Option<Result<SinceVerify, HistoryUnread>>, NaiveAware> {
    let mut walk = None;
    let mut toplevel = None;
    if let Some(anchor) = verified_head {
        let located = origin.toplevel_and_head(cwd);
        if deadline() {
            return Ok(Some(Err(HistoryUnread::CallBudget)));
        }
        if let Some((top, head)) = located {
            let given_up = unwalked.count();
            walk = origin.commits_since_anchor(cwd, anchor, &top, &head, unwalked);
            if deadline() {
                return Ok(Some(Err(HistoryUnread::CallBudget)));
            }
            toplevel = Some(top);
            if walk.is_none() {
                if let Some(why) = unwalked.given_up_since(given_up) {
                    return Ok(Some(Err(why)));
                }
            }
        }
    }
    let (count, basis) = match &walk {
        Some(w) => (w.commits.len(), BASIS_REACHABILITY),
        None => {
            let history = dates();
            if deadline() {
                return Ok(Some(Err(HistoryUnread::CallBudget)));
            }
            let view = history
                .as_ref()
                .map(|h| h.as_ref().map(Vec::as_slice).map_err(|w| *w));
            match dates_since(view, last_verified_at)? {
                None => return Ok(None),
                Some(Err(why)) => return Ok(Some(Err(why))),
                Some(Ok(n)) => (n, BASIS_AUTHOR_DATE),
            }
        }
    };
    Ok(Some(Ok(SinceVerify {
        count,
        basis,
        walk,
        toplevel,
    })))
}
