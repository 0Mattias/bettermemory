//! `detect_path_drift` of `verify_anchoring.py` and its two readers
//! (reference lines 893-1234), and `holds_commit` (574-595).
//!
//! Effects are arguments. The host ([`Host`]) answers `os.name` and the
//! `os.path` calls of `attestation_paths`; the module functions reach the
//! read through [`DriftReads`] (the passes' [`ClaimReads`] with
//! `_is_multi_segment_routelike` and `_worktree_root_is_live`); the claim
//! anchor is a [`ReadAnchor`]; `_extract_candidates` runs the crate's
//! scan (without the body parse memo) with the home and user lookup of
//! [`Effects`]; `_RELATIVE_CITATION_RE` is the passes' closure.
//!
//! The report and the body pass hold `str`; a path or key holding a lone
//! surrogate declines ([`Raised::NotPorted`]) where the reference answers.

use std::cell::{Cell, RefCell};
use std::collections::{BTreeSet, HashSet};

use crate::attestation_paths::{Host, Paths, Raised};
use crate::fsdata::Flavour;
use crate::fsdata::Fs;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::githead::discovery::{locate, Env};
use crate::gitobjects::files::reader::Fault;
use crate::gitobjects::files::{ObjectReader, Stop};
use crate::gitobjects::Raised as ObjRaised;
use crate::path_drift_steps::{check_body_candidates, Buckets};
use crate::pyvalue::PyStr;
use crate::verify_anchoring::anchor::CONVICTING_ANCHORS;
use crate::verify_anchoring::attest::{normalize_attestations, pure_text};
use crate::verify_anchoring::passes::{
    check_anchored_attestations, check_anchored_citations, read_attestations, read_citations,
    relative_attestation_keys, AnchoredRead, CitationMatches, ClaimReads, ReadAnchor,
};
use crate::verify_anchoring::report::PathDriftReport;
use crate::verify_candidates::scan::{extract_candidates, Candidate, ScanError, UserLookup};

/// The module functions a drift read calls beyond the passes' own.
pub trait DriftReads: ClaimReads {
    /// `_is_multi_segment_routelike(path)`.
    fn routelike(&mut self, path: &[u32]) -> bool;
    /// `_worktree_root_is_live(root)`.
    fn root_is_live(&mut self, root: &[u32]) -> Result<bool, Raised>;
}

/// The effects of one `detect_path_drift` call.
pub struct Effects<'a, 'b> {
    pub host: &'a dyn Host,
    pub reads: &'a mut dyn DriftReads,
    /// What `~` expands to in the candidate scan (None: unknown).
    pub home: Option<&'a [u32]>,
    /// `pwd.getpwnam` for `~name` in the candidate scan.
    pub lookup: &'a mut UserLookup<'b>,
    pub matches: CitationMatches<'a>,
}

/// What a drift read raises: the host's raise (or a decline), or the
/// candidate scan's.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DriftError {
    Raised(Raised),
    Scan(ScanError),
}

impl From<Raised> for DriftError {
    fn from(e: Raised) -> Self {
        DriftError::Raised(e)
    }
}

impl From<ScanError> for DriftError {
    fn from(e: ScanError) -> Self {
        DriftError::Scan(e)
    }
}

