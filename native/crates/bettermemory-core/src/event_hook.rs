//! `events.py`'s suppression switch and a hook's deadline (lines
//! 538-653): `suppressed`, `deadline`, `_NOT_DONE`, `StoreBusy`,
//! `_store_busy`, `within_hook_budget` and `hook_body`, with the SQLite
//! busy test other modules share (`primary_code`, `is_busy`).
//!
//! The reference keeps the two values in `ContextVar`s (`_SUPPRESSED`,
//! `_DEADLINE`). Here one owned [`HookScope`] holds both and the code that
//! reads them receives the scope by reference: there is no global. Each
//! scope sets its value for the body and gives back the one it found on
//! every return, as `ContextVar.reset` does in the reference's `finally`.
//! A panic inside a body is not caught, so a scope that a panic crossed is
//! not reused. The clock is an argument and the store's busy timeout is the
//! trait [`BusyTimeout`].

use crate::daemon_client::hook_budget_seconds;

/// `sqlite3.SQLITE_BUSY`.
pub const SQLITE_BUSY: i64 = 5;
/// `sqlite3.SQLITE_LOCKED`.
pub const SQLITE_LOCKED: i64 = 6;

/// `_NOT_DONE`: what a hook does not do when its state row cannot land,
/// by hook event.
pub const NOT_DONE: [(&str, &str); 3] = [
    ("session-start", "the brief was not shown"),
    ("prompt", "the recall was not shown"),
    ("stop", "the turn was not audited"),
];

/// `_NOT_DONE.get(event, 'nothing was shown')`.
pub fn not_done(event: &str) -> &'static str {
    NOT_DONE
        .iter()
        .find(|(name, _)| *name == event)
        .map_or("nothing was shown", |(_, text)| text)
}

/// The text of the `StoreBusy` that `within_hook_budget` raises for
/// `event`.
pub fn store_busy_message(event: &str) -> String {
    format!(
        "the store was busy, so {}: another writer held it past this hook's \
         time; run `bettermemory doctor`",
        not_done(event)
    )
}

/// `code & 0xFF`: an SQLite result code's primary code. The sign is kept
/// as Python's two's-complement `&` keeps it (`-1 & 0xFF` is 255).
pub fn primary_code(code: i64) -> i64 {
    code & 0xFF
}

/// `_store_busy`: an exception of class `class` (its type's name) whose
/// `sqlite_errorcode` is `code` (None where it is absent or not an `int`;
/// a `bool` is its `int`) is `sqlite3.OperationalError` with the primary
/// code BUSY or LOCKED. No class derives from `OperationalError` in the
/// standard library, so the name decides `isinstance`.
pub fn is_busy(class: &str, code: Option<i64>) -> bool {
    class == "OperationalError"
        && code.is_some_and(|c| matches!(primary_code(c), SQLITE_BUSY | SQLITE_LOCKED))
}

/// The store's side of `within_hook_budget`: `Store.busy_timeout` and
/// `Store.set_busy_timeout`, in seconds. `E` is the body's error type:
/// `set_busy_timeout` fails as the reference's does (`ProgrammingError`
/// on a closed connection, `ValueError` on a timeout that is not finite
/// or is negative).
pub trait BusyTimeout<E> {
    /// `store.busy_timeout`; None where the store holds none.
    fn busy_timeout(&self) -> Option<f64>;
    /// `store.set_busy_timeout(seconds)`.
    fn set_busy_timeout(&mut self, seconds: f64) -> Result<(), E>;
}

/// What `within_hook_budget` asks of an error its body raised: the
/// exception's class name and its `sqlite_errorcode`.
pub trait Raised {
    /// `type(exc).__name__`.
    fn class_name(&self) -> &str;
    /// `getattr(exc, "sqlite_errorcode", None)` when it is an `int`.
    fn sqlite_code(&self) -> Option<i64>;
}

/// How a hook body under `within_hook_budget` failed.
#[derive(Debug, Clone, PartialEq)]
pub enum HookFailure<E> {
    /// `StoreBusy(message)` raised from `cause`, a busy store's error.
    StoreBusy { message: String, cause: E },
    /// Any other error, raised again unchanged.
    Raised(E),
}

