//! Branches which can change a refusal, retire a record or file the wrong detector.
#[path = "common/review_decision.rs"]
mod cases;

use bettermemory_core::review::*;
use bettermemory_core::unicode::cps;

fn options() -> ReviewOptions<'static> {
    ReviewOptions::new(
        Tau {
            r#unsafe: 0.3,
            secret: 0.3,
        },
        true,
        true,
    )
}

#[test]
fn threshold_and_standing_conflict_precede_every_other_rule() {
    let mut a = cases::answers(
        vec![(cps("r"), cps("restates")), (cps("c"), cps("conflict"))],
        0.3,
        0.3,
    );
    let r = RelatedSet {
        entries: ["r", "c", "c"]
            .map(|id| cases::entry(cps(id), true))
            .to_vec(),
        report: None,
    };
    let standing = [cps("c")];
    let o = ReviewOptions {
        said: true,
        standing_ids: &standing,
        write_standing: true,
        ..options()
    };
    assert_eq!(apply_review(&a, &r, &o).action, cps(ACTION_SECRET));
    a.secret = 0.3_f64.next_down();
    assert_eq!(apply_review(&a, &r, &o).action, cps(ACTION_UNSAFE));
    a.r#unsafe = f64::NAN;
    let refused = apply_review(&a, &r, &o);
    assert_eq!(
        refused,
        ReviewDecision {
            action: cps(ACTION_STANDING),
            standing: vec![cps("c"), cps("c")],
            ..Default::default()
        }
    );
}

#[test]
fn restatement_precedes_unsaid_replacement_and_can_be_carried_through() {
    let a = cases::answers(vec![(cps("r"), cps("restates"))], 0.0, 0.0);
    let r = RelatedSet {
        entries: vec![cases::entry(cps("r"), true)],
        report: Some(CueReport {
            supersedes: vec![cps("past")],
            conflicts: vec![],
        }),
    };
    let o = ReviewOptions {
        write_standing: true,
        ..options()
    };
    let restated = apply_review(&a, &r, &o);
    assert_eq!(restated.action, cps(ACTION_RESTATES));
    assert_eq!(restated.restated, [cps("r")]);
    let declared = [cps("first"), cps("first")];
    let o = ReviewOptions {
        declared_ids: &declared,
        ..o
    };
    let unsaid = apply_review(&a, &r, &o);
    assert_eq!(
        unsaid,
        ReviewDecision {
            action: cps(ACTION_STANDING),
            standing: vec![cps("first"), cps("past")],
            standing_write: true,
            ..Default::default()
        }
    );
    let said = ReviewOptions { said: true, ..o };
    let committed = apply_review(&a, &r, &said);
    assert_eq!(
        committed,
        ReviewDecision {
            action: cps(ACTION_COMMIT),
            restated: vec![cps("r")],
            ..Default::default()
        }
    );
}

#[test]
fn updated_declared_and_cue_ids_have_distinct_duplicate_rules() {
    let a = cases::answers(
        vec![(cps("a"), cps("update")), (cps("x"), cps("extends"))],
        0.0,
        0.0,
    );
    let r = RelatedSet {
        entries: ["a", "a"].map(|id| cases::entry(cps(id), false)).to_vec(),
        report: Some(CueReport {
            supersedes: ["b", "c", "x", "c", "a"].map(cps).to_vec(),
            conflicts: vec![cps("decoy")],
        }),
    };
    assert_eq!(
        replaced_ids(&a, &r, &["a", "b", "b"].map(cps)),
        ["a", "a", "b", "c", "c"].map(cps)
    );
}

#[test]
fn disputed_cues_keep_extends_unless_declared_and_file_once_per_entry() {
    let labels = [
        "conflict",
        "extends",
        "unrelated",
        "conflict",
        "extends",
        "update",
        "update",
        "update",
    ];
    let ids = ["a", "b", "c", "d", "e", "f", "g", "h"];
    let a = cases::answers(
        ids.iter()
            .zip(labels)
            .map(|(id, l)| (cps(id), cps(l)))
            .collect(),
        0.0,
        0.0,
    );
    let r = RelatedSet {
        entries: ids
            .iter()
            .map(|id| cases::entry(cps(id), *id != "g"))
            .collect(),
        report: Some(CueReport {
            supersedes: ["a", "b", "c", "h"].map(cps).to_vec(),
            conflicts: vec![cps("d")],
        }),
    };
    let declared = [cps("e")];
    let decision = apply_review(
        &a,
        &r,
        &ReviewOptions {
            declared_ids: &declared,
            ..options()
        },
    );
    assert_eq!(
        decision,
        ReviewDecision {
            action: cps(ACTION_COMMIT),
            extends: vec![cps("b")],
            filings: [
                ("a", DETECTOR_REVIEW_DISAGREES),
                ("b", DETECTOR_REVIEW_DISAGREES),
                ("c", DETECTOR_REVIEW_DISAGREES),
                ("d", DETECTOR_REVIEW)
            ]
            .map(|(id, d)| (cps(id), cps(d)))
            .to_vec(),
            cue_dropped: ["a", "b", "c"].map(cps).to_vec(),
            links: vec![cps("f")],
            update_suggested: vec![cps("g")],
            ..Default::default()
        }
    );
}

