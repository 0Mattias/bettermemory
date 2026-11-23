//! Size bounds, long Python strings and scores supplied directly to the pure API.
#[path = "common/review_decision.rs"]
mod cases;
mod timing;

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

fn updates(n: usize) -> (ReviewAnswers, RelatedSet) {
    let ids: Vec<_> = (0..n).map(|i| cps(&format!("id-{i}"))).collect();
    let answers = cases::answers(
        ids.iter()
            .rev()
            .map(|id| (id.clone(), cps(LABEL_UPDATE)))
            .collect(),
        0.0,
        0.0,
    );
    let related = RelatedSet {
        entries: ids.into_iter().map(|id| cases::entry(id, true)).collect(),
        report: None,
    };
    (answers, related)
}

#[test]
fn reversed_answers_and_many_distinct_replacements_grow_linearly() {
    let small = updates(1500);
    let large = updates(15000);
    let o = options();
    let run = |(a, r): &(ReviewAnswers, RelatedSet)| {
        timing::once(|| (apply_review(a, r, &o), replaced_ids(a, r, &[])))
    };
    timing::grows_within(
        "review decisions and replacements",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
    let links = apply_review(&large.0, &large.1, &o).links;
    assert_eq!(links.len(), 15000);
    assert_eq!(links.first(), Some(&cps("id-0")));
    assert_eq!(links.last(), Some(&cps("id-14999")));
}

#[test]
fn long_ids_remain_distinct_at_their_last_python_code_point() {
    let mut a_id = vec![0x10000; 32000];
    a_id.push(0xdcff);
    let mut b_id = a_id.clone();
    *b_id.last_mut().unwrap() = 0xfffd;
    let a = cases::answers(
        vec![
            (a_id.clone(), cps(LABEL_CONFLICT)),
            (b_id.clone(), cps(LABEL_UPDATE)),
        ],
        0.0,
        0.0,
    );
    let r = RelatedSet {
        entries: vec![
            cases::entry(a_id.clone(), true),
            cases::entry(b_id.clone(), true),
        ],
        report: Some(CueReport {
            supersedes: vec![a_id.clone()],
            conflicts: vec![],
        }),
    };
    let d = apply_review(&a, &r, &options());
    assert_eq!(d.cue_dropped, [a_id]);
    assert_eq!(d.links.as_slice(), std::slice::from_ref(&b_id));
    assert_eq!(replaced_ids(&a, &r, &[]), [b_id]);
}

#[test]
fn direct_infinite_and_nan_thresholds_follow_float_comparisons() {
    let a = cases::answers(vec![], f64::NEG_INFINITY, f64::INFINITY);
    let r = RelatedSet::default();
    for (unsafe_threshold, secret, expected) in [
        (0.3, f64::INFINITY, ACTION_SECRET),
        (f64::NEG_INFINITY, f64::NAN, ACTION_UNSAFE),
        (f64::NAN, f64::NAN, ACTION_COMMIT),
    ] {
        let tau = Tau {
            r#unsafe: unsafe_threshold,
            secret,
        };
        let o = ReviewOptions { tau, ..options() };
        assert_eq!(apply_review(&a, &r, &o).action, cps(expected));
    }
}

#[test]
fn large_one_shot_id_streams_keep_only_the_first_occurrence_order() {
    let a = cases::answers(vec![], 0.0, 0.0);
    let r = RelatedSet::default();
    let mut first = vec![0x10000; 256];
    first.push(0xdcff);
    let mut second = first.clone();
    *second.last_mut().unwrap() = 0xfffd;
    let stream = || {
        Ok::<_, ()>((0..150_000).map(|i| {
            Ok(if i == 2 {
                second.clone()
            } else {
                first.clone()
            })
        }))
    };
    let expected = [first.clone(), second.clone()];
    assert_eq!(replaced_ids_iter(&a, &r, stream).unwrap(), expected);
    let o = ReviewOptions {
        write_standing: true,
        ..options()
    };
    assert_eq!(
        apply_review_iter(&a, &r, &o, stream, stream)
            .unwrap()
            .standing,
        expected
    );
}
