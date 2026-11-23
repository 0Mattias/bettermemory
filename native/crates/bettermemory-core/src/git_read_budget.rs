//! `git_read_budget.py` whole: one shared deadline for the history reads
//! of a call.
//!
//! The reference keeps the owner, the active budget and the root in
//! context variables. Here they are a [`Scope`] the caller holds and
//! threads through; each function sets and resets them as a stack, in the
//! reference's order. `_PROGRESS` is a [`Progresses`] the caller holds,
//! keyed by a store's identity and the qualified name, its lock a mutex.
//! The clock is an argument (`_clock`, `time.monotonic`), read wherever
//! the reference reads it. A budget is shared as `Rc<RefCell<_>>`, as the
//! context variables share one object; the progress it writes back is
//! shared across threads.

use std::cell::RefCell;
use std::collections::HashMap;
use std::hash::Hash;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

pub use crate::gitsystem::ask::READ_SECONDS;
use crate::origin_history::HistoryUnread;
use crate::pyvalue::PyStr;

/// A root and whether it is a home root (`tuple[str, bool]`).
pub type Root = (PyStr, bool);

/// `ReadDeadline`: this call has no Git-read time left.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReadDeadline;

/// `_Progress`: the resume point one call leaves for the next.
pub type Progress = Arc<Mutex<Option<Root>>>;

/// `ReadBudget`.
#[derive(Clone, Debug, Default)]
pub struct ReadBudget {
    pub progress: Option<Progress>,
    pub resume: Option<Root>,
    pub deadline: Option<f64>,
    pub first_home: Option<PyStr>,
    pub last_root: Option<Root>,
    pub deferred: Option<Root>,
}

/// A budget as the context variables share it.
pub type Shared = Rc<RefCell<ReadBudget>>;

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl ReadBudget {
    /// `ReadBudget.allowance`.
    pub fn allowance(
        &mut self,
        clock: &dyn Fn() -> f64,
        requested: f64,
        root: &[u32],
        home: bool,
    ) -> Result<(f64, bool), ReadDeadline> {
        let now = clock();
        let deadline = *self.deadline.get_or_insert(now + READ_SECONDS);
        let remaining = deadline - now;
        self.last_root = Some((root.to_vec(), home));
        if remaining <= 0.0 {
            return Err(self.exhausted());
        }
        if home && self.first_home.is_none() {
            self.first_home = Some(root.to_vec());
        }
        // `min(requested, remaining)`: `requested` unless `remaining` is less.
        let given = if remaining < requested {
            remaining
        } else {
            requested
        };
        Ok((given, remaining < requested))
    }

    /// `ReadBudget.exhausted`: it always raises; the answer is the error.
    pub fn exhausted(&mut self) -> ReadDeadline {
        if self.deferred.is_none() {
            if let Some((root, true)) = &self.last_root {
                let first = self.first_home.as_ref() == Some(root);
                self.deferred = Some((root.clone(), first));
            }
        }
        ReadDeadline
    }

    /// `ReadBudget.finish`.
    pub fn finish(&self) {
        if let Some(progress) = &self.progress {
            *lock(progress) = self.deferred.clone();
        }
    }
}

/// `_OWNER`, `_ACTIVE` and `_ROOT`, as the caller holds them.
#[derive(Clone, Debug, Default)]
pub struct Scope {
    pub owner: Option<Shared>,
    pub active: Option<Shared>,
    pub root: Option<Root>,
}

impl Scope {
    /// `_ACTIVE.get() or _OWNER.get()`.
    fn current(&self) -> Option<Shared> {
        self.active.clone().or_else(|| self.owner.clone())
    }
}

/// A store as `_progress` keys it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Store<K> {
    /// None: no progress.
    Absent,
    /// A store `WeakKeyDictionary` refuses (unhashable, or no weak
    /// reference to it): the TypeError branch, no progress.
    Refused,
    /// A store by its identity (its hash and equality).
    Keyed(K),
}

/// `_PROGRESS` and `_LOCK`.
#[derive(Debug)]
pub struct Progresses<K> {
    entries: Mutex<HashMap<K, HashMap<String, Progress>>>,
}

impl<K: Hash + Eq + Clone> Default for Progresses<K> {
    fn default() -> Self {
        Progresses {
            entries: Mutex::new(HashMap::new()),
        }
    }
}

impl<K: Hash + Eq + Clone> Progresses<K> {
    /// `_progress(store, name)`.
    pub fn progress(&self, store: &Store<K>, name: &str) -> Option<Progress> {
        let Store::Keyed(key) = store else {
            return None;
        };
        let mut entries = lock(&self.entries);
        let names = entries.entry(key.clone()).or_default();
        Some(names.entry(name.to_owned()).or_default().clone())
    }