fn flavour_of(host: &dyn Host) -> Flavour {
    if host.nt() {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

/// A code point text as `str`; a lone surrogate declines.
fn text(p: &[u32]) -> Result<String, Raised> {
    p.iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
        .ok_or(Raised::NotPorted)
}

fn texts(items: &[PyStr]) -> Result<Vec<String>, Raised> {
    items.iter().map(|p| text(p)).collect()
}

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn attested_sets(
    host: &dyn Host,
    reads: &mut dyn DriftReads,
    verified_paths: &Paths,
    absent_paths: &Paths,
) -> Result<(BTreeSet<PyStr>, BTreeSet<PyStr>), Raised> {
    let cell = RefCell::new(reads);
    let keys = |p: &[u32]| cell.borrow_mut().key(p);
    let verified = normalize_attestations(host, verified_paths, &keys)?;
    let absent = normalize_attestations(host, absent_paths, &keys)?;
    Ok((verified, absent))
}

/// `detect_path_drift(body, verified_paths=, absent_paths=,
/// worktree_root=, anchor=)`.
pub fn detect_path_drift(
    fx: &mut Effects<'_, '_>,
    body: &[u32],
    verified_paths: &Paths,
    absent_paths: &Paths,
    worktree_root: Option<&[u32]>,
    anchor: Option<&dyn ReadAnchor>,
) -> Result<PathDriftReport, DriftError> {
    if let Some(anchor) = anchor {
        return detect_anchored(fx, body, verified_paths, absent_paths, anchor);
    }
    let candidates = extract_candidates(body, fx.home, fx.lookup)?;
    let anchor_root = match worktree_root {
        Some(root) if fx.reads.root_is_live(root)? => Some(pure_text(flavour_of(fx.host), root)?),
        _ => None,
    };
    if candidates.is_empty() && anchor_root.is_none() {
        return Ok(PathDriftReport::default());
    }
    detect_unanchored(
        fx,
        body,
        candidates,
        verified_paths,
        absent_paths,
        anchor_root.as_deref(),
    )
}

/// `_detect_unanchored(body, candidates, verified_paths, absent_paths,
/// anchor_root)`. `anchor_root` is `str` of the live root's `Path`.
pub fn detect_unanchored(
    fx: &mut Effects<'_, '_>,
    body: &[u32],
    mut candidates: Vec<Candidate>,
    verified_paths: &Paths,
    absent_paths: &Paths,
    anchor_root: Option<&[u32]>,
) -> Result<PathDriftReport, DriftError> {
    let host = fx.host;
    let (verified_set, absent_set) = attested_sets(host, fx.reads, verified_paths, absent_paths)?;
    if let Some(root) = anchor_root {
        let deferred =
            relative_attestation_keys(host, fx.reads, root, verified_paths, absent_paths)?;
        candidates.retain(|c| !deferred.contains(&fx.reads.key(&c.0)));
    }
    let as_str = |set: &BTreeSet<PyStr>| -> Result<HashSet<String>, Raised> {
        set.iter().map(|p| text(p)).collect()
    };
    let (verified_str, absent_str) = (as_str(&verified_set)?, as_str(&absent_set)?);
    let listed = candidates
        .iter()
        .map(|(p, d, s)| Ok((text(p)?, *d, *s)))
        .collect::<Result<Vec<_>, Raised>>()?;
    let mut b = Buckets::default();
    let declined = Cell::new(false);
    {
        let reads = RefCell::new(&mut *fx.reads);
        check_body_candidates(
            &listed,
            &verified_str,
            &absent_str,
            &mut b,
            &mut |p| reads.borrow_mut().path_exists(&cps(p)),
            &mut |p| reads.borrow_mut().routelike(&cps(p)),
            &mut |p| {
                let key = reads.borrow_mut().key(&cps(p));
                text(&key).unwrap_or_else(|_| {
                    declined.set(true);
                    String::new()
                })
            },
        );
    }
    if declined.get() {
        return Err(Raised::NotPorted.into());
    }
    let mut out = AnchoredRead {
        checked: b.checked.iter().map(|p| cps(p)).collect(),
        missing: b.missing.iter().map(|p| cps(p)).collect(),
        claim_anchored: b.claim_anchored.iter().map(|p| cps(p)).collect(),
        verified: b.verified.iter().map(|p| cps(p)).collect(),
        expected_absent: b.expected_absent.iter().map(|p| cps(p)).collect(),
        dropped_as_route: b.dropped_as_route.iter().map(|p| cps(p)).collect(),
        seen: b.seen.iter().map(|p| cps(p)).collect(),
        ..AnchoredRead::default()
    };
    check_anchored_attestations(
        host,
        fx.reads,
        anchor_root,
        verified_paths,
        absent_paths,
        &mut out,
    )?;
    if let Some(root) = anchor_root {
        check_anchored_citations(host, fx.reads, root, body, fx.matches, &mut out)?;
    }
    Ok(PathDriftReport {
        checked: texts(&out.checked)?,
        missing: texts(&out.missing)?,
        verified: texts(&out.verified)?,
        expected_absent: texts(&out.expected_absent)?,
        dropped_as_route: texts(&out.dropped_as_route)?,
        claim_anchored_missing: texts(&out.claim_anchored)?,
        ..PathDriftReport::default()
    })
}

/// `_AnchoredRead.report(rule)`.
pub fn anchored_report(out: &AnchoredRead, rule: &str) -> Result<PathDriftReport, Raised> {
    Ok(PathDriftReport {
        checked: texts(&out.checked)?,
        missing: texts(&out.missing)?,
        verified: texts(&out.verified)?,
        expected_absent: texts(&out.expected_absent)?,
        dropped_as_route: texts(&out.dropped_as_route)?,
        claim_anchored_missing: texts(&out.claim_anchored)?,
        unconfirmed: texts(&out.unconfirmed)?,
        unconfirmed_reasons: out.reasons.clone(),
        present_but_attested_absent: texts(&out.present_absent)?,
        anchor: Some(rule.to_owned()),
    })
}

/// `path.split(" ", 1)[0]`.
fn before_space(path: &[u32]) -> &[u32] {
    let end = path.iter().position(|&c| c == 0x20).unwrap_or(path.len());
    &path[..end]
}

/// `_detect_anchored(body, verified_paths, absent_paths, anchor)`.
pub fn detect_anchored(
    fx: &mut Effects<'_, '_>,
    body: &[u32],
    verified_paths: &Paths,
    absent_paths: &Paths,
    anchor: &dyn ReadAnchor,
) -> Result<PathDriftReport, DriftError> {
    let host = fx.host;
    let mut out = AnchoredRead::default();
    let (verified_set, absent_set) = attested_sets(host, fx.reads, verified_paths, absent_paths)?;
    let deferred = match anchor.root() {
        Some(root) => {
            relative_attestation_keys(host, fx.reads, &root, verified_paths, absent_paths)?
        }
        None => HashSet::new(),
    };
    let reads = &mut *fx.reads;
    for (path, drop_if_missing, bare_spaced) in extract_candidates(body, fx.home, fx.lookup)? {
        let norm = reads.key(&path);
        let attested = verified_set.contains(&norm) || absent_set.contains(&norm);
        let (target, rule) = anchor.absolute(&path);
        if let Some(t) = &target {
            if deferred.contains(&reads.key(t)) {
                continue;
            }
        }
        let Some(target) = target else {
            if attested && out.first(reads, &path) {
                out.unconfirm(&path, &rule);
            }
            continue;
        };
        let convicting = CONVICTING_ANCHORS.contains(&rule.as_str());
        let exists = reads.path_exists(&target);
        if !exists && !attested {
            if bare_spaced {
                let cited_prefix = before_space(&path);
                let (prefix, _prefix_rule) = anchor.absolute(cited_prefix);
                if let Some(prefix) = prefix {
                    if reads.path_exists(&prefix) && out.first(reads, &prefix) {
                        out.checked.push(prefix.clone());
                        if verified_set.contains(&reads.key(cited_prefix)) {
                            out.verified.push(prefix);
                        }
                    }
                }
                continue;
            }
            if drop_if_missing {
                continue;
            }
            if reads.routelike(&target) {
                out.dropped_as_route.push(path);
                continue;
            }
        }
        if !out.first(reads, &target) {
            continue;
        }
        if !exists {
            if absent_set.contains(&norm) {
                out.checked.push(target.clone());
                out.expected_absent.push(target);
            } else {
                let claim = verified_set.contains(&norm);
                out.miss(reads, &target, convicting, &rule, claim);
            }
            continue;
        }
        if absent_set.contains(&norm) {
            if convicting {
                out.checked.push(target.clone());
                out.present_absent.push(target);
            } else {
                out.unconfirm(&target, &rule);
            }
            continue;
        }
        out.checked.push(target.clone());
        if verified_set.contains(&norm) {
            out.verified.push(target);
        }
    }
    read_attestations(host, reads, anchor, verified_paths, absent_paths, &mut out)?;
    if anchor.root().is_some() {
        read_citations(host, reads, anchor, body, fx.matches, &mut out)?;
    }
    Ok(anchored_report(&out, &anchor.rule())?)
}

/// `holds_commit(root, sha)`: the store of the checkout at `root`
/// (`locate`, not walking) asked `has(sha)`. An `OSError` or `ValueError`
/// answers False, as the reference catches them; any other raise of the
/// reader (`IndexError`, `struct.error`, `OverflowError`, `KeyError`) and
/// a decline of the Windows flavour are passed up.
pub fn holds_commit<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    root: &[u8],
    sha: &str,
) -> Result<bool, Fault> {
    let Some(found) = locate(rule, env, root, false, false) else {
        return Ok(false);
    };
    let ctx = rule.ctx;
    let mut reader =
        ObjectReader::new(ctx.fs, ctx.flavour, ctx.cwd, *rule.git, &found.gd.commondir);
    match reader.has(sha) {
        Ok(held) => Ok(held),
        Err(Fault::Stop(Stop::Raised(_)) | Fault::Raised(ObjRaised::ValueError)) => Ok(false),
        Err(e) => Err(e),
    }
}
