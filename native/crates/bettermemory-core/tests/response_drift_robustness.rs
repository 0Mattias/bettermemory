//! Many rows through one pass, the memo at its cap, and hostile stamps
//! and anchors: no panic, each key resolved once, and time linear in the
//! rows.
//!
//! A row with an anchor compares it through the host's `Path`, which the
//! port declines under the Windows flavour (ENG-124): the cases that
//! resolve such rows run on POSIX only (`response_drift_not_posix.rs`
//! pins the decline); the hostile rows, whose anchors are given, run
//! everywhere.
mod response_drift_script;
#[cfg(unix)]
mod timing;

use bettermemory_core::admission::DriftScope;
use bettermemory_core::attestation_paths::Record;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::githead::signature::Signature;
use bettermemory_core::origin_dates::memo::Timestamps;
use bettermemory_core::origin_gitfiles::attrs::AttributeFiles;
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::walk::WalkFiles;
use bettermemory_core::origin_history::{HistoryUnread, Key, Walked};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::response_drift::{
    Chain, DriftPass, DriftReads, HitArgs, LookupError, Memo, Resolve, Row,
};
use bettermemory_core::response_history::{HistoryReads, Read};
#[cfg(unix)]
use bettermemory_core::response_memo::DRIFT_MEMO_CAP;
use bettermemory_core::verify::hit::HitDrift;
use bettermemory_core::verify::status::ResolvedCommitDrift;
use bettermemory_core::verify_candidates::scan::UserHome;
use response_drift_script::{attributes, cps, signature, walk_files};

/// Counts each read; every answer holds still.
#[derive(Default)]
struct Lean {
    attrs: usize,
    chains: usize,
    walks: usize,
    sigs: usize,
    /// The tallies `calls` answers in turn; (0, 0) once they run out.
    tallies: Vec<(i64, i64)>,
}

impl HistoryReads for Lean {
    type Error = ();
    fn files_pair(&mut self, _: &mut Scope, _: &[u32]) -> Read<Option<Key>, ()> {
        Ok(Some((cps("/r"), cps("H"))))
    }
    fn git_pair(&mut self, _: &mut Scope, _: &[u32]) -> Read<Option<Key>, ()> {
        panic!("the files settle the pair")
    }
    fn discovery_moved(&mut self, _: &mut Scope) -> Read<bool, ()> {
        Ok(false)
    }
    fn located_head(&mut self, _: &mut Scope, _: &[u32]) -> Read<Option<PyStr>, ()> {
        Ok(Some(cps("H")))
    }
    fn dates(&mut self, _: &mut Scope, _: &[u32], _: Option<&Key>) -> Read<Timestamps, ()> {
        Ok(Timestamps::Absent)
    }
    fn note_unread(&mut self, _: &mut Scope, _: HistoryUnread) -> Read<(), ()> {
        Ok(())
    }
}

impl DriftReads for Lean {
    fn signature(&mut self, _: &mut Scope, _: &[u32], _: bool) -> Result<Signature, ()> {
        self.sigs += 1;
        Ok(signature("s"))
    }
    fn attribute_files(
        &mut self,
        _: &mut Scope,
        _: &[u32],
        _: &[u32],
        _: &mut Held,
    ) -> Result<Option<AttributeFiles>, ()> {
        self.attrs += 1;
        Ok(Some(attributes(1)))
    }
    fn attribute_chain(
        &mut self,
        _: &mut Scope,
        _: &[u32],
        _: &[PyStr],
        held: &mut Held,
    ) -> Result<Option<Chain>, ()> {
        self.chains += 1;
        held.insert(b"/r".to_vec(), (1, 1));
        Ok(Some(vec![Some([1, 0, 0, 0, 0])]))
    }
    fn walk_files(
        &mut self,
        _: &mut Scope,
        _: &[u32],
        _: &mut Held,
    ) -> Result<Option<WalkFiles>, ()> {
        self.walks += 1;
        Ok(Some(walk_files(1)))
    }
    fn files_held(
        &mut self,
        _: &mut Scope,
        _: &[u32],
        _: &[u32],
        _: Option<&AttributeFiles>,
        _: &[(Vec<PyStr>, Option<Chain>)],
        _: Option<&WalkFiles>,
        _: &Held,
    ) -> Result<bool, ()> {
        Ok(true)
    }
    fn calls(&mut self) -> (i64, i64) {
        if self.tallies.is_empty() {
            (0, 0)
        } else {
            self.tallies.remove(0)
        }
    }
    fn now(&mut self) -> f64 {
        0.0
    }
    fn home(&mut self) -> Option<PyStr> {
        None
    }
    fn user_home(&mut self, _: &[u32]) -> UserHome {
        UserHome::Unknown
    }
}

