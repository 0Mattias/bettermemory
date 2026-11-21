//! A caller's resolution (`location.py` 1420-1997), its first unit: the
//! per-process state around a resolution. The machine opened once and the
//! stores it was recorded in (`process_machine`, `_recorded`,
//! `_clear_machine`, 1439-1478), the per-location locks (`_Held`,
//! `_LOCKS`, `_location_lock`, 1481-1519), `_cwd_of` (1522-1528), where
//! each harness session stands (`_Stand`, `_STANDS`, `_keep_stand`,
//! `_stand_of`, `forget_session`, `_clear_stands`, 1598-1654),
//! `_with_file_errors` (1803-1815) and `_same_namespace` (1879-1890).
//!
//! The reference keeps each table in a module global behind a
//! `threading.Lock`; here each is a value the caller owns
//! ([`ProcessMachine`], [`Locks`], [`Stands`]) behind a [`Mutex`], kept in
//! insertion order as a `dict` keeps it. Every read of the machine is an
//! argument: `open_machine`, `Membership.record_machine`, `os.getcwd`,
//! `object_key` and `_hostname` are closures the caller hands in.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use crate::capture::location::Location;
use crate::capture::Key;
use crate::machine::MachineIdentity;
use crate::printable::printable;
use crate::pyvalue::PyStr;
use crate::store::caller::Resolution;

/// `location_git.CACHE_CAP` (`location_git.py` 148). The fold of the
/// lane that builds `location_cache` replaces it with
/// `location_cache::CACHE_CAP`.
const CACHE_CAP: usize = 256;

/// `STANDS_CAP` (`location.py` 1614): the most sessions whose stand the
/// process keeps; the oldest goes first.
pub const STANDS_CAP: usize = CACHE_CAP * 4;

/// The most locks `_location_lock` keeps before it evicts the unused ones
/// (`CACHE_CAP * 4`, `location.py` 1505).
pub const LOCKS_CAP: usize = CACHE_CAP * 4;

fn locked<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// `_MACHINE` and `_RECORDED` under `_MACHINE_LOCK`.
#[derive(Debug, Default)]
pub struct ProcessMachine {
    machine: Mutex<Option<MachineIdentity>>,
    recorded: Mutex<HashSet<(PyStr, PyStr)>>,
}

impl ProcessMachine {
    pub fn new() -> ProcessMachine {
        ProcessMachine::default()
    }

    /// `process_machine()`: the machine `open` opens, once; an error from
    /// `open` keeps nothing, so the next call opens again.
    pub fn process_machine<E>(
        &self,
        open: impl FnOnce() -> Result<MachineIdentity, E>,
    ) -> Result<MachineIdentity, E> {
        let mut held = locked(&self.machine);
        if held.is_none() {
            *held = Some(open()?);
        }
        Ok(held.clone().expect("opened above"))
    }

    /// `_recorded(store, model, identity)`: `record` (the model's
    /// `record_machine`) unless this store path and machine id were
    /// recorded before; the pair is kept only after `record` returns. The
    /// check and the keeping are two holds of the lock, as in the
    /// reference, so two threads may both record.
    pub fn recorded<E>(
        &self,
        store_path: &[u32],
        identity: &MachineIdentity,
        record: impl FnOnce(&MachineIdentity) -> Result<(), E>,
    ) -> Result<(), E> {
        let key = (store_path.to_vec(), identity.id.clone());
        if locked(&self.recorded).contains(&key) {
            return Ok(());
        }
        record(identity)?;
        locked(&self.recorded).insert(key);
        Ok(())
    }

    /// `_clear_machine()`.
    pub fn clear(&self) {
        let mut held = locked(&self.machine);
        *held = None;
        locked(&self.recorded).clear();
    }
}

/// `_Held`: a location's lock and the number of threads that hold it or
/// wait for it (read and written under the table's guard).
#[derive(Debug, Default)]
pub struct Held {
    lock: Mutex<()>,
    users: Mutex<usize>,
}

/// `_LOCKS` under `_LOCKS_GUARD`, in insertion order.
#[derive(Debug, Default)]
pub struct Locks {
    table: Mutex<Vec<(PyStr, Arc<Held>)>>,
}

