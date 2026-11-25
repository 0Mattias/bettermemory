//! Hostile input to unit health_model-2: 100,000 rows in each list,
//! counts at both ends of `i64`, 10,000 causes and 100,000 unread ids,
//! every float threshold class; no panic in debug or release, and each
//! `to_dict` and `unread_fields` linear in its input.

mod timing;

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_model::{
    self as hm, ActorSlice, ActorSlices, ColdEndorsementMemories, CommitDriftDebt, CommitDriftRow,
    CrossRepoDrift, CrossRepoDriftGroup, MemoryStats, RecentSilentMiss, SilentMissStats,
    VerificationDebt,
};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::origin_history::HistoryUnread as H;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

const COUNTS: [i64; 5] = [0, 1, -1, i64::MAX, i64::MIN];
const CAUSES: [H; 6] = [
    H::Timeout,
    H::OverBudget,
    H::Shallow,
    H::Error,
    H::UnconfirmedCut,
    H::CallBudget,
];

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

fn drift_row(i: usize) -> CommitDriftRow {
    CommitDriftRow::new(
        cps(&format!("m{i}")),
        vec![cps("projects:a")],
        cps("s"),
        Some(d1()),
        i as i64,
    )
}

fn dump(v: &PyValue) {
    black_box(json::dumps(v, DumpOptions::DEFAULT).expect("dumps"));
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

#[test]
fn counts_at_the_ends_of_i64() {
    for &a in &COUNTS {
        for &b in &COUNTS {
            for t in [
                0.0,
                -0.0,
                f64::NAN,
                f64::INFINITY,
                f64::NEG_INFINITY,
                5e-324,
                1.0,
                f64::MAX,
            ] {
                black_box(hm::weakly_endorsed(a, b, t));
                let c = hm::clamp_ratio_threshold(t);
                assert!((0.0..=1.0).contains(&c) && c.to_bits() >> 63 == 0, "{t}");
            }
            let mut v = VerificationDebt::new(a);
            v.never_verified_total = b;
            v.stale_total = a;
            v.fresh_count = b;
            v.never_verified_checkable = a;
            v.stale_checkable = b;
            dump(&v.to_dict().expect("no rows"));
            let mut d = CommitDriftDebt::new(None, None);
            d.total_drifted = a;
            d.unconfirmed = b;
            d.unread = a;
            d.unread_causes = vec![(cps("timeout"), b)];
            dump(&d.to_dict());
            let mut g = CrossRepoDriftGroup::new(cps("r"), cps("w"));
            g.total_drifted = a;
            g.candidates = b;
            g.unread = b;
            let mut r = drift_row(0);
            r.commits_since_verify = a;
            g.rows = vec![r];
            dump(&g.to_dict());
            dump(
                &CrossRepoDrift {
                    groups: vec![g],
                    skipped: Vec::new(),
                    total_drifted: a,
                    unanchorable: b,
                }
                .to_dict(),
            );
            let mut c = ColdEndorsementMemories::new(a);
            c.total = b;
            dump(&c.to_dict().expect("no rows"));
            dump(
                &SilentMissStats {
                    audited_total: a,
                    miss_total: b,
                    unique_miss_memories: a,
                    no_signal_total: b,
                }
                .to_dict(),
            );
            let mut s = ActorSlices::default();
            s.undeclared.memories = a;
            s.undeclared.models = vec![(cps("m"), b)];
            dump(&s.to_dict());
        }
    }
    dump(&RecentSilentMiss::default().to_dict());
}

#[test]
fn a_hundred_thousand_rows_in_each_list() {
    const N: usize = 100_000;
    let stats: Vec<MemoryStats> = (0..N).map(memory).collect();
    let rows: Vec<CommitDriftRow> = (0..N).map(drift_row).collect();
    let mut v = VerificationDebt::new(30);
    v.never_verified = stats.clone();
    v.stale = stats.clone();
    let dv = v.to_dict().expect("aware rows");
    assert_eq!(len_of(&dv, "never_verified"), N);
    assert_eq!(len_of(&dv, "stale"), N);
    let mut c = ColdEndorsementMemories::new(30);
    c.rows = stats;
    assert_eq!(len_of(&c.to_dict().expect("aware rows"), "rows"), N);
    let mut d = CommitDriftDebt::new(Some(cps("r")), Some(cps("c")));
    d.rows = rows.clone();
    assert_eq!(len_of(&d.to_dict(), "rows"), N);
    let mut g = CrossRepoDriftGroup::new(cps("r"), cps("w"));
    g.rows = rows;
    let groups: Vec<CrossRepoDriftGroup> = (0..N)
        .map(|i| CrossRepoDriftGroup::new(cps(&format!("r{i}")), cps("w")))
        .collect();
    let skipped: Vec<PyValue> = (0..N)
        .map(|i| PyValue::Dict(vec![(PyValue::str("repo"), PyValue::str(&i.to_string()))]))
        .collect();
    let x = CrossRepoDrift {
        groups,
        skipped,
        total_drifted: 0,
        unanchorable: 0,
    };
    let dx = x.to_dict();
    assert_eq!(len_of(&dx, "groups"), N);
    assert_eq!(len_of(&dx, "skipped"), N);
    assert_eq!(len_of(&g.to_dict(), "rows"), N);
    let a = ActorSlices {
        declared: (0..N)
            .map(|i| ActorSlice {
                client: Some(cps(&format!("c{i}"))),
                ..ActorSlice::default()
            })
            .collect(),
        undeclared: ActorSlice::default(),
    };
    assert_eq!(len_of(&a.to_dict(), "declared"), N);
    // A raise at the last of 100,000 rows still raises.
    let mut late = memory(N);
    late.last_contradicted_at = Some(d1());
    late.last_verified_at = Some(DateTime::new(2026, 3, 1, 12, 0, 0, 0, None).expect("naive"));
    v.stale.push(late);
    assert_eq!(v.to_dict(), Err(hm::HealthModelError::TypeError));
}

fn unread_map(n: usize) -> Vec<(PyStr, H)> {
    (0..n)
        .map(|j| (cps(&format!("u{}", n - j)), CAUSES[j % CAUSES.len()]))
        .collect()
}

#[test]
fn unread_fields_over_a_hundred_thousand_ids() {
    let big = unread_map(100_000);
    let f = hm::unread_fields(&big);
    assert_eq!(f.unread, 100_000);
    assert_eq!(f.unread_ids.len(), hm::COMMIT_DRIFT_DEBT_CAP);
    assert_eq!(f.unread_causes.len(), 6);
    assert_eq!(f.unread_causes.iter().map(|(_, n)| n).sum::<i64>(), 100_000);
    let small = unread_map(10_000);
    timing::grows_within(
        "unread_fields over the ids",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(hm::unread_fields(black_box(&small)))),
        &mut || timing::once(|| black_box(hm::unread_fields(black_box(&big)))),
    );
}

