//! A caller's resolution (`location.py` 1420-1997), unit loccaller: the
//! resolution itself. `capture_location` (1420-1431), `_model`
//! (1531-1549), `Caller` with `home` and `add_head_witness` (1552-1595),
//! `resolve_caller` (1657-1688), `_caller` (1691-1767), `_resolved`
//! (1818-1828), `resolve_here` (1862-1876) and `_hooked` (1982-1997).
//!
//! Every collaborator is an argument. The membership model is a
//! [`CallerModel`] made by a closure (`_model`'s `Membership(...)`); the
//! capture cache is a [`Capturer`]; `checkout_token.remint` is a closure
//! the model's token writer calls; `_earlier_tree` and `_machineless`
//! (unit locearly) are closures. The per-process tables are
//! [`Process`]'s, from `location_caller`.

use std::cell::RefCell;
use std::rc::Rc;

use crate::capture::location::{to_location, Location};
use crate::capture::{Capture, Kind, State};
use crate::checkout_token::Lineage;
use crate::location_caller::{with_file_errors, Locks, ProcessMachine, Stand, Stands};
use crate::machine::MachineIdentity;
use crate::pyvalue::PyStr;
use crate::store::caller::Resolution;
use crate::unicode;

/// `location_git.FETCH_BUDGET` (`location_git.py` 127), in seconds.
pub const FETCH_BUDGET: f64 = 0.3;

/// `location_git.NO_CWD` (`location_git.py` 186).
pub const NO_CWD: &str = "<no working directory>";

/// The module globals a resolution reads: the machine opened once, the
/// per-location locks and where each session stands.
#[derive(Debug, Default)]
pub struct Process {
    pub machine: ProcessMachine,
    pub locks: Locks,
    pub stands: Stands,
}

/// The two values of `config.membership` a resolution reads.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Policy {
    pub write_token: bool,
    pub strict: bool,
}

/// `store_schema.RecordHome`: the project, checkout and rule a record
/// written now is given.
#[derive(Clone, Debug, PartialEq)]
pub struct RecordHome {
    pub project: PyStr,
    pub checkout: Option<PyStr>,
    pub rule: PyStr,
}

/// The model's token writer: given the lineage of the checkout it
/// re-mints (`k.lineage`), the token written and its status, or None
/// where the caller has no git directory.
pub type TokenWriter<'a> = Box<dyn FnMut(Option<Lineage>) -> Option<(Option<PyStr>, PyStr)> + 'a>;

/// What `location` asks of `membership.Membership`.
pub trait CallerModel {
    type Error;
    /// `Membership.record_machine(identity)`.
    fn record_machine(&mut self, identity: &MachineIdentity) -> Result<(), Self::Error>;
    /// `Membership.resolve(loc, strict=strict)`.
    fn resolve(&mut self, loc: &Location, strict: bool) -> Result<Resolution, Self::Error>;
    /// `Membership.session_resolution(loc, resolution, path)`.
    fn session_resolution(
        &mut self,
        loc: &Location,
        stand: &Resolution,
        path: &[u32],
    ) -> Result<Option<Resolution>, Self::Error>;
    /// `Membership.unresolved_home(loc, session=session)`.
    fn unresolved_home(
        &mut self,
        loc: &Location,
        session: Option<&[u32]>,
    ) -> Result<RecordHome, Self::Error>;
    /// `Membership.add_witness(project, head, source=..., history=...)`;
    /// the history is the repository's common directory, where it has one.
    fn add_witness(
        &mut self,
        project: &[u32],
        head: &[u32],
        source: &[u32],
        history: Option<&[u32]>,
    ) -> Result<(), Self::Error>;
}

/// The capture cache: `capture`, `forget` and `_history_of`.
pub trait Capturer {
    /// `capture(cwd, write_token=..., budget=..., use_cache=...)`.
    fn capture(&mut self, cwd: &[u32], write_token: bool, budget: f64, use_cache: bool) -> Capture;
    /// `forget(cwd)`.
    fn forget(&mut self, cwd: &[u32]);
    /// `_history_of(cap)`: the common directory a history reader reads,
    /// for a repository's capture that was not evicted.
    fn history_of(&self, cap: &Capture) -> Option<PyStr> {
        history_of(cap)
    }
}

/// `_history_of(cap)` (`location.py` 1414-1417).
pub fn history_of(cap: &Capture) -> Option<PyStr> {
    if cap.state != State::Repo || cap.evicted {
        return None;
    }
    cap.common_dir.clone().filter(|d| !d.is_empty())
}

