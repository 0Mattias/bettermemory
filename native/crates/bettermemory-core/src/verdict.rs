//! One staleness verdict per memory, for `memory_show` (`verdict.py`):
//! `MemoryVerdict`, `UnresolvedVerdict`, `resolve_verdicts`,
//! `memoised_verdicts`, `memory_verdict`, `_open_legs` and `_verdict`.
//! `_OBJECTED` and `with_objection` live in `response_reason`.
//!
//! The commit legs (`commit_legs.CommitLegs`) are the trait [`CommitLegs`],
//! opened by a constructor closure; the claim anchoring is the trait
//! [`VerdictAnchoring`], which [`Anchoring`] implements; the path drift
//! read (`detect_path_drift`) and `unchecked_verdict` are closures. The
//! calls reach them in the reference's order: the legs (or else the
//! anchoring) primed with every id, then per row the calendar status, the
//! anchor, the path drift read and the commit leg's lookup, and the legs'
//! store at the end of a resolving call that raised nothing.

use crate::attestation_paths::{record_path_refs, PathRefs, Record};
use crate::datetime::DateTime;
use crate::pyvalue::PyStr;
use crate::verify::status::{
    commit_drift_status, staleness_verdict_from_report, CommitDriftStatus, ResolvedCommitDrift,
};
use crate::verify_anchoring::anchor::{Anchoring, ClaimAnchor};
use crate::verify_anchoring::report::PathDriftReport;
use crate::verify_basis::CommitDriftUnread;
use crate::verify_status::{compute_verification_status, NaiveAwareMix, VerificationStatus};

/// The fields of a `Memory` a verdict reads.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct VerdictMemory {
    pub id: String,
    pub body: PyStr,
    pub last_verified_at: Option<DateTime>,
    /// `memory.origin.worktree_root`, None also where `origin` is None.
    pub worktree_root: Option<PyStr>,
    /// The attestation fields `record_paths` reads.
    pub record: Record,
}

/// What `CommitLegs.lookup` resolves: a `ResolvedCommitDrift`, a
/// `CommitDriftUnread`, a `CommitLegUnchecked` (opaque here) or None.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Resolved<U> {
    Drift(ResolvedCommitDrift),
    Unread(CommitDriftUnread),
    Unchecked(U),
    None,
}

/// `commit_legs.CommitLegs`, as the verdict calls it.
pub trait CommitLegs {
    /// `CommitLegUnchecked`.
    type Unchecked: Clone;
    /// `active`: whether any record's leg is read.
    fn active(&self) -> bool;
    /// `prime(ids)`.
    fn prime(&mut self, ids: &[String]);
    /// `lookup(memory, last_verified_at, resolve=resolve)`: (answered, resolved).
    fn lookup(
        &mut self,
        memory: &VerdictMemory,
        last_verified_at: Option<&DateTime>,
        resolve: bool,
    ) -> (bool, Resolved<Self::Unchecked>);
    /// `store()`.
    fn store(&mut self);
}

/// `verify_anchoring.Anchoring`, as the verdict calls it.
pub trait VerdictAnchoring {
    type Anchor;
    /// `prime(ids)`.
    fn prime(&mut self, ids: &[String]);
    /// `for_record(id, origin_root=...)`.
    fn for_record(&mut self, id: &str, origin_root: Option<&[u32]>) -> Option<Self::Anchor>;
}

impl VerdictAnchoring for Anchoring {
    type Anchor = ClaimAnchor;
    fn prime(&mut self, ids: &[String]) {
        Anchoring::prime(self, ids);
    }
    fn for_record(&mut self, id: &str, origin_root: Option<&[u32]>) -> Option<ClaimAnchor> {
        self.for_record_at(id, origin_root)
    }
}

/// `MemoryVerdict`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemoryVerdict<U> {
    pub id: String,
    pub verification: VerificationStatus,
    pub path_drift: PathDriftReport,
    pub commit_drift: Option<CommitDriftStatus>,
    pub staleness_verdict: String,
    pub commit_drift_unread: Option<CommitDriftUnread>,
    pub commit_drift_unchecked: Option<U>,
}

/// `UnresolvedVerdict`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnresolvedVerdict {
    pub id: String,
    pub verification: VerificationStatus,
    pub path_drift: PathDriftReport,
}

/// `MemoryVerdict | UnresolvedVerdict`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Verdict<U> {
    Full(MemoryVerdict<U>),
    Unresolved(UnresolvedVerdict),
}

/// What the reference raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum VerdictError {
    /// `TypeError` of `compute_verification_status`: a naive `now` against
    /// an aware stamp, or the reverse.
    NaiveAwareMix,
    /// `AssertionError` of `resolve_verdicts`: the legs did not answer a row
    /// under `resolve=True`.
    Unanswered,
}

impl From<NaiveAwareMix> for VerdictError {
    fn from(_: NaiveAwareMix) -> Self {
        VerdictError::NaiveAwareMix
    }
}

/// `detect_path_drift(body, verified_paths=, absent_paths=, worktree_root=, anchor=)`.
pub type PathDriftFn<'f, A> = dyn FnMut(&PyStr, &PathRefs<'_>, &PathRefs<'_>, Option<&[u32]>, Option<&A>) -> PathDriftReport
    + 'f;