/// The calling context's `_SUPPRESSED` and `_DEADLINE`.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct HookScope {
    suppressed: bool,
    deadline: Option<f64>,
}

impl HookScope {
    /// Outside any hook body and any dry run: the two defaults.
    pub fn new() -> Self {
        Self::default()
    }

    /// `deadline()`: the clock reading by which the current hook body's
    /// rows are tried, or None outside one.
    pub fn deadline(&self) -> Option<f64> {
        self.deadline
    }

    /// `_SUPPRESSED.get()`.
    pub fn is_suppressed(&self) -> bool {
        self.suppressed
    }

    /// `with suppressed(): body`: no row of any class is recorded inside;
    /// the flag found is given back after.
    pub fn suppressed<R>(&mut self, body: impl FnOnce(&mut Self) -> R) -> R {
        let held = self.suppressed;
        self.suppressed = true;
        let answer = body(self);
        self.suppressed = held;
        answer
    }

    /// `with within_hook_budget(event, store): body`. The deadline is one
    /// clock reading plus the event's budget, or the deadline already set
    /// when that is earlier (`min(held, at)`: `at` only when it is less).
    /// A store whose busy timeout exceeds the budget waits the budget
    /// inside and gets its timeout back after. A body's error that
    /// `is_busy` names becomes [`HookFailure::StoreBusy`]; any other is
    /// raised again.
    ///
    /// `set_busy_timeout` failing is answered as the reference answers it.
    /// The lowering call runs before the `try`: its error is raised as it
    /// is and the new deadline stays set (the reference never resets
    /// that token). The restoring call runs in `finally` after the
    /// deadline is given back: its error replaces the body's outcome,
    /// value or error, and the timeout stays at the budget.
    pub fn within_hook_budget<R, E: Raised>(
        &mut self,
        event: &str,
        mut store: Option<&mut dyn BusyTimeout<E>>,
        monotonic: &mut dyn FnMut() -> f64,
        body: impl FnOnce(&mut Self, Option<&mut dyn BusyTimeout<E>>) -> Result<R, E>,
    ) -> Result<R, HookFailure<E>> {
        let budget = hook_budget_seconds(event);
        let at = monotonic() + budget;
        let found = self.deadline;
        self.deadline = Some(match found {
            None => at,
            Some(held) if at < held => at,
            Some(held) => held,
        });
        let waited = store.as_deref().and_then(|s| s.busy_timeout());
        let lowered = waited.filter(|w| *w > budget);
        if let (Some(s), Some(_)) = (store.as_mut(), lowered) {
            s.set_busy_timeout(budget).map_err(HookFailure::Raised)?;
        }
        let lent: Option<&mut dyn BusyTimeout<E>> = match store {
            Some(ref mut s) => Some(&mut **s),
            None => None,
        };
        let answer = body(self, lent);
        self.deadline = found;
        if let (Some(s), Some(w)) = (store, lowered) {
            s.set_busy_timeout(w).map_err(HookFailure::Raised)?;
        }
        answer.map_err(|exc| {
            if is_busy(exc.class_name(), exc.sqlite_code()) {
                HookFailure::StoreBusy {
                    message: store_busy_message(event),
                    cause: exc,
                }
            } else {
                HookFailure::Raised(exc)
            }
        })
    }

    /// `hook_body(event)` around a body called with `store=`: the store
    /// counts only where it has `set_busy_timeout`, which the caller
    /// settles by passing it or None; the body then runs under
    /// `within_hook_budget`.
    pub fn hook_body<R, E: Raised>(
        &mut self,
        event: &str,
        store: Option<&mut dyn BusyTimeout<E>>,
        monotonic: &mut dyn FnMut() -> f64,
        body: impl FnOnce(&mut Self, Option<&mut dyn BusyTimeout<E>>) -> Result<R, E>,
    ) -> Result<R, HookFailure<E>> {
        self.within_hook_budget(event, store, monotonic, body)
    }
}