#[test]
fn score_refusals_leave_iterators_unopened_and_a_shared_source_is_consumed_once() {
    use std::cell::RefCell;
    use std::iter::from_fn;
    let r = RelatedSet::default();
    let o = ReviewOptions {
        write_standing: true,
        ..options()
    };
    let a = cases::answers(vec![], 0.3, 0.3);
    type EmptyIds<E> = Result<[Result<Vec<u32>, E>; 0], E>;
    let never = || -> EmptyIds<()> { panic!("opened on a score refusal") };
    assert_eq!(
        apply_review_iter(&a, &r, &o, never, never).unwrap().action,
        cps(ACTION_SECRET)
    );
    let a = cases::answers(vec![], 0.3, 0.0);
    assert_eq!(
        apply_review_iter(&a, &r, &o, never, never).unwrap().action,
        cps(ACTION_UNSAFE)
    );
    let a = cases::answers(vec![], 0.0, 0.0);
    let shared = RefCell::new([Ok::<_, ()>(cps("external"))].into_iter());
    let read = || Ok(from_fn(|| shared.borrow_mut().next()));
    assert_eq!(
        apply_review_iter(&a, &r, &o, read, read).unwrap().action,
        cps(ACTION_COMMIT)
    );
    let fresh = || Ok::<_, ()>([Ok(cps("external"))]);
    assert_eq!(
        apply_review_iter(&a, &r, &o, fresh, fresh)
            .unwrap()
            .standing,
        [cps("external")]
    );
}

#[test]
fn iterable_errors_preserve_the_original_value_and_stop_before_the_next_item() {
    use std::cell::RefCell;
    use std::iter::from_fn;
    use std::rc::Rc;
    let a = cases::answers(vec![], 0.0, 0.0);
    let r = RelatedSet::default();
    let o = options();
    // The source can fail before returning its iterator, or partway through.
    let raised = Rc::new(vec![0xdcff]);
    let calls = RefCell::new(Vec::new());
    let source = |name, fails| {
        calls.borrow_mut().push((name, 0));
        let mut n = 0;
        let calls = &calls;
        let fault = raised.clone();
        Ok(from_fn(move || {
            n += 1;
            calls.borrow_mut().push((name, n));
            match n {
                1 => Some(Ok(cps("id"))),
                2 if fails => Some(Err(fault.clone())),
                2 => None,
                _ => panic!("consumed past the end"),
            }
        }))
    };
    for first in [true, false] {
        calls.borrow_mut().clear();
        let error = apply_review_iter(
            &a,
            &r,
            &o,
            || source("declared", true),
            || source("standing", first),
        )
        .unwrap_err();
        assert!(Rc::ptr_eq(&error, &raised));
        let mut expected = vec![("standing", 0), ("standing", 1), ("standing", 2)];
        if !first {
            expected.extend([("declared", 0), ("declared", 1), ("declared", 2)]);
        }
        assert_eq!(*calls.borrow(), expected);
    }
    calls.borrow_mut().clear();
    let e = replaced_ids_iter(&a, &r, || source("declared", true)).unwrap_err();
    assert!(Rc::ptr_eq(&raised, &e));
    assert_eq!(
        *calls.borrow(),
        [("declared", 0), ("declared", 1), ("declared", 2)]
    );
    type EmptyIds = Result<[Result<Vec<u32>, Rc<Vec<u32>>>; 0], Rc<Vec<u32>>>;
    let no_iterator = || -> EmptyIds { Err(raised.clone()) };
    assert!(Rc::ptr_eq(
        &raised,
        &apply_review_iter(
            &a,
            &r,
            &o,
            || -> EmptyIds { panic!("opened second source") },
            no_iterator
        )
        .unwrap_err()
    ));
    assert!(Rc::ptr_eq(
        &raised,
        &replaced_ids_iter(&a, &r, no_iterator).unwrap_err()
    ));
}
