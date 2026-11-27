//! Unit health_model-3 of `health_model.py` (lines 1244-1567): the
//! recommendation and provenance types and `HealthReport`, the aggregate
//! `memory_health` answers, each with its `to_dict`.
//!
//! As in units 1 and 2, the types hold Rust values and `to_dict` answers a
//! [`PyValue::Dict`] in the reference's key order. A dict field of the
//! reference (`ProvenanceDebt.counts`, `HealthReport.scope_distribution`)
//! is held as `(key, count)` pairs in insertion order, and `dict(...)` of
//! it writes them as given. `EpisodeVolume` is `store::journal`'s; its
//! `to_dict` (`store_schema.py` 551-558) is [`episode_volume_dict`] here.

use super::{
    dict, int, text, ActorSlices, Answer, ColdEndorsementMemories, CommitDriftDebt, CrossRepoDrift,
    MarkerStats, MemoryStats, RecentSilentMiss, ScopeHealth, SilentMissStats, TelemetryCoverage,
    VerificationDebt, COLD_ENDORSEMENT_MIN_RETRIEVALS,
};
use crate::datetime::DateTime;
use crate::pyvalue::{PyStr, PyValue};
use crate::store::journal::EpisodeVolume;
use crate::time_utils::isoformat_utc_optional;

/// `_RECOMMENDATION_ROW_CAP`: the most `memory_ids` one recommendation
/// carries.
pub const RECOMMENDATION_ROW_CAP: usize = 10;

/// `_RECOMMENDATION_SIZE_FLOOR`: the least bucket size a size-driven
/// recommendation kind fires at.
pub const RECOMMENDATION_SIZE_FLOOR: i64 = 3;

/// `RECOMMENDATION_KINDS`: the closed set of kinds, in the reference's
/// order.
pub const RECOMMENDATION_KINDS: [&str; 7] = [
    "remove_dead_weight",
    "resolve_contradicted",
    "cleanup_cold_endorsements",
    "verify_drifted",
    "spot_check_unread",
    "review_unaccounted",
    "fix_typo_scopes",
];

/// `_PROVENANCE_ROW_CAP`: the most `unaccounted` rows `ProvenanceDebt`
/// inlines.
pub const PROVENANCE_ROW_CAP: usize = 20;

/// `_iso` of a datetime that is never None: its UTC isoformat as text.
fn iso_text(dt: &DateTime) -> PyValue {
    isoformat_utc_optional(Some(dt)).map_or(PyValue::None, |s| PyValue::str(&s))
}

fn texts(items: &[PyStr]) -> PyValue {
    PyValue::List(items.iter().map(|s| text(s)).collect())
}

fn opt_text(s: Option<&PyStr>) -> PyValue {
    s.map_or(PyValue::None, |s| text(s))
}

/// `dict(d)` of a `dict[str, int]` held as pairs in insertion order. A
/// dict's keys are distinct; a repeated key is refused in debug builds,
/// and a release build writes every pair as given.
fn census(items: &[(PyStr, i64)]) -> PyValue {
    debug_assert!(
        {
            let mut seen = std::collections::HashSet::new();
            items.iter().all(|(k, _)| seen.insert(k))
        },
        "a census holds a repeated key; a dict's keys are distinct"
    );
    PyValue::Dict(items.iter().map(|(k, n)| (text(k), int(*n))).collect())
}

/// `[s.to_dict() for s in rows]` of `MemoryStats` rows: the first row
/// that raises is the answer.
fn stats_list(rows: &[MemoryStats]) -> Answer<PyValue> {
    rows.iter()
        .map(MemoryStats::to_dict)
        .collect::<Answer<Vec<_>>>()
        .map(PyValue::List)
}

fn list<T>(rows: &[T], f: impl Fn(&T) -> PyValue) -> PyValue {
    PyValue::List(rows.iter().map(f).collect())
}

/// `Recommendation`: one curation suggestion distilled from a bucket.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Recommendation {
    pub kind: PyStr,
    pub summary: PyStr,
    pub action: PyStr,
    pub count: i64,
    pub memory_ids: Vec<PyStr>,
    pub scope: Option<PyStr>,
}

