//! Hostile input for `verdict`: a large batch, integer bounds on
//! `stale_after_days` and the drift count, long path lists. Nothing
//! panics, in debug or release, and a batch's time grows linearly with
//! its length, as the reference's loop does.

mod timing;

use bettermemory_core::attestation_paths::{PathRefs, Record};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::unicode::cps;
use bettermemory_core::verdict::{
    memoised_verdicts, resolve_verdicts, CommitLegs, Reads, Resolved, VerdictAnchoring,
    VerdictMemory,
};
use bettermemory_core::verify::status::ResolvedCommitDrift;
use bettermemory_core::verify_anchoring::report::PathDriftReport;

struct Legs(i64);

impl CommitLegs for Legs {
    type Unchecked = ();
    fn active(&self) -> bool {
        true
    }
    fn prime(&mut self, _: &[String]) {}
    fn lookup(&mut self, _: &VerdictMemory, _: Option<&DateTime>, r: bool) -> (bool, Resolved<()>) {
        (r, Resolved::Drift(ResolvedCommitDrift::new(self.0)))
    }
    fn store(&mut self) {}
}

struct NoAnch;

impl VerdictAnchoring for NoAnch {
    type Anchor = ();
    fn prime(&mut self, _: &[String]) {}
    fn for_record(&mut self, _: &str, _: Option<&[u32]>) -> Option<()> {
        None
    }
}

fn pd(
    _: &Vec<u32>,
    v: &PathRefs<'_>,
    _: &PathRefs<'_>,
    _: Option<&[u32]>,
    _: Option<&()>,
) -> PathDriftReport {
    PathDriftReport {
        checked: vec![v.items.len().to_string()],
        ..Default::default()
    }
}

fn unchecked(v: &str) -> String {
    v.to_string()
}

fn batch(n: usize) -> Vec<VerdictMemory> {
    let lva = DateTime::from_isoformat(&cps("2026-01-01T00:00:00+00:00")).unwrap();
    (0..n)
        .map(|i| VerdictMemory {
            id: format!("m{i}"),
            body: cps("b"),
            last_verified_at: Some(lva),
            worktree_root: None,
            record: Record::default(),
        })
        .collect()
}

fn now() -> DateTime {
    DateTime::from_isoformat(&cps("2026-03-01T00:00:00+00:00")).unwrap()
}

#[test]
fn integer_bounds_do_not_panic() {
    let ms = batch(2);
    for s in [i64::MIN, -1, 0, 1, i64::MAX] {
        for count in [i64::MIN, -1, 0, 1, i64::MAX] {
            let mut f = pd;
            let mut reads = Reads::<NoAnch> {
                anchoring: None,
                path_drift: &mut f,
                unchecked_verdict: &unchecked,
            };
            let got = resolve_verdicts(&ms, || Legs(count), now(), s, &mut reads).unwrap();
            assert_eq!(got.len(), 2);
            let got = memoised_verdicts(&ms, || Legs(count), now(), s, &mut reads).unwrap();
            assert_eq!(got.len(), 2);
        }
    }
}

#[test]
fn long_path_lists_pass_through() {
    let mut ms = batch(1);
    ms[0].record.verified_paths = (0..200_000).map(|i| cps(&format!("p{i}"))).collect();
    let mut f = pd;
    let mut reads = Reads::<NoAnch> {
        anchoring: None,
        path_drift: &mut f,
        unchecked_verdict: &unchecked,
    };
    let got = resolve_verdicts(&ms, || Legs(0), now(), 30, &mut reads).unwrap();
    assert_eq!(got[0].path_drift.checked, ["200000"]);
}

#[test]
fn a_batch_grows_linearly() {
    let (small, large) = (batch(2_000), batch(20_000));
    let run = |ms: &Vec<VerdictMemory>| {
        let ms = ms.clone();
        timing::once(move || {
            let mut f = pd;
            let mut reads = Reads::<NoAnch> {
                anchoring: None,
                path_drift: &mut f,
                unchecked_verdict: &unchecked,
            };
            resolve_verdicts(&ms, || Legs(3), now(), 30, &mut reads).unwrap()
        })
    };
    let mut s = || run(&small);
    let mut l = || run(&large);
    // The clone is timed with the batch: both are linear.
    timing::grows_within("resolve_verdicts batch", timing::TENFOLD, &mut s, &mut l);
}
