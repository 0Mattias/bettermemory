//! Hostile input to unit health_model-3: counts at both ends of `i64`,
//! datetimes at the ends of the calendar, 100,000 rows in every bucket of
//! a report (a raise at the last row still raises); no panic in debug or
//! release, and `HealthReport.to_dict` linear in its rows.

mod timing;

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_model::{
    HealthModelError, HealthReport, MarkerStats, MemoryStats, ProvenanceDebt, ProvenanceRow,
    RecentSilentMiss, Recommendation, ScopeHealth,
};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::journal::EpisodeVolume;
use bettermemory_core::unicode::cps;

fn d1() -> DateTime {
    DateTime::new(2026, 3, 1, 12, 0, 0, 0, Some(0)).expect("a datetime")
}

fn memory(i: usize) -> MemoryStats {
    MemoryStats::new(
        cps(&format!("m{i}")),
        vec![cps("global")],
        cps("s"),
        d1(),
        d1(),
    )
}

fn len_of(v: &PyValue, key: &str) -> usize {
    let PyValue::Dict(items) = v else {
        panic!("a dict")
    };
    let (_, value) = items
        .iter()
        .find(|(k, _)| *k == PyValue::str(key))
        .expect("the key");
    match value {
        PyValue::List(l) => l.len(),
        PyValue::Dict(d) => d.len(),
        other => panic!("not a container: {other:?}"),
    }
}

/// A report with `n` rows in each of its lists and dicts.
fn report_of(n: usize) -> HealthReport {
    let mut r = HealthReport::new(d1(), 30, n as i64, n as i64, 1);
    let stats: Vec<MemoryStats> = (0..n).map(memory).collect();
    r.dead_weight = stats.clone();
    r.cold_memories = stats.clone();
    r.heavily_used = stats.clone();
    r.contradicted = stats;
    r.marker_stats = (0..n)
        .map(|i| MarkerStats {
            marker: cps(&format!("k{i}")),
            fire_count: i as i64,
            override_count: 1,
        })
        .collect();
    r.scope_distribution = (0..n).map(|i| (cps(&format!("s{i}")), i as i64)).collect();
    r.scope_health = (0..n)
        .map(|i| ScopeHealth {
            scope: cps(&format!("s{i}")),
            ..ScopeHealth::default()
        })
        .collect();
    r.rare_scopes = (0..n).map(|i| cps(&format!("r{i}"))).collect();
    r.recent_silent_misses = (0..n)
        .map(|i| RecentSilentMiss {
            event_id: Some(cps(&format!("e{i}"))),
            ..RecentSilentMiss::default()
        })
        .collect();
    r.recommendations = (0..n)
        .map(|i| Recommendation::new(cps("verify_drifted"), cps("s"), cps("a"), i as i64))
        .collect();
    let mut p = ProvenanceDebt::new(
        (0..n).map(|i| (cps(&format!("p{i}")), 1)).collect(),
        n as i64,
    );
    p.unaccounted = (0..n)
        .map(|i| ProvenanceRow {
            id: cps(&format!("m{i}")),
            scopes: vec![],
            summary: cps("s"),
            created: d1(),
        })
        .collect();
    r.provenance = Some(p);
    r
}

#[test]
fn counts_and_datetimes_at_their_ends() {
    for (n, offset) in [
        (i64::MIN, Some(-86_340_000_000)),
        (i64::MAX, Some(86_340_000_000)),
    ] {
        let year = if n < 0 { 1 } else { 9999 };
        let at = DateTime::new(year, 1, 1, 0, 0, 0, 999_999, offset).expect("a datetime");
        let mut r = HealthReport::new(at, n, n, n, n);
        r.orphan_use_events = n;
        r.scope_distribution = vec![(cps("a"), n)];
        r.verification_debt.stale_after_days = n;
        r.cold_endorsement_memories.min_retrievals = n;
        r.episode_volume = Some(EpisodeVolume {
            sessions: n,
            episodes: n,
            bytes: n,
            prunable_sessions: n,
            ttl_days: n,
        });
        r.recommendations = vec![Recommendation::new(cps(""), cps(""), cps(""), n)];
        r.provenance = Some(ProvenanceDebt::new(vec![(cps(""), n)], n));
        let d = r.to_dict().expect("no raising row");
        let text = json::dumps(&d, DumpOptions::DEFAULT).expect("dumps");
        let digits: Vec<u32> = n.to_string().chars().map(u32::from).collect();
        assert!(text.windows(digits.len()).any(|w| w == digits.as_slice()));
    }
}

#[test]
fn a_hundred_thousand_rows_in_every_bucket() {
    const N: usize = 100_000;
    let mut r = report_of(N);
    let d = r.to_dict().expect("aware rows");
    for key in [
        "dead_weight",
        "cold_memories",
        "heavily_used",
        "contradicted",
        "marker_stats",
        "scope_distribution",
        "scope_health",
        "rare_scopes",
        "recent_silent_misses",
        "recommendations",
    ] {
        assert_eq!(len_of(&d, key), N, "{key}");
    }
    // A raise at the last of 100,000 rows of the last bucket still raises.
    let mut late = memory(N);
    late.last_contradicted_at = Some(d1());
    late.last_verified_at = Some(DateTime::new(2026, 3, 1, 12, 0, 0, 0, None).expect("naive"));
    r.contradicted.push(late);
    assert_eq!(r.to_dict(), Err(HealthModelError::TypeError));
}

#[test]
fn to_dict_is_linear_in_the_rows() {
    let (small, big) = (report_of(10_000), report_of(100_000));
    timing::grows_within(
        "HealthReport.to_dict over the rows",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(small.to_dict())),
        &mut || timing::once(|| black_box(big.to_dict())),
    );
}
