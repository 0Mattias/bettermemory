//! `verify.py`'s two-leg narrowing (lines at v9): `_claim_legs` 705-743,
//! `_resolve_reachable` 746-838, `_resolve_with_claims` 841-921 and
//! `resolve_commit_drift` 595-671. `resolve_commit_drift_count`, which the
//! last calls for a memory with neither claims nor a walk, is a closure.

use std::collections::BTreeSet;

use crate::claim_drift::LiteralLines;
use crate::claims::literal::governed_claim_paths;
use crate::claims::{claim_paths, path_key, Claim};
use crate::datetime::DateTime;
use crate::origin_history::ReachableWalk;
use crate::pyvalue::PyStr;
use crate::verify::status::{gated_claims, unreadable, ResolvedCommitDrift};
use crate::verify::weak::{
    after_read, weak_tier_evaluation, DriftGit, LiteralPathspecs, Place, Set, WeakError,
};
use crate::verify_basis::{BASIS_AUTHOR_DATE, BASIS_REACHABILITY};

/// What every resolver reads through: git, where it runs, the
/// `literal_pathspecs` spelling (which reads the environment) and the
/// literal reader the claim index uses.
#[derive(Clone, Copy)]
pub struct Reads<'a> {
    pub git: &'a dyn DriftGit,
    pub at: Place<'a>,
    pub literal_pathspecs: LiteralPathspecs<'a>,
    pub lit: &'a dyn LiteralLines,
}

/// The two legs, `(ungoverned, governed)`.
pub type Legs = (Vec<PyStr>, Vec<PyStr>);

/// What a resolver answers: None where commit drift does not apply.
pub type Resolved = Result<Option<ResolvedCommitDrift>, WeakError>;

fn resolved(count: usize, checked: usize, drifted: Vec<PyStr>, basis: &str) -> ResolvedCommitDrift {
    ResolvedCommitDrift {
        count: i64::try_from(count).unwrap_or(i64::MAX),
        claims_checked: i64::try_from(checked).unwrap_or(i64::MAX),
        claims_drifted: drifted,
        basis: basis.to_string(),
    }
}

/// `_claim_legs`: `(ungoverned, governed)` pathspecs, or None when git
/// cannot resolve the anchors.
pub fn claim_legs(
    r: Reads,
    anchors: &[PyStr],
    claims: &[Claim],
) -> Result<Option<Legs>, WeakError> {
    let governed = if claims.is_empty() {
        Vec::new()
    } else {
        governed_claim_paths(claims)?
    };
    let keys: BTreeSet<PyStr> = governed.iter().map(|p| path_key(p)).collect();
    let mut specs = Vec::new();
    if !anchors.is_empty() {
        match after_read(r.git, r.git.resolve_repo_pathspecs(r.at, anchors))? {
            None => return Ok(None),
            Some(resolved) => specs = resolved,
        }
    }
    specs.extend(claim_paths(claims));
    let mut seen = BTreeSet::new();
    let ungoverned = specs
        .into_iter()
        .filter(|s| seen.insert(s.clone()) && !keys.contains(&path_key(s)))
        .collect();
    Ok(Some((ungoverned, governed)))
}

/// One governed window through the `weak` tier: its implicated commits,
/// or the whole window where the tier declines.
fn governed_window(
    r: Reads,
    post: &[PyStr],
    governed: &[PyStr],
    claims: &[Claim],
    spell: &dyn crate::verify::weak::Spelling,
    escalating: &mut Set,
) -> Result<Vec<PyStr>, WeakError> {
    let (drifted, implicated) = weak_tier_evaluation(
        r.git,
        r.at,
        post,
        governed,
        claims,
        Some(spell),
        r.literal_pathspecs,
        r.lit,
    )?;
    escalating.extend(implicated.unwrap_or_else(|| post.iter().cloned().collect()));
    Ok(drifted)
}

