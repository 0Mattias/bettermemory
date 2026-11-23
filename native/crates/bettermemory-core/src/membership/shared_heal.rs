//! The heal half of `membership_shared.py` (ENG-215), unit 2a of the
//! module's cut: `_carriers` (one read of the checkouts' token index),
//! `_restamped`, `_healed`, `_chosen`, `retired_into` and `_folded`, as
//! methods of [`Membership`]. The pure rules they call are unit 1's
//! (`crate::membership_shared`); `_carriers` is passed to them here.
//!
//! `Store.membership_log` is not on [`MembershipStore`]: it is the
//! [`MembershipLog`] trait, which `_healed`, `_chosen`, `retired_into`
//! and `_folded` are bound on. A log row's payload that `json.loads`
//! refuses with ValueError is passed over; RecursionError propagates, as
//! the reference's `except ValueError` lets it.

use std::collections::{BTreeMap, BTreeSet};

use super::{key_error, Error, Membership, MembershipStore, Query, Result2};
use crate::capture::location::Location;
use crate::capture::Stamp;
use crate::checkout_token;
use crate::json;
use crate::log::LogRow;
use crate::membership_lines::Raised;
use crate::membership_records::records::{found_token, Checkout};
use crate::membership_rows::{checkout_from, root_file};
use crate::membership_shared::{self as ms, RowError, ASK_WHICH, HEAL, NAMING, RESTAMPED_BY};
use crate::pyvalue::{py_str, repr_str, PyError, PyStr, PyValue};
use crate::unicode::{cps, to_string};

/// `Store.membership_log(kinds, checkout=...)`: the log rows of `kinds`
/// (every membership kind where None), in log order; with `checkout`,
/// only those whose payload names it as its `checkout`.
pub trait MembershipLog: MembershipStore {
    fn membership_log(
        &self,
        kinds: Option<&[&str]>,
        checkout: Option<&[u32]>,
    ) -> Result<Vec<LogRow>, Self::Error>;
}

fn raised<T, E>(class: &'static str) -> Result2<T, E> {
    Err(Error::Raised(Raised {
        class,
        message: String::new(),
    }))
}

fn py<E>(e: PyError) -> Error<E> {
    Error::Raised(Raised {
        class: e.name(),
        message: String::new(),
    })
}

pub(crate) fn token_error<E>(e: &checkout_token::Raised) -> Error<E> {
    Error::Raised(Raised {
        class: e.class(),
        message: String::new(),
    })
}

fn row_error<T, E>(e: RowError) -> Result2<T, E> {
    match e {
        RowError::Key(k) => key_error(&cps(k)),
        RowError::Py(p) => Err(py(p)),
        RowError::AttributeError => raised("AttributeError"),
    }
}

/// Python's truth value of a JSON value.
fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) | PyValue::Path(s) => !s.is_empty(),
        PyValue::DateTime(_) => true,
        PyValue::Tuple(v) | PyValue::List(v) => !v.is_empty(),
        PyValue::Dict(v) => !v.is_empty(),
    }
}

/// `json.loads(row.payload)`: None where it raises ValueError.
fn payload<E>(row: &LogRow) -> Result2<Option<PyValue>, E> {
    match json::loads(&cps(&row.payload)) {
        Ok(p) => Ok(Some(p)),
        Err(PyError::ValueError) => Ok(None),
        Err(e) => Err(py(e)),
    }
}

/// `{tuple(s) for s in stamps}`.
fn stamp_set(stamps: &[Stamp]) -> BTreeSet<(&PyStr, &[String; 4])> {
    stamps.iter().map(|s| (&s.name, &s.numbers)).collect()
}

impl<S: MembershipStore> Membership<S> {
    /// `_carriers(self, tokens)` (329-336): the checkouts recording any
    /// of `tokens`, their views not read.
    pub fn carriers(&self, tokens: &[PyStr]) -> Result2<Vec<Checkout>, S::Error> {
        let wanted: BTreeSet<&PyStr> = tokens.iter().collect();
        let values = wanted
            .into_iter()
            .map(|t| PyValue::Str(t.clone()))
            .collect();
        let rows = self.read(
            "checkouts",
            &Query {
                column_in: Some((cps("token"), values)),
                ..Query::default()
            },
        )?;
        rows.iter()
            .map(|r| checkout_from(r, &[]).map_err(Error::Raised))
            .collect()
    }

    /// `_restamped(self, k, loc, how)` (372-393): `k` with the stamps
    /// and ticks read now, written where they differ.
    pub fn restamped(
        &mut self,
        k: Checkout,
        loc: &Location,
        how: &[u32],
    ) -> Result2<Checkout, S::Error> {
        if !RESTAMPED_BY.iter().any(|h| cps(h) == how) || loc.stamps.is_empty() {
            return Ok(k);
        }
        let same = stamp_set(&loc.stamps) == stamp_set(&k.stamps);
        if same && (loc.ticks.is_none() || loc.ticks == k.ticks) {
            return Ok(k);
        }
        let k = Checkout {
            stamps: loc.stamps.clone(),
            ticks: loc.ticks,
            ..k
        };
        self.put_checkout(k)
    }
}

