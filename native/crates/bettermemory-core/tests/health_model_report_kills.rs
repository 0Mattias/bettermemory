//! Cases a plausible wrong port of unit health_model-3 fails: the key
//! order of `HealthReport.to_dict` (`cross_repo_drift` before
//! `commit_drift_debt`), a None bucket written as `{}` or a set bucket as
//! None, another default floor, a census sorted, `created` written without
//! `_iso`, `EpisodeVolume`'s keys reordered, `memory_ids` capped, and a
//! raising `MemoryStats` row skipped in any bucket that holds one.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_model::{
    self as hm, CommitDriftDebt, CrossRepoDrift, HealthModelError, HealthReport, MemoryStats,
    ProvenanceDebt, ProvenanceRow, Recommendation, TelemetryCoverage,
};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::journal::EpisodeVolume;
use bettermemory_core::unicode::cps;

fn d1() -> DateTime {
    DateTime::new(2026, 3, 1, 12, 0, 0, 0, Some(0)).expect("a datetime")
}

fn naive() -> DateTime {
    DateTime::new(2026, 3, 1, 12, 0, 0, 0, None).expect("a naive datetime")
}

fn report() -> HealthReport {
    HealthReport::new(d1(), 30, 120, 900, 14)
}

fn memory() -> MemoryStats {
    MemoryStats::new(cps("m-1"), vec![cps("global")], cps("s"), d1(), d1())
}

/// A row whose `to_dict` raises TypeError: an aware contradiction ordered
/// against a naive verification.
fn raising() -> MemoryStats {
    let mut m = memory();
    m.last_contradicted_at = Some(d1());
    m.last_verified_at = Some(naive());
    m
}

fn items(v: &PyValue) -> &[(PyValue, PyValue)] {
    let PyValue::Dict(items) = v else {
        panic!("a dict: {v:?}")
    };
    items
}

fn keys(v: &PyValue) -> Vec<String> {
    items(v)
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(s) => s
                .iter()
                .map(|&c| char::from_u32(c).expect("a char"))
                .collect(),
            other => panic!("a text key: {other:?}"),
        })
        .collect()
}

fn get<'a>(v: &'a PyValue, key: &str) -> &'a PyValue {
    &items(v)
        .iter()
        .find(|(k, _)| *k == PyValue::str(key))
        .unwrap_or_else(|| panic!("no key {key}"))
        .1
}

#[test]
fn the_report_keys_are_in_the_reference_order_not_the_field_order() {
    let d = report().to_dict().expect("no raising row");
    assert_eq!(
        keys(&d),
        [
            "generated_at",
            "window_days",
            "total_active_memories",
            "total_events",
            "distinct_sessions",
            "dead_weight",
            "cold_memories",
            "heavily_used",
            "contradicted",
            "marker_stats",
            "scope_distribution",
            "scope_health",
            "rare_scopes",
            "actor_slices",
            "orphan_use_events",
            "verification_debt",
            "cross_repo_drift",
            "commit_drift_debt",
            "provenance",
            "silent_misses",
            "recent_silent_misses",
            "cold_endorsement_memories",
            "recommendations",
            "telemetry_coverage",
            "episode_volume",
        ]
    );
}

#[test]
fn an_unset_optional_bucket_is_none_and_a_set_empty_one_is_a_dict() {
    let d = report().to_dict().expect("no raising row");
    for key in [
        "cross_repo_drift",
        "commit_drift_debt",
        "provenance",
        "telemetry_coverage",
        "episode_volume",
    ] {
        assert_eq!(get(&d, key), &PyValue::None, "{key}");
    }
    let mut r = report();
    r.cross_repo_drift = Some(CrossRepoDrift::default());
    r.commit_drift_debt = Some(CommitDriftDebt::new(None, None));
    r.provenance = Some(ProvenanceDebt::default());
    r.telemetry_coverage = Some(TelemetryCoverage::new(0, false, false));
    r.episode_volume = Some(EpisodeVolume {
        sessions: 0,
        episodes: 0,
        bytes: 0,
        prunable_sessions: 0,
        ttl_days: 0,
    });
    let d = r.to_dict().expect("no raising row");
    assert_eq!(keys(get(&d, "cross_repo_drift"))[0], "total_drifted");
    assert_eq!(keys(get(&d, "commit_drift_debt"))[0], "current_repo");
    assert_eq!(
        keys(get(&d, "provenance")),
        ["counts", "unaccounted_total", "unaccounted"]
    );
    assert_eq!(
        keys(get(&d, "telemetry_coverage"))[0],
        "hook_telemetry_events"
    );
    assert_eq!(
        keys(get(&d, "episode_volume")),
        [
            "sessions",
            "episodes",
            "bytes",
            "prunable_sessions",
            "ttl_days"
        ]
    );
}

