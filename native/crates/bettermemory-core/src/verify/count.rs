//! `verify.py`'s commit-drift count (lines at v9):
//! `resolve_commit_drift_count` 924-1059, `_quiescent_drift_applicable`
//! 1062-1153, `_anchor_specs` 1156-1176, `applicable_unread` 1179-1199,
//! `compute_commit_drift` 1201-1334 under `git_read_budget.unread`, and
//! `_narrow_commit_drift` 1337-1411. `resolve_hit_drift` (1414-1533) is
//! left for a later unit.
//!
//! The two git reads `weak`'s [`DriftGit`] does not hold
//! (`origin.repo_toplevel` and
//! `origin_dates.commit_author_timestamps_touching_pathspecs`) are
//! [`CountGit`]'s. The record's anchor is its `commit_root` as a closure,
//! `commit_anchors.project_commit_anchors` is a closure answering `(paths,
//! unmapped)`, and the count since the verify is `verify_basis`'s, read
//! through the caller's [`AnchorWalk`].

use std::cell::Cell;
use std::collections::BTreeSet;

use crate::attestation_paths::{Paths, Raised};
use crate::claim_drift::LiteralLines;
use crate::claims::literal::load_claims;
use crate::claims::{claim_paths, Claim};
use crate::datetime::DateTime;
use crate::fsdata::Flavour;
use crate::git_read_budget::{unread, Raise, ReadDeadline, Scope};
use crate::origin_history::{HistoryUnread, ReachableWalk, Unwalked};
use crate::pyvalue::{PyStr, PyValue};
use crate::verify::anchors::{commit_drift_anchor_paths, AnchorError};
use crate::verify::legs::{resolve_commit_drift, resolve_reachable, resolve_with_claims, Reads};
use crate::verify::status::{
    commit_drift_status, gated_claims, unreadable, CommitDriftStatus, ResolvedCommitDrift,
};
use crate::verify::weak::{after_read, DriftGit, LiteralPathspecs, Place, WeakError};
use crate::verify_anchoring::attest::pure_text;
use crate::verify_basis::{
    count_since_verify_until, date_count_uncertainty, dates_since, AnchorWalk,
};
use crate::verify_candidates::scan::UserLookup;

/// The git reads of the count beyond [`DriftGit`]'s.
pub trait CountGit: DriftGit {
    /// `origin.repo_toplevel(cwd)`.
    fn repo_toplevel(&self, cwd: &[u32]) -> Option<PyStr>;
    /// `origin_dates.commit_author_timestamps_touching_pathspecs`.
    fn commit_author_timestamps_touching_pathspecs(
        &self,
        at: Place,
        pathspecs: &[PyStr],
    ) -> Option<Vec<DateTime>>;
}

/// What every step reads through: [`Reads`] over a [`CountGit`].
#[derive(Clone, Copy)]
pub struct CountReads<'a> {
    pub git: &'a dyn CountGit,
    pub at: Place<'a>,
    pub literal_pathspecs: LiteralPathspecs<'a>,
    pub lit: &'a dyn LiteralLines,
}

impl<'a> CountReads<'a> {
    fn reads(self) -> Reads<'a> {
        Reads {
            git: self.git,
            at: self.at,
            literal_pathspecs: self.literal_pathspecs,
            lit: self.lit,
        }
    }

    fn with_top(self, toplevel: Option<&'a [u32]>) -> CountReads<'a> {
        CountReads {
            at: Place {
                cwd: self.at.cwd,
                toplevel,
            },
            ..self
        }
    }
}

/// What stops an answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CountError {
    Weak(WeakError),
    Anchor(AnchorError),
    Root(Raised),
}

impl From<WeakError> for CountError {
    fn from(e: WeakError) -> Self {
        CountError::Weak(e)
    }
}

fn aware(since: &DateTime) -> DateTime {
    let mut out = *since;
    if out.offset_us.is_none() {
        out.offset_us = Some(0);
    }
    out
}

