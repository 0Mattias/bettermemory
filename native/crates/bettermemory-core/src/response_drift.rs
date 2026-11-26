//! The row half of `_response._DriftPass` (`_response.py` 157-190, 228,
//! 382-435, 517-733): the per-hit memo's key (`_DriftKey`) and its one
//! process-wide instance, `lookup`, `_key`, `unstored` and `store`, over
//! the location and history half in [`HistoryPass`].
//!
//! Effects reach the pass through [`DriftReads`], which extends
//! [`HistoryReads`] with the reads this half adds (the head's signature,
//! the attribute and walk signatures, `files_held`, the git call tallies,
//! the monotonic clock, and the home and user table the anchors read).
//! The resolution itself is a [`Resolve`]: [`HitResolver`] calls
//! `verify::hit::resolve_hit_drift` with the pass's walked map and its
//! lazy `timestamps`.

use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::sync::{Arc, LazyLock};

use crate::admission::DriftScope;
use crate::attestation_paths::{commit_paths, Raised, Record};
use crate::caches;
use crate::claims::literal::{governed_claim_paths, load_claims};
use crate::claims::Claim;
use crate::datetime::DateTime;
use crate::git_read_budget::{read, Raise, ReadDeadline, Scope};
use crate::gitconfig::legacy::Stamp;
use crate::githead::signature::Signature;
use crate::origin_dates::memo::Timestamps;
use crate::origin_gitfiles::attrs::AttributeFiles;
use crate::origin_gitfiles::facts::Held;
use crate::origin_gitfiles::walk::WalkFiles;
use crate::origin_history::{HistoryUnread, Key, ReachableWalk, Unwalked, Walked};
use crate::pysyntax::literal::LiteralError;
use crate::pyvalue::{PyStr, PyValue};
use crate::response_history::{HistoryPass, HistoryReads, Read};
use crate::response_memo::DriftMemo;
use crate::verify::anchors::{commit_drift_anchor_paths, AnchorError};
use crate::verify::count::CountReads;
use crate::verify::hit::{resolve_hit_drift, Hit, HitDrift};
use crate::verify::weak::{Place, WeakError};
use crate::verify_candidates::scan::UserHome;

/// What `gitattributes_signature` keys one set of governed files on.
pub type Chain = Vec<Option<Stamp>>;

/// `_DriftKey`: root, head, cwd, anchors, the record's raw claims, the
/// verify instant, `verified_head`, the attribute files (the repository's
/// and the governed files' chain) where a governed claim reads them, and
/// the files beside the walk. Equal as the reference's tuple is: `since`
/// by its instant, as aware datetimes compare.
#[derive(Clone, Debug)]
pub struct DriftKey {
    pub root: PyStr,
    pub head: PyStr,
    pub cwd: PyStr,
    pub anchors: Vec<PyStr>,
    pub claims: Vec<PyStr>,
    pub since: DateTime,
    pub verified_head: Option<PyStr>,
    pub attributes: Option<(AttributeFiles, Chain)>,
    pub walk_files: WalkFiles,
}

/// A datetime as `==` reads it: an aware one by its instant, a naive one
/// by its fields, and never a naive one equal to an aware one.
fn instant(d: &DateTime) -> (bool, i128) {
    match d.instant_us() {
        Some(us) => (true, us),
        None => (false, d.wall_micros()),
    }
}

impl PartialEq for DriftKey {
    fn eq(&self, o: &Self) -> bool {
        self.root == o.root
            && self.head == o.head
            && self.cwd == o.cwd
            && self.anchors == o.anchors
            && self.claims == o.claims
            && instant(&self.since) == instant(&o.since)
            && self.verified_head == o.verified_head
            && self.attributes == o.attributes
            && self.walk_files == o.walk_files
    }
}

impl Eq for DriftKey {}

/// Hashes the parts that are text and the instant; the signatures take
/// part in equality only.
impl Hash for DriftKey {
    fn hash<H: Hasher>(&self, h: &mut H) {
        (
            &self.root,
            &self.head,
            &self.cwd,
            &self.anchors,
            &self.claims,
        )
            .hash(h);
        instant(&self.since).hash(h);
        self.verified_head.hash(h);
    }
}

/// `_DRIFT_MEMO` with `_DRIFT_TIMED_OUT`.
pub type Memo = DriftMemo<DriftKey, HitDrift>;

