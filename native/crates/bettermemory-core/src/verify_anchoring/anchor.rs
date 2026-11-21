//! The anchoring vocabulary, `ClaimAnchor` and `Anchoring` of
//! `verify_anchoring.py` (reference lines 298-571): where one record's
//! claims are read (design 3.6), and the per-request memos that read each
//! home, anchor, translation and held commit once.
//!
//! The membership model is the trait [`MembershipModel`], holding exactly
//! the calls `Anchoring` makes; `holds_commit` and `os.path.expanduser`
//! are closures, and `commit_anchors.expected_home`, `relative_under` and
//! `_paths.inside` are the trait [`AnchorPaths`].

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::rc::Rc;

/// `CONVICTING_ANCHORS`: the rules under which an absence is evidence.
pub const CONVICTING_ANCHORS: [&str; 3] = ["home_checkout", "checkout", "absolute"];
/// `UNANCHORED`: an absolute path under a view with no live view here.
pub const UNANCHORED: &str = "unanchored";
/// `SPARSE_CHECKOUT`: an absence a sparse checkout explains.
pub const SPARSE_CHECKOUT: &str = "sparse_checkout";
/// `UNCONFIRMED_COMMIT`: a cited commit no live store here holds.
pub const UNCONFIRMED_COMMIT: &str = "unconfirmed";
/// `GLOBAL_RECORD`: the rule of a record homed in no project.
pub const GLOBAL_RECORD: &str = "global_record";
/// `_HELD_COMMIT_MEMO_CAP`: answers one request's `holds` keeps.
pub const HELD_COMMIT_MEMO_CAP: usize = 4096;

/// `holds_commit(root, sha)` or a memo of it.
pub type HoldsFn = Rc<dyn Fn(&str, &str) -> bool>;
/// `Membership.translate` as a claim anchor calls it: (target, rule).
pub type TranslateFn = Rc<dyn Fn(&str) -> (String, String)>;
/// `os.path.expanduser`.
pub type ExpandFn = Rc<dyn Fn(&str) -> String>;

/// A record's home row: its project and its checkout.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Home {
    pub project: String,
    pub checkout: Option<String>,
}

/// One recorded view of a checkout: the machine and the path.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct View {
    pub machine: String,
    pub path: String,
}

/// The fields of a checkout `Anchoring` reads.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Checkout {
    pub id: String,
    pub project: String,
    pub remotes: Vec<String>,
    pub views: Vec<View>,
}

/// The caller's resolution as `Anchoring` reads it: its checkout id.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CallerView {
    pub checkout: Option<String>,
}

/// `Membership.checkout`'s `KeyError` for an unknown id.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct UnknownCheckout;

/// The membership model's calls `Anchoring` makes, in its spelling.
pub trait MembershipModel {
    fn homes(&self, record_ids: &[String]) -> HashMap<String, Home>;
    fn home(&self, record_id: &str) -> Option<Home>;
    fn live_view(&self, checkout: &Checkout) -> Option<String>;
    fn checkout(&self, checkout_id: &str) -> Result<Checkout, UnknownCheckout>;
    fn anchor(&self, record_id: &str, caller: Option<&CallerView>) -> (Option<String>, String);
    fn checkouts(&self) -> Vec<Checkout>;
    fn canonical(&self, project: Option<&str>) -> Option<String>;
    fn translate(&self, path: &str, caller: Option<&CallerView>) -> (String, String);
    /// The attribute `machine`, read once for each view compared.
    fn machine(&self) -> String;
}

/// `commit_anchors.expected_home`, `commit_anchors.relative_under` and
/// `_paths.inside`.
pub trait AnchorPaths {
    fn expected_home(&self, path: &str, roots: &[String]) -> bool;
    fn relative_under(&self, path: &str, root: Option<&str>) -> Option<String>;
    fn inside(&self, path: &str, root: &str) -> bool;
}

