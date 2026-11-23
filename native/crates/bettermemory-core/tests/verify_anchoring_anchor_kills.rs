//! Cases a plausible wrong port of `verify_anchoring`'s report and
//! anchors fails: a memo that forgets a False, a memo keyed by the commit
//! alone, an empty commit read as None, an empty origin root counted, a
//! path under no view returned expanded, the anchors keyed by record.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify_anchoring::anchor::{
    AnchorPaths, Anchoring, CallerView, Checkout, ClaimAnchor, Home, MembershipModel,
    UnknownCheckout, View, HELD_COMMIT_MEMO_CAP, UNANCHORED,
};
use bettermemory_core::verify_anchoring::report::{PathDriftReport, ZipLengthError};

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

#[derive(Default)]
struct Model {
    anchors: Cell<usize>,
    translated: Cell<usize>,
}

impl MembershipModel for Model {
    fn homes(&self, _: &[String]) -> HashMap<String, Home> {
        HashMap::new()
    }
    fn home(&self, id: &str) -> Option<Home> {
        (id != "none").then(|| Home {
            project: "p".into(),
            checkout: Some("k".into()),
        })
    }
    fn live_view(&self, _: &Checkout) -> Option<String> {
        Some("/w".into())
    }
    fn checkout(&self, _: &str) -> Result<Checkout, UnknownCheckout> {
        Err(UnknownCheckout)
    }
    fn anchor(&self, id: &str, _: Option<&CallerView>) -> (Option<String>, String) {
        self.anchors.set(self.anchors.get() + 1);
        (Some(format!("/{id}")), "project_checkout".into())
    }
    fn checkouts(&self) -> Vec<Checkout> {
        vec![Checkout {
            id: "k".into(),
            project: "p".into(),
            remotes: vec![],
            views: vec![View {
                machine: "m".into(),
                path: "/h/u/v".into(),
            }],
        }]
    }
    fn canonical(&self, p: Option<&str>) -> Option<String> {
        p.map(str::to_string)
    }
    fn translate(&self, path: &str, _: Option<&CallerView>) -> (String, String) {
        self.translated.set(self.translated.get() + 1);
        (path.to_string(), "checkout".into())
    }
    fn machine(&self) -> String {
        "m".into()
    }
}

fn anchoring(model: Rc<Model>, calls: Rc<RefCell<Vec<(String, String)>>>) -> Anchoring {
    let holds = Rc::new(move |r: &str, s: &str| {
        calls.borrow_mut().push((r.into(), s.into()));
        s == "yes"
    });
    let expand = Rc::new(|p: &str| match p.strip_prefix('~') {
        Some(rest) => format!("/h/u{rest}"),
        None => p.to_string(),
    });
    Anchoring::new(model, None, holds, Rc::new(Plain), expand)
}

#[test]
fn a_false_answer_is_kept_and_keys_hold_the_root() {
    let calls = Rc::new(RefCell::new(Vec::new()));
    let a = anchoring(Rc::new(Model::default()), calls.clone());
    assert!(!a.holds("/r", "no"));
    assert!(!a.holds("/r", "no"));
    assert!(!a.holds("/s", "no"));
    assert_eq!(calls.borrow().len(), 2);
}

#[test]
fn the_memo_drops_the_least_recently_asked() {
    let calls = Rc::new(RefCell::new(Vec::new()));
    let a = anchoring(Rc::new(Model::default()), calls.clone());
    for i in 0..HELD_COMMIT_MEMO_CAP {
        a.holds("/r", &format!("s{i}"));
    }
    a.holds("/r", "s0");
    a.holds("/r", "new");
    let before = calls.borrow().len();
    a.holds("/r", "s0");
    assert_eq!(calls.borrow().len(), before);
    a.holds("/r", "s1");
    assert_eq!(calls.borrow().len(), before + 1);
}