/// The key `_clear_drift_memo` is registered under with [`caches`].
pub const CLEAR_KEY: &str = "_response._clear_drift_memo";

static DRIFT_MEMO: LazyLock<Memo> = LazyLock::new(|| {
    caches::register(CLEAR_KEY, clear_drift_memo);
    Memo::new()
});

/// The process's one memo, registered with [`caches`] on first use (an
/// unused memo is empty, so a clearing before then has nothing to clear).
pub fn drift_memo() -> &'static Memo {
    &DRIFT_MEMO
}

/// `_clear_drift_memo`.
pub fn clear_drift_memo() {
    DRIFT_MEMO.clear();
}

/// The reads this half adds to [`HistoryReads`]. Paths are Python code
/// points, as there.
pub trait DriftReads: HistoryReads {
    /// `githead.signature(cwd, owner=owner)`.
    fn signature(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        owner: bool,
    ) -> Result<Signature, Self::Error>;
    /// `attribute_files_signature(cwd, root, directories=held)`.
    fn attribute_files(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        root: &[u32],
        held: &mut Held,
    ) -> Result<Option<AttributeFiles>, Self::Error>;
    /// `gitattributes_signature(root, governed, directories=held)`.
    fn attribute_chain(
        &mut self,
        scope: &mut Scope,
        root: &[u32],
        governed: &[PyStr],
        held: &mut Held,
    ) -> Result<Option<Chain>, Self::Error>;
    /// `walk_files_signature(root, directories=held)`.
    fn walk_files(
        &mut self,
        scope: &mut Scope,
        root: &[u32],
        held: &mut Held,
    ) -> Result<Option<WalkFiles>, Self::Error>;
    /// `files_held(cwd, root, repository, chains, walk_files, held)`.
    #[allow(clippy::too_many_arguments)]
    fn files_held(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        root: &[u32],
        repository: Option<&AttributeFiles>,
        chains: &[(Vec<PyStr>, Option<Chain>)],
        walk_files: Option<&WalkFiles>,
        held: &Held,
    ) -> Result<bool, Self::Error>;
    /// `(failed_git_calls(), timed_out_calls())` of this thread.
    fn calls(&mut self) -> (i64, i64);
    /// `time.monotonic()`.
    fn now(&mut self) -> f64;
    /// The home directory the anchors expand `~` against.
    fn home(&mut self) -> Option<PyStr>;
    /// `pwd.getpwnam(name)`'s home, for a `~user` anchor.
    fn user_home(&mut self, name: &[u32]) -> UserHome;
}

/// What a row's lookup raises besides its reads' errors.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LookupError<E> {
    Read(E),
    Anchors(AnchorError),
    Paths(Raised),
    Claims(LiteralError),
    Drift(WeakError),
}

impl<E> From<WeakError> for LookupError<E> {
    fn from(e: WeakError) -> Self {
        LookupError::Drift(e)
    }
}

/// The fields of a record `lookup` reads.
#[derive(Clone, Copy, Debug)]
pub struct Row<'a> {
    pub id: &'a str,
    /// `record.origin.repo` is not None.
    pub origin_repo: bool,
    pub body: &'a [u32],
    pub paths: &'a Record,
    /// `record.claims`, raw.
    pub claims: &'a [PyStr],
    pub verified_head: Option<&'a [u32]>,
}

/// `resolve_hit_drift`'s arguments as `lookup` passes them.
#[derive(Clone, Copy, Debug)]
pub struct HitArgs<'a> {
    pub cwd: &'a [u32],
    pub located: Option<&'a Key>,
    pub anchors: &'a [PyStr],
    pub claims: &'a [Claim],
    pub since: &'a DateTime,
    pub verified_head: Option<&'a [u32]>,
}

/// One row's resolution, given the pass's walked map and its lazy
/// whole-history read.
pub trait Resolve<E> {
    fn resolve(
        &mut self,
        hit: HitArgs<'_>,
        walked: &mut Walked,
        timestamps: &mut dyn FnMut() -> Result<Timestamps, E>,
    ) -> Result<HitDrift, LookupError<E>>;
}

/// `origin.commits_since_anchor` over the pass's walked map.
pub type WalkFn<'w> = dyn FnMut(
        &mut Walked,
        Place,
        &[u32],
        &[u32],
        &mut Unwalked,
    ) -> Result<Option<Arc<ReachableWalk>>, WeakError>
    + 'w;