    /// The resume the entry of `store` and `name` holds, without making
    /// one: None where there is no entry.
    pub fn peek(&self, store: &Store<K>, name: &str) -> Option<Option<Root>> {
        let Store::Keyed(key) = store else {
            return None;
        };
        let entries = lock(&self.entries);
        let progress = entries.get(key)?.get(name)?;
        let resume = lock(progress).clone();
        Some(resume)
    }
}

/// `call_scope` owns only the owner binding. Finish the original budget
/// after removing that binding, including when the caller unwinds.
struct CallOwner<'a> {
    scope: &'a mut Scope,
    previous: Option<Shared>,
    budget: Shared,
}

impl Drop for CallOwner<'_> {
    fn drop(&mut self) {
        self.scope.owner = self.previous.take();
        self.budget.borrow().finish();
    }
}

/// `call_scope(store, name)` around `body`.
pub fn call_scope<K: Hash + Eq + Clone, R>(
    scope: &mut Scope,
    progresses: &Progresses<K>,
    store: &Store<K>,
    name: &str,
    body: impl FnOnce(&mut Scope, &Shared) -> R,
) -> R {
    if let Some(existing) = scope.owner.clone() {
        return body(scope, &existing);
    }
    let progress = progresses.progress(store, name);
    let resume = progress.as_ref().and_then(|p| lock(p).clone());
    let budget: Shared = Rc::new(RefCell::new(ReadBudget {
        progress,
        resume,
        ..ReadBudget::default()
    }));
    let previous = scope.owner.replace(budget.clone());
    let owner = CallOwner {
        scope,
        previous,
        budget,
    };
    body(owner.scope, &owner.budget)
}

/// `call` (and `async_call`): the scope of `module.qualname` on the first
/// argument's store.
pub fn call<K: Hash + Eq + Clone, R>(
    scope: &mut Scope,
    progresses: &Progresses<K>,
    store: &Store<K>,
    qualified: &str,
    body: impl FnOnce(&mut Scope) -> R,
) -> R {
    call_scope(scope, progresses, store, qualified, |s, _| body(s))
}

/// `health_call`: `limit_none` where `estate_root_limit=None` was passed,
/// which opens no scope.
pub fn health_call<K: Hash + Eq + Clone, R>(
    scope: &mut Scope,
    progresses: &Progresses<K>,
    limit_none: bool,
    body: impl FnOnce(&mut Scope) -> R,
) -> R {
    if limit_none {
        return body(scope);
    }
    call_scope(scope, progresses, &Store::Absent, "", |s, _| body(s))
}

/// Restore only the bindings this reader installed. Changes inside the
/// shared budget, or to a binding the reader did not set, remain visible.
struct ReadBindings<'a> {
    scope: &'a mut Scope,
    previous_active: Option<Shared>,
    previous_root: Option<Option<Root>>,
}

impl Drop for ReadBindings<'_> {
    fn drop(&mut self) {
        if let Some(previous) = self.previous_root.take() {
            self.scope.root = previous;
        }
        self.scope.active = self.previous_active.take();
    }
}

fn with_read_bindings<R>(
    scope: &mut Scope,
    budget: Shared,
    root: Option<Root>,
    body: impl FnOnce(&mut Scope) -> R,
) -> R {
    let previous_active = scope.active.replace(budget);
    let previous_root = root.map(|value| scope.root.replace(value));
    let bindings = ReadBindings {
        scope,
        previous_active,
        previous_root,
    };
    body(bindings.scope)
}

/// `reading(root, home=home)` around `body`.
pub fn reading<R>(
    scope: &mut Scope,
    root: &[u32],
    home: bool,
    body: impl FnOnce(&mut Scope, &Shared) -> R,
) -> R {
    let budget = scope.current().unwrap_or_default();
    with_read_bindings(
        scope,
        budget.clone(),
        Some((root.to_vec(), home)),
        |scope| body(scope, &budget),
    )
}

/// `expired()`.
pub fn expired(scope: &Scope, clock: &dyn Fn() -> f64) -> bool {
    match scope.current() {
        None => false,
        Some(b) => {
            let deadline = b.borrow().deadline;
            match deadline {
                None => false,
                Some(d) => clock() >= d,
            }
        }
    }
}

/// The lock `wait_for` waits on (an `RLock`).
pub trait WaitLock {
    /// `acquire()` with no timeout, or `acquire(timeout=...)`.
    fn acquire(&mut self, timeout: Option<f64>) -> bool;
    fn release(&mut self);
}

