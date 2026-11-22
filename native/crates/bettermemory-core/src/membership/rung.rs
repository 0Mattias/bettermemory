//! Rung 2 of the resolution ladder (`membership_resolve.py` 398-516):
//! the token a git directory holds now (`_token_now`, `_token_read`) and
//! the checkout recorded at a location's object on its machine
//! (`_by_object`, `_rung_two`).
//!
//! Effects are arguments. `_token_now` reads the token through the
//! `read` it is given (the caller wires [`token_read`] over
//! [`token_file`]); [`token_file`] takes the stat and the eviction test
//! (`location._evicted(os.stat(path))`) as one closure; `_continuity`'s
//! `_shared_remint` is the caller's [`Remint`], as in `views`.

use super::shared_heal::MembershipLog;
use super::views::Remint;
use super::{Membership, MembershipStore, Result2};
use crate::capture::location::{Location, Sameness};
use crate::checkout_token::read::Fault;
use crate::checkout_token::{self, token_path};
use crate::fsdata::{Fs, OsError, PyErr};
use crate::gitfile_rule::gitdir::Ctx;
use crate::location_answer::PROBE_BUDGET;
use crate::location_continuity::Witness;
use crate::location_reads::{bounded_read, Board, Read};
use crate::membership_records::records::{found_token, Checkout, HistoryReader};
use crate::membership_shared::{ASK_WHICH, HEAL};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// What `_rung_two` returns, in its order: the checkout, how it is
/// continued, the earlier one returned for the question naming both,
/// and the one to retire into the first.
pub type RungTwo = (
    Option<Checkout>,
    Witness,
    Option<Checkout>,
    Option<Checkout>,
);

/// `_token_read(git_dir)` as `_token_now` calls it, which the caller
/// supplies.
pub type TokenNow<'a, E> = &'a mut dyn FnMut(&PyStr) -> Result2<Option<PyStr>, E>;

/// The board `_token_read`'s bounded reads run on.
pub type TokenBoard = Board<&'static str, Option<PyStr>, Fault>;

/// `_token_read`'s inner `read()`: None where `evicted` (the stat of the
/// token's path and `location._evicted` of it) says a provider holds the
/// file elsewhere, else `checkout_token.read(git_dir).token`. An error
/// of the stat is the closure's `Err`, raised as the reference raises it.
pub fn token_file<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    git_dir: &str,
    evicted: &dyn Fn(&[u8]) -> Result<bool, OsError>,
    pause: &mut dyn FnMut(f64),
) -> Result<Option<PyStr>, Fault> {
    let path = token_path(git_dir, ctx.flavour);
    if evicted(path.as_bytes()).map_err(Fault::Raised)? {
        return Ok(None);
    }
    Ok(checkout_token::read::read(ctx, git_dir, pause)?
        .token
        .map(|t| cps(&t)))
}

/// `_token_read(git_dir)`: `read` run within [`PROBE_BUDGET`] on `board`
/// under `path` (`str(token_path(git_dir))`) and ``"token"``; None where
/// it did not answer in time or raised an OSError. A ValueError (a NUL
/// in the path) and a read not ported pass on.
pub fn token_read(
    board: &TokenBoard,
    path: &str,
    read: impl FnOnce() -> Result<Option<PyStr>, Fault> + Send + 'static,
) -> Result<Option<PyStr>, Fault> {
    match bounded_read(board, path, "token", read, PROBE_BUDGET) {
        Ok(Read::Value(got)) => Ok(got),
        Ok(Read::Unanswered | Read::NotRead) => Ok(None),
        Err(Fault::Raised(e)) if e.class != PyErr::Value => Ok(None),
        Err(e) => Err(e),
    }
}

fn truthy(s: Option<&PyStr>) -> Option<&PyStr> {
    s.filter(|t| !t.is_empty())
}

fn at_object(loc: &Location, key: &crate::capture::location::ObjectKey) -> Vec<(PyStr, PyValue)> {
    vec![
        (cps("machine"), PyValue::Str(loc.machine.clone())),
        (cps("object_device"), PyValue::Str(key.device.clone())),
        (cps("object_inode"), PyValue::Str(key.inode.clone())),
    ]
}

impl<S: MembershipStore> Membership<S> {
    /// `_token_now(self, loc)` (398-431): `loc` holding the token its git
    /// directory holds now, read through `read` (`_token_read`), where a
    /// checkout of its machine is recorded at its object and none records
    /// its token; `loc` as given otherwise.
    pub fn token_now(
        &self,
        loc: Location,
        read: TokenNow<'_, S::Error>,
    ) -> Result2<Location, S::Error> {
        let token = truthy(loc.token.as_ref())
            .or(loc.token_written.as_ref())
            .cloned();
        let (Some(token), Some(git_dir), Some(key)) = (
            truthy(token.as_ref()).cloned(),
            truthy(loc.git_dir.as_ref()).cloned(),
            loc.object_key.clone(),
        ) else {
            return Ok(loc);
        };
        if !(loc.machine == self.machine && self.reads_here()) {
            return Ok(loc);
        }
        let at = self.checkouts_where(Some(at_object(&loc, &key)), None)?;
        if at.is_empty() || at.iter().any(|k| k.token.as_ref() == Some(&token)) {
            return Ok(loc);
        }
        let found = read(&git_dir)?;
        match found {
            Some(found) if found != token => Ok(Location {
                token: Some(found),
                token_written: None,
                token_status: cps("present"),
                ..loc
            }),
            _ => Ok(loc),
        }
    }
}

impl<S: MembershipLog> Membership<S> {
    /// `_by_object(self, loc)` (454-458): the first two of
    /// [`Membership::rung_two`].
    pub fn by_object(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        remint: Remint<'_, S::Error>,
    ) -> Result2<(Option<Checkout>, Witness), S::Error> {
        let (k, how, _first, _settled) = self.rung_two(loc, history, remint)?;
        Ok((k, how))
    }

    /// `_rung_two(self, loc)` (461-516). `history` is `loc.history` and
    /// `remint` stands for `_shared_remint`, both passed to `_continuity`.
    pub fn rung_two(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        remint: Remint<'_, S::Error>,
    ) -> Result2<RungTwo, S::Error> {
        let Some(key) = loc.object_key.clone() else {
            return Ok((None, "", None, None));
        };
        let mut reminted: Option<Checkout> = None;
        for k in self.checkouts_where(Some(at_object(loc, &key)), None)? {
            let Some(own) = k.object_key.as_ref() else {
                continue;
            };
            if self.retired_into(&k)?.is_some() {
                continue;
            }
            let verdict = own.compare(Some(&key));
            if verdict == Sameness::Same {
                return Ok((Some(k), "birth", None, None));
            }
            if verdict != Sameness::Unproven
                || (reminted.is_some() && k.token.as_ref() != found_token(&k, loc))
            {
                continue;
            }
            let (ok, how) = self.continuity(&k, loc, history, &mut *remint)?;
            if ok {
                if let Some(first) = reminted.as_ref() {
                    let read = self.healed(first, &k, loc)?;
                    if read == HEAL {
                        return Ok((reminted, "remint", None, Some(k)));
                    }
                    if read == ASK_WHICH {
                        return Ok((Some(k), how, reminted, None));
                    }
                }
            }
            if ok && (how != "remint" || reminted.is_some()) {
                return Ok((Some(k), how, None, None));
            }
            if ok {
                reminted = Some(k);
            }
        }
        match reminted {
            None => Ok((None, "", None, None)),
            Some(r) => Ok((Some(r), "remint", None, None)),
        }
    }
}