/// `verify.resolve_hit_drift` as `lookup` calls it: `count.at` is replaced
/// by the pass's directory and located root.
pub struct HitResolver<'a, 'w> {
    pub count: CountReads<'a>,
    pub unwalked: &'a mut Unwalked,
    pub walk: &'a mut WalkFn<'w>,
}

impl<E> Resolve<E> for HitResolver<'_, '_> {
    fn resolve(
        &mut self,
        hit: HitArgs<'_>,
        walked: &mut Walked,
        timestamps: &mut dyn FnMut() -> Result<Timestamps, E>,
    ) -> Result<HitDrift, LookupError<E>> {
        let at = Place {
            cwd: hit.cwd,
            toplevel: hit.located.map(|k| k.0.as_slice()),
        };
        let walk = &mut *self.walk;
        resolve_hit_drift(
            CountReads { at, ..self.count },
            Hit {
                since: hit.since,
                head: hit.located.map(|k| k.1.as_slice()),
                verified_head: hit.verified_head,
                anchors: hit.anchors,
                claims: hit.claims,
            },
            self.unwalked,
            |at, anchor, head, unwalked| {
                walk(walked, at, anchor, head, unwalked).map_err(LookupError::Drift)
            },
            || timestamps().map_err(LookupError::Read),
        )
    }
}

/// A dict of resolutions in insertion order.
#[derive(Clone, Debug, Default)]
struct Kept {
    order: Vec<DriftKey>,
    values: HashMap<DriftKey, HitDrift>,
}

impl Kept {
    fn insert(&mut self, key: DriftKey, value: HitDrift) {
        if let Some(slot) = self.values.get_mut(&key) {
            *slot = value;
            return;
        }
        self.order.push(key.clone());
        self.values.insert(key, value);
    }

    fn items(&self) -> Vec<(DriftKey, HitDrift)> {
        self.order
            .iter()
            .map(|k| (k.clone(), self.values[k].clone()))
            .collect()
    }
}

/// `Raise<E>` of a [`HistoryReads`] read, as `lookup` raises it.
fn lift<E>(r: Raise<E>) -> Raise<LookupError<E>> {
    match r {
        Raise::Deadline(d) => Raise::Deadline(d),
        Raise::Other(e) => Raise::Other(LookupError::Read(e)),
    }
}

fn other<E>(e: LookupError<E>) -> Raise<LookupError<E>> {
    match e {
        LookupError::Drift(WeakError::ReadDeadline) => Raise::Deadline(ReadDeadline),
        e => Raise::Other(e),
    }
}

/// One drift pass: the history half and the state its rows key, recall,
/// resolve and store through.
pub struct DriftPass<'m, S> {
    pub history: HistoryPass,
    admission: S,
    memo: &'m Memo,
    resolved: Kept,
    timed_out: Kept,
    walked: Walked,
    /// None while unread (`_UNREAD`); else what the read answered.
    repository: Option<Option<AttributeFiles>>,
    chains: Vec<(Vec<PyStr>, Option<Chain>)>,
    chain_at: HashMap<Vec<PyStr>, usize>,
    /// None while unread (`_walk_files_read` False).
    walk_files: Option<Option<WalkFiles>>,
    held: Held,
    signature: Option<Signature>,
    broken: bool,
}

impl<'m, S: DriftScope> DriftPass<'m, S> {
    /// `_DriftPass(cwd, caller_repo, admission, at_home=)` over `memo`
    /// (the process's is [`drift_memo`]).
    pub fn open<R: HistoryReads + ?Sized>(
        reads: &mut R,
        scope: &mut Scope,
        cwd: PyStr,
        at_home: bool,
        admission: S,
        memo: &'m Memo,
    ) -> Read<Self, R::Error> {
        Ok(DriftPass {
            history: HistoryPass::open(reads, scope, cwd, at_home)?,
            admission,
            memo,
            resolved: Kept::default(),
            timed_out: Kept::default(),
            walked: Walked::new(),
            repository: None,
            chains: Vec::new(),
            chain_at: HashMap::new(),
            walk_files: None,
            held: Held::new(),
            signature: None,
            broken: false,
        })
    }