/// `_resolve_reachable`: the narrowing over a reachable walk.
pub fn resolve_reachable(
    r: Reads,
    walk: &ReachableWalk,
    anchors: &[PyStr],
    claims: &[Claim],
) -> Resolved {
    let checked = claims.len();
    let conservative = Some(resolved(
        walk.commits.len(),
        checked,
        Vec::new(),
        BASIS_REACHABILITY,
    ));
    let Some((ungoverned, governed)) = claim_legs(r, anchors, claims)? else {
        return Ok(conservative);
    };
    if ungoverned.is_empty() && governed.is_empty() {
        return Ok(None);
    }
    if !walk.listed {
        return Ok(conservative);
    }
    let combined: Vec<PyStr> = ungoverned.iter().chain(&governed).cloned().collect();
    let spell = after_read(
        r.git,
        r.git.spelled(r.at, &combined, Some(&walk.anchor), None),
    )?;
    if spell.declined() {
        return Ok(conservative);
    }
    let mut escalating = Set::new();
    if !ungoverned.is_empty() {
        escalating.extend(walk.shas_touching(&spell.names(&ungoverned)));
    }
    let mut drifted = Vec::new();
    if !governed.is_empty() {
        let touched = walk.shas_touching(&spell.names(&governed));
        let post: Vec<PyStr> = walk
            .commits
            .iter()
            .filter(|s| touched.contains(*s))
            .cloned()
            .collect();
        if !post.is_empty() {
            drifted = governed_window(r, &post, &governed, claims, &*spell, &mut escalating)?;
        }
    }
    if !escalating.is_empty() {
        return Ok(Some(resolved(
            escalating.len(),
            checked,
            drifted,
            BASIS_REACHABILITY,
        )));
    }
    let specs = (r.literal_pathspecs)(&spell.names(&combined));
    Ok(
        match after_read(r.git, r.git.paths_in_history(r.at, &specs))? {
            None => conservative,
            Some(false) => None,
            Some(true) => Some(resolved(0, checked, Vec::new(), BASIS_REACHABILITY)),
        },
    )
}

/// `ts > since` as Python orders two datetimes.
fn after(ts: &DateTime, since: &DateTime) -> Result<bool, WeakError> {
    match (ts.instant_us(), since.instant_us()) {
        (Some(a), Some(b)) => Ok(a > b),
        (None, None) => Ok(ts.wall_micros() > since.wall_micros()),
        _ => Err(WeakError::NaiveAware),
    }
}

/// `_resolve_with_claims`: the two legs over the author-date logs.
pub fn resolve_with_claims(
    r: Reads,
    since: &DateTime,
    unfiltered: i64,
    anchors: &[PyStr],
    claims: &[Claim],
) -> Resolved {
    let checked = claims.len();
    let mut conservative = resolved(0, checked, Vec::new(), BASIS_AUTHOR_DATE);
    conservative.count = unfiltered;
    let conservative = Some(conservative);
    let Some((ungoverned, governed)) = claim_legs(r, anchors, claims)? else {
        return Ok(conservative);
    };
    let mut since = *since;
    if since.offset_us.is_none() {
        since.offset_us = Some(0);
    }
    let combined: Vec<PyStr> = ungoverned.iter().chain(&governed).cloned().collect();
    let spell = after_read(r.git, r.git.spelled(r.at, &combined, None, Some(&since)))?;
    if spell.declined() {
        return Ok(conservative);
    }
    let mut escalating = Set::new();
    let mut every_leg_phantom = true;
    let mut drifted = Vec::new();
    for (leg, is_governed) in [(&ungoverned, false), (&governed, true)] {
        if leg.is_empty() {
            continue;
        }
        let specs = (r.literal_pathspecs)(&spell.names(leg));
        let pairs = r
            .git
            .commit_author_sha_pairs_touching_pathspecs(r.at, &specs);
        let Some(pairs) = after_read(r.git, pairs)? else {
            return Ok(conservative);
        };
        if pairs.is_empty() {
            continue;
        }
        every_leg_phantom = false;
        let mut post = Vec::new();
        for (ts, sha) in pairs {
            if after(&ts, &since)? {
                post.push(sha);
            }
        }
        if !is_governed {
            escalating.extend(post);
        } else if !post.is_empty() {
            drifted = governed_window(r, &post, &governed, claims, &*spell, &mut escalating)?;
        }
    }
    if every_leg_phantom {
        return Ok(None);
    }
    Ok(Some(resolved(
        escalating.len(),
        checked,
        drifted,
        BASIS_AUTHOR_DATE,
    )))
}

/// `resolve_commit_drift`. `count` is `resolve_commit_drift_count` with no
/// claims, called only where neither a walk nor a claim is given.
pub fn resolve_commit_drift(
    r: Reads,
    since: &DateTime,
    unfiltered: i64,
    anchors: &[PyStr],
    claims: &[Claim],
    walk: Option<&ReachableWalk>,
    count: &mut dyn FnMut() -> Option<i64>,
) -> Resolved {
    if unreadable(claims) {
        let checked = claims.len();
        return Ok(Some(match walk {
            Some(w) => resolved(w.commits.len(), checked, Vec::new(), BASIS_REACHABILITY),
            None => {
                let mut out = resolved(0, checked, Vec::new(), BASIS_AUTHOR_DATE);
                out.count = unfiltered;
                out
            }
        }));
    }
    let claims = gated_claims(claims);
    if let Some(walk) = walk {
        return resolve_reachable(r, walk, anchors, &claims);
    }
    if !claims.is_empty() {
        return resolve_with_claims(r, since, unfiltered, anchors, &claims);
    }
    Ok(count().map(ResolvedCommitDrift::new))
}
