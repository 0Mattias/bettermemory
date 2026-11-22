//! The membership model of one machine over one store (`membership.py`),
//! units membership-1 and membership-2: the state of `Membership`, its
//! store plumbing, its projects with their merges followed, the members
//! index, the witness and file-binding reads, the checkout reads, and
//! (`writes`) the evidence, boundary and checkout writes; (`touch`) the
//! sightings of a known checkout and its token re-mints.
//!
//! The model reaches the store only through [`MembershipStore`] and the
//! files a view names only through a [`Liveness`] it is given, so every
//! rule runs on a scripted store. The units that follow (membwrite,
//! membprop, membview) add further `impl<S: MembershipStore>
//! Membership<S>` blocks over the `pub(crate)` state and helpers here.

pub mod history;
pub mod machines;
pub mod projects;
pub mod proposals;
pub mod queries;
pub mod reads;
pub mod touch;
pub mod views;
pub mod vocab;
pub mod writes;

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::rc::Rc;

use crate::datetime::{DateError, DateTime};
use crate::machine::MachineIdentity;
use crate::membership_lines::Raised;
use crate::membership_records::records::{Checkout, LiveKey, Project, View};
use crate::membership_rows::jsonable;
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::unicode::{cps, to_string};

/// What one write of the store answers: its log row's sequence number.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Written {
    pub seq: i64,
}

/// The keywords of `Store.membership_rows`, in the reference's order:
/// `where` (columns equal to values, None matching null), `column_in`
/// (a column holding one of the values) and `order_by` (a leading `-`
/// sorts descending; empty orders by the key).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Query {
    pub where_: Option<Vec<(PyStr, PyValue)>>,
    pub column_in: Option<(PyStr, Vec<PyValue>)>,
    pub order_by: Vec<PyStr>,
}

/// The store as the model uses it (`store_membership.py`). Every method
/// takes `&self`: an implementation keeps its connection behind interior
/// mutability, as a SQLite connection does, so a transaction can be held
/// while the model calls back into it.
///
/// `batch` is atomic: when `f` returns an error, every put, delete and
/// membership row written inside it is rolled back, and the error is
/// returned. `Membership::call` relies on this: a call that fails leaves
/// no partial writes.
pub trait MembershipStore {
    /// The store's own failure.
    type Error;
    /// `Store.in_batch`: whether a batch is open.
    fn in_batch(&self) -> bool;
    /// `Store.batch()`: one transaction over `f`, rolled back on its error.
    fn batch<T>(
        &self,
        f: &mut dyn FnMut() -> Result<T, Error<Self::Error>>,
    ) -> Result<T, Error<Self::Error>>;
    /// `Store.put_membership(kind, row, session=session)`.
    fn put_membership(
        &self,
        kind: &[u32],
        row: &PyValue,
        session: Option<&[u32]>,
    ) -> Result<Written, Self::Error>;
    /// `Store.delete_membership(kind, key, session=session)`.
    fn delete_membership(
        &self,
        kind: &[u32],
        key: &PyValue,
        session: Option<&[u32]>,
    ) -> Result<Written, Self::Error>;
    /// `Store.record_membership(kind, payload, session=session)`.
    fn record_membership(
        &self,
        event_kind: &[u32],
        payload: &PyValue,
        session: Option<&[u32]>,
    ) -> Result<Written, Self::Error>;
    /// `Store.membership_rows(table, **query)`: each row a dict of its
    /// columns, the `_json` ones decoded.
    fn membership_rows(&self, table: &str, query: &Query) -> Result<Vec<PyValue>, Self::Error>;
    /// `Store.next_membership_id(table, prefix)`.
    fn next_membership_id(&self, table: &str, prefix: &str) -> Result<PyStr, Self::Error>;
    /// `Store.next_membership_number(table, column)`.
    fn next_membership_number(&self, table: &str, column: &str) -> Result<i64, Self::Error>;
}

/// What a model call fails with: the store's error, or what the
/// reference raises (its class and `str()` of it).
#[derive(Clone, Debug, PartialEq)]
pub enum Error<E> {
    Store(E),
    Raised(Raised),
}

pub type Result2<T, E> = Result<T, Error<E>>;

pub(crate) fn raise<T, E>(class: &'static str, message: String) -> Result2<T, E> {
    Err(Error::Raised(Raised { class, message }))
}

/// `KeyError(key)`: `str()` of it is the key's repr.
pub(crate) fn key_error<T, E>(key: &[u32]) -> Result2<T, E> {
    raise("KeyError", to_string(&repr_str(key)).unwrap_or_default())
}