    /// The resolutions held for `store` (`_resolved`, `_timed_out`), in
    /// the order they were kept.
    pub fn kept(&self) -> [Vec<(DriftKey, HitDrift)>; 2] {
        [self.resolved.items(), self.timed_out.items()]
    }

    /// The walks this pass remembers (`_walked`).
    pub fn walked(&self) -> &Walked {
        &self.walked
    }

    /// `unstored`.
    pub fn unstored(&self) -> usize {
        self.resolved.order.len() + self.timed_out.order.len()
    }

    /// `lookup(record, last_verified_at, resolve=, anchor_paths=)` under
    /// `git_read_budget.hit`: a deadline answers `(true, CALL_BUDGET)`.
    #[allow(clippy::too_many_arguments)]
    pub fn lookup<R: DriftReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
        row: &Row,
        last_verified_at: Option<&DateTime>,
        resolve: bool,
        anchor_paths: Option<&[PyStr]>,
        resolver: &mut dyn Resolve<R::Error>,
    ) -> Result<(bool, HitDrift), LookupError<R::Error>> {
        let first = Some((self.history.cwd().to_vec(), self.history.at_home()));
        read(
            scope,
            first,
            |scope| {
                self.lookup_body(
                    reads,
                    scope,
                    row,
                    last_verified_at,
                    resolve,
                    anchor_paths,
                    resolver,
                )
            },
            || (true, Some(Err(HistoryUnread::CallBudget))),
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn lookup_body<R: DriftReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
        row: &Row,
        last_verified_at: Option<&DateTime>,
        resolve: bool,
        anchor_paths: Option<&[PyStr]>,
        resolver: &mut dyn Resolve<R::Error>,
    ) -> Result<(bool, HitDrift), Raise<LookupError<R::Error>>> {
        let Some(stamp) = last_verified_at else {
            return Ok((true, None));
        };
        if !row.origin_repo && !self.history.at_home() {
            return Ok((true, None));
        }
        if !self.admission.drift_counts(row.id) {
            return Ok((true, None));
        }
        let paths = commit_paths(row.paths).map_err(|e| other(LookupError::Paths(e)))?;
        let home = reads.home();
        let mut users = |name: &[u32]| reads.user_home(name);
        let mut anchors = commit_drift_anchor_paths(row.body, &paths, home.as_deref(), &mut users)
            .map_err(|e| other(LookupError::Anchors(e)))?;
        if let Some(given) = anchor_paths {
            anchors = given.to_vec();
        }
        let parsed = if row.claims.is_empty() {
            Vec::new()
        } else {
            let raw: Vec<PyValue> = row.claims.iter().cloned().map(PyValue::Str).collect();
            load_claims(&raw).map_err(|e| other(LookupError::Claims(e)))?
        };
        if anchors.is_empty() && parsed.is_empty() {
            return Ok((true, None));
        }
        let since = stamp.assume_utc();
        let key = self.key(reads, scope, row, &anchors, &parsed, since, resolve)?;
        if let Some(key) = &key {
            if let Some(found) = self.memo.recall(key, reads.now()) {
                return Ok((true, found));
            }
            for kept in [&self.resolved, &self.timed_out] {
                if let Some(value) = kept.values.get(key) {
                    return Ok((true, value.clone()));
                }
            }
        }
        if !resolve {
            return Ok((false, None));
        }
        let cwd = self.history.cwd().to_vec();
        if self.history.keyed().is_some() && self.signature.is_none() {
            let owned = self.history.owned();
            let read = reads.signature(scope, &cwd, owned);
            self.signature = Some(read.map_err(|e| Raise::Other(LookupError::Read(e)))?);
        }
        let (failed, timed_out) = reads.calls();
        let located = self.history.located(reads, scope).map_err(lift)?.cloned();
        let hit = HitArgs {
            cwd: &cwd,
            located: located.as_ref(),
            anchors: &anchors,
            claims: &parsed,
            since: &since,
            verified_head: row.verified_head,
        };
        let history = &mut self.history;
        let mut timestamps = || history.timestamps(reads, scope);
        let resolved = resolver
            .resolve(hit, &mut self.walked, &mut timestamps)
            .map_err(other)?;
        if let Some(key) = key {
            if resolved != Some(Err(HistoryUnread::CallBudget))
                && self.history.history_at(&key.head)
            {
                let (failed_now, timed_out_now) = reads.calls();
                // Python's ints: the differences of any two tallies, exact.
                let moved = i128::from(failed_now) - i128::from(failed);
                if moved == 0 {
                    self.resolved.insert(key, resolved.clone());
                } else if moved == i128::from(timed_out_now) - i128::from(timed_out) {
                    self.timed_out.insert(key, resolved.clone());
                }
            }
        }
        Ok((true, resolved))
    }

    /// `_key`: None where the root and the head are not keyed, the walk
    /// files are not read, or a governed row's attribute files are not.
    #[allow(clippy::too_many_arguments)]
    fn key<R: DriftReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
        row: &Row,
        anchors: &[PyStr],
        parsed: &[Claim],
        since: DateTime,
        resolve: bool,
    ) -> Result<Option<DriftKey>, Raise<LookupError<R::Error>>> {
        let keyed = self
            .history
            .keyed_pair(reads, scope, resolve)
            .map_err(lift)?;
        let Some((root, head)) = keyed.cloned() else {
            return Ok(None);
        };
        let fail = |e| Raise::Other(LookupError::Read(e));
        let governed = governed_claim_paths(parsed).map_err(|e| other(LookupError::Claims(e)))?;
        let mut attributes = None;
        if !governed.is_empty() {
            if self.repository.is_none() {
                let cwd = self.history.cwd();
                let read = reads.attribute_files(scope, cwd, &root, &mut self.held);
                self.repository = Some(read.map_err(fail)?);
            }
            let chain = match self.chain_at.get(&governed) {
                Some(&i) => self.chains[i].1.clone(),
                None => {
                    let read = reads.attribute_chain(scope, &root, &governed, &mut self.held);
                    let chain = read.map_err(fail)?;
                    self.chain_at.insert(governed.clone(), self.chains.len());
                    self.chains.push((governed.clone(), chain.clone()));
                    chain
                }
            };
            if let (Some(Some(repository)), Some(chain)) = (&self.repository, chain) {
                attributes = Some((repository.clone(), chain));
            }
        }
        if self.walk_files.is_none() {
            let read = reads.walk_files(scope, &root, &mut self.held);
            self.walk_files = Some(read.map_err(fail)?);
        }
        let Some(Some(walk_files)) = &self.walk_files else {
            return Ok(None);
        };
        if !governed.is_empty() && attributes.is_none() {
            return Ok(None);
        }
        Ok(Some(DriftKey {
            root,
            head,
            cwd: self.history.cwd().to_vec(),
            anchors: anchors.to_vec(),
            claims: row.claims.to_vec(),
            since,
            verified_head: row.verified_head.map(<[u32]>::to_vec),
            attributes,
            walk_files: walk_files.clone(),
        }))
    }

    /// `store`: merge what the pass holds into the memo while what it was
    /// keyed on reads as it did; a failed check discards it and breaks the
    /// pass, so every later store with something to merge fails too.
    pub fn store<R: DriftReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
    ) -> Read<bool, R::Error> {
        if self.resolved.order.is_empty() && self.timed_out.order.is_empty() {
            return Ok(!self.broken);
        }
        let held = match self.history.keyed().cloned() {
            Some(keyed) if !self.broken => self.holds(reads, scope, &keyed)?,
            _ => false,
        };
        let resolved = std::mem::take(&mut self.resolved);
        let timed_out = std::mem::take(&mut self.timed_out);
        if held {
            let now = reads.now();
            self.memo
                .remember(&resolved.items(), &timed_out.items(), now);
            return Ok(true);
        }
        self.broken = true;
        Ok(false)
    }

    /// The checks of `store` (707-727), in order.
    fn holds<R: DriftReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
        keyed: &Key,
    ) -> Read<bool, R::Error> {
        let cwd = self.history.cwd().to_vec();
        let owned = self.history.owned();
        let same = if owned {
            reads.files_pair(scope, &cwd)?.as_ref() == Some(keyed)
        } else {
            reads.located_head(scope, &keyed.0)?.as_deref() == Some(keyed.1.as_slice())
        };
        if !same {
            return Ok(false);
        }
        let now = reads.signature(scope, &cwd, owned).map_err(Raise::Other)?;
        if Some(&now) != self.signature.as_ref() {
            return Ok(false);
        }
        let repository = self.repository.as_ref().and_then(Option::as_ref);
        let walk_files = self.walk_files.as_ref().and_then(Option::as_ref);
        reads
            .files_held(
                scope,
                &cwd,
                &keyed.0,
                repository,
                &self.chains,
                walk_files,
                &self.held,
            )
            .map_err(Raise::Other)
    }
}