/// The effects one call reads through.
pub struct Reads<'f, A: VerdictAnchoring> {
    pub anchoring: Option<&'f mut A>,
    pub path_drift: &'f mut PathDriftFn<'f, A::Anchor>,
    /// `commit_legs.unchecked_verdict`.
    pub unchecked_verdict: &'f dyn Fn(&str) -> String,
}

/// `_open_legs`: the legs the constructor builds, or None where they are
/// not active.
pub fn open_legs<L: CommitLegs>(make: impl FnOnce() -> L) -> Option<L> {
    let legs = make();
    legs.active().then_some(legs)
}

fn prime<L: CommitLegs, A: VerdictAnchoring>(
    drift: &mut Option<L>,
    reads: &mut Reads<'_, A>,
    memories: &[VerdictMemory],
) {
    let ids: Vec<String> = memories.iter().map(|m| m.id.clone()).collect();
    if let Some(d) = drift.as_mut() {
        d.prime(&ids);
    } else if let Some(a) = reads.anchoring.as_mut() {
        a.prime(&ids);
    }
}

/// `resolve_verdicts`: every memory's verdict, in order.
pub fn resolve_verdicts<L: CommitLegs, A: VerdictAnchoring>(
    memories: &[VerdictMemory],
    make_legs: impl FnOnce() -> L,
    now: DateTime,
    stale_after_days: i64,
    reads: &mut Reads<'_, A>,
) -> Result<Vec<MemoryVerdict<L::Unchecked>>, VerdictError> {
    let mut drift = open_legs(make_legs);
    prime(&mut drift, reads, memories);
    let mut out = Vec::with_capacity(memories.len());
    for memory in memories {
        match verdict(memory, drift.as_mut(), now, stale_after_days, true, reads)? {
            Verdict::Full(v) => out.push(v),
            Verdict::Unresolved(_) => return Err(VerdictError::Unanswered),
        }
    }
    if let Some(d) = drift.as_mut() {
        d.store();
    }
    Ok(out)
}

/// `memoised_verdicts`: every memory's verdict from the memo alone.
pub fn memoised_verdicts<L: CommitLegs, A: VerdictAnchoring>(
    memories: &[VerdictMemory],
    make_legs: impl FnOnce() -> L,
    now: DateTime,
    stale_after_days: i64,
    reads: &mut Reads<'_, A>,
) -> Result<Vec<Verdict<L::Unchecked>>, VerdictError> {
    let mut drift = open_legs(make_legs);
    prime(&mut drift, reads, memories);
    memories
        .iter()
        .map(|m| verdict(m, drift.as_mut(), now, stale_after_days, false, reads))
        .collect()
}

/// `memory_verdict`: `resolve_verdicts` for one memory.
pub fn memory_verdict<L: CommitLegs, A: VerdictAnchoring>(
    memory: &VerdictMemory,
    make_legs: impl FnOnce() -> L,
    now: DateTime,
    stale_after_days: i64,
    reads: &mut Reads<'_, A>,
) -> Result<MemoryVerdict<L::Unchecked>, VerdictError> {
    let mut all = resolve_verdicts(
        std::slice::from_ref(memory),
        make_legs,
        now,
        stale_after_days,
        reads,
    )?;
    Ok(all.remove(0))
}

/// `_verdict`: one row's verdict through `drift`.
pub fn verdict<L: CommitLegs, A: VerdictAnchoring>(
    memory: &VerdictMemory,
    drift: Option<&mut L>,
    now: DateTime,
    stale_after_days: i64,
    resolve: bool,
    reads: &mut Reads<'_, A>,
) -> Result<Verdict<L::Unchecked>, VerdictError> {
    let verification = compute_verification_status(memory.last_verified_at, now, stale_after_days)?;
    let origin_root = memory.worktree_root.as_deref();
    let anchor = match reads.anchoring.as_mut() {
        Some(a) => a.for_record(&memory.id, origin_root),
        None => None,
    };
    let verified = record_path_refs(&memory.record, false);
    let absent = record_path_refs(&memory.record, true);
    let root = if anchor.is_none() { origin_root } else { None };
    let path_drift = (reads.path_drift)(&memory.body, &verified, &absent, root, anchor.as_ref());
    let mut commit_drift = None;
    let mut unread = None;
    let mut unchecked = None;
    if let Some(d) = drift {
        let (answered, resolved) = d.lookup(memory, memory.last_verified_at.as_ref(), resolve);
        if !answered {
            return Ok(Verdict::Unresolved(UnresolvedVerdict {
                id: memory.id.clone(),
                verification,
                path_drift,
            }));
        }
        match resolved {
            Resolved::Unread(u) => unread = Some(u),
            Resolved::Unchecked(u) => unchecked = Some(u),
            Resolved::Drift(r) => commit_drift = Some(commit_drift_status(&r)),
            Resolved::None => {}
        }
    }
    let rollup = staleness_verdict_from_report(
        &verification,
        &path_drift,
        commit_drift.as_ref().map(|c| c.commits_since_verify),
    );
    let staleness_verdict = if unread.is_some() {
        "spot_check_required".to_string()
    } else if unchecked.is_some() {
        (reads.unchecked_verdict)(rollup)
    } else {
        rollup.to_string()
    };
    Ok(Verdict::Full(MemoryVerdict {
        id: memory.id.clone(),
        verification,
        path_drift,
        commit_drift,
        staleness_verdict,
        commit_drift_unread: unread,
        commit_drift_unchecked: unchecked,
    }))
}
