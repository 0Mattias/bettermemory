//! Commit drift for one search hit (`verify.resolve_hit_drift`). The
//! response pass supplies the location, owns the walks it keeps between
//! hits, and decides whether a completed result can enter its memo.

use std::sync::Arc;

use super::count::{quiescent_applicable, resolve_at, try_applicable_unread, CountReads};
use super::status::ResolvedCommitDrift;
use super::weak::{Place, WeakError};
use crate::claims::Claim;
use crate::datetime::DateTime;
use crate::origin_dates::memo::Timestamps;
use crate::origin_history::{HistoryUnread, ReachableWalk, Unwalked};
use crate::pyvalue::PyStr;
use crate::verify_basis::{
    date_count_uncertainty, dates_since, BASIS_AUTHOR_DATE, BASIS_REACHABILITY,
};

/// The stamp and evidence for this hit. A present empty head is passed
/// through to the Git reader just like any other present head.
pub struct Hit<'a> {
    pub since: &'a DateTime,
    pub head: Option<&'a [u32]>,
    pub verified_head: Option<&'a [u32]>,
    pub anchors: &'a [PyStr],
    pub claims: &'a [Claim],
}

/// None omits the measurement. A returned unread reason is a value a
/// response pass may report. Errors raised by reads leave the outer Result.
pub type HitDrift = Option<Result<ResolvedCommitDrift, HistoryUnread>>;

/// Resolve one hit. `walk` borrows the caller's per-pass walked map,
/// while the pass's lazy `timestamps` reader may reuse a shared date list.
/// The undecorated reference allows the original provider error to escape.
pub fn resolve_hit_drift<E: From<WeakError>>(
    reads: CountReads,
    hit: Hit,
    unwalked: &mut Unwalked,
    mut walk: impl FnMut(Place, &[u32], &[u32], &mut Unwalked) -> Result<Option<Arc<ReachableWalk>>, E>,
    timestamps: impl FnOnce() -> Result<Timestamps, E>,
) -> Result<HitDrift, E> {
    let applicable = |reason| {
        try_applicable_unread(reason, reads.git, reads.at, hit.anchors, hit.claims)
            .map(|value| value.map(Err))
            .map_err(|error| E::from(WeakError::from(error)))
    };
    let walked = match (hit.verified_head, hit.head) {
        (Some(anchor), Some(head)) => {
            let before = unwalked.count();
            let result = walk(reads.at, anchor, head, unwalked)?;
            if result.is_none() {
                if let Some(why) = unwalked.given_up_since(before) {
                    return applicable(why);
                }
            }
            result
        }
        _ => None,
    };
    let (count, basis) = if let Some(w) = &walked {
        (w.commits.len(), BASIS_REACHABILITY)
    } else {
        let history = timestamps()?;
        let dates = match &history {
            Timestamps::Absent => None,
            Timestamps::Unread(why) => Some(Err(*why)),
            Timestamps::Dates(dates) => Some(Ok(dates.as_slice())),
        };
        match dates_since(dates, hit.since).map_err(|_| WeakError::NaiveAware)? {
            None => return Ok(None),
            Some(Err(why)) => return applicable(why),
            Some(Ok(n)) => (n, BASIS_AUTHOR_DATE),
        }
    };
    let count = i64::try_from(count).unwrap_or(i64::MAX);
    let resolved = if count > 0 {
        let Some(resolved) = resolve_at(
            reads,
            hit.since,
            count,
            hit.anchors,
            hit.claims,
            walked.as_deref(),
        )?
        else {
            return Ok(None);
        };
        resolved
    } else {
        if !quiescent_applicable(reads, hit.anchors, hit.claims).map_err(WeakError::from)? {
            return Ok(None);
        }
        ResolvedCommitDrift {
            basis: basis.to_owned(),
            ..ResolvedCommitDrift::new(0)
        }
    };
    Ok(Some(
        match date_count_uncertainty(resolved.count, &resolved.basis) {
            Some(why) => Err(why),
            None => Ok(resolved),
        },
    ))
}