/// `Liveness`: what a view's path holds now.
pub type Liveness = Box<dyn Fn(&Checkout, &View) -> PyStr>;
/// `path_liveness_many`: many views read at once.
pub type LivenessMany = Box<dyn Fn(&[(Checkout, View)]) -> Vec<Option<PyStr>>>;
/// `TokenWriter`: the token and status written, or None.
pub type TokenWriter = Box<dyn Fn(&Checkout) -> Option<(Option<PyStr>, PyStr)>>;
/// `now`: stamps rows; read by no rule.
pub type Now = Box<dyn Fn() -> DateTime>;

/// The liveness a model reads views with. `Default` is
/// `location.path_liveness` with its batch reader
/// (`path_liveness_many`), supplied by the location unit; `Given` is any
/// other, read one view at a time (`_PATH_LIVENESS`, 251, and 283-285).
pub enum LivenessSeam {
    Default { one: Liveness, many: LivenessMany },
    Given(Liveness),
}

/// The keywords of `Membership(...)` past the store and the machine.
pub struct Options {
    /// `liveness`: the location's own when [`LivenessSeam::Default`].
    pub liveness: LivenessSeam,
    pub token_writer: Option<TokenWriter>,
    pub now: Now,
    pub session: Option<PyStr>,
}

/// `_projects`: the project rows by id, in the order read (a dict's).
#[derive(Clone, Debug, Default)]
pub struct ProjectMap {
    pub(crate) order: Vec<PyStr>,
    pub(crate) by_id: HashMap<PyStr, Project>,
}

impl ProjectMap {
    pub fn get(&self, id: &[u32]) -> Option<&Project> {
        self.by_id.get(id)
    }
    /// `d[id] = p`: a new id at the end, a known one kept in place.
    pub fn insert(&mut self, id: PyStr, p: Project) {
        if self.by_id.insert(id.clone(), p).is_none() {
            self.order.push(id);
        }
    }
    pub fn values(&self) -> impl Iterator<Item = &Project> {
        self.order.iter().map(|id| &self.by_id[id])
    }
    pub fn ids(&self) -> &[PyStr] {
        &self.order
    }
}

/// `Membership`: the model of one machine over one store.
pub struct Membership<S: MembershipStore> {
    pub(crate) store: Rc<S>,
    pub machine: PyStr,
    pub(crate) liveness: Liveness,
    pub(crate) liveness_many: Option<LivenessMany>,
    pub(crate) token_writer: Option<TokenWriter>,
    pub(crate) now: Now,
    pub(crate) session: Option<PyStr>,
    projects: Option<ProjectMap>,
    /// Bumped each time `all_projects` reads the projects anew, as the
    /// reference binds a new `_projects` dict: the members index is valid
    /// while it was built at the same generation. Keyed to the read itself,
    /// so any reset of `projects` to `None` invalidates the index.
    generation: u64,
    pub(crate) index: Option<(u64, BTreeMap<PyStr, Vec<PyStr>>)>,
    pub(crate) rows: Vec<i64>,
    pub(crate) remint: Option<PyStr>,
    /// `_live`: liveness read in a `liveness_held` span, by what each read
    /// depends on; None outside one.
    pub(crate) live: Option<HashMap<LiveKey, PyStr>>,
    /// `_ancestry`: git's reading of tied witnesses, read before the write
    /// transaction by the resolution that wanted it; None where no
    /// resolution can read it outside its transaction.
    pub ancestry: Option<BTreeMap<Vec<PyStr>, Option<BTreeSet<PyStr>>>>,
}

impl<S: MembershipStore> Membership<S> {
    /// `Membership(store, machine, ...)` (268-303).
    pub fn new(store: S, machine: PyStr, options: Options) -> Self {
        let (liveness, liveness_many) = match options.liveness {
            LivenessSeam::Default { one, many } => (one, Some(many)),
            LivenessSeam::Given(one) => (one, None),
        };
        Membership {
            store: Rc::new(store),
            machine,
            liveness,
            liveness_many,
            token_writer: options.token_writer,
            now: options.now,
            session: options.session,
            projects: None,
            generation: 0,
            index: None,
            rows: Vec::new(),
            remint: None,
            live: None,
            ancestry: None,
        }
    }

    /// `Membership.open(store, identity, ...)` (311-337): the model, then
    /// its machine row through `record` (`record_machine`, lane membprop).
    pub fn open(
        store: S,
        identity: &MachineIdentity,
        options: Options,
        record: impl FnOnce(&mut Self, &MachineIdentity) -> Result2<(), S::Error>,
    ) -> Result2<Self, S::Error> {
        let mut model = Self::new(store, identity.id.clone(), options);
        record(&mut model, identity)?;
        Ok(model)
    }

