//! Hostile use of the anchors: megabyte paths, a memo past its cap many
//! times over, many records of one home. Nothing panics, the held memo
//! stays at its cap, and its time grows linearly with the asks.

mod timing;

use std::collections::HashMap;
use std::rc::Rc;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify_anchoring::anchor::{
    AnchorPaths, Anchoring, CallerView, Checkout, ClaimAnchor, Home, MembershipModel,
    UnknownCheckout, View, HELD_COMMIT_MEMO_CAP,
};
use bettermemory_core::verify_anchoring::report::PathDriftReport;

struct Plain;

impl AnchorPaths for Plain {
    fn expected_home(&self, path: &str, roots: &[PyStr]) -> bool {
        let path = cps(path);
        roots
            .iter()
            .any(|r| bettermemory_core::capture::paths::inside(&path, r))
    }
    fn relative_under(&self, path: &str, root: Option<&str>) -> Option<String> {
        let root = root?;
        self.inside(path, root).then(|| path.to_string())
    }
    fn inside(&self, path: &str, root: &str) -> bool {
        path == root || path.starts_with(&format!("{root}/"))
    }
}

struct Model;

impl MembershipModel for Model {
    fn homes(&self, ids: &[String]) -> HashMap<String, Home> {
        ids.iter()
            .map(|i| {
                (
                    i.clone(),
                    Home {
                        project: "p".into(),
                        checkout: None,
                    },
                )
            })
            .collect()
    }
    fn home(&self, _: &str) -> Option<Home> {
        Some(Home {
            project: "p".into(),
            checkout: Some("k".into()),
        })
    }
    fn live_view(&self, _: &Checkout) -> Option<String> {
        None
    }
    fn checkout(&self, _: &str) -> Result<Checkout, UnknownCheckout> {
        Err(UnknownCheckout)
    }
    fn anchor(&self, _: &str, _: Option<&CallerView>) -> (Option<String>, String) {
        (Some("/w".into()), "home_checkout".into())
    }
    fn checkouts(&self) -> Vec<Checkout> {
        vec![Checkout {
            id: "k".into(),
            project: "p".into(),
            remotes: vec!["r".into()],
            views: vec![View {
                machine: "m".into(),
                path: "/v".into(),
            }],
        }]
    }
    fn canonical(&self, p: Option<&str>) -> Option<String> {
        p.map(str::to_string)
    }
    fn translate(&self, path: &str, _: Option<&CallerView>) -> (String, String) {
        (path.to_string(), "checkout".into())
    }
    fn machine(&self) -> String {
        "m".into()
    }
}

fn anchoring() -> Anchoring {
    let caller = Some(CallerView {
        checkout: Some("k".into()),
    });
    Anchoring::new(
        Rc::new(Model),
        caller,
        Rc::new(|_: &str, s: &str| s.len().is_multiple_of(2)),
        Rc::new(Plain),
        Rc::new(|p: &str| p.to_string()),
    )
}

#[test]
fn huge_paths_and_many_records_do_not_panic() {
    let mut a = anchoring();
    let long = format!("/v/{}", "x/".repeat(500_000));
    assert_eq!(a.translate(&long).1, "checkout");
    assert_eq!(a.translate(&"\u{10FFFF}".repeat(100_000)).1, "absolute");
    let ids: Vec<String> = (0..50_000).map(|i| format!("r{i}")).collect();
    a.prime(&ids);
    for id in &ids[..1000] {
        assert!(a.for_record(id, None).is_some());
    }
    let anchor = a.for_record("r1", Some(&long)).expect("an anchor");
    assert_eq!(anchor.absolute(&long, &Plain).1, "checkout");
    assert_eq!(
        anchor.commit_root(Some(&long), &|_: &str, _: &str| true).1,
        "home_checkout"
    );
    let report = PathDriftReport {
        unconfirmed: vec![long.clone(); 1000],
        unconfirmed_reasons: vec![String::new(); 1000],
        anchor: Some(String::new()),
        ..PathDriftReport::default()
    };
    assert_eq!(report.to_dict().expect("a dict").len(), 9);
    let bare = ClaimAnchor::new(None, "");
    assert_eq!(
        bare.commit_root(None, &|_: &str, _: &str| true).1,
        "unconfirmed"
    );
}

#[test]
fn the_memo_grows_linearly_past_its_cap() {
    let run = |n: usize| {
        let a = anchoring();
        let mut held = 0usize;
        for i in 0..n {
            held += usize::from(a.holds("/r", &format!("s{}", i % (HELD_COMMIT_MEMO_CAP * 3))));
        }
        held
    };
    assert!(run(20_000) > 0);
    timing::grows_within(
        "20k and 200k asks",
        timing::TENFOLD,
        &mut || timing::once(|| run(20_000)),
        &mut || timing::once(|| run(200_000)),
    );
}