/// A lock taken by [`Locks::take`] and not yet given back.
#[derive(Debug)]
pub struct Taken(Arc<Held>);

fn push_ascii(out: &mut PyStr, text: &str) {
    out.extend(text.bytes().map(u32::from));
}

/// The name of `loc`'s lock: `machine:device:inode` where it has an
/// object key, else `machine:` and its root, or its cwd where the root is
/// None or empty (`loc.root or loc.cwd`).
pub fn lock_name(loc: &Location) -> PyStr {
    let mut name = loc.machine.clone();
    push_ascii(&mut name, ":");
    match &loc.object_key {
        Some(key) => {
            name.extend_from_slice(&key.device);
            push_ascii(&mut name, ":");
            name.extend_from_slice(&key.inode);
        }
        None => match &loc.root {
            Some(root) if !root.is_empty() => name.extend_from_slice(root),
            _ => name.extend_from_slice(&loc.cwd),
        },
    }
    name
}

impl Locks {
    pub fn new() -> Locks {
        Locks::default()
    }

    /// The first half of `_location_lock` under the guard: the lock named
    /// `name`, made where there is none (first dropping, where the table
    /// holds [`LOCKS_CAP`] or more, every lock no thread uses), its users
    /// counted up. The lock is not acquired.
    pub fn take(&self, name: &[u32]) -> Taken {
        let mut table = locked(&self.table);
        let found = table
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, h)| h.clone());
        let held = match found {
            Some(held) => held,
            None => {
                if table.len() >= LOCKS_CAP {
                    table.retain(|(_, h)| *locked(&h.users) != 0);
                }
                let held = Arc::new(Held::default());
                table.push((name.to_vec(), held.clone()));
                held
            }
        };
        *locked(&held.users) += 1;
        Taken(held)
    }

    /// The `finally` of `_location_lock`: the users counted down.
    pub fn give_back(&self, taken: Taken) {
        let _table = locked(&self.table);
        let mut users = locked(&taken.0.users);
        *users = users.saturating_sub(1);
    }

    /// `with _location_lock(loc): f()`: `f` run holding `loc`'s lock; the
    /// users are counted down however `f` ends.
    pub fn with_location_lock<R>(&self, loc: &Location, f: impl FnOnce() -> R) -> R {
        struct Back<'a>(&'a Locks, Option<Taken>);
        impl Drop for Back<'_> {
            fn drop(&mut self) {
                if let Some(taken) = self.1.take() {
                    self.0.give_back(taken);
                }
            }
        }
        let taken = self.take(&lock_name(loc));
        let held = taken.0.clone();
        let _back = Back(self, Some(taken));
        let _guard = locked(&held.lock);
        f()
    }

    /// The table: each lock's name and users, in insertion order.
    pub fn snapshot(&self) -> Vec<(PyStr, usize)> {
        locked(&self.table)
            .iter()
            .map(|(k, h)| (k.clone(), *locked(&h.users)))
            .collect()
    }
}

/// `_cwd_of(cwd)`: `cwd` where given, else what `getcwd` answers (None
/// where `os.getcwd` raises).
pub fn cwd_of<T>(cwd: Option<T>, getcwd: impl FnOnce() -> Option<T>) -> Option<T> {
    match cwd {
        Some(cwd) => Some(cwd),
        None => getcwd(),
    }
}

/// `_Stand`: a session's latest hook resolution, the machine and path it
/// was resolved under.
#[derive(Clone, Debug, PartialEq)]
pub struct Stand {
    pub machine: PyStr,
    pub path: PyStr,
    pub resolution: Resolution,
}

/// `_STANDS` under `_STANDS_LOCK`, keyed by store path and session, in
/// insertion order: a map from key to its sequence number and stand, and
/// the keys by sequence number, so each operation takes the time of a
/// hash lookup and a tree step rather than a scan of the table.
#[derive(Debug, Default)]
pub struct Stands {
    table: Mutex<StandTable>,
}