/// Where one record's claims are read.
#[derive(Clone)]
pub struct ClaimAnchor {
    pub root: Option<String>,
    pub rule: String,
    pub translate: Option<TranslateFn>,
    pub caller_root: Option<String>,
    pub holds: Option<HoldsFn>,
    pub origin_root: Option<String>,
    pub recorded_roots: Vec<String>,
    pub commit_standin: bool,
}

impl ClaimAnchor {
    /// The plain anchor: no translation, no memo, no recorded roots.
    pub fn new(root: Option<String>, rule: &str) -> ClaimAnchor {
        ClaimAnchor {
            root,
            rule: rule.to_string(),
            translate: None,
            caller_root: None,
            holds: None,
            origin_root: None,
            recorded_roots: Vec::new(),
            commit_standin: false,
        }
    }

    /// `convicting`: a root under a convicting rule.
    pub fn convicting(&self) -> bool {
        self.root.is_some() && CONVICTING_ANCHORS.contains(&self.rule.as_str())
    }

    /// `declared_root`: the root when convicting or the caller's own
    /// checkout of the project, else None.
    pub fn declared_root(&self) -> Option<&str> {
        if self.convicting() || self.rule == "caller_checkout_same_project" {
            return self.root.as_deref();
        }
        None
    }

    /// `absolute(path)`: the path to stat and its rule, or None with it.
    pub fn absolute(&self, path: &str, paths: &dyn AnchorPaths) -> (Option<String>, String) {
        let Some(translate) = &self.translate else {
            return (Some(path.to_string()), "absolute".to_string());
        };
        let (target, rule) = translate(path);
        if rule == UNANCHORED {
            return (None, rule);
        }
        let mut roots = self.recorded_roots.clone();
        if let Some(origin) = self.origin_root.as_ref().filter(|o| !o.is_empty()) {
            roots.push(origin.clone());
        }
        if rule == "absolute"
            && paths.expected_home(path, &roots)
            && paths.relative_under(path, self.root.as_deref()).is_none()
        {
            return (None, UNANCHORED.to_string());
        }
        (Some(target), rule)
    }

    /// `commit_root(commit)`: where a cited commit is resolved. Without a
    /// memo of its own (`holds` None) the anchor asks `holds_commit`.
    pub fn commit_root(
        &self,
        commit: Option<&str>,
        holds_commit: &dyn Fn(&str, &str) -> bool,
    ) -> (Option<String>, String) {
        if self.root.is_some() && self.rule == "home_checkout" {
            return (self.root.clone(), "home_checkout".to_string());
        }
        let held = |root: &str, sha: &str| match &self.holds {
            Some(h) => h(root, sha),
            None => holds_commit(root, sha),
        };
        if let (Some(c), Some(caller)) = (commit, &self.caller_root) {
            if !c.is_empty() && held(caller, c) {
                return (Some(caller.clone()), "caller_checkout".to_string());
            }
        }
        if let Some(root) = &self.root {
            let standin = match commit {
                Some(c) => Some(root) != self.caller_root.as_ref() && held(root, c),
                None => self.commit_standin,
            };
            if standin {
                return (Some(root.clone()), self.rule.clone());
            }
        }
        (None, UNCONFIRMED_COMMIT.to_string())
    }
}

/// The least recently asked (root, sha) answers, `OrderedDict` style.
#[derive(Default)]
struct HeldMemo {
    answers: HashMap<(String, String), (bool, u64)>,
    order: BTreeMap<u64, (String, String)>,
    tick: u64,
}

/// The part of a request's anchoring the anchors' closures share.
struct Shared {
    model: Rc<dyn MembershipModel>,
    caller: Option<CallerView>,
    holds_commit: HoldsFn,
    paths: Rc<dyn AnchorPaths>,
    expanduser: ExpandFn,
    translated: RefCell<HashMap<String, (String, String)>>,
    views: RefCell<Option<Rc<Vec<String>>>>,
    held: RefCell<HeldMemo>,
}