#[derive(Default)]
struct Counting(usize);

impl Resolve<()> for Counting {
    fn resolve(
        &mut self,
        _: HitArgs<'_>,
        _: &mut Walked,
        _: &mut dyn FnMut() -> Result<Timestamps, ()>,
    ) -> Result<HitDrift, LookupError<()>> {
        self.0 += 1;
        Ok(Some(Ok(ResolvedCommitDrift::new(self.0 as i64))))
    }
}

struct Every;
impl DriftScope for Every {
    fn prime(&mut self, _: &[String]) {}
    fn drift_counts(&mut self, _: &str) -> bool {
        true
    }
}

#[cfg(unix)]
fn stamp() -> DateTime {
    DateTime::from_isoformat(&cps("2024-01-02T03:04:05+00:00")).unwrap()
}

/// `n` governed rows over three verified heads in one pass.
#[cfg(unix)]
fn rows(n: usize, memo: &Memo) -> (Lean, Counting, usize) {
    let mut reads = Lean::default();
    let mut resolver = Counting::default();
    let mut scope = Scope::default();
    let mut pass =
        DriftPass::open(&mut reads, &mut scope, cps("/r/s"), false, Every, memo).unwrap();
    let record = Record {
        verified_paths: vec![cps("src/a.py")],
        ..Record::default()
    };
    let claims = vec![cps("src/a.py")];
    let heads = [cps("A"), cps("B"), cps("C")];
    let since = stamp();
    for i in 0..n {
        let row = Row {
            id: "m",
            origin_repo: true,
            body: &[],
            paths: &record,
            claims: &claims,
            verified_head: Some(&heads[i % 3]),
        };
        let (found, drift) = pass
            .lookup(
                &mut reads,
                &mut scope,
                &row,
                Some(&since),
                true,
                None,
                &mut resolver,
            )
            .unwrap();
        assert!(found);
        assert_eq!(
            drift,
            Some(Ok(ResolvedCommitDrift::new((i % 3) as i64 + 1)))
        );
    }
    let unstored = pass.unstored();
    assert!(pass.store(&mut reads, &mut scope).unwrap());
    (reads, resolver, unstored)
}

#[test]
#[cfg(unix)]
fn ten_thousand_rows_resolve_three_keys_once_each() {
    let memo = Memo::new();
    let (reads, resolver, unstored) = rows(10_000, &memo);
    assert_eq!(resolver.0, 3);
    assert_eq!(unstored, 3);
    assert_eq!((reads.attrs, reads.chains, reads.walks), (1, 1, 1));
    assert_eq!(
        reads.sigs, 2,
        "once before the first resolution, once in store"
    );
    assert_eq!(memo.memo_keys().len(), 3);
}

#[test]
#[cfg(unix)]
fn rows_grow_linearly() {
    timing::grows_within(
        "lookup: 1,000 and 10,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| rows(1_000, &Memo::new())),
        &mut || timing::once(|| rows(10_000, &Memo::new())),
    );
}

#[cfg(unix)]
fn lookup_heads(memo: &Memo, heads: &[&str]) -> usize {
    let mut reads = Lean::default();
    let mut resolver = Counting::default();
    let mut scope = Scope::default();
    let mut pass =
        DriftPass::open(&mut reads, &mut scope, cps("/r/s"), false, Every, memo).unwrap();
    let record = Record {
        verified_paths: vec![cps("src/a.py")],
        ..Record::default()
    };
    let since = stamp();
    for head in heads {
        let vh = cps(head);
        let row = Row {
            id: "m",
            origin_repo: true,
            body: &[],
            paths: &record,
            claims: &[],
            verified_head: Some(&vh),
        };
        pass.lookup(
            &mut reads,
            &mut scope,
            &row,
            Some(&since),
            true,
            None,
            &mut resolver,
        )
        .unwrap();
    }
    assert!(pass.store(&mut reads, &mut scope).unwrap());
    resolver.0
}

#[cfg(unix)]
fn memo_heads(memo: &Memo) -> Vec<String> {
    memo.memo_keys()
        .iter()
        .map(|k| response_drift_script::text(k.verified_head.as_deref().unwrap()))
        .collect()
}