#[test]
fn ten_thousand_causes() {
    // `_unread_dict` copies whatever causes and ids it is given, uncapped.
    let causes = |n: usize| -> Vec<(PyStr, i64)> {
        (0..n)
            .map(|i| (cps(&format!("cause-{}", n - i)), i as i64 - 1))
            .collect()
    };
    let idlist = |n: usize| -> Vec<PyStr> { (0..n).map(|i| cps(&format!("i{i}"))).collect() };
    let mut d = CommitDriftDebt::new(None, None);
    d.unread = i64::MIN;
    d.unread_causes = causes(10_000);
    d.unread_ids = idlist(10_000);
    let dd = d.to_dict();
    assert_eq!(len_of(&dd, "unread_causes"), 10_000);
    assert_eq!(len_of(&dd, "unread_ids"), 10_000);
    dump(&dd);
    let (sc, si) = (causes(10_000), idlist(10_000));
    let (bc, bi) = (causes(100_000), idlist(100_000));
    timing::grows_within(
        "_unread_dict over the causes and ids",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(hm::unread_dict(1, black_box(&sc), black_box(&si)))),
        &mut || timing::once(|| black_box(hm::unread_dict(1, black_box(&bc), black_box(&bi)))),
    );
}

#[test]
fn to_dict_is_linear_in_the_rows() {
    let stats = |n: usize| -> VerificationDebt {
        let mut v = VerificationDebt::new(30);
        v.stale = (0..n).map(memory).collect();
        v
    };
    let (small, big) = (stats(5_000), stats(50_000));
    timing::grows_within(
        "VerificationDebt.to_dict over the rows",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(small.to_dict())),
        &mut || timing::once(|| black_box(big.to_dict())),
    );
    let drift = |n: usize| -> CommitDriftDebt {
        let mut d = CommitDriftDebt::new(None, None);
        d.rows = (0..n).map(drift_row).collect();
        d
    };
    let (small, big) = (drift(10_000), drift(100_000));
    timing::grows_within(
        "CommitDriftDebt.to_dict over the rows",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(small.to_dict())),
        &mut || timing::once(|| black_box(big.to_dict())),
    );
}