/// `wait_for(lock)` around `body`, which is given whether it holds it.
pub fn wait_for<L: WaitLock + ?Sized, R>(
    scope: &Scope,
    clock: &dyn Fn() -> f64,
    lock: &mut L,
    body: impl FnOnce(bool) -> R,
) -> R {
    let acquired = match &scope.owner {
        None => lock.acquire(None),
        Some(budget) => {
            let now = clock();
            let deadline = *budget
                .borrow_mut()
                .deadline
                .get_or_insert(now + READ_SECONDS);
            let remaining = deadline - now;
            remaining > 0.0 && lock.acquire(Some(remaining))
        }
    };
    let out = body(acquired);
    if acquired {
        lock.release();
    }
    out
}

/// What a read raises: the deadline, or anything else.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Raise<E> {
    Deadline(ReadDeadline),
    Other(E),
}

/// `_read(fn, fallback)`: `first` is the first argument's `(str(cwd),
/// bool(at_home))` where it has both attributes.
pub fn read<R, E>(
    scope: &mut Scope,
    first: Option<Root>,
    body: impl FnOnce(&mut Scope) -> Result<R, Raise<E>>,
    fallback: impl FnOnce() -> R,
) -> Result<R, E> {
    let budget = scope.current().unwrap_or_default();
    with_read_bindings(scope, budget, first, |scope| match body(scope) {
        Ok(v) => Ok(v),
        Err(Raise::Deadline(_)) => Ok(fallback()),
        Err(Raise::Other(e)) => Err(e),
    })
}

/// `unread(fn)`.
pub fn unread<R: From<HistoryUnread>, E>(
    scope: &mut Scope,
    first: Option<Root>,
    body: impl FnOnce(&mut Scope) -> Result<R, Raise<E>>,
) -> Result<R, E> {
    read(scope, first, body, || R::from(HistoryUnread::CallBudget))
}

/// `hit(fn)`.
pub fn hit<R: From<HistoryUnread>, E>(
    scope: &mut Scope,
    first: Option<Root>,
    body: impl FnOnce(&mut Scope) -> Result<(bool, R), Raise<E>>,
) -> Result<(bool, R), E> {
    read(scope, first, body, || {
        (true, R::from(HistoryUnread::CallBudget))
    })
}

/// `allowance(timeout, cwd, git_dir)`: `cwd` is `str(cwd)` where given.
pub fn allowance(
    scope: &Scope,
    clock: &dyn Fn() -> f64,
    timeout: f64,
    cwd: Option<&[u32]>,
    git_dir: Option<&[u32]>,
) -> Result<(f64, bool), ReadDeadline> {
    let Some(budget) = &scope.active else {
        return Ok((timeout, false));
    };
    let (root, home) = match &scope.root {
        Some(r) => r.clone(),
        // `str(cwd if cwd is not None else git_dir)`: `str(None)` is `None`.
        None => (
            cwd.or(git_dir)
                .map_or_else(|| "None".chars().map(u32::from).collect(), <[u32]>::to_vec),
            true,
        ),
    };
    budget.borrow_mut().allowance(clock, timeout, &root, home)
}

/// What the module-level `exhausted()` raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exhausted {
    ReadDeadline,
    /// No active budget: `assert budget is not None`.
    AssertionError,
}

/// `exhausted()`: it always raises.
pub fn exhausted(scope: &Scope) -> Exhausted {
    match &scope.active {
        None => Exhausted::AssertionError,
        Some(b) => {
            b.borrow_mut().exhausted();
            Exhausted::ReadDeadline
        }
    }
}

/// `for_resume(rows, root)`.
pub fn for_resume<T: Clone>(
    scope: &Scope,
    rows: &[T],
    root: impl Fn(&T) -> Option<PyStr>,
) -> Vec<T> {
    let resume = scope.owner.as_ref().and_then(|b| b.borrow().resume.clone());
    let Some((wanted, after)) = resume else {
        return rows.to_vec();
    };
    let indexes: Vec<usize> = rows
        .iter()
        .enumerate()
        .filter(|(_, r)| root(r).as_ref() == Some(&wanted))
        .map(|(i, _)| i)
        .collect();
    let (Some(&first), Some(&last)) = (indexes.first(), indexes.last()) else {
        return rows.to_vec();
    };
    let start = if after { last + 1 } else { first };
    let mut out = rows[start..].to_vec();
    out.extend_from_slice(&rows[..start]);
    out
}