impl Shared {
    fn holds(&self, root: &str, sha: &str) -> bool {
        let key = (root.to_string(), sha.to_string());
        {
            let mut memo = self.held.borrow_mut();
            memo.tick += 1;
            let tick = memo.tick;
            if let Some((found, old)) = memo.answers.get(&key).copied() {
                memo.order.remove(&old);
                memo.order.insert(tick, key.clone());
                memo.answers.insert(key, (found, tick));
                return found;
            }
        }
        let found = (self.holds_commit)(root, sha);
        let mut memo = self.held.borrow_mut();
        memo.tick += 1;
        let tick = memo.tick;
        memo.order.insert(tick, key.clone());
        memo.answers.insert(key, (found, tick));
        if memo.answers.len() > HELD_COMMIT_MEMO_CAP {
            if let Some((_, oldest)) = memo.order.pop_first() {
                memo.answers.remove(&oldest);
            }
        }
        found
    }

    fn translate(&self, path: &str) -> (String, String) {
        if let Some(hit) = self.translated.borrow().get(path) {
            return hit.clone();
        }
        let expanded = (self.expanduser)(path);
        let views = self.view_paths();
        let hit = if views.iter().any(|v| self.paths.inside(&expanded, v)) {
            self.model.translate(&expanded, self.caller.as_ref())
        } else {
            (path.to_string(), "absolute".to_string())
        };
        self.translated
            .borrow_mut()
            .insert(path.to_string(), hit.clone());
        hit
    }

    fn view_paths(&self) -> Rc<Vec<String>> {
        if let Some(v) = self.views.borrow().as_ref() {
            return v.clone();
        }
        let views: Vec<String> = self
            .model
            .checkouts()
            .into_iter()
            .flat_map(|k| k.views.into_iter().map(|v| v.path))
            .collect();
        let views = Rc::new(views);
        *self.views.borrow_mut() = Some(views.clone());
        views
    }
}

type AnchorKey = (String, Option<String>, Option<String>);

/// The claim anchors of one request (`Anchoring`). Not shared across
/// threads, as the model is not.
pub struct Anchoring {
    shared: Rc<Shared>,
    homes: HashMap<String, Option<Home>>,
    anchors: HashMap<AnchorKey, ClaimAnchor>,
    checkout_roots: HashMap<String, (Vec<String>, Vec<String>)>,
    caller_root: Option<Option<String>>,
}

impl Anchoring {
    pub fn new(
        model: Rc<dyn MembershipModel>,
        caller: Option<CallerView>,
        holds_commit: HoldsFn,
        paths: Rc<dyn AnchorPaths>,
        expanduser: ExpandFn,
    ) -> Anchoring {
        Anchoring {
            shared: Rc::new(Shared {
                model,
                caller,
                holds_commit,
                paths,
                expanduser,
                translated: RefCell::new(HashMap::new()),
                views: RefCell::new(None),
                held: RefCell::new(HeldMemo::default()),
            }),
            homes: HashMap::new(),
            anchors: HashMap::new(),
            checkout_roots: HashMap::new(),
            caller_root: None,
        }
    }

    /// `prime`: the homes of the ids not yet read, in one query (an id
    /// given twice is asked twice, as the reference's list asks it).
    pub fn prime(&mut self, record_ids: &[String]) {
        let wanted: Vec<String> = record_ids
            .iter()
            .filter(|r| !self.homes.contains_key(*r))
            .cloned()
            .collect();
        if wanted.is_empty() {
            return;
        }
        let found = self.shared.model.homes(&wanted);
        for rid in wanted {
            let home = found.get(&rid).cloned();
            self.homes.insert(rid, home);
        }
    }

    /// `home`: read once per id, None included.
    pub fn home(&mut self, record_id: &str) -> Option<Home> {
        if !self.homes.contains_key(record_id) {
            let home = self.shared.model.home(record_id);
            self.homes.insert(record_id.to_string(), home);
        }
        self.homes[record_id].clone()
    }

