//! Unit search-1b: `search._build_hit` (reference lines 1041-1118) and
//! `search._claim_anchors` (1964-1979), the hit one scored record makes
//! and the claim anchors of a page of records.
//!
//! The drift read is an argument ([`Drift`]): `detect_path_drift` with
//! its effects ([`Effects`] answers it), called only when the caller asks
//! for path drift. The snippet memo is the caller's ([`SnippetMemo`]), and
//! so is the anchoring ([`RecordAnchors`], which `Anchoring` answers).

use crate::attestation_paths::{record_paths, Paths, Record};
use crate::records::summaries::MemoryHit;
use crate::records::Memory;
use crate::unicode::cps;
use crate::verify_anchoring::anchor::{Anchoring, ClaimAnchor};
use crate::verify_anchoring::detect::{detect_path_drift, DriftError, Effects};
use crate::verify_anchoring::passes::ReadAnchor;
use crate::verify_anchoring::report::PathDriftReport;

use super::num::round4;
use super::relevance;
use super::snippet::SnippetMemo;

/// `_query_biased_snippet`'s default `max_chars`.
pub const SNIPPET_MAX_CHARS: i64 = 200;

/// `detect_path_drift(body, verified_paths=, absent_paths=,
/// worktree_root=, anchor=)`.
pub trait Drift {
    fn detect(
        &mut self,
        body: &[u32],
        verified_paths: &Paths,
        absent_paths: &Paths,
        worktree_root: Option<&[u32]>,
        anchor: Option<&dyn ReadAnchor>,
    ) -> Result<PathDriftReport, DriftError>;
}

impl Drift for Effects<'_, '_> {
    fn detect(
        &mut self,
        body: &[u32],
        verified_paths: &Paths,
        absent_paths: &Paths,
        worktree_root: Option<&[u32]>,
        anchor: Option<&dyn ReadAnchor>,
    ) -> Result<PathDriftReport, DriftError> {
        detect_path_drift(
            self,
            body,
            verified_paths,
            absent_paths,
            worktree_root,
            anchor,
        )
    }
}

/// One scored record as `_build_hit` takes it.
pub struct Scored<'a> {
    pub memory: &'a Memory,
    /// The unrounded score.
    pub score: f64,
    pub matched: &'a [String],
    pub query_unique: usize,
}

/// `_build_hit(memory, score, matched, query_unique=, anchor=,
/// path_drift=)`. With `path_drift` false the drift read is not made and
/// the hit carries the empty report; the origin's `worktree_root` is
/// passed only to a read with no anchor.
pub fn build_hit(
    memo: &SnippetMemo,
    drift: &mut dyn Drift,
    scored: &Scored<'_>,
    anchor: Option<&dyn ReadAnchor>,
    path_drift: bool,
) -> Result<MemoryHit, DriftError> {
    let memory = &scored.memory.fields;
    let report = if path_drift {
        let record = Record {
            verified_paths: memory.verified_paths.clone(),
            verified_absent_paths: memory.verified_absent_paths.clone(),
            verified_path_targets: memory.verified_path_targets.clone(),
            verified_absent_path_targets: memory.verified_absent_path_targets.clone(),
        };
        let worktree_root = match (anchor, &memory.origin) {
            (None, Some(origin)) => origin.worktree_root.as_deref(),
            _ => None,
        };
        drift.detect(
            &memory.body,
            &record_paths(&record, false),
            &record_paths(&record, true),
            worktree_root,
            anchor,
        )?
    } else {
        PathDriftReport::default()
    };
    let matched = scored.matched;
    let texts = |v: &[String]| v.iter().map(|s| cps(s)).collect::<Vec<_>>();
    let count = |v: &[String]| i64::try_from(v.len()).unwrap_or(i64::MAX);
    Ok(MemoryHit {
        id: memory.id.clone(),
        scopes: memory.scopes.clone(),
        confidence: memory.confidence,
        category: memory.category,
        snippet: memo.query_biased_snippet(&memory.body, matched, SNIPPET_MAX_CHARS),
        score: round4(scored.score),
        relevance: cps(relevance(matched.len(), scored.query_unique)),
        match_terms: texts(matched),
        query_unique: i64::try_from(scored.query_unique).unwrap_or(i64::MAX),
        created: memory.created,
        updated: memory.updated,
        last_verified_at: memory.last_verified_at,
        path_drift_checked: count(&report.checked),
        path_drift_missing: count(&report.missing),
        path_drift_checked_paths: texts(&report.checked),
        path_drift_missing_paths: texts(&report.missing),
        path_drift_verified_paths: texts(&report.verified),
        path_drift_expected_absent_paths: texts(&report.expected_absent),
        path_drift_present_but_attested_absent: texts(&report.present_but_attested_absent),
        path_drift_dropped_as_route_paths: texts(&report.dropped_as_route),
        path_drift_claim_anchored_missing_paths: texts(&report.claim_anchored_missing),
    })
}

/// The two calls `_claim_anchors` makes of an `Anchoring`.
pub trait RecordAnchors {
    /// `prime(record_ids)`.
    fn prime(&mut self, record_ids: &[String]);
    /// `for_record(record_id, origin_root=)`.
    fn for_record(&mut self, record_id: &str, origin_root: Option<&[u32]>) -> Option<ClaimAnchor>;
}

impl RecordAnchors for Anchoring {
    fn prime(&mut self, record_ids: &[String]) {
        Anchoring::prime(self, record_ids);
    }

    fn for_record(&mut self, record_id: &str, origin_root: Option<&[u32]>) -> Option<ClaimAnchor> {
        self.for_record_at(record_id, origin_root)
    }
}

/// A record id as `str`. `Memory` holds an id that passed the ULID check
/// (26 Crockford base-32 letters), so no code point is lost.
fn id_text(id: &[u32]) -> String {
    id.iter().filter_map(|&c| char::from_u32(c)).collect()
}

/// `_claim_anchors(anchoring, memories)`: None for every record without
/// an anchoring; else the homes primed once, every id in order, then each
/// record's anchor with its origin's `worktree_root`.
pub fn claim_anchors(
    anchoring: Option<&mut dyn RecordAnchors>,
    memories: &[Memory],
) -> Vec<Option<ClaimAnchor>> {
    let Some(anchoring) = anchoring else {
        return vec![None; memories.len()];
    };
    let ids: Vec<String> = memories.iter().map(|m| id_text(&m.fields.id)).collect();
    anchoring.prime(&ids);
    memories
        .iter()
        .zip(&ids)
        .map(|(memory, id)| {
            let origin = memory.fields.origin.as_ref();
            anchoring.for_record(id, origin.and_then(|o| o.worktree_root.as_deref()))
        })
        .collect()
}