/// `Caller`: the location read, its resolution and the model that
/// decided it. `history` is the location's history reader, by its
/// common directory.
#[derive(Debug)]
pub struct Caller<M> {
    pub location: Location,
    pub history: Option<PyStr>,
    pub resolution: Resolution,
    pub model: M,
}

impl<M: CallerModel> Caller<M> {
    /// `Caller.home()`: the resolution's home, or the location's
    /// provisional one where the outcome is indeterminate or deferred or
    /// the resolution names no project.
    pub fn home(&mut self) -> Result<RecordHome, M::Error> {
        let res = &self.resolution;
        let unread =
            res.outcome == unicode::cps("indeterminate") || res.outcome == unicode::cps("deferred");
        match &res.project {
            Some(project) if !unread => Ok(RecordHome {
                project: project.clone(),
                checkout: res.checkout.clone(),
                rule: res.rule.clone(),
            }),
            _ => {
                let session = self.location.session.clone();
                self.model
                    .unresolved_home(&self.location, session.as_deref())
            }
        }
    }

    /// `Caller.add_head_witness(source)`: HEAD as a witness of the
    /// caller's project, its source `source@checkout` where the checkout
    /// is not empty; nothing without a project, in global, or without a
    /// head.
    pub fn add_head_witness(&mut self, source: &[u32]) -> Result<(), M::Error> {
        let Some(project) = self.resolution.project.clone() else {
            return Ok(());
        };
        let head = match &self.location.head {
            Some(h) if !h.is_empty() => h.clone(),
            _ => return Ok(()),
        };
        if project == unicode::cps("global") {
            return Ok(());
        }
        let mut src = source.to_vec();
        if let Some(c) = self.resolution.checkout.as_ref().filter(|c| !c.is_empty()) {
            src.push(u32::from('@'));
            src.extend_from_slice(c);
        }
        self.model
            .add_witness(&project, &head, &src, self.history.as_deref())
    }
}

/// `capture_location(cwd, machine=..., session=..., config=..., budget=...)`:
/// the capture at `cwd` as a location, with its history reader.
pub fn capture_location<C: Capturer>(
    capturer: &mut C,
    cwd: &[u32],
    machine: &[u32],
    session: Option<&[u32]>,
    write_token: bool,
    budget: f64,
) -> (Location, Option<PyStr>) {
    let cap = capturer.capture(cwd, write_token, budget, true);
    let history = capturer.history_of(&cap);
    (to_location(&cap, machine, session), history)
}

/// `_model(store, identity, session, token_writer=...)`: the model `new`
/// makes, the machine recorded in the store once per process.
pub fn model<'a, M: CallerModel>(
    process: &Process,
    store_path: &[u32],
    identity: &MachineIdentity,
    session: Option<&[u32]>,
    writer: Option<TokenWriter<'a>>,
    new: impl FnOnce(&MachineIdentity, Option<&[u32]>, Option<TokenWriter<'a>>) -> Result<M, M::Error>,
) -> Result<M, M::Error> {
    let mut made = new(identity, session, writer)?;
    process
        .machine
        .recorded(store_path, identity, |id| made.record_machine(id))?;
    Ok(made)
}

/// The arguments of `resolve_caller` and `_caller` that are values.
#[derive(Clone, Debug)]
pub struct Request {
    pub store_path: PyStr,
    pub policy: Policy,
    pub cwd: Option<PyStr>,
    pub session: Option<PyStr>,
    pub identity: Option<MachineIdentity>,
    pub budget: f64,
}

/// The collaborators of `_caller`: `open_machine` (behind
/// `process_machine`), the capture cache, the model's constructor,
/// `checkout_token.remint` (git directory, policy, bare, lineage),
/// `_earlier_tree` and `_machineless`. The model's constructor and
/// `_earlier_tree` may fail as `Membership(...)`, `model.checkouts()` and
/// `gitdir_pin.put` may raise, and the error leaves `_caller` before the
/// lock is taken.
pub struct Hands<'a, C, M, O, N, R, T, X> {
    pub open_machine: O,
    pub capturer: &'a mut C,
    pub new_model: N,
    pub remint: R,
    pub earlier_tree: T,
    pub machineless: X,
    pub _model: std::marker::PhantomData<M>,
}