impl<S: MembershipLog> Membership<S> {
    /// `_healed(self, kept, later, loc)` (396-447): `HEAL`, `ASK_WHICH`
    /// or the empty string.
    pub fn healed(
        &mut self,
        kept: &Checkout,
        later: &Checkout,
        loc: &Location,
    ) -> Result2<&'static str, S::Error> {
        let Some(found) = found_token(later, loc) else {
            return Ok("");
        };
        let Some(kept_token) = kept.token.as_ref().filter(|t| !t.is_empty()) else {
            return Ok("");
        };
        if later.token.as_ref() != Some(found) || kept.stamps.is_empty() {
            return Ok("");
        }
        if found == kept_token {
            return Ok("");
        }
        let lineage = kept.lineage();
        if checkout_token::succeeds(&PyValue::Str(found.clone()), lineage.as_ref())
            .map_err(|e| token_error(&e))?
        {
            return Ok("");
        }
        if !(ms::read_here(kept, loc) || ms::moved_under(kept, loc)) {
            return Ok("");
        }
        let carriers = self.carriers(&[kept_token.clone(), found.clone()])?;
        if !ms::continues(kept, loc, found, &carriers).map_err(|e| token_error(&e))? {
            return Ok("");
        }
        let mut projects = BTreeSet::new();
        for c in &carriers {
            if c.token.as_ref() == Some(found) && c.machine != kept.machine {
                projects.insert(self.canon(&c.project)?);
            }
        }
        let mine = self.canon(&later.project)?;
        if !projects.contains(&mine) || self.chosen(kept, later, loc)? {
            return Ok("");
        }
        if mine == self.canon(&kept.project)? {
            return Ok(HEAL);
        }
        let homes = self.read(
            "homes",
            &Query {
                where_: Some(vec![(cps("checkout"), PyValue::Str(later.id.clone()))]),
                ..Query::default()
            },
        )?;
        Ok(if homes.is_empty() { HEAL } else { ASK_WHICH })
    }

    /// `_chosen(self, kept, later, loc)` (450-463).
    pub fn chosen(
        &mut self,
        kept: &Checkout,
        later: &Checkout,
        loc: &Location,
    ) -> Result2<bool, S::Error> {
        let bound = match root_file(loc) {
            Some(rf) => {
                let id = rf.id.clone();
                self.file_binding_read(&id)?
            }
            None => None,
        };
        if let Some(bound) = bound {
            if self.canon(&bound)? != self.canon(&kept.project)? {
                return Ok(true);
            }
        }
        let rows = self.read(
            "proposals",
            &Query {
                where_: Some(vec![(cps("checkout"), PyValue::Str(later.id.clone()))]),
                ..Query::default()
            },
        )?;
        match ms::asked_where(&rows) {
            Ok(true) => return Ok(true),
            Ok(false) => {}
            Err(e) => return row_error(e),
        }
        let log = self
            .store()
            .membership_log(Some(&NAMING), Some(&later.id))
            .map_err(Error::Store)?;
        let silent = ms::joined_silently_by_token(&later.id, &log).map_err(py)?;
        Ok(!silent)
    }

    /// `retired_into(self, k)` (784-804): the `into` of `k`'s latest
    /// `checkout_retired` row that is a dict, None where `k` is not
    /// retired or that row names none.
    pub fn retired_into(&self, k: &Checkout) -> Result2<Option<PyStr>, S::Error> {
        if !k.retired {
            return Ok(None);
        }
        let mut into = None;
        let rows = self
            .store()
            .membership_log(Some(&["checkout_retired"]), Some(&k.id))
            .map_err(Error::Store)?;
        for row in &rows {
            let Some(p) = payload(row)? else { continue };
            if !matches!(p, PyValue::Dict(_)) {
                continue;
            }
            into = match p.get("into").filter(|v| truthy(v)) {
                Some(v) => Some(py_str(v).map_err(py)?).filter(|s| !s.is_empty()),
                None => None,
            };
        }
        Ok(into)
    }

    /// `_folded(self, checkout)` (807-825): the checkouts retired into
    /// `checkout`, and into those, and so on.
    pub fn folded(&self, checkout: &[u32]) -> Result2<BTreeSet<PyStr>, S::Error> {
        let mut into: BTreeMap<PyStr, BTreeSet<PyStr>> = BTreeMap::new();
        let rows = self
            .store()
            .membership_log(Some(&["checkout_retired"]), None)
            .map_err(Error::Store)?;
        for row in &rows {
            let Some(p) = payload(row)? else { continue };
            if !matches!(p, PyValue::Dict(_)) {
                continue;
            }
            let Some(target) = p.get("into").filter(|v| truthy(v)) else {
                continue;
            };
            let target = py_str(target).map_err(py)?;
            let Some(c) = p.get("checkout") else {
                return Err(Error::Raised(Raised {
                    class: "KeyError",
                    message: to_string(&repr_str(&cps("checkout"))).unwrap_or_default(),
                }));
            };
            let c = py_str(c).map_err(py)?;
            into.entry(target).or_default().insert(c);
        }
        let mut out: BTreeSet<PyStr> = BTreeSet::new();
        let mut todo = vec![checkout.to_vec()];
        while let Some(next) = todo.pop() {
            for c in into.get(&next).into_iter().flatten() {
                if out.contains(c) || c.as_slice() == checkout {
                    continue;
                }
                out.insert(c.clone());
                todo.push(c.clone());
            }
        }
        Ok(out)
    }
}