#[test]
#[cfg(unix)]
fn the_memo_at_its_cap_evicts_the_least_recently_used() {
    let memo = Memo::with_cap(3);
    assert_eq!(lookup_heads(&memo, &["a", "b", "c", "d", "e"]), 5);
    assert_eq!(memo_heads(&memo), ["c", "d", "e"]);
    // A hit moves to the end; a key resolved again keeps its place.
    assert_eq!(lookup_heads(&memo, &["c", "f"]), 1);
    assert_eq!(memo_heads(&memo), ["e", "c", "f"]);
    assert_eq!(lookup_heads(&memo, &["d"]), 1);
    assert_eq!(memo_heads(&memo), ["c", "f", "d"]);

    let full = Memo::new();
    let heads: Vec<String> = (0..DRIFT_MEMO_CAP + 7).map(|i| i.to_string()).collect();
    let refs: Vec<&str> = heads.iter().map(String::as_str).collect();
    assert_eq!(lookup_heads(&full, &refs), DRIFT_MEMO_CAP + 7);
    let kept = memo_heads(&full);
    assert_eq!(kept.len(), DRIFT_MEMO_CAP);
    assert_eq!(kept[0], "7");
}

#[test]
fn hostile_stamps_and_anchors_do_not_panic() {
    let memo = Memo::new();
    let mut reads = Lean::default();
    let mut resolver = Counting::default();
    let mut scope = Scope::default();
    let mut pass =
        DriftPass::open(&mut reads, &mut scope, cps("/r/s"), false, Every, &memo).unwrap();
    let record = Record::default();
    let anchors: Vec<PyStr> = (0..100_000).map(|i| cps(&format!("src/{i}.py"))).collect();
    let id = "x".repeat(1 << 20);
    let mut answers = 0;
    for since in [
        "0001-01-01T00:00:00",
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59.999999",
        "9999-12-31T23:59:59.999999-23:59:59.999999",
    ] {
        let since = DateTime::from_isoformat(&cps(since)).unwrap();
        let row = Row {
            id: &id,
            origin_repo: true,
            body: &[0xD800, 0x7E, 0x2F, 0],
            paths: &record,
            claims: &[cps("\u{0}"), vec![0xDC80], cps("::")],
            verified_head: Some(&[0xDFFF]),
        };
        for given in [Some(anchors.as_slice()), None] {
            // An error the reference raises is an answer here too.
            let found = pass.lookup(
                &mut reads,
                &mut scope,
                &row,
                Some(&since),
                true,
                given,
                &mut resolver,
            );
            answers += found.map_or(1, |f| usize::from(f.0));
        }
    }
    assert_eq!(answers, 8);
    assert!(pass.store(&mut reads, &mut scope).unwrap());
}

/// Tallies at the ends of i64: the differences are Python's exact ints,
/// so neither build panics or wraps. A failure count that moved by
/// 2**64 - 1 is neither zero nor the timed-out count's move, and the row
/// is not kept (the reference's answer); one that moved by as much as the
/// timed-out count is kept as timed out.
#[test]
#[cfg(unix)]
fn extreme_tallies_answer_as_python_ints_do() {
    let record = Record {
        verified_paths: vec![cps("src/a.py")],
        ..Record::default()
    };
    let since = stamp();
    let row = Row {
        id: "m",
        origin_repo: true,
        body: &[],
        paths: &record,
        claims: &[],
        verified_head: None,
    };
    let (lo, hi) = (i64::MIN, i64::MAX);
    for (what, tallies, kept) in [
        ("up", vec![(lo, 0), (hi, 0)], [0, 0]),
        ("down", vec![(hi, 0), (lo, 0)], [0, 0]),
        (
            "up, every failure a timeout",
            vec![(lo, lo), (hi, hi)],
            [0, 1],
        ),
        (
            "down, every failure a timeout",
            vec![(hi, hi), (lo, lo)],
            [0, 1],
        ),
        // Wrapped, the failure count's move (-1) equals the timed-out
        // count's; exact, 2**64 - 1 does not.
        (
            "a move that wraps to the timed-out move",
            vec![(lo, 0), (hi, -1)],
            [0, 0],
        ),
        ("still", vec![(hi, lo), (hi, lo)], [1, 0]),
    ] {
        let memo = Memo::new();
        let mut reads = Lean {
            tallies,
            ..Lean::default()
        };
        let mut resolver = Counting::default();
        let mut scope = Scope::default();
        let mut pass =
            DriftPass::open(&mut reads, &mut scope, cps("/r/s"), false, Every, &memo).unwrap();
        let (found, _) = pass
            .lookup(
                &mut reads,
                &mut scope,
                &row,
                Some(&since),
                true,
                None,
                &mut resolver,
            )
            .unwrap();
        assert!(found, "{what}");
        let [resolved, timed_out] = pass.kept();
        assert_eq!([resolved.len(), timed_out.len()], kept, "{what}");
    }
}