/// What `_machineless(store, config, cwd, session, error, stand)` is
/// given: the cwd `_caller` was given (what `resolve_here` resolved, the
/// process cwd where its caller gave none), the session, the error
/// `open_machine` raised and the session's stand.
#[derive(Debug)]
pub struct Machineless<'s, E> {
    pub cwd: Option<&'s [u32]>,
    pub session: Option<&'s [u32]>,
    pub error: E,
    pub stand: Option<&'s Stand>,
}

struct Writing {
    git_dir: Option<PyStr>,
    bare: bool,
    wrote: Vec<Option<PyStr>>,
}

fn unreadable(machine: &[u32], session: Option<&[u32]>) -> Location {
    let mut cap = Capture::new(unicode::cps(NO_CWD), State::Indeterminate);
    cap.detail = unicode::cps("the caller has no working directory");
    to_location(&cap, machine, session)
}

/// `_resolved(model, loc, config, stand)`: the session's checkout that
/// `stand` names where the model finds it holds the location, else the
/// ladder's resolution.
pub fn resolved<M: CallerModel>(
    model: &mut M,
    loc: &Location,
    strict: bool,
    stand: Option<&Stand>,
) -> Result<Resolution, M::Error> {
    if let Some(stand) = stand {
        if let Some(res) = model.session_resolution(loc, &stand.resolution, &stand.path)? {
            return Ok(res);
        }
    }
    model.resolve(loc, strict)
}

/// `_caller(store, config, cwd, session=..., identity=..., budget=...,
/// stand=...)`.
pub fn caller<'a, C, M, E, O, N, R, T, X>(
    process: &Process,
    req: &Request,
    stand: Option<&Stand>,
    hands: Hands<'_, C, M, O, N, R, T, X>,
) -> Result<Caller<M>, M::Error>
where
    C: Capturer,
    M: CallerModel,
    O: FnOnce() -> Result<MachineIdentity, E>,
    N: FnOnce(&MachineIdentity, Option<&[u32]>, Option<TokenWriter<'a>>) -> Result<M, M::Error>,
    R: FnMut(&[u32], bool, bool, Option<Lineage>) -> (Option<PyStr>, PyStr) + 'a,
    T: FnOnce(&M, &Capture) -> Result<bool, M::Error>,
    X: FnOnce(Machineless<'_, E>) -> Result<Caller<M>, M::Error>,
{
    let Hands {
        open_machine,
        capturer,
        new_model,
        mut remint,
        earlier_tree,
        machineless,
        ..
    } = hands;
    let session = req.session.as_deref();
    let identity = match &req.identity {
        Some(id) => id.clone(),
        None => match process.machine.process_machine(open_machine) {
            Ok(id) => id,
            Err(error) => {
                return machineless(Machineless {
                    cwd: req.cwd.as_deref(),
                    session,
                    error,
                    stand,
                })
            }
        },
    };
    let mut errors: Vec<PyStr> = Vec::new();
    let mut cap: Option<Capture> = None;
    let (mut loc, mut history) = match &req.cwd {
        None => (unreadable(&identity.id, session), None),
        Some(where_) => {
            let c = capturer.capture(where_, req.policy.write_token, req.budget, true);
            errors = c.file_errors.clone();
            let h = capturer.history_of(&c);
            let l = to_location(&c, &identity.id, session);
            cap = Some(c);
            (l, h)
        }
    };
    let writing = Rc::new(RefCell::new(Writing {
        git_dir: loc.git_dir.clone(),
        bare: loc.kind == Some(Kind::Bare),
        wrote: Vec::new(),
    }));
    let shared = writing.clone();
    let policy = req.policy.write_token;
    let writer: TokenWriter<'a> = Box::new(move |lineage| {
        let mut w = shared.borrow_mut();
        let git_dir = w.git_dir.clone().filter(|g| !g.is_empty())?;
        let (token, status) = remint(&git_dir, policy, w.bare, lineage);
        w.wrote.push(token.clone());
        Some((token, status))
    });
    let mut model = model(
        process,
        &req.store_path,
        &identity,
        session,
        Some(writer),
        new_model,
    )?;
    if let (Some(where_), Some(c)) = (&req.cwd, &cap) {
        if earlier_tree(&model, c)? {
            capturer.forget(where_);
            let c = capturer.capture(where_, req.policy.write_token, req.budget, false);
            errors = c.file_errors.clone();
            history = capturer.history_of(&c);
            loc = to_location(&c, &identity.id, session);
            let mut w = writing.borrow_mut();
            w.git_dir = loc.git_dir.clone();
            w.bare = loc.kind == Some(Kind::Bare);
        }
    }
    let res = process.locks.with_location_lock(&loc, || {
        resolved(&mut model, &loc, req.policy.strict, stand)
    })?;
    let wrote = !writing.borrow().wrote.is_empty();
    if let (true, Some(where_)) = (wrote, &req.cwd) {
        capturer.forget(where_);
    }
    Ok(Caller {
        resolution: with_file_errors(&res, &errors),
        location: loc,
        history,
        model,
    })
}