#[derive(Debug, Default)]
struct StandTable {
    next: u64,
    by_key: HashMap<(PyStr, PyStr), (u64, Stand)>,
    order: BTreeMap<u64, (PyStr, PyStr)>,
}

impl StandTable {
    fn drop_key(&mut self, key: &(PyStr, PyStr)) {
        if let Some((seq, _)) = self.by_key.remove(key) {
            self.order.remove(&seq);
        }
    }
}

impl Stands {
    pub fn new() -> Stands {
        Stands::default()
    }

    /// `_keep_stand(store, session, loc, res)`: nothing without a session;
    /// else the session's stand in this store dropped, and kept anew at
    /// the end where `res` has a checkout, the oldest dropped first while
    /// the table holds [`STANDS_CAP`] or more.
    pub fn keep_stand(
        &self,
        store_path: &[u32],
        session: Option<&[u32]>,
        loc: &Location,
        res: &Resolution,
    ) {
        let Some(session) = session else { return };
        let key = (store_path.to_vec(), session.to_vec());
        let mut table = locked(&self.table);
        table.drop_key(&key);
        if res.checkout.is_none() {
            return;
        }
        while table.by_key.len() >= STANDS_CAP {
            let Some((_, oldest)) = table.order.pop_first() else {
                break;
            };
            table.by_key.remove(&oldest);
        }
        let stand = Stand {
            machine: loc.machine.clone(),
            path: loc.cwd.clone(),
            resolution: res.clone(),
        };
        let seq = table.next;
        table.next += 1;
        table.order.insert(seq, key.clone());
        table.by_key.insert(key, (seq, stand));
    }

    /// `_stand_of(store, session)`.
    pub fn stand_of(&self, store_path: &[u32], session: Option<&[u32]>) -> Option<Stand> {
        let session = session?;
        let key = (store_path.to_vec(), session.to_vec());
        locked(&self.table)
            .by_key
            .get(&key)
            .map(|(_, stand)| stand.clone())
    }

    /// `forget_session(session)`: its stand in every store dropped.
    pub fn forget_session(&self, session: &[u32]) {
        let mut table = locked(&self.table);
        let gone: Vec<(PyStr, PyStr)> = table
            .by_key
            .keys()
            .filter(|k| k.1 == session)
            .cloned()
            .collect();
        for key in &gone {
            table.drop_key(key);
        }
    }

    /// `_clear_stands()`.
    pub fn clear(&self) {
        let mut table = locked(&self.table);
        table.by_key.clear();
        table.order.clear();
    }

    /// The table's keys, in insertion order.
    pub fn keys(&self) -> Vec<(PyStr, PyStr)> {
        locked(&self.table).order.values().cloned().collect()
    }
}

/// `_with_file_errors(res, errors)`: `res` with each error written through
/// `printable` after its lines, each one its lines did not already hold.
/// Errors are not compared with each other: two equal errors both go in.
pub fn with_file_errors(res: &Resolution, errors: &[PyStr]) -> Resolution {
    let mut out = res.clone();
    if errors.is_empty() {
        return out;
    }
    for shown in errors.iter().map(|e| printable(e)) {
        if !res.lines.contains(&shown) {
            out.lines.push(shown);
        }
    }
    out
}

/// `_same_namespace(cap_cwd, key, hostname)`: whether the object `here`
/// answers (`object_key(cap_cwd)`, asked only where `key` is given) is
/// the client's by device and inode, and by birth where both have one,
/// on the host the client named (`local`: `_hostname()`, asked only
/// where `hostname` is given). Names are bytes as `os.fsencode` spells
/// them.
pub fn same_namespace(
    key: Option<&Key>,
    here: impl FnOnce() -> Option<Key>,
    hostname: Option<&[u8]>,
    local: impl FnOnce() -> Option<Vec<u8>>,
) -> bool {
    let Some(key) = key else { return false };
    let Some(here) = here() else { return false };
    if here.0 != key.0 || here.1 != key.1 {
        return false;
    }
    if let (Some(a), Some(b)) = (&here.2, &key.2) {
        if a != b {
            return false;
        }
    }
    match hostname {
        None => true,
        Some(name) => local().as_deref() == Some(name),
    }
}