impl Recommendation {
    /// The dataclass's constructor with `memory_ids` empty and no scope.
    pub fn new(kind: PyStr, summary: PyStr, action: PyStr, count: i64) -> Self {
        Recommendation {
            kind,
            summary,
            action,
            count,
            memory_ids: Vec::new(),
            scope: None,
        }
    }

    /// `to_dict`, `memory_ids` copied.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("kind", text(&self.kind)),
            ("summary", text(&self.summary)),
            ("action", text(&self.action)),
            ("count", int(self.count)),
            ("memory_ids", texts(&self.memory_ids)),
            ("scope", opt_text(self.scope.as_ref())),
        ])
    }
}

/// `ProvenanceRow`: one memory the provenance bucket surfaces.
#[derive(Clone, Debug, PartialEq)]
pub struct ProvenanceRow {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub summary: PyStr,
    pub created: DateTime,
}

impl ProvenanceRow {
    /// `to_dict`, `created` through `_iso`.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("id", text(&self.id)),
            ("scopes", texts(&self.scopes)),
            ("summary", text(&self.summary)),
            ("created", iso_text(&self.created)),
        ])
    }
}

/// `ProvenanceDebt`: the per-label census of how memories entered the
/// store, the unaccounted total and its capped rows.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ProvenanceDebt {
    pub counts: Vec<(PyStr, i64)>,
    pub unaccounted_total: i64,
    pub unaccounted: Vec<ProvenanceRow>,
}

impl ProvenanceDebt {
    /// The dataclass's constructor with no `unaccounted` rows.
    pub fn new(counts: Vec<(PyStr, i64)>, unaccounted_total: i64) -> Self {
        ProvenanceDebt {
            counts,
            unaccounted_total,
            unaccounted: Vec::new(),
        }
    }

    /// `to_dict`: `counts` in insertion order, then the total and rows.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("counts", census(&self.counts)),
            ("unaccounted_total", int(self.unaccounted_total)),
            (
                "unaccounted",
                list(&self.unaccounted, ProvenanceRow::to_dict),
            ),
        ])
    }
}

/// `EpisodeVolume.to_dict` (`store_schema.py` 551-558), in its key order.
pub fn episode_volume_dict(v: &EpisodeVolume) -> PyValue {
    dict(vec![
        ("sessions", int(v.sessions)),
        ("episodes", int(v.episodes)),
        ("bytes", int(v.bytes)),
        ("prunable_sessions", int(v.prunable_sessions)),
        ("ttl_days", int(v.ttl_days)),
    ])
}

/// `HealthReport`: the full aggregate view `memory_health` returns.
#[derive(Clone, Debug, PartialEq)]
pub struct HealthReport {
    pub generated_at: DateTime,
    pub window_days: i64,
    pub total_active_memories: i64,
    pub total_events: i64,
    pub distinct_sessions: i64,
    pub dead_weight: Vec<MemoryStats>,
    pub cold_memories: Vec<MemoryStats>,
    pub heavily_used: Vec<MemoryStats>,
    pub contradicted: Vec<MemoryStats>,
    pub marker_stats: Vec<MarkerStats>,
    /// A `dict[str, int]`, as pairs in insertion order.
    pub scope_distribution: Vec<(PyStr, i64)>,
    pub scope_health: Vec<ScopeHealth>,
    pub rare_scopes: Vec<PyStr>,
    pub actor_slices: ActorSlices,
    pub orphan_use_events: i64,
    pub verification_debt: VerificationDebt,
    pub commit_drift_debt: Option<CommitDriftDebt>,
    pub cross_repo_drift: Option<CrossRepoDrift>,
    pub provenance: Option<ProvenanceDebt>,
    pub silent_misses: SilentMissStats,
    pub recent_silent_misses: Vec<RecentSilentMiss>,
    pub cold_endorsement_memories: ColdEndorsementMemories,
    pub recommendations: Vec<Recommendation>,
    pub telemetry_coverage: Option<TelemetryCoverage>,
    pub episode_volume: Option<EpisodeVolume>,
}