/// `resolve_commit_drift_count`: None where commit drift does not apply.
pub fn resolve_commit_drift_count(
    c: CountReads,
    since: &DateTime,
    unfiltered: i64,
    anchors: &[PyStr],
    claims: &[Claim],
    walk: Option<&ReachableWalk>,
) -> Result<Option<i64>, WeakError> {
    if unreadable(claims) {
        return Ok(Some(walk.map_or(unfiltered, |w| {
            i64::try_from(w.commits.len()).unwrap_or(i64::MAX)
        })));
    }
    let claims = gated_claims(claims);
    if let Some(walk) = walk {
        return Ok(resolve_reachable(c.reads(), walk, anchors, &claims)?.map(|r| r.count));
    }
    if !claims.is_empty() {
        let r = resolve_with_claims(c.reads(), since, unfiltered, anchors, &claims)?;
        return Ok(r.map(|r| r.count));
    }
    if anchors.is_empty() {
        return Ok(None);
    }
    let Some(specs) = after_read(c.git, c.git.resolve_repo_pathspecs(c.at, anchors))? else {
        return Ok(Some(unfiltered));
    };
    if specs.is_empty() {
        return Ok(None);
    }
    let spell = after_read(c.git, c.git.spelled(c.at, &specs, None, Some(since)))?;
    if spell.declined() {
        return Ok(Some(unfiltered));
    }
    let pathspecs = (c.literal_pathspecs)(&spell.names(&specs));
    let touching = c
        .git
        .commit_author_timestamps_touching_pathspecs(c.at, &pathspecs);
    let Some(touching) = after_read(c.git, touching)? else {
        return Ok(Some(unfiltered));
    };
    if touching.is_empty() {
        return Ok(None);
    }
    match dates_since(Some(Ok(&touching)), &aware(since)) {
        Ok(Some(Ok(n))) => Ok(Some(i64::try_from(n).unwrap_or(i64::MAX))),
        Ok(_) => Ok(None),
        Err(_) => Err(WeakError::NaiveAware),
    }
}

/// `_anchor_specs`: None where git could not resolve the anchors.
pub fn anchor_specs(
    git: &dyn DriftGit,
    at: Place,
    anchors: &[PyStr],
    claims: &[Claim],
) -> Option<Vec<PyStr>> {
    let mut specs = Vec::new();
    if !anchors.is_empty() {
        specs = git.resolve_repo_pathspecs(at, anchors)?;
    }
    specs.extend(claim_paths(&gated_claims(claims)));
    let mut seen = BTreeSet::new();
    Some(
        specs
            .into_iter()
            .filter(|s| seen.insert(s.clone()))
            .collect(),
    )
}

/// `toplevel`, or `repo_toplevel(cwd)` where it is None.
fn toplevel_of(git: &dyn CountGit, at: Place) -> Result<Option<PyStr>, ReadDeadline> {
    match at.toplevel {
        Some(t) => Ok(Some(t.to_vec())),
        None => after_read(git, git.repo_toplevel(at.cwd)),
    }
}

/// `applicable_unread`: `why`, or None where every anchor escapes the
/// repository and no claim governs a path.
pub fn applicable_unread(
    why: HistoryUnread,
    git: &dyn CountGit,
    at: Place,
    anchors: &[PyStr],
    claims: &[Claim],
) -> Option<HistoryUnread> {
    let Ok(top) = toplevel_of(git, at) else {
        return Some(HistoryUnread::CallBudget);
    };
    let at = Place {
        cwd: at.cwd,
        toplevel: top.as_deref(),
    };
    match after_read(git, anchor_specs(git, at, anchors, claims)) {
        Err(_) => Some(HistoryUnread::CallBudget),
        Ok(Some(s)) if s.is_empty() => None,
        _ => Some(why),
    }
}

