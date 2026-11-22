//! The refusal classes and the dependency bundle of `_handlers.py`
//! (unit handlers-2a): `RefusedError`, `_in_the_way`, `RetryableRefusal`,
//! `KeptWriteRefusal`, `store_refusal`, `_ABSENT`, `_kept_or_retryable`,
//! `_refusals_as_tool_errors`, `_read_only` and `ToolHandlers`'
//! `__init__`, `for_request`, `with_store` and `_run`.
//!
//! The store's error classification (`store.retryable_code`) is another
//! module's, so every function that needs it takes it as an argument
//! ([`Retryable`]). A tool body is a closure that answers a value or a
//! [`Failure`], the exception it raised; the bodies run synchronously,
//! which is what the reference's inline run is (they await nothing that
//! yields).

use std::any::Any;
use std::sync::Arc;

use crate::config::Config;
use crate::control_tokens::{defang_control_tokens, named_tokens};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::session::SessionSource;
use crate::store::schema::{absent_text, Absent};
use crate::unicode::cps;

/// `store.STORE_FULL`, the one code `_in_the_way` and the retryable
/// refusal's remedy single out.
pub const STORE_FULL: &str = "store_full";

/// `sqlite3.SQLITE_READONLY`.
pub const SQLITE_READONLY: i64 = 8;

/// A `sqlite3.OperationalError` a body raised: its `sqlite_errorcode`
/// when that is an `int` (None otherwise), wrapped to 64 bits two's
/// complement as `wrap_code` does, which keeps the low byte, the only
/// part either check reads; and its `str()`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SqliteFailure {
    pub code: Option<i64>,
    pub text: PyStr,
}

/// A Python `int` spelled in decimal (an optional `-`, then digits),
/// wrapped to 64 bits two's complement: the value mod 2**64. None when
/// the text is not such a spelling.
pub fn wrap_code(decimal: &str) -> Option<i64> {
    let (neg, digits) = match decimal.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, decimal),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let mag = digits.bytes().fold(0u64, |acc, b| {
        acc.wrapping_mul(10).wrapping_add(u64::from(b - b'0'))
    });
    let wrapped = if neg { mag.wrapping_neg() } else { mag };
    Some(wrapped as i64)
}

/// `store.retryable_code` over an operational error: the code a client
/// branches on, or None.
pub type Retryable<'a> = &'a dyn Fn(&SqliteFailure) -> Option<&'static str>;

/// Which `ToolError` class a refusal is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RefusalClass {
    /// The SDK's own `ToolError`, raised by a body as it is.
    ToolError,
    /// `RefusedError`.
    Refused,
    /// `RetryableRefusal`.
    Retryable,
    /// `KeptWriteRefusal`.
    KeptWrite,
}

impl RefusalClass {
    /// The class's name in the reference.
    pub fn name(self) -> &'static str {
        match self {
            RefusalClass::ToolError => "ToolError",
            RefusalClass::Refused => "RefusedError",
            RefusalClass::Retryable => "RetryableRefusal",
            RefusalClass::KeptWrite => "KeptWriteRefusal",
        }
    }

    /// The class attribute `retryable`, which only the two store refusals
    /// define.
    pub fn retryable(self) -> Option<bool> {
        match self {
            RefusalClass::Retryable => Some(true),
            RefusalClass::KeptWrite => Some(false),
            _ => None,
        }
    }
}

/// A `ToolError` with its text and, for the two store refusals, `code`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub class: RefusalClass,
    pub text: PyStr,
    pub code: Option<&'static str>,
}

/// What a tool body raised.
#[derive(Clone, Debug, PartialEq)]
pub enum Failure {
    /// A `ValueError` (not a `ToolError`) with its `str()`.
    Value(PyStr),
    /// One of `_ABSENT`, by kind, raised with these args.
    Absent(Absent, Vec<PyValue>),
    /// A `sqlite3.OperationalError`.
    Sqlite(SqliteFailure),
    /// A `ToolError`, the refusal classes among them.
    Tool(Refusal),
    /// An exception `_refusals_as_tool_errors` lets through, named.
    Other(PyStr),
    /// An error `absent_text` itself raised.
    Py(PyError),
}

/// `_in_the_way(code)`.
pub fn in_the_way(code: &str) -> &'static str {
    if code == STORE_FULL {
        return "the disk or the database is full, so the store could not take a write";
    }
    "another process or worker held the store's write lock longer than this call waits for it"
}

