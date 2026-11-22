//! A caller's resolution (`location.py` 1420-1997), unit lochook:
//! `resolve_hook` (1893-1979), a hook's caller resolved from what it
//! posted.
//!
//! Four ways out, in the reference's order: no location mapping (an rc3
//! client) is `_hooked` at the cwd; an indeterminate probe is the
//! unreadable location resolved by the process's model (the machineless
//! model where the machine cannot be opened or recorded); a client whose
//! object this process reads on the same host is `_hooked` at the posted
//! cwd; else the location token and the client's machine are asked for,
//! and resolved with the client answering through an [`Exchange`].
//!
//! Every collaborator is an argument: `_hooked` (unit loccaller's
//! [`crate::location_resolve::hooked`] with its hands) is a closure, as are
//! `os.getcwd`, `object_key`, `_hostname`, `open_machine` and the model's
//! two constructors. The exchanges kept between rounds, the monotonic
//! clock and the sixteen random bytes of a nonce are arguments too.

use crate::capture::location::{to_location, Location, ObjectKey};
use crate::capture::{object_key_of, Capture, Key, State};
use crate::location_answer::Exchange;
use crate::location_caller::{cwd_of, same_namespace, with_file_errors};
use crate::location_resolve::{CallerModel, Policy, Process};
use crate::location_witness::{py_eq, ExchangeReads, Exchanges, NeedWitnesses};
use crate::machine::{identity_from, truthy, MachineIdentity};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::repository_history::INDEPENDENT_MAX;
use crate::store::caller::Resolution;
use crate::unicode::cps;
use crate::witness_questions::{AnsweredLiveness, AnsweredToken};

/// `location_git.NO_MACHINE` (`location_git.py` 192): the machine of the
/// model made where this process's machine cannot be opened.
pub const NO_MACHINE: &str = "<no machine>";

/// The readers a hook's model is made with: the client's answers to the
/// liveness questions and its re-mint.
pub struct Answered<'p> {
    pub liveness: AnsweredLiveness,
    pub writer: AnsweredToken<'p>,
}

/// What `resolve_hook` asks of `membership.Membership` beyond
/// [`CallerModel`].
pub trait HookModel: CallerModel {
    /// `model.machine`: the machine id the model was made for.
    fn machine(&self) -> PyStr;
    /// Whether an error is an `OSError`, which the probe's `_model`
    /// catches.
    fn is_os_error(error: &Self::Error) -> bool;
    /// `model.checkouts(token=token)`: each match's machine and object key.
    fn checkouts_of(&mut self, token: &[u32]) -> Vec<(PyStr, Option<ObjectKey>)>;
    /// `model.resolve(loc, strict=strict, declared=reads)`; the inner
    /// `Err` is the `NeedWitnesses` it raises.
    fn resolve_declared(
        &mut self,
        loc: &Location,
        strict: bool,
        declared: &mut ExchangeReads,
    ) -> Result<Result<Resolution, NeedWitnesses>, Self::Error>;
}

/// What `resolve_hook` answers: a resolution, or the next question.
/// A resolution is the common answer, so it is held inline.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, PartialEq)]
pub enum Hooked {
    Resolved(Resolution),
    Asked(Exchange),
}

/// A raise that leaves `resolve_hook`: the model's, or one of Python's
/// own (`str()`, `int()`, a mapping spread).
#[derive(Clone, Debug, PartialEq)]
pub enum HookError<E> {
    Model(E),
    Raised(PyError),
}

impl<E> From<PyError> for HookError<E> {
    fn from(e: PyError) -> Self {
        HookError::Raised(e)
    }
}

/// The arguments of `resolve_hook` that are values.
#[derive(Clone, Copy, Debug)]
pub struct HookRequest<'p> {
    pub store_path: &'p [u32],
    pub policy: Policy,
    pub cwd: Option<&'p [u32]>,
    pub posted: Option<&'p PyValue>,
    pub session: Option<&'p [u32]>,
}

