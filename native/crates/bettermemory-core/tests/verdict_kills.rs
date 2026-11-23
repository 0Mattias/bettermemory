//! Cases a plausible wrong `verdict` fails: the unread history ranked above
//! the unchecked leg, the unchecked verdict taken from the closure, the
//! memo's unanswered row kept apart from a resolving call's, the anchoring
//! primed only without active legs, the recorded root dropped where an
//! anchor reads, and the store skipped after a raise.

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::attestation_paths::PathRefs;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_history::HistoryUnread;
use bettermemory_core::unicode::cps;
use bettermemory_core::verdict::{
    memoised_verdicts, memory_verdict, open_legs, resolve_verdicts, CommitLegs, Reads, Resolved,
    Verdict, VerdictAnchoring, VerdictError, VerdictMemory,
};
use bettermemory_core::verify::status::ResolvedCommitDrift;
use bettermemory_core::verify_anchoring::report::PathDriftReport;

type Log = Rc<RefCell<Vec<String>>>;
type Answer = Box<dyn Fn(&str) -> (bool, Resolved<&'static str>)>;

struct Legs {
    active: bool,
    answer: Answer,
    log: Log,
}

impl CommitLegs for Legs {
    type Unchecked = &'static str;
    fn active(&self) -> bool {
        self.active
    }
    fn prime(&mut self, ids: &[String]) {
        self.log
            .borrow_mut()
            .push(format!("legs.prime {}", ids.join(",")));
    }
    fn lookup(
        &mut self,
        m: &VerdictMemory,
        _: Option<&DateTime>,
        resolve: bool,
    ) -> (bool, Resolved<&'static str>) {
        self.log
            .borrow_mut()
            .push(format!("lookup {} {resolve}", m.id));
        (self.answer)(&m.id)
    }
    fn store(&mut self) {
        self.log.borrow_mut().push("store".into());
    }
}

struct Anch {
    log: Log,
    anchored: bool,
}

impl VerdictAnchoring for Anch {
    type Anchor = u8;
    fn prime(&mut self, ids: &[String]) {
        self.log
            .borrow_mut()
            .push(format!("anch.prime {}", ids.join(",")));
    }
    fn for_record(&mut self, _: &str, _: Option<&[u32]>) -> Option<u8> {
        self.anchored.then_some(7)
    }
}

fn now() -> DateTime {
    DateTime::from_isoformat(&cps("2026-03-01T00:00:00+00:00")).unwrap()
}

fn mem(id: &str, lva: Option<&str>) -> VerdictMemory {
    VerdictMemory {
        id: id.into(),
        body: cps(id),
        last_verified_at: lva.map(|t| DateTime::from_isoformat(&cps(t)).unwrap()),
        worktree_root: Some(cps(&format!("/w/{id}"))),
        ..Default::default()
    }
}

fn legs(log: &Log, active: bool, a: fn(&str) -> (bool, Resolved<&'static str>)) -> Legs {
    Legs {
        active,
        answer: Box::new(a),
        log: log.clone(),
    }
}

fn no_drift(
    _: &Vec<u32>,
    _: &PathRefs<'_>,
    _: &PathRefs<'_>,
    _: Option<&[u32]>,
    _: Option<&u8>,
) -> PathDriftReport {
    PathDriftReport::default()
}

const FRESH: Option<&str> = Some("2026-02-28T00:00:00+00:00");

fn marked(v: &str) -> String {
    format!("marked:{v}")
}

#[test]
fn unread_is_required_and_unchecked_goes_through_the_closure() {
    let log: Log = Rc::default();
    let mut pd = no_drift;
    let mut reads = Reads::<Anch> {
        anchoring: None,
        path_drift: &mut pd,
        unchecked_verdict: &marked,
    };
    let ms = [mem("u", FRESH), mem("c", FRESH), mem("d", FRESH)];
    let got = resolve_verdicts(
        &ms,
        || {
            legs(&log, true, |id| match id {
                "u" => (true, Resolved::Unread(HistoryUnread::Shallow)),
                "c" => (true, Resolved::Unchecked("why")),
                _ => (true, Resolved::Drift(ResolvedCommitDrift::new(0))),
            })
        },
        now(),
        30,
        &mut reads,
    )
    .unwrap();
    assert_eq!(got[0].staleness_verdict, "spot_check_required");
    assert_eq!(got[0].commit_drift_unread, Some(HistoryUnread::Shallow));
    assert!(got[0].commit_drift.is_none());
    assert_eq!(got[1].staleness_verdict, "marked:fresh");
    assert_eq!(got[1].commit_drift_unchecked, Some("why"));
    assert_eq!(got[2].staleness_verdict, "fresh");
    assert_eq!(got[2].commit_drift.as_ref().unwrap().status, "clean");
    assert_eq!(log.borrow().last().unwrap(), "store");
}

#[test]
fn memo_answers_unresolved_where_resolving_raises() {
    let log: Log = Rc::default();
    let mut pd = no_drift;
    let mut reads = Reads::<Anch> {
        anchoring: None,
        path_drift: &mut pd,
        unchecked_verdict: &marked,
    };
    let ms = [mem("a", FRESH), mem("b", FRESH), mem("c", FRESH)];
    let ans: fn(&str) -> (bool, Resolved<&'static str>) = |id| (id != "b", Resolved::None);
    let got = memoised_verdicts(&ms, || legs(&log, true, ans), now(), 30, &mut reads).unwrap();
    assert!(matches!(got[0], Verdict::Full(_)));
    assert!(matches!(got[1], Verdict::Unresolved(_)));
    assert!(matches!(got[2], Verdict::Full(_)));
    assert!(log.borrow().iter().all(|l| l != "store"));
    log.borrow_mut().clear();
    let got = resolve_verdicts(&ms, || legs(&log, true, ans), now(), 30, &mut reads);
    assert_eq!(got, Err(VerdictError::Unanswered));
    assert_eq!(
        *log.borrow(),
        ["legs.prime a,b,c", "lookup a true", "lookup b true"]
    );
}

#[test]
fn anchoring_primed_only_without_active_legs_and_root_dropped_when_anchored() {
    let log: Log = Rc::default();
    let roots: Rc<RefCell<Vec<Option<String>>>> = Rc::default();
    let r2 = roots.clone();
    let mut pd = move |_: &Vec<u32>,
                       _: &PathRefs<'_>,
                       _: &PathRefs<'_>,
                       root: Option<&[u32]>,
                       a: Option<&u8>| {
        r2.borrow_mut().push(
            root.map(|r| format!("{}{a:?}", bettermemory_core::unicode::to_string(r).unwrap())),
        );
        PathDriftReport::default()
    };
    let mut anch = Anch {
        log: log.clone(),
        anchored: true,
    };
    let mut reads = Reads {
        anchoring: Some(&mut anch),
        path_drift: &mut pd,
        unchecked_verdict: &marked,
    };
    let ms = [mem("a", None)];
    let none: fn(&str) -> (bool, Resolved<&'static str>) = |_| (true, Resolved::None);
    resolve_verdicts(&ms, || legs(&log, true, none), now(), 30, &mut reads).unwrap();
    resolve_verdicts(&ms, || legs(&log, false, none), now(), 30, &mut reads).unwrap();
    assert_eq!(
        *log.borrow(),
        ["legs.prime a", "lookup a true", "store", "anch.prime a"]
    );
    assert_eq!(*roots.borrow(), [None, None]);
    reads.anchoring.as_mut().unwrap().anchored = false;
    memory_verdict(&ms[0], || legs(&log, false, none), now(), 30, &mut reads).unwrap();
    assert_eq!(roots.borrow()[2].as_deref(), Some("/w/aNone"));
}

#[test]
fn a_raise_mid_list_skips_the_store_and_later_rows() {
    let log: Log = Rc::default();
    let mut pd = no_drift;
    let mut reads = Reads::<Anch> {
        anchoring: None,
        path_drift: &mut pd,
        unchecked_verdict: &marked,
    };
    let naive = DateTime::from_isoformat(&cps("2026-03-01T00:00:00")).unwrap();
    let ms = [mem("a", None), mem("b", FRESH), mem("c", None)];
    let none: fn(&str) -> (bool, Resolved<&'static str>) = |_| (true, Resolved::None);
    let got = resolve_verdicts(&ms, || legs(&log, true, none), naive, 30, &mut reads);
    assert_eq!(got, Err(VerdictError::NaiveAwareMix));
    assert_eq!(*log.borrow(), ["legs.prime a,b,c", "lookup a true"]);
}

#[test]
fn open_legs_keeps_the_active_test() {
    let log: Log = Rc::default();
    let none: fn(&str) -> (bool, Resolved<&'static str>) = |_| (true, Resolved::None);
    assert!(open_legs(|| legs(&log, false, none)).is_none());
    assert!(open_legs(|| legs(&log, true, none)).is_some());
}

#[test]
fn an_empty_active_pass_primes_and_only_resolution_stores() {
    let log: Log = Rc::default();
    let mut pd = no_drift;
    let mut reads = Reads::<Anch> {
        anchoring: None,
        path_drift: &mut pd,
        unchecked_verdict: &marked,
    };
    let make = || legs(&log, true, |_| panic!("looked up an absent memory"));
    assert!(resolve_verdicts(&[], make, now(), 30, &mut reads)
        .unwrap()
        .is_empty());
    assert_eq!(*log.borrow(), ["legs.prime ", "store"]);
    log.borrow_mut().clear();
    assert!(memoised_verdicts(&[], make, now(), 30, &mut reads)
        .unwrap()
        .is_empty());
    assert_eq!(*log.borrow(), ["legs.prime "]);
}

#[test]
fn both_path_reads_receive_the_original_code_points() {
    const ROOT: &[u32] = &[47, 0xdc80, 47, 0xdcff];
    struct Unhomed;
    impl VerdictAnchoring for Unhomed {
        type Anchor = ();
        fn prime(&mut self, _: &[String]) {}
        fn for_record(&mut self, _: &str, root: Option<&[u32]>) -> Option<()> {
            assert_eq!(root, Some(ROOT));
            None
        }
    }
    let mut memory = mem("r", None);
    memory.worktree_root = Some(ROOT.to_vec());
    let mut pd =
        |_: &Vec<u32>, _: &PathRefs<'_>, _: &PathRefs<'_>, root: Option<&[u32]>, _: Option<&()>| {
            assert_eq!(root, Some(ROOT));
            PathDriftReport::default()
        };
    let mut reads = Reads {
        anchoring: Some(&mut Unhomed),
        path_drift: &mut pd,
        unchecked_verdict: &marked,
    };
    memory_verdict(
        &memory,
        || legs(&Rc::default(), false, |_| unreachable!()),
        now(),
        30,
        &mut reads,
    )
    .unwrap();
}

#[test]
fn path_drift_borrows_the_records_legacy_and_literal_paths() {
    let mut memory = mem("r", FRESH);
    memory.record.verified_paths = vec![vec![0xdc80; 60_000]];
    memory.record.verified_absent_path_targets = Some(vec![
        (cps("inert"), None),
        (cps("absent"), Some(vec![0xdcff; 60_000])),
    ]);
    let legacy = memory.record.verified_paths[0].as_slice();
    let literal = memory.record.verified_absent_path_targets.as_ref().unwrap()[1]
        .1
        .as_deref()
        .unwrap();
    let mut read = |_: &Vec<u32>,
                    verified: &PathRefs<'_>,
                    absent: &PathRefs<'_>,
                    _: Option<&[u32]>,
                    _: Option<&u8>| {
        assert_eq!(verified.items.len(), 1);
        assert_eq!(absent.items.len(), 1);
        assert!(std::ptr::eq(verified.items[0], legacy));
        assert!(std::ptr::eq(absent.items[0], literal));
        PathDriftReport::default()
    };
    let mut reads = Reads::<Anch> {
        anchoring: None,
        path_drift: &mut read,
        unchecked_verdict: &marked,
    };
    memory_verdict(
        &memory,
        || legs(&Rc::default(), false, |_| unreachable!()),
        now(),
        30,
        &mut reads,
    )
    .unwrap();
}