/// `_quiescent_drift_applicable`.
pub fn quiescent_drift_applicable(c: CountReads, anchors: &[PyStr], claims: &[Claim]) -> bool {
    quiescent_applicable(c, anchors, claims).unwrap_or(true)
}

fn quiescent_applicable(
    c: CountReads,
    anchors: &[PyStr],
    claims: &[Claim],
) -> Result<bool, ReadDeadline> {
    let top = toplevel_of(c.git, c.at)?;
    let c = c.with_top(top.as_deref());
    let Some(combined) = after_read(c.git, anchor_specs(c.git, c.at, anchors, claims))? else {
        return Ok(true);
    };
    if combined.is_empty() {
        return Ok(false);
    }
    let spell = after_read(c.git, c.git.spelled(c.at, &combined, None, None))?;
    if spell.declined() {
        return Ok(true);
    }
    let pathspecs = (c.literal_pathspecs)(&spell.names(&combined));
    Ok(after_read(c.git, c.git.paths_in_history(c.at, &pathspecs))?.unwrap_or(true))
}

/// `_narrow_commit_drift`: the narrowed resolution, or None.
#[allow(clippy::too_many_arguments)]
pub fn narrow_commit_drift(
    c: CountReads,
    since: &DateTime,
    count: i64,
    anchors: &[PyStr],
    claims: &[Claim],
    walk: Option<&ReachableWalk>,
    basis: &str,
) -> Result<Option<ResolvedCommitDrift>, WeakError> {
    let mut out = ResolvedCommitDrift::new(count);
    out.basis = basis.to_string();
    if count > 0 {
        let top = toplevel_of(c.git, c.at)?;
        let c = c.with_top(top.as_deref());
        let failed = Cell::new(None);
        let mut count_only = || {
            resolve_commit_drift_count(c, since, count, anchors, &[], None).unwrap_or_else(|e| {
                failed.set(Some(e));
                None
            })
        };
        let resolved = resolve_commit_drift(
            c.reads(),
            since,
            count,
            anchors,
            claims,
            walk,
            &mut count_only,
        )?;
        if let Some(e) = failed.take() {
            return Err(e);
        }
        let Some(resolved) = resolved else {
            return Ok(None);
        };
        out.count = resolved.count;
        out.claims_checked = resolved.claims_checked;
        out.claims_drifted = resolved.claims_drifted;
    } else if !quiescent_applicable(c, anchors, claims)? {
        return Ok(None);
    }
    Ok(Some(out))
}

/// What `compute_commit_drift` answers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CommitDrift {
    None,
    Status(CommitDriftStatus),
    Unread(HistoryUnread),
}

impl From<HistoryUnread> for CommitDrift {
    fn from(why: HistoryUnread) -> Self {
        CommitDrift::Unread(why)
    }
}

/// `origin_dates.commit_author_timestamps(cwd)`.
pub type Timestamps<'a> = &'a mut dyn FnMut(&[u32]) -> Option<Result<Vec<DateTime>, HistoryUnread>>;
/// The anchor's `commit_root(verified_head)` root.
pub type CommitRoot<'a> = &'a dyn Fn(Option<&[u32]>) -> Option<PyStr>;
/// `project_commit_anchors(paths, anchor)`: `(paths, unmapped)`.
pub type Project<'a> = &'a dyn Fn(&[PyStr]) -> (Vec<PyStr>, bool);

/// The record `compute_commit_drift` reads.
pub struct Record<'a> {
    pub last_verified_at: Option<DateTime>,
    pub verified_paths: &'a Paths,
    pub body: &'a [u32],
    pub claims: &'a [PyStr],
    pub verified_head: Option<&'a [u32]>,
}

