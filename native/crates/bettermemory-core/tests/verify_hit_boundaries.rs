//! Directed checks on the caller-owned effects: original exceptions and
//! the same shared histories across a series of hits.

mod verify_count_script;
mod verify_weak_script;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_dates::memo::Timestamps;
use bettermemory_core::origin_history::{ReachableWalk, Unwalked};
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::count::CountReads;
use bettermemory_core::verify::hit::{resolve_hit_drift, Hit};
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::{Place, WeakError};
use serde_json::json;
use std::sync::Arc;
use verify_count_script::Counted;
use verify_weak_script::{claims, literal};

#[derive(Debug)]
enum Error {
    Provider(Arc<str>),
    Native(WeakError),
}

impl From<WeakError> for Error {
    fn from(error: WeakError) -> Self {
        Self::Native(error)
    }
}

#[test]
fn original_errors_leave_at_the_read_that_raised_them() {
    let git = Counted::new(&json!({}));
    let lit = LiteralReader::default();
    let reads = CountReads {
        git: &git,
        at: Place {
            cwd: &[],
            toplevel: None,
        },
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let since = DateTime::from_isoformat(&cps("2024-01-01")).unwrap();
    let error: Arc<str> = Arc::from("caller-specific error");
    let mut unwalked = Unwalked::new();
    for head in [Some(&[][..]), None] {
        let hit = Hit {
            since: &since,
            head,
            verified_head: Some(&[]),
            anchors: &[],
            claims: &[],
        };
        let raised = resolve_hit_drift(
            reads,
            hit,
            &mut unwalked,
            |at, anchor, head, _| {
                assert_eq!(at.toplevel, None);
                assert!(head.is_empty() && anchor.is_empty());
                Err(Error::Provider(error.clone()))
            },
            || {
                assert!(head.is_none(), "a failed walk must end this hit");
                Err(Error::Provider(error.clone()))
            },
        );
        let Err(Error::Provider(actual)) = raised else {
            panic!("expected provider error: {raised:?}");
        };
        assert!(Arc::ptr_eq(&actual, &error));
        assert!(git.script.calls.borrow().is_empty());
    }
    let hit = Hit {
        since: &since,
        head: None,
        verified_head: None,
        anchors: &[],
        claims: &[],
    };
    let aware = DateTime::from_isoformat(&cps("2024-01-01T00:00:00+00:00")).unwrap();
    let got = resolve_hit_drift::<Error>(
        reads,
        hit,
        &mut unwalked,
        |_, _, _, _| panic!("unexpected walk"),
        || Ok(Timestamps::Dates(Arc::new(vec![aware]))),
    );
    assert!(matches!(got, Err(Error::Native(WeakError::NaiveAware))));
}

#[test]
fn many_hits_reuse_their_reported_walk_or_whole_history() {
    let git = Counted::new(&json!({}));
    let lit = LiteralReader::default();
    let reads = CountReads {
        git: &git,
        at: Place {
            cwd: &[],
            toplevel: None,
        },
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let since = DateTime::from_isoformat(&cps("2024-01-01T10:00:00+00:00")).unwrap();
    let after = DateTime::from_isoformat(&cps("2024-01-01T10:00:00.000001+00:00")).unwrap();
    let history = Arc::new(vec![after; 32_768]);
    let walked = Arc::new(ReachableWalk {
        anchor: cps("a"),
        head: cps("h"),
        commits: vec![cps("x"); 16_384],
        touched: Default::default(),
        listed: true,
        timed_out: false,
    });
    // A stored unreadable claim carries the repo-wide count straight through.
    let declared = claims(&json!([["path", "a\nb", "", ""]]));
    let mut unwalked = Unwalked::new();
    let mut dates_read = 0;
    for with_walk in [true, false, true, false] {
        let hit = Hit {
            since: &since,
            head: with_walk.then_some(walked.head.as_slice()),
            verified_head: Some(&walked.anchor),
            anchors: &[],
            claims: &declared,
        };
        let found = resolve_hit_drift::<WeakError>(
            reads,
            hit,
            &mut unwalked,
            |_, a, h, _| {
                assert_eq!(a, walked.anchor);
                assert_eq!(h, walked.head);
                Ok(Some(walked.clone()))
            },
            || {
                dates_read += 1;
                Ok(Timestamps::Dates(history.clone()))
            },
        )
        .unwrap()
        .unwrap()
        .unwrap();
        assert_eq!(found.count, if with_walk { 16_384 } else { 32_768 });
        assert_eq!(
            found.basis,
            if with_walk {
                "reachability"
            } else {
                "author-date"
            }
        );
        assert_eq!(found.claims_checked, 1);
    }
    assert_eq!(dates_read, 2);
    assert!(git.script.calls.borrow().is_empty());
}
