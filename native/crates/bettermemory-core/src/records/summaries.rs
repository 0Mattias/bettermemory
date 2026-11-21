//! The result models of `models.py` the crate builds from validated
//! records: `MemoryHit` (one memory_search result), `MemorySummary` (one
//! row of `Store.list_summaries`) and `TombstonedSummary` (one row of
//! `list_tombstones`), with the two listings' scope filters.
//!
//! None of the three is ever `model_dump`ed in the reference, and every
//! construction site passes values already validated (`search._build_hit`,
//! `store_records.list_summaries` and `list_tombstones`), so these are
//! plain values: no pydantic validation and no JSON spelling.

use std::collections::HashSet;

use crate::datetime::DateTime;
use crate::models_text::{first_summary_line, SUMMARY_MAX_CHARS};
use crate::pyvalue::PyStr;
use crate::unicode;

use super::{Actor, Category, Confidence, Memory, RecordFields, Tombstone};

/// `models.MemoryHit`, fields in the model's order.
#[derive(Clone, Debug, PartialEq)]
pub struct MemoryHit {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub confidence: Confidence,
    pub snippet: PyStr,
    pub score: f64,
    pub relevance: PyStr,
    pub match_terms: Vec<PyStr>,
    pub created: DateTime,
    pub updated: DateTime,
    pub last_verified_at: Option<DateTime>,
    pub path_drift_checked: i64,
    pub path_drift_missing: i64,
    pub path_drift_checked_paths: Vec<PyStr>,
    pub path_drift_missing_paths: Vec<PyStr>,
    pub path_drift_verified_paths: Vec<PyStr>,
    pub path_drift_expected_absent_paths: Vec<PyStr>,
    pub path_drift_present_but_attested_absent: Vec<PyStr>,
    pub path_drift_dropped_as_route_paths: Vec<PyStr>,
    pub path_drift_claim_anchored_missing_paths: Vec<PyStr>,
    pub category: Option<Category>,
    pub query_unique: i64,
}

impl MemoryHit {
    /// `MemoryHit(id=, scopes=, confidence=, snippet=, score=, created=,
    /// updated=)`: every other field at the model's default (relevance
    /// `"medium"`, empty lists, zero counts, None).
    pub fn new(
        id: PyStr,
        scopes: Vec<PyStr>,
        confidence: Confidence,
        snippet: PyStr,
        score: f64,
        created: DateTime,
        updated: DateTime,
    ) -> MemoryHit {
        MemoryHit {
            id,
            scopes,
            confidence,
            snippet,
            score,
            relevance: unicode::cps("medium"),
            match_terms: Vec::new(),
            created,
            updated,
            last_verified_at: None,
            path_drift_checked: 0,
            path_drift_missing: 0,
            path_drift_checked_paths: Vec::new(),
            path_drift_missing_paths: Vec::new(),
            path_drift_verified_paths: Vec::new(),
            path_drift_expected_absent_paths: Vec::new(),
            path_drift_present_but_attested_absent: Vec::new(),
            path_drift_dropped_as_route_paths: Vec::new(),
            path_drift_claim_anchored_missing_paths: Vec::new(),
            category: None,
            query_unique: 0,
        }
    }
}

/// `models.MemorySummary`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemorySummary {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub confidence: Confidence,
    pub summary: PyStr,
    pub created: DateTime,
    pub updated: DateTime,
    pub last_verified_at: Option<DateTime>,
    pub category: Option<Category>,
    pub actor: Option<Actor>,
}

impl MemorySummary {
    /// The summary `list_summaries` builds of `memory`.
    pub fn of(memory: &Memory) -> MemorySummary {
        let f = &memory.fields;
        MemorySummary {
            id: f.id.clone(),
            scopes: f.scopes.clone(),
            confidence: f.confidence,
            summary: first_summary_line(&f.body, SUMMARY_MAX_CHARS),
            created: f.created,
            updated: f.updated,
            last_verified_at: f.last_verified_at,
            category: f.category,
            actor: f.actor.clone(),
        }
    }
}

/// `models.TombstonedSummary`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TombstonedSummary {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub confidence: Confidence,
    pub summary: PyStr,
    pub created: DateTime,
    pub updated: DateTime,
    pub last_verified_at: Option<DateTime>,
    pub category: Option<Category>,
    pub removed: DateTime,
    pub removed_reason: PyStr,
    pub removed_session: Option<PyStr>,
}

impl TombstonedSummary {
    /// The summary `list_tombstones` builds of `dead`.
    pub fn of(dead: &Tombstone) -> TombstonedSummary {
        let f = &dead.fields;
        TombstonedSummary {
            id: f.id.clone(),
            scopes: f.scopes.clone(),
            confidence: f.confidence,
            summary: first_summary_line(&f.body, SUMMARY_MAX_CHARS),
            created: f.created,
            updated: f.updated,
            last_verified_at: f.last_verified_at,
            category: f.category,
            removed: dead.removed,
            removed_reason: dead.removed_reason.clone(),
            removed_session: dead.removed_session.clone(),
        }
    }
}

/// The listings' filter: `wanted = set(scopes) if scopes else None`, then
/// a record is kept when `wanted` is None or shares a scope with it.
fn wanted(scopes: Option<&[PyStr]>) -> Option<HashSet<&[u32]>> {
    match scopes {
        None | Some([]) => None,
        Some(scopes) => Some(scopes.iter().map(Vec::as_slice).collect()),
    }
}

fn kept(fields: &RecordFields, wanted: &Option<HashSet<&[u32]>>) -> bool {
    wanted
        .as_ref()
        .is_none_or(|w| fields.scopes.iter().any(|s| w.contains(s.as_slice())))
}

/// `list_summaries(scopes)` over `memories`, in their order.
pub fn summaries(memories: &[Memory], scopes: Option<&[PyStr]>) -> Vec<MemorySummary> {
    let wanted = wanted(scopes);
    memories
        .iter()
        .filter(|m| kept(&m.fields, &wanted))
        .map(MemorySummary::of)
        .collect()
}

/// `list_tombstones(scopes)` over `tombstones`, in their order.
pub fn tombstone_summaries(
    tombstones: &[Tombstone],
    scopes: Option<&[PyStr]>,
) -> Vec<TombstonedSummary> {
    let wanted = wanted(scopes);
    tombstones
        .iter()
        .filter(|t| kept(&t.fields, &wanted))
        .map(TombstonedSummary::of)
        .collect()
}