impl HealthReport {
    /// The dataclass's constructor with every defaulted field at the
    /// default its factory makes: empty lists and dicts, an empty
    /// `ActorSlices` (its `undeclared` slice `ActorSlice(client=None)`),
    /// `VerificationDebt(stale_after_days=30)`, `SilentMissStats()`,
    /// `ColdEndorsementMemories(min_retrievals=30)`, zero orphans and None
    /// for each optional bucket.
    pub fn new(
        generated_at: DateTime,
        window_days: i64,
        total_active_memories: i64,
        total_events: i64,
        distinct_sessions: i64,
    ) -> Self {
        HealthReport {
            generated_at,
            window_days,
            total_active_memories,
            total_events,
            distinct_sessions,
            dead_weight: Vec::new(),
            cold_memories: Vec::new(),
            heavily_used: Vec::new(),
            contradicted: Vec::new(),
            marker_stats: Vec::new(),
            scope_distribution: Vec::new(),
            scope_health: Vec::new(),
            rare_scopes: Vec::new(),
            actor_slices: ActorSlices::default(),
            orphan_use_events: 0,
            verification_debt: VerificationDebt::new(30),
            commit_drift_debt: None,
            cross_repo_drift: None,
            provenance: None,
            silent_misses: SilentMissStats::default(),
            recent_silent_misses: Vec::new(),
            cold_endorsement_memories: ColdEndorsementMemories::new(
                COLD_ENDORSEMENT_MIN_RETRIEVALS,
            ),
            recommendations: Vec::new(),
            telemetry_coverage: None,
            episode_volume: None,
        }
    }

    /// `to_dict` in the reference's key order, which writes
    /// `cross_repo_drift` before `commit_drift_debt`. The dict literal is
    /// evaluated in key order, so the answer is TypeError from the first
    /// `MemoryStats` row that raises: `dead_weight`'s, then
    /// `cold_memories`', `heavily_used`'s, `contradicted`'s,
    /// `verification_debt`'s and `cold_endorsement_memories`'.
    pub fn to_dict(&self) -> Answer<PyValue> {
        Ok(dict(vec![
            ("generated_at", iso_text(&self.generated_at)),
            ("window_days", int(self.window_days)),
            ("total_active_memories", int(self.total_active_memories)),
            ("total_events", int(self.total_events)),
            ("distinct_sessions", int(self.distinct_sessions)),
            ("dead_weight", stats_list(&self.dead_weight)?),
            ("cold_memories", stats_list(&self.cold_memories)?),
            ("heavily_used", stats_list(&self.heavily_used)?),
            ("contradicted", stats_list(&self.contradicted)?),
            (
                "marker_stats",
                list(&self.marker_stats, MarkerStats::to_dict),
            ),
            ("scope_distribution", census(&self.scope_distribution)),
            (
                "scope_health",
                list(&self.scope_health, ScopeHealth::to_dict),
            ),
            ("rare_scopes", texts(&self.rare_scopes)),
            ("actor_slices", self.actor_slices.to_dict()),
            ("orphan_use_events", int(self.orphan_use_events)),
            ("verification_debt", self.verification_debt.to_dict()?),
            (
                "cross_repo_drift",
                self.cross_repo_drift
                    .as_ref()
                    .map_or(PyValue::None, CrossRepoDrift::to_dict),
            ),
            (
                "commit_drift_debt",
                self.commit_drift_debt
                    .as_ref()
                    .map_or(PyValue::None, CommitDriftDebt::to_dict),
            ),
            (
                "provenance",
                self.provenance
                    .as_ref()
                    .map_or(PyValue::None, ProvenanceDebt::to_dict),
            ),
            ("silent_misses", self.silent_misses.to_dict()),
            (
                "recent_silent_misses",
                list(&self.recent_silent_misses, RecentSilentMiss::to_dict),
            ),
            (
                "cold_endorsement_memories",
                self.cold_endorsement_memories.to_dict()?,
            ),
            (
                "recommendations",
                list(&self.recommendations, Recommendation::to_dict),
            ),
            (
                "telemetry_coverage",
                self.telemetry_coverage
                    .as_ref()
                    .map_or(PyValue::None, TelemetryCoverage::to_dict),
            ),
            (
                "episode_volume",
                self.episode_volume
                    .as_ref()
                    .map_or(PyValue::None, episode_volume_dict),
            ),
        ]))
    }
}