/// The effects `compute_commit_drift` reads through.
pub struct Effects<'a, 'l> {
    pub git: &'a dyn CountGit,
    pub literal_pathspecs: LiteralPathspecs<'a>,
    pub lit: &'a dyn LiteralLines,
    pub origin: &'a mut dyn AnchorWalk,
    pub unwalked: &'a mut Unwalked,
    pub timestamps: Timestamps<'a>,
    /// None for a record with no anchor.
    pub commit_root: Option<CommitRoot<'a>>,
    pub project: Project<'a>,
    pub home: Option<&'a [u32]>,
    pub lookup: &'a mut UserLookup<'l>,
}

/// `compute_commit_drift`, under `git_read_budget.unread`.
pub fn compute_commit_drift(
    scope: &mut Scope,
    record: &Record,
    fx: &mut Effects,
) -> Result<CommitDrift, CountError> {
    unread(scope, None, |_| match commit_drift_body(record, fx) {
        Err(CountError::Weak(WeakError::ReadDeadline)) => Err(Raise::Deadline(ReadDeadline)),
        out => out.map_err(Raise::Other),
    })
}

fn commit_drift_body(record: &Record, fx: &mut Effects) -> Result<CommitDrift, CountError> {
    let Some(mut since) = record.last_verified_at else {
        return Ok(CommitDrift::None);
    };
    let Some(commit_root) = fx.commit_root else {
        return Ok(CommitDrift::None);
    };
    let Some(root) =
        after_read(fx.git, commit_root(record.verified_head)).map_err(WeakError::from)?
    else {
        return Ok(CommitDrift::None);
    };
    let cwd = pure_text(Flavour::host(), &root).map_err(CountError::Root)?;
    let found = commit_drift_anchor_paths(record.body, record.verified_paths, fx.home, fx.lookup)
        .map_err(CountError::Anchor)?;
    let parsed = if record.claims.is_empty() {
        Vec::new()
    } else {
        let raw: Vec<PyValue> = record.claims.iter().cloned().map(PyValue::Str).collect();
        load_claims(&raw).map_err(|e| CountError::Weak(WeakError::Literal(e)))?
    };
    let (anchors, unmapped) = after_read(fx.git, (fx.project)(&found)).map_err(WeakError::from)?;
    if unmapped {
        return Ok(CommitDrift::None);
    }
    if anchors.is_empty() && parsed.is_empty() {
        return Ok(CommitDrift::None);
    }
    since = aware(&since);
    let timestamps = &mut *fx.timestamps;
    let counted = count_since_verify_until(
        fx.origin,
        fx.unwalked,
        &cwd,
        &since,
        record.verified_head,
        || timestamps(&cwd),
        || fx.git.deadline(),
    )
    .map_err(|_| CountError::Weak(WeakError::NaiveAware))?;
    if fx.git.deadline() {
        return Ok(CommitDrift::Unread(HistoryUnread::CallBudget));
    }
    let at = Place {
        cwd: &cwd,
        toplevel: None,
    };
    let counted = match counted {
        None => return Ok(CommitDrift::None),
        Some(Err(why)) => {
            return Ok(applicable_unread(why, fx.git, at, &anchors, &parsed)
                .map_or(CommitDrift::None, CommitDrift::Unread));
        }
        Some(Ok(counted)) => counted,
    };
    let c = CountReads {
        git: fx.git,
        at: Place {
            cwd: &cwd,
            toplevel: counted.toplevel.as_deref(),
        },
        literal_pathspecs: fx.literal_pathspecs,
        lit: fx.lit,
    };
    let count = i64::try_from(counted.count).unwrap_or(i64::MAX);
    let resolved = narrow_commit_drift(
        c,
        &since,
        count,
        &anchors,
        &parsed,
        counted.walk.as_ref(),
        counted.basis,
    )?;
    Ok(match resolved {
        None => CommitDrift::None,
        Some(r) => match date_count_uncertainty(r.count, &r.basis) {
            Some(why) => CommitDrift::Unread(why),
            None => CommitDrift::Status(commit_drift_status(&r)),
        },
    })
}