    pub fn store(&self) -> &S {
        &self.store
    }

    /// The liveness the model was given.
    pub fn liveness(&self) -> &Liveness {
        &self.liveness
    }

    /// The token writer the model was given.
    pub fn token_writer(&self) -> Option<&TokenWriter> {
        self.token_writer.as_ref()
    }

    /// The sequence numbers of the rows the current call wrote.
    pub fn rows(&self) -> &[i64] {
        &self.rows
    }

    /// `reads_here` (304-310): the liveness is the location's own. The
    /// reference tests `liveness is _PATH_LIVENESS`, so passing the
    /// location's own liveness explicitly also counts; closures have no
    /// identity here, so only [`LivenessSeam::Default`] counts and a caller
    /// holding the location's liveness passes it as `Default`.
    pub fn reads_here(&self) -> bool {
        self.liveness_many.is_some()
    }

    /// `self._projects = None`: the next read of the projects reads the
    /// store anew and rebuilds the members index.
    pub fn forget_projects(&mut self) {
        self.projects = None;
    }

    pub(crate) fn generation(&self) -> u64 {
        self.generation
    }

    /// `_call()` (338-350): a fresh read of the projects, the rows
    /// collected anew, and one store transaction unless one is open.
    pub fn call<T>(
        &mut self,
        mut f: impl FnMut(&mut Self) -> Result2<T, S::Error>,
    ) -> Result2<T, S::Error> {
        self.forget_projects();
        self.rows = Vec::new();
        self.remint = None;
        let store = Rc::clone(&self.store);
        if store.in_batch() {
            return f(self);
        }
        let mut once = Some(&mut f);
        store.batch(&mut || match once.take() {
            Some(f) => f(self),
            None => unreachable!("a batch runs its body once"),
        })
    }

    /// `_stamp()` (351-353): `now()` in UTC, `+00:00` spelled `Z`. A
    /// naive `now` is read as local time by the reference; not ported.
    pub fn stamp(&self) -> Result2<PyStr, S::Error> {
        match (self.now)().to_utc() {
            Some(Ok(dt)) => Ok(cps(&dt.isoformat_utc())),
            Some(Err(DateError::OverflowError)) => {
                raise("OverflowError", "date value out of range".into())
            }
            Some(Err(DateError::ValueError)) => raise("ValueError", String::new()),
            None => raise("NotPorted", "a naive now is local time".into()),
        }
    }

    /// `_put(kind, row)` (354-357).
    pub fn put(&mut self, kind: &str, row: &PyValue) -> Result2<(), S::Error> {
        let w = self
            .store
            .put_membership(&cps(kind), row, self.session.as_deref())
            .map_err(Error::Store)?;
        self.rows.push(w.seq);
        Ok(())
    }

    /// `_delete(kind, key)` (358-361).
    pub fn delete(&mut self, kind: &str, key: &PyValue) -> Result2<(), S::Error> {
        let w = self
            .store
            .delete_membership(&cps(kind), key, self.session.as_deref())
            .map_err(Error::Store)?;
        self.rows.push(w.seq);
        Ok(())
    }

    /// `_event(event_kind, **payload)` (362-369): the payload through
    /// `_jsonable` first, so a payload it refuses writes nothing.
    pub fn event(
        &mut self,
        event_kind: &str,
        payload: Vec<(&str, PyValue)>,
    ) -> Result2<(), S::Error> {
        let dict = PyValue::Dict(
            payload
                .into_iter()
                .map(|(k, v)| (PyValue::str(k), v))
                .collect(),
        );
        let payload = jsonable(&dict).map_err(Error::Raised)?;
        let w = self
            .store
            .record_membership(&cps(event_kind), &payload, self.session.as_deref())
            .map_err(Error::Store)?;
        self.rows.push(w.seq);
        Ok(())
    }

    pub fn read(&self, table: &str, query: &Query) -> Result2<Vec<PyValue>, S::Error> {
        self.store
            .membership_rows(table, query)
            .map_err(Error::Store)
    }
}

/// A column of a row, KeyError with the key's repr when missing.
pub(crate) fn column<'a, E>(row: &'a PyValue, key: &str) -> Result2<&'a PyValue, E> {
    match row.get(key) {
        Some(v) => Ok(v),
        None => key_error(&cps(key)),
    }
}

/// `str(value)`.
pub(crate) fn str_of<E>(value: &PyValue) -> Result2<PyStr, E> {
    crate::pyvalue::py_str(value).map_err(|e| {
        Error::Raised(Raised {
            class: "RecursionError",
            message: format!("{e:?}"),
        })
    })
}