#[test]
fn the_default_buckets_carry_the_reference_floors() {
    let r = report();
    assert_eq!(r.verification_debt.stale_after_days, 30);
    assert_eq!(r.cold_endorsement_memories.min_retrievals, 30);
    assert_eq!(r.orphan_use_events, 0);
    assert_eq!(r.actor_slices.undeclared.client, None);
    let d = r.to_dict().expect("no raising row");
    assert_eq!(
        get(get(&d, "verification_debt"), "stale_after_days"),
        &PyValue::int(30)
    );
    assert_eq!(
        get(get(&d, "cold_endorsement_memories"), "min_retrievals"),
        &PyValue::int(30)
    );
    assert_eq!(
        get(get(get(&d, "actor_slices"), "undeclared"), "client"),
        &PyValue::None
    );
    assert_eq!(
        get(get(&d, "silent_misses"), "audited_total"),
        &PyValue::int(0)
    );
}

#[test]
fn a_census_keeps_its_insertion_order() {
    let mut r = report();
    r.scope_distribution = vec![(cps("z"), 3), (cps("a"), 1), (cps("m"), 2)];
    r.provenance = Some(ProvenanceDebt::new(
        vec![
            (cps("written"), 4),
            (cps("pulled"), 1),
            (cps("unaccounted"), 2),
        ],
        2,
    ));
    let d = r.to_dict().expect("no raising row");
    assert_eq!(keys(get(&d, "scope_distribution")), ["z", "a", "m"]);
    assert_eq!(
        keys(get(get(&d, "provenance"), "counts")),
        ["written", "pulled", "unaccounted"]
    );
}

#[test]
fn created_and_generated_at_go_through_iso() {
    let row = ProvenanceRow {
        id: cps("m"),
        scopes: vec![],
        summary: cps("s"),
        created: d1(),
    };
    assert_eq!(
        get(&row.to_dict(), "created"),
        &PyValue::str("2026-03-01T12:00:00Z")
    );
    let plus5 = DateTime::new(2026, 3, 1, 12, 0, 0, 7, Some(5 * 3_600_000_000)).expect("aware");
    let row = ProvenanceRow {
        created: plus5,
        ..row
    };
    assert_eq!(
        get(&row.to_dict(), "created"),
        &PyValue::str("2026-03-01T12:00:00.000007+05:00")
    );
    let mut r = report();
    r.generated_at = naive();
    let d = r.to_dict().expect("no raising row");
    assert_eq!(
        get(&d, "generated_at"),
        &PyValue::str("2026-03-01T12:00:00")
    );
}

#[test]
fn memory_ids_are_copied_whole_and_kinds_in_order() {
    let mut rec = Recommendation::new(cps("remove_dead_weight"), cps("s"), cps("a"), 12);
    rec.memory_ids = (0..12).map(|i| cps(&format!("m{i}"))).collect();
    let d = rec.to_dict();
    let PyValue::List(ids) = get(&d, "memory_ids") else {
        panic!("a list")
    };
    assert_eq!(ids.len(), 12, "the cap is the caller's, not to_dict's");
    assert_eq!(get(&d, "scope"), &PyValue::None);
    assert_eq!(
        keys(&d),
        ["kind", "summary", "action", "count", "memory_ids", "scope"]
    );
    assert_eq!(hm::RECOMMENDATION_KINDS[0], "remove_dead_weight");
    assert_eq!(hm::RECOMMENDATION_KINDS[6], "fix_typo_scopes");
    assert_eq!(
        (
            hm::RECOMMENDATION_ROW_CAP,
            hm::RECOMMENDATION_SIZE_FLOOR,
            hm::PROVENANCE_ROW_CAP
        ),
        (10, 3, 20)
    );
}

#[test]
fn a_raising_row_raises_from_every_bucket_that_holds_one() {
    type Put = fn(&mut HealthReport);
    let puts: [(&str, Put); 6] = [
        ("dead_weight", |r| r.dead_weight = vec![memory(), raising()]),
        ("cold_memories", |r| r.cold_memories = vec![raising()]),
        ("heavily_used", |r| {
            r.heavily_used = vec![memory(), raising()]
        }),
        ("contradicted", |r| r.contradicted = vec![raising()]),
        ("verification_debt", |r| {
            r.verification_debt.stale = vec![raising()]
        }),
        ("cold_endorsement_memories", |r| {
            r.cold_endorsement_memories.rows = vec![raising()]
        }),
    ];
    for (name, put) in puts {
        let mut r = report();
        put(&mut r);
        assert_eq!(r.to_dict(), Err(HealthModelError::TypeError), "{name}");
    }
    let mut r = report();
    r.contradicted = vec![memory()];
    r.cold_endorsement_memories.rows = vec![memory()];
    assert!(r.to_dict().is_ok(), "aware rows do not raise");
}