/// The collaborators of `resolve_hook`. `hooked` is `_hooked(store,
/// config, cwd, session)`; `new_model` is `Membership(store, identity.id,
/// liveness=..., token_writer=..., session=...)`; `no_machine` is
/// `Membership(store, NO_MACHINE, session=...)`.
pub struct HookHands<'x, G, K, H, J, O, N, F> {
    pub getcwd: G,
    pub object_key: K,
    pub hostname: H,
    pub hooked: J,
    pub open_machine: O,
    pub new_model: N,
    pub no_machine: F,
    pub exchanges: &'x mut Exchanges,
    pub now: f64,
    pub fresh: [u8; 16],
}

fn hook_model<'p, M: HookModel>(
    process: &Process,
    store_path: &[u32],
    identity: &MachineIdentity,
    session: Option<&[u32]>,
    answered: Option<Answered<'p>>,
    new: impl FnOnce(&MachineIdentity, Option<&[u32]>, Option<Answered<'p>>) -> Result<M, M::Error>,
) -> Result<M, M::Error> {
    let mut made = new(identity, session, answered)?;
    process
        .machine
        .recorded(store_path, identity, |id| made.record_machine(id))?;
    Ok(made)
}

/// A host name as `==` compares it: every code point, four bytes each.
fn host_bytes(name: &[u32]) -> Vec<u8> {
    name.iter().flat_map(|c| c.to_le_bytes()).collect()
}

/// `{**question, **carried, "write_token": policy}`: a key already held
/// keeps its place and takes the new value.
fn spread(into: &mut Vec<(PyValue, PyValue)>, key: &str, value: PyValue) {
    let k = PyValue::str(key);
    match into.iter_mut().find(|(have, _)| py_eq(have, &k)) {
        Some(slot) => slot.1 = value,
        None => into.push((k, value)),
    }
}