/// `resolve_caller(store, config, cwd, session=..., identity=...,
/// budget=...)`: `_caller` with the session's stand in this store.
pub fn resolve_caller<'a, C, M, E, O, N, R, T, X>(
    process: &Process,
    req: &Request,
    hands: Hands<'_, C, M, O, N, R, T, X>,
) -> Result<Caller<M>, M::Error>
where
    C: Capturer,
    M: CallerModel,
    O: FnOnce() -> Result<MachineIdentity, E>,
    N: FnOnce(&MachineIdentity, Option<&[u32]>, Option<TokenWriter<'a>>) -> Result<M, M::Error>,
    R: FnMut(&[u32], bool, bool, Option<Lineage>) -> (Option<PyStr>, PyStr) + 'a,
    T: FnOnce(&M, &Capture) -> Result<bool, M::Error>,
    X: FnOnce(Machineless<'_, E>) -> Result<Caller<M>, M::Error>,
{
    let stand = process
        .stands
        .stand_of(&req.store_path, req.session.as_deref());
    caller(process, req, stand.as_ref(), hands)
}

/// `resolve_here(store, config, cwd, ...)`: the resolution of
/// `resolve_caller` at `cwd`, or where it is None at what `getcwd`
/// answers (None where `os.getcwd` raises).
pub fn resolve_here<'a, C, M, E, O, N, R, T, X>(
    process: &Process,
    req: &Request,
    getcwd: impl FnOnce() -> Option<PyStr>,
    hands: Hands<'_, C, M, O, N, R, T, X>,
) -> Result<Resolution, M::Error>
where
    C: Capturer,
    M: CallerModel,
    O: FnOnce() -> Result<MachineIdentity, E>,
    N: FnOnce(&MachineIdentity, Option<&[u32]>, Option<TokenWriter<'a>>) -> Result<M, M::Error>,
    R: FnMut(&[u32], bool, bool, Option<Lineage>) -> (Option<PyStr>, PyStr) + 'a,
    T: FnOnce(&M, &Capture) -> Result<bool, M::Error>,
    X: FnOnce(Machineless<'_, E>) -> Result<Caller<M>, M::Error>,
{
    let mut req = req.clone();
    req.cwd = crate::location_caller::cwd_of(req.cwd.take(), getcwd);
    Ok(resolve_caller(process, &req, hands)?.resolution)
}

/// `_hooked(store, config, cwd, session)`: `_caller` with the process's
/// machine, `FETCH_BUDGET` and no stand, its resolution kept as the
/// session's stand.
pub fn hooked<'a, C, M, E, O, N, R, T, X>(
    process: &Process,
    store_path: &[u32],
    policy: Policy,
    cwd: Option<&[u32]>,
    session: Option<&[u32]>,
    hands: Hands<'_, C, M, O, N, R, T, X>,
) -> Result<Resolution, M::Error>
where
    C: Capturer,
    M: CallerModel,
    O: FnOnce() -> Result<MachineIdentity, E>,
    N: FnOnce(&MachineIdentity, Option<&[u32]>, Option<TokenWriter<'a>>) -> Result<M, M::Error>,
    R: FnMut(&[u32], bool, bool, Option<Lineage>) -> (Option<PyStr>, PyStr) + 'a,
    T: FnOnce(&M, &Capture) -> Result<bool, M::Error>,
    X: FnOnce(Machineless<'_, E>) -> Result<Caller<M>, M::Error>,
{
    let req = Request {
        store_path: store_path.to_vec(),
        policy,
        cwd: cwd.map(<[u32]>::to_vec),
        session: session.map(<[u32]>::to_vec),
        identity: None,
        budget: FETCH_BUDGET,
    };
    let c = caller(process, &req, None, hands)?;
    process
        .stands
        .keep_stand(store_path, session, &c.location, &c.resolution);
    Ok(c.resolution)
}