/// `RetryableRefusal(code, detail)`.
pub fn retryable_refusal(code: &'static str, detail: &[u32]) -> Refusal {
    let then = if code == STORE_FULL {
        "Free disk space, then retry the call."
    } else {
        "Retry the call."
    };
    let mut text = cps(&format!("{code} (retryable): {} (", in_the_way(code)));
    text.extend_from_slice(detail);
    text.extend(cps(&format!(
        "). The write this call was making was not kept. {then}"
    )));
    Refusal {
        class: RefusalClass::Retryable,
        text,
        code: Some(code),
    }
}

/// `KeptWriteRefusal(code, detail)`.
pub fn kept_write_refusal(code: &'static str, detail: &[u32]) -> Refusal {
    let mut text = cps(&format!(
        "{code} (not retryable): this call had committed its write to the store when a later step of it found that {} (",
        in_the_way(code)
    ));
    text.extend_from_slice(detail);
    text.extend(cps(
        "). What it committed stays. Do not retry the call, which would make the write again; read the store to see what it holds.",
    ));
    Refusal {
        class: RefusalClass::KeptWrite,
        text,
        code: Some(code),
    }
}

/// `store_refusal(exc)` for an operational error.
pub fn store_refusal(exc: &SqliteFailure, retryable: Retryable) -> Option<Refusal> {
    retryable(exc).map(|code| retryable_refusal(code, &exc.text))
}

/// `_read_only(exc)`: the code's low byte is `SQLITE_READONLY`. Python's
/// `&` on a negative int reads its two's complement, as `i64`'s does.
pub fn read_only(exc: &SqliteFailure) -> bool {
    exc.code.is_some_and(|c| c & 0xFF == SQLITE_READONLY)
}

/// The text `RefusedError` carries for a write to a store open for
/// reading only.
fn read_only_text(detail: &[u32]) -> PyStr {
    let mut text = cps("the store is open for reading only, so this call wrote nothing (");
    text.extend_from_slice(detail);
    text.extend(cps(
        "): a later bettermemory raised the schema revision a writer needs (meta's min_writer_revision), or the store's file cannot be written; upgrade bettermemory, or make the file writable",
    ));
    text
}

/// `_refusals_as_tool_errors(fn)` applied to one call: `call` is the
/// tool method's run. A `ToolError` passes as it is; a `ValueError` and
/// an `_ABSENT` error become a `RefusedError` with their text defanged;
/// an operational error is a `RetryableRefusal` when `retryable` names a
/// code, a `RefusedError` when it is a read-only refusal, else raised as
/// it is; anything else is raised as it is. The value is `named_tokens`
/// of what the call answered.
pub fn refusals_as_tool_errors(
    call: impl FnOnce() -> Result<PyValue, Failure>,
    retryable: Retryable,
) -> Result<PyValue, Failure> {
    let refused = |text: PyStr| {
        Failure::Tool(Refusal {
            class: RefusalClass::Refused,
            text,
            code: None,
        })
    };
    match call() {
        Ok(value) => Ok(named_tokens(&value)),
        Err(Failure::Value(text)) => Err(refused(defang_control_tokens(&text))),
        Err(Failure::Absent(kind, args)) => match absent_text(kind, &args) {
            Ok(text) => Err(refused(defang_control_tokens(&text))),
            Err(e) => Err(Failure::Py(e)),
        },
        Err(Failure::Sqlite(exc)) => {
            if let Some(refusal) = store_refusal(&exc, retryable) {
                return Err(Failure::Tool(refusal));
            }
            if read_only(&exc) {
                return Err(refused(read_only_text(&exc.text)));
            }
            Err(Failure::Sqlite(exc))
        }
        Err(other) => Err(other),
    }
}

/// What the bundle needs of its store: `Store.commits` and the identity
/// `resolved is self.store`.
pub trait BundleStore: Clone {
    fn commits(&self) -> u64;
    fn same_store(a: &Self, b: &Self) -> bool;
}

/// `dataclasses.replace(recorder, store=store)`.
pub trait RebindStore<S> {
    fn with_store(&self, store: S) -> Self;
}

/// `StoreSource`: the store a request runs against. The request's
/// context is opaque here; the forwarders settle its type.
pub trait StoreSource<S> {
    fn for_request(&self, ctx: Option<&dyn Any>) -> S;
}

/// `DefaultStoreSource(store)`: `store` for every request.
pub struct DefaultStoreSource<S>(pub S);