    /// `caller_root`: the live view of the caller's checkout, read once.
    pub fn caller_root(&mut self) -> Option<String> {
        if self.caller_root.is_none() {
            let model = &self.shared.model;
            let checkout = self
                .shared
                .caller
                .as_ref()
                .and_then(|c| c.checkout.as_ref());
            let root = checkout.and_then(|id| match model.checkout(id) {
                Ok(k) => model.live_view(&k),
                Err(UnknownCheckout) => None,
            });
            self.caller_root = Some(root);
        }
        self.caller_root.clone().flatten()
    }

    /// `for_record`: the record's anchor, shared by records of one home
    /// and origin root, or None with no home row.
    pub fn for_record(
        &mut self,
        record_id: &str,
        origin_root: Option<&str>,
    ) -> Option<ClaimAnchor> {
        let home = self.home(record_id)?;
        let key = (
            home.project.clone(),
            home.checkout.clone(),
            origin_root.map(str::to_string),
        );
        if let Some(found) = self.anchors.get(&key) {
            return Some(found.clone());
        }
        let (root, rule) = self
            .shared
            .model
            .anchor(record_id, self.shared.caller.as_ref());
        let (roots, remotes) = self.recorded_checkout(home.checkout.as_deref());
        let caller_root = self.caller_root();
        let commit_standin = self.commit_standin(root.as_deref(), &rule, &home.project, &remotes);
        let shared = self.shared.clone();
        let translate: TranslateFn = Rc::new(move |p: &str| shared.translate(p));
        let shared = self.shared.clone();
        let holds: HoldsFn = Rc::new(move |r: &str, s: &str| shared.holds(r, s));
        let found = ClaimAnchor {
            root,
            rule,
            translate: Some(translate),
            caller_root,
            holds: Some(holds),
            origin_root: origin_root.map(str::to_string),
            recorded_roots: roots,
            commit_standin,
        };
        self.anchors.insert(key, found.clone());
        Some(found)
    }

    fn recorded_checkout(&mut self, checkout: Option<&str>) -> (Vec<String>, Vec<String>) {
        let Some(checkout) = checkout else {
            return (Vec::new(), Vec::new());
        };
        if !self.checkout_roots.contains_key(checkout) {
            let read = match self.shared.model.checkout(checkout) {
                Ok(known) => (
                    known.views.iter().map(|v| v.path.clone()).collect(),
                    known.remotes.clone(),
                ),
                Err(UnknownCheckout) => (Vec::new(), Vec::new()),
            };
            self.checkout_roots.insert(checkout.to_string(), read);
        }
        self.checkout_roots[checkout].clone()
    }

    fn commit_standin(
        &self,
        root: Option<&str>,
        rule: &str,
        project: &str,
        remotes: &[String],
    ) -> bool {
        let Some(root) = root else { return false };
        if rule == "home_checkout" || remotes.is_empty() {
            return false;
        }
        let model = &self.shared.model;
        for checkout in model.checkouts() {
            if model.canonical(Some(&checkout.project)).as_deref() != Some(project) {
                continue;
            }
            if !remotes.iter().any(|r| checkout.remotes.contains(r)) {
                continue;
            }
            let viewed = checkout
                .views
                .iter()
                .any(|v| v.machine == model.machine() && v.path == root);
            if viewed && model.live_view(&checkout).as_deref() == Some(root) {
                return true;
            }
        }
        false
    }

    /// `holds`: `holds_commit` read once per (root, sha), least recently
    /// asked dropped past `HELD_COMMIT_MEMO_CAP`.
    pub fn holds(&self, root: &str, sha: &str) -> bool {
        self.shared.holds(root, sha)
    }

    /// `translate`: `Membership.translate` of the expanded path when it
    /// is inside a recorded view, else the path as spelled, `absolute`.
    pub fn translate(&self, path: &str) -> (String, String) {
        self.shared.translate(path)
    }
}