#[test]
fn an_empty_commit_is_not_none() {
    let mut anchor = ClaimAnchor::new(Some("/w".into()), "project_checkout");
    anchor.caller_root = Some("/c".into());
    anchor.commit_standin = true;
    let asked = RefCell::new(Vec::new());
    let holds = |r: &str, s: &str| {
        asked.borrow_mut().push((r.to_string(), s.to_string()));
        true
    };
    assert_eq!(
        anchor.commit_root(Some(""), &holds),
        (Some("/w".into()), "project_checkout".into())
    );
    assert_eq!(
        asked.borrow().as_slice(),
        &[("/w".to_string(), String::new())]
    );
    anchor.caller_root = Some("/w".into());
    assert_eq!(anchor.commit_root(Some("c"), &holds).0, Some("/w".into()));
    assert_eq!(
        anchor.commit_root(Some("x"), &|_: &str, _: &str| false).1,
        "unconfirmed"
    );
}

#[test]
fn an_empty_origin_root_is_no_root() {
    let mut anchor = ClaimAnchor::new(Some("/w".into()), "project_checkout");
    anchor.translate = Some(Rc::new(|p: &str| (p.to_string(), "absolute".to_string())));
    anchor.origin_root = Some(Vec::new());
    assert_eq!(
        anchor.absolute("/x", &Plain),
        (Some("/x".into()), "absolute".into())
    );
    anchor.origin_root = Some(cps("/o"));
    assert_eq!(anchor.absolute("/o/x", &Plain), (None, UNANCHORED.into()));
    assert_eq!(anchor.absolute("/o", &Plain), (None, UNANCHORED.into()));
}

#[test]
fn a_path_under_no_view_keeps_its_spelling() {
    let model = Rc::new(Model::default());
    let a = anchoring(model.clone(), Rc::new(RefCell::new(Vec::new())));
    assert_eq!(a.translate("~/x"), ("~/x".into(), "absolute".into()));
    assert_eq!(a.translate("~/v/x"), ("/h/u/v/x".into(), "checkout".into()));
    a.translate("~/v/x");
    assert_eq!(model.translated.get(), 1);
}

#[test]
fn anchors_are_shared_by_home_not_by_record() {
    let model = Rc::new(Model::default());
    let mut a = anchoring(model.clone(), Rc::new(RefCell::new(Vec::new())));
    let first = a.for_record("one", None).expect("an anchor");
    let second = a.for_record("two", None).expect("an anchor");
    assert_eq!(second.root, first.root);
    assert_eq!(model.anchors.get(), 1);
    assert!(a.for_record("two", Some("/o")).is_some());
    assert_eq!(model.anchors.get(), 2);
    assert!(a.for_record("none", None).is_none());
}

#[test]
fn convicting_needs_a_root_and_declared_root_a_rule() {
    assert!(!ClaimAnchor::new(None, "home_checkout").convicting());
    assert!(ClaimAnchor::new(Some("/w".into()), "absolute").convicting());
    let same = ClaimAnchor::new(Some("/w".into()), "caller_checkout_same_project");
    assert_eq!(same.declared_root(), Some("/w"));
    assert_eq!(
        ClaimAnchor::new(Some("/w".into()), "project_checkout").declared_root(),
        None
    );
}

#[test]
fn the_report_zip_is_strict_and_only_anchored() {
    let mut r = PathDriftReport {
        unconfirmed: vec!["/z".into()],
        ..PathDriftReport::default()
    };
    assert_eq!(r.to_dict().expect("no anchor").len(), 6);
    r.anchor = Some(String::new());
    assert_eq!(r.to_dict(), Err(ZipLengthError));
    r.unconfirmed_reasons = vec!["x".into()];
    assert_eq!(r.to_dict().expect("anchored").len(), 9);
    assert!(!r.should_report());
    r.present_but_attested_absent = vec!["/a".into()];
    assert!(r.should_report() && !r.has_drift());
}

#[test]
fn origin_code_points_distinguish_anchors_in_the_same_home() {
    let model = Rc::new(Model::default());
    let mut anchors = anchoring(model.clone(), Rc::default());
    for suffix in [0xdc80, 0xfffd, 0xdcff, 0xdc80] {
        let root = [47, suffix];
        let anchor = bettermemory_core::verdict::VerdictAnchoring::for_record(
            &mut anchors,
            "one",
            Some(&root),
        )
        .unwrap();
        assert_eq!(anchor.origin_root.as_deref(), Some(root.as_slice()));
    }
    assert_eq!(model.anchors.get(), 3);
}