impl<S: Clone> StoreSource<S> for DefaultStoreSource<S> {
    fn for_request(&self, _ctx: Option<&dyn Any>) -> S {
        self.0.clone()
    }
}

/// A tool body over a bundle.
pub type Body<'a, S, R, B> = &'a dyn Fn(&ToolHandlers<S, R, B>) -> Result<PyValue, Failure>;

/// `Offload`: given a function of a worker's store, runs it on a worker
/// and answers with its result.
pub type Offload<S> =
    Arc<dyn Fn(&mut dyn FnMut(S) -> Result<PyValue, Failure>) -> Result<PyValue, Failure>>;

/// `_kept_or_retryable(make, deps)`: the body over `deps`, a busy, locked
/// or full store answered by whether the store's commits moved.
pub fn kept_or_retryable<S: BundleStore, R, B>(
    make: Body<S, R, B>,
    deps: &ToolHandlers<S, R, B>,
    retryable: Retryable,
) -> Result<PyValue, Failure> {
    let before = deps.store.commits();
    match make(deps) {
        Err(Failure::Sqlite(exc)) => {
            let Some(code) = retryable(&exc) else {
                return Err(Failure::Sqlite(exc));
            };
            if deps.store.commits() == before {
                Err(Failure::Tool(retryable_refusal(code, &exc.text)))
            } else {
                Err(Failure::Tool(kept_write_refusal(code, &exc.text)))
            }
        }
        other => other,
    }
}

/// `ToolHandlers`: the dependency bundle every handler takes.
pub struct ToolHandlers<S, R, B> {
    pub config: Config,
    pub store: S,
    pub offload: Option<Offload<S>>,
    pub sessions: Arc<dyn SessionSource>,
    pub recorder: R,
    pub responses: B,
    pub store_source: Arc<dyn StoreSource<S>>,
}

impl<S: Clone, R: Clone, B: Clone> Clone for ToolHandlers<S, R, B> {
    fn clone(&self) -> Self {
        ToolHandlers {
            config: self.config.clone(),
            store: self.store.clone(),
            offload: self.offload.clone(),
            sessions: Arc::clone(&self.sessions),
            recorder: self.recorder.clone(),
            responses: self.responses.clone(),
            store_source: Arc::clone(&self.store_source),
        }
    }
}

impl<S, R, B> ToolHandlers<S, R, B>
where
    S: BundleStore + 'static,
    R: Clone + RebindStore<S>,
    B: Clone,
{
    /// `ToolHandlers(...)`: `store_source` None is
    /// `DefaultStoreSource(store)`.
    pub fn new(
        config: Config,
        store: S,
        sessions: Arc<dyn SessionSource>,
        recorder: R,
        responses: B,
        store_source: Option<Arc<dyn StoreSource<S>>>,
        offload: Option<Offload<S>>,
    ) -> Self {
        let store_source =
            store_source.unwrap_or_else(|| Arc::new(DefaultStoreSource(store.clone())));
        ToolHandlers {
            config,
            store,
            offload,
            sessions,
            recorder,
            responses,
            store_source,
        }
    }

    /// `for_request(ctx)`: this bundle when the resolved store is its
    /// store, else a copy bound to the resolved store with no offload.
    /// The copy is shallow: the recorder is not rebound.
    pub fn for_request(&self, ctx: Option<&dyn Any>) -> std::borrow::Cow<'_, Self> {
        let resolved = self.store_source.for_request(ctx);
        if S::same_store(&resolved, &self.store) {
            return std::borrow::Cow::Borrowed(self);
        }
        let mut clone = self.clone();
        clone.store = resolved;
        clone.offload = None;
        std::borrow::Cow::Owned(clone)
    }

    /// `with_store(store)`: the store and the recorder rebound.
    pub fn with_store(&self, store: S) -> Self {
        let mut clone = self.clone();
        clone.recorder = self.recorder.with_store(store.clone());
        clone.store = store;
        clone
    }

    /// `_run(make)`: inline without an offload, else on a worker bound
    /// to the worker's store.
    pub fn run(&self, make: Body<S, R, B>, retryable: Retryable) -> Result<PyValue, Failure> {
        match &self.offload {
            None => kept_or_retryable(make, self, retryable),
            Some(offload) => {
                offload(&mut |store| kept_or_retryable(make, &self.with_store(store), retryable))
            }
        }
    }
}