/// `resolve_hook(store, config, cwd, posted, session=session)`.
pub fn resolve_hook<'p, M, G, K, H, J, O, N, F>(
    process: &Process,
    req: HookRequest<'p>,
    hands: HookHands<'_, G, K, H, J, O, N, F>,
) -> Result<Hooked, HookError<M::Error>>
where
    M: HookModel,
    G: FnOnce() -> Option<PyStr>,
    K: FnOnce(&[u32]) -> Option<Key>,
    H: FnOnce() -> Option<PyStr>,
    J: FnOnce(Option<PyStr>) -> Result<Resolution, M::Error>,
    O: FnOnce() -> Result<MachineIdentity, M::Error>,
    N: FnOnce(&MachineIdentity, Option<&[u32]>, Option<Answered<'p>>) -> Result<M, M::Error>,
    F: FnOnce(Option<&[u32]>) -> Result<M, M::Error>,
{
    let HookHands {
        getcwd,
        object_key,
        hostname,
        hooked,
        open_machine,
        new_model,
        no_machine,
        exchanges,
        now,
        fresh,
    } = hands;
    let session = req.session;
    let none = PyValue::None;
    let raw = match req.posted.and_then(|p| p.get("location")) {
        Some(raw @ PyValue::Dict(_)) => raw,
        _ => {
            let cwd = cwd_of(req.cwd.map(<[u32]>::to_vec), getcwd);
            return hooked(cwd).map(Hooked::Resolved).map_err(HookError::Model);
        }
    };
    let probing = truthy(raw.get("probe").unwrap_or(&none));
    if matches!(raw.get("state"), Some(PyValue::Str(s)) if *s == cps("indeterminate")) && probing {
        let opened = process
            .machine
            .process_machine(open_machine)
            .and_then(|id| hook_model(process, req.store_path, &id, session, None, new_model));
        let mut model = match opened {
            Ok(m) => m,
            Err(e) if M::is_os_error(&e) => no_machine(session).map_err(HookError::Model)?,
            Err(e) => return Err(HookError::Model(e)),
        };
        let cwd = match raw.get("cwd") {
            Some(v) if truthy(v) => crate::capture::str_of(v, crate::capture::STR_DEPTH_TOP)?,
            _ => req.cwd.map(<[u32]>::to_vec).unwrap_or_default(),
        };
        let mut cap = Capture::new(cwd, State::Indeterminate);
        cap.detail = match raw.get("detail") {
            Some(PyValue::Str(d)) => d.clone(),
            _ => cps("cwd unreadable"),
        };
        let unread = to_location(&cap, &model.machine(), session);
        let res = model
            .resolve(&unread, req.policy.strict)
            .map_err(HookError::Model)?;
        process
            .stands
            .keep_stand(req.store_path, session, &unread, &res);
        return Ok(Hooked::Resolved(res));
    }
    let posted_key = match object_key_of(raw.get("cwd_object")) {
        Ok(k) => k,
        Err(PyError::ValueError) => None,
        Err(e) => return Err(e.into()),
    };
    if let Some(PyValue::Str(posted_cwd)) = raw.get("cwd") {
        let host = match raw.get("hostname") {
            Some(PyValue::Str(h)) => Some(host_bytes(h)),
            _ => None,
        };
        if !posted_cwd.is_empty()
            && same_namespace(
                posted_key.as_ref(),
                || object_key(posted_cwd),
                host.as_deref(),
                || hostname().map(|h| host_bytes(&h)),
            )
        {
            return hooked(Some(posted_cwd.clone()))
                .map(Hooked::Resolved)
                .map_err(HookError::Model);
        }
    }
    let policy = PyValue::Bool(req.policy.write_token);
    let cap = if probing {
        None
    } else {
        Capture::from_token(raw)?
    };
    let identity = identity_from(req.posted.and_then(|p| p.get("machine")).unwrap_or(&none))?;
    let (Some(cap), Some(identity)) = (cap, identity) else {
        let mut q = Vec::new();
        spread(&mut q, "location", PyValue::Bool(true));
        spread(&mut q, "machine", PyValue::Bool(true));
        spread(&mut q, "write_token", policy);
        return Ok(Hooked::Asked(Exchange {
            question: PyValue::Dict(q),
        }));
    };
    let given = req.posted.and_then(|p| p.get("answers"));
    static EMPTY: PyValue = PyValue::Dict(Vec::new());
    let (answers, posted): (&'p PyValue, bool) = match given {
        Some(g @ PyValue::Dict(_)) => (g, true),
        _ => (&EMPTY, false),
    };
    let liveness = AnsweredLiveness::new(answers)?;
    let answered = Answered {
        liveness,
        writer: AnsweredToken::new(answers, posted),
    };
    let mut model = hook_model(
        process,
        req.store_path,
        &identity,
        session,
        Some(answered),
        new_model,
    )
    .map_err(HookError::Model)?;
    let mut reads = ExchangeReads::new(
        answers,
        session.map(<[u32]>::to_vec),
        identity.id.clone(),
        exchanges,
        now,
        fresh,
        INDEPENDENT_MAX,
    )?;
    let writer = AnsweredToken::new(answers, posted);
    let loc = writer.located(&to_location(&cap, &identity.id, session), &mut |t| {
        model.checkouts_of(t)
    });
    let strict = req.policy.strict;
    let got = process
        .locks
        .with_location_lock(&loc, || model.resolve_declared(&loc, strict, &mut reads))
        .map_err(HookError::Model)?;
    let res = match got {
        Ok(res) => res,
        Err(need) => {
            let PyValue::Dict(q) = &need.question else {
                return Err(HookError::Raised(PyError::TypeError));
            };
            let mut q = q.clone();
            if let Some(nonce) = reads.carried(exchanges, now) {
                spread(&mut q, "exchange", PyValue::str(&nonce));
            }
            spread(&mut q, "write_token", policy);
            return Ok(Hooked::Asked(Exchange {
                question: PyValue::Dict(q),
            }));
        }
    };
    let res = with_file_errors(&res, &cap.file_errors);
    process
        .stands
        .keep_stand(req.store_path, session, &loc, &res);
    Ok(Hooked::Resolved(res))
}