/// [`DriftReads`] over the POSIX readers, beside
/// [`crate::response_history::posix::OriginReads`].
#[cfg(unix)]
pub mod posix {
    use super::*;
    use crate::fsdata::Fs;
    use crate::gitfile_rule::bounded::Answers;
    use crate::gitfile_rule::follow::{fsencode, Records};
    use crate::githead::discovery::{Env, Host, Owners};
    use crate::githead::signature::{signature, undetermined};
    use crate::origin_dates::memo::ShallowHold;
    use crate::origin_gitfiles::attrs::{
        attribute_files_signature, gitattributes_signature, IndexMemo,
    };
    use crate::origin_gitfiles::facts::NotPorted;
    use crate::origin_gitfiles::root::ConfigMemo;
    use crate::origin_gitfiles::walk::{files_held, walk_files_signature, Keyed, Machine};
    use crate::origin_history::ValueError;
    use crate::response_history::posix::{OriginReads, UnsupportedPath};
    use crate::verify_candidates::scan::UserLookup;

    /// An exception `githead.signature` does not catch, by class name.
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub struct Uncaught(pub &'static str);

    /// The environment the signatures read beside the discovery variables.
    #[derive(Clone, Debug, Default)]
    pub struct Beside {
        /// GIT_INDEX_FILE is set.
        pub index_file_set: bool,
        pub xdg_config_home: Option<Vec<u8>>,
        pub home: Option<Vec<u8>>,
    }

    /// The origin readers and what this half reads beside them.
    pub struct DriftOrigin<'a, F, A, R, G, H, E, O>
    where
        F: Fs,
        A: Answers + ?Sized,
        R: Records + ?Sized,
        H: ShallowHold + ?Sized,
        O: Owners + ?Sized,
    {
        pub origin: OriginReads<'a, F, A, R, G, H, E>,
        pub host: &'a Host<'a, O>,
        pub config: &'a mut ConfigMemo,
        pub index: &'a mut IndexMemo,
        pub beside: &'a dyn Fn() -> Beside,
        pub calls: &'a dyn Fn() -> (i64, i64),
        pub clock: &'a dyn Fn() -> f64,
        pub home: Option<PyStr>,
        pub users: &'a mut UserLookup<'a>,
    }

    fn encoded<E: From<UnsupportedPath>>(path: &[u32]) -> Result<Vec<u8>, E> {
        fsencode(path).ok_or_else(|| UnsupportedPath(path.to_vec()).into())
    }

    impl<F, A, R, G, H, E, O> HistoryReads for DriftOrigin<'_, F, A, R, G, H, E, O>
    where
        F: Fs,
        A: Answers + ?Sized,
        R: Records + ?Sized,
        G: PartialEq + Clone,
        H: ShallowHold + ?Sized,
        O: Owners + ?Sized,
        E: From<ValueError> + From<UnsupportedPath>,
    {
        type Error = E;
        fn files_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, E> {
            self.origin.files_pair(scope, cwd)
        }
        fn git_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, E> {
            self.origin.git_pair(scope, cwd)
        }
        fn discovery_moved(&mut self, scope: &mut Scope) -> Read<bool, E> {
            self.origin.discovery_moved(scope)
        }
        fn located_head(&mut self, scope: &mut Scope, root: &[u32]) -> Read<Option<PyStr>, E> {
            self.origin.located_head(scope, root)
        }
        fn dates(
            &mut self,
            scope: &mut Scope,
            cwd: &[u32],
            located: Option<&Key>,
        ) -> Read<Timestamps, E> {
            self.origin.dates(scope, cwd, located)
        }
        fn note_unread(&mut self, scope: &mut Scope, why: HistoryUnread) -> Read<(), E> {
            self.origin.note_unread(scope, why)
        }
    }

    impl<F, A, R, G, H, E, O> DriftReads for DriftOrigin<'_, F, A, R, G, H, E, O>
    where
        F: Fs,
        A: Answers + ?Sized,
        R: Records + ?Sized,
        G: PartialEq + Clone,
        H: ShallowHold + ?Sized,
        O: Owners + ?Sized,
        E: From<ValueError> + From<UnsupportedPath> + From<NotPorted> + From<Uncaught>,
    {
        fn signature(&mut self, _: &mut Scope, cwd: &[u32], owner: bool) -> Result<Signature, E> {
            let env: Env = (self.origin.env)();
            let Some(cwd) = fsencode(cwd) else {
                // The ValueError of an unencodable path is caught there.
                return Ok(Signature {
                    first: undetermined(),
                    head: None,
                    config: None,
                    head_file: None,
                    refs: None,
                    packed: None,
                    git_dir: env.git_dir,
                    git_work_tree: env.git_work_tree,
                    ceilings: env.ceilings,
                });
            };
            signature(self.origin.rule, self.host, &env, &cwd, owner)
                .map_err(|c| Uncaught(c).into())
        }
        fn attribute_files(
            &mut self,
            _: &mut Scope,
            cwd: &[u32],
            root: &[u32],
            held: &mut Held,
        ) -> Result<Option<AttributeFiles>, E> {
            let (cwd, root) = (encoded::<E>(cwd)?, encoded::<E>(root)?);
            let b = (self.beside)();
            let env = (self.origin.env)();
            Ok(attribute_files_signature(
                self.config,
                self.origin.rule,
                self.host,
                &env,
                b.xdg_config_home.as_deref(),
                b.home.as_deref(),
                &cwd,
                &root,
                Some(held),
            )?)
        }
        fn attribute_chain(
            &mut self,
            _: &mut Scope,
            root: &[u32],
            governed: &[PyStr],
            held: &mut Held,
        ) -> Result<Option<Chain>, E> {
            let root = encoded::<E>(root)?;
            // A path os.lstat cannot encode raises ValueError there, which
            // answers None before any directory is held.
            let Some(specs) = governed
                .iter()
                .map(|p| fsencode(p))
                .collect::<Option<Vec<_>>>()
            else {
                return Ok(None);
            };
            let specs: Vec<&[u8]> = specs.iter().map(Vec::as_slice).collect();
            Ok(gitattributes_signature(
                self.origin.rule.ctx,
                &root,
                &specs,
                Some(held),
            )?)
        }
        fn walk_files(
            &mut self,
            _: &mut Scope,
            root: &[u32],
            held: &mut Held,
        ) -> Result<Option<WalkFiles>, E> {
            let root = encoded::<E>(root)?;
            let index_file_set = (self.beside)().index_file_set;
            let env = (self.origin.env)();
            Ok(walk_files_signature(
                self.config,
                self.index,
                self.origin.rule,
                self.host,
                &env,
                index_file_set,
                &root,
                Some(held),
            )?)
        }
        fn files_held(
            &mut self,
            _: &mut Scope,
            cwd: &[u32],
            root: &[u32],
            repository: Option<&AttributeFiles>,
            chains: &[(Vec<PyStr>, Option<Chain>)],
            walk_files: Option<&WalkFiles>,
            held: &Held,
        ) -> Result<bool, E> {
            let (cwd, root) = (encoded::<E>(cwd)?, encoded::<E>(root)?);
            let chains = chains
                .iter()
                .filter_map(|(specs, chain)| {
                    let specs = specs.iter().map(|p| fsencode(p)).collect::<Option<_>>()?;
                    Some((specs, chain.clone()))
                })
                .collect();
            let keyed = Keyed {
                repository: repository.cloned(),
                chains,
                walk_files: walk_files.cloned(),
                directories: held.clone(),
            };
            let b = (self.beside)();
            let env = (self.origin.env)();
            let machine = Machine {
                env: &env,
                index_file_set: b.index_file_set,
                xdg_config_home: b.xdg_config_home.as_deref(),
                home: b.home.as_deref(),
            };
            let rule = self.origin.rule;
            Ok(files_held(
                self.config,
                self.index,
                rule,
                self.host,
                &machine,
                &cwd,
                &root,
                &keyed,
            )?)
        }
        fn calls(&mut self) -> (i64, i64) {
            (self.calls)()
        }
        fn now(&mut self) -> f64 {
            (self.clock)()
        }
        fn home(&mut self) -> Option<PyStr> {
            self.home.clone()
        }
        fn user_home(&mut self, name: &[u32]) -> UserHome {
            (self.users)(name)
        }
    }
}
