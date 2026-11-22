//! The sightings and tokens of the membership model (`membership.py`),
//! unit membership-4a (mtoken): `_touch_view` (1029-1078),
//! `_checkout_name` (1598-1601), `_give_token` (1602-1647),
//! `token_written` (1648-1674) and `_foreign` (1835-1844).

use super::views::is;
use super::vocab::{LIVE_STATES, SUPERSEDED};
use super::{Error, Membership, MembershipStore, Result2};
use crate::capture::location::Location;
use crate::capture::paths::norm;
use crate::checkout_token;
use crate::datetime::parse_event_ts;
use crate::membership_lines::Raised;
use crate::membership_records::paths::{basename, same_path};
use crate::membership_records::records::{Checkout, View, BARE};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// `VIEW_REFRESH` (`membership_records.py` 152): one day, in microseconds.
pub const VIEW_REFRESH_US: i128 = 86_400_000_000;
/// `OTHER_MACHINE_SKIP` (`membership_records.py` 1021).
pub const OTHER_MACHINE_SKIP: &str = "skipped:other_machine";
/// `checkout_token.CONTENDED` (`checkout_token.py` 177).
pub const CONTENDED: &str = "skipped:contended";

fn s(v: &[u32]) -> PyValue {
    PyValue::Str(v.to_vec())
}

fn opt(v: Option<&[u32]>) -> PyValue {
    v.map_or(PyValue::None, s)
}

/// `_foreign(k, token, status)`: a token found in place (`exists`) that is
/// not a re-mint of `k`'s lineage. A raise of `succeeds` carries its class
/// and an empty message: `checkout_token::Raised` holds no text. No input
/// reaches that raise, since a lineage whose token, device or inode would
/// not encode is read as None and `succeeds` then answers False.
pub fn foreign<E>(k: &Checkout, token: Option<&[u32]>, status: &[u32]) -> Result2<bool, E> {
    if !is(status, "exists") {
        return Ok(false);
    }
    let token = opt(token);
    let ok = checkout_token::succeeds(&token, k.lineage().as_ref()).map_err(|e| {
        Error::Raised(Raised {
            class: e.class(),
            message: String::new(),
        })
    })?;
    Ok(!ok)
}

impl<S: MembershipStore> Membership<S> {
    /// `_remint`: the checkout whose token the caller re-mints after the
    /// reply, set by `give_token` when no writer wrote it.
    pub fn remint(&self) -> Option<&PyStr> {
        self.remint.as_ref()
    }

    /// `_touch_view(k, loc)` (1029-1078): a known path is written again
    /// only when it is not the newest view here or its sighting is stale;
    /// a new path adds a view, `checkout_moved` when no older view here
    /// is live, else `checkout_view_added`.
    pub fn touch_view(&mut self, k: Checkout, loc: &Location) -> Result2<Checkout, S::Error> {
        let mut k = k;
        if k.retired {
            k.retired = false;
            k = self.put_checkout(k)?;
        }
        let at = match &loc.root {
            Some(r) if !r.is_empty() => r,
            _ => &loc.cwd,
        };
        let path = norm(at);
        let stamp = self.stamp()?;
        // `views_on`: the indices of the views on the machine, stably by order.
        let mut here: Vec<usize> = (0..k.views.len())
            .filter(|&i| k.views[i].machine == loc.machine)
            .collect();
        here.sort_by_key(|&i| k.views[i].order);
        let newest = here.iter().map(|&i| k.views[i].order).max().unwrap_or(0);
        for &i in &here {
            if !same_path(&k.views[i].path, &path) {
                continue;
            }
            let v = &k.views[i];
            let stale = match parse_event_ts(&v.last_seen).and_then(|t| t.instant_us()) {
                None => true,
                Some(last) => match (self.now)().instant_us() {
                    Some(now) => now - last >= VIEW_REFRESH_US,
                    None => {
                        return super::raise(
                            "TypeError",
                            "can't subtract offset-naive and offset-aware datetimes".into(),
                        )
                    }
                },
            };
            if v.order != newest || stale {
                let order = self
                    .store
                    .next_membership_number("checkout_views", "seen_order")
                    .map_err(Error::Store)?;
                let fresh = View {
                    last_seen: stamp,
                    order,
                    ..v.clone()
                };
                self.put_view(&fresh)?;
                k.views[i] = fresh;
            }
            return Ok(k);
        }
        let mut live_old = false;
        for &i in &here {
            let v = k.views[i].clone();
            let state = self.live_state(&k, &v);
            if LIVE_STATES.iter().any(|w| is(&state, w)) {
                live_old = true;
            }
        }
        let moved = !here.is_empty() && !live_old;
        let previous = here.last().map(|&i| k.views[i].path.as_slice());
        let event = if moved {
            "checkout_moved"
        } else {
            "checkout_view_added"
        };
        self.event(
            event,
            vec![
                ("checkout", s(&k.id)),
                ("machine", s(&loc.machine)),
                ("path", s(&path)),
                ("previous", opt(previous)),
                ("evidence", PyValue::str("same filesystem object")),
            ],
        )?;
        let order = self
            .store
            .next_membership_number("checkout_views", "seen_order")
            .map_err(Error::Store)?;
        let view = View {
            checkout: k.id.clone(),
            machine: loc.machine.clone(),
            path,
            first_seen: stamp.clone(),
            last_seen: stamp,
            order,
        };
        self.put_view(&view)?;
        k.views.push(view);
        Ok(k)
    }

    /// `_checkout_name(k)` (1598-1601): the base name of its newest view,
    /// else its id.
    pub fn checkout_name(&self, k: &Checkout) -> PyStr {
        k.newest_view()
            .map_or_else(|| k.id.clone(), |v| basename(&v.path))
    }

    /// `_give_token(k)` (1602-1647): the writer's token recorded, or the
    /// checkout marked superseded and left to the caller (no writer, or
    /// none written), to its next sighting there (another machine), or to
    /// the next sighting with a row (contended, a foreign token found).
    pub fn give_token(&mut self, k: &Checkout) -> Result2<(), S::Error> {
        if k.git_dir.is_none() || is(&k.kind, BARE) {
            return Ok(());
        }
        let written = match &self.token_writer {
            Some(w) => w(k).map_err(Error::Raised)?,
            None => None,
        };
        let left = match &written {
            None => true,
            Some((token, status)) => {
                is(status, OTHER_MACHINE_SKIP)
                    || is(status, CONTENDED)
                    || foreign(k, token.as_deref(), status)?
            }
        };
        if left {
            if !is(&k.token_status, SUPERSEDED) {
                let mut put = k.clone();
                put.token_status = cps(SUPERSEDED);
                self.put_checkout(put)?;
            }
            match &written {
                None => self.remint = Some(k.id.clone()),
                Some((_, status)) if !is(status, OTHER_MACHINE_SKIP) => {
                    self.event(
                        "token_reminted",
                        vec![
                            ("checkout", s(&k.id)),
                            ("status", s(status)),
                            ("written", PyValue::Bool(false)),
                        ],
                    )?;
                }
                Some(_) => {}
            }
            return Ok(());
        }
        let Some((token, status)) = written else {
            unreachable!("a missing write is left above")
        };
        self.record_token(k, token.as_deref(), &status, false, false)
    }

    /// The tail both writers share: a token put with its status, or a
    /// superseded checkout given the status, then `token_reminted`.
    /// `token_written` also keeps a checkout superseded when the status
    /// is `superseded` itself (`guarded`); `_give_token` does not test it.
    fn record_token(
        &mut self,
        k: &Checkout,
        token: Option<&[u32]>,
        status: &[u32],
        kept: bool,
        guarded: bool,
    ) -> Result2<(), S::Error> {
        let mut put = k.clone();
        match token {
            Some(t) if !t.is_empty() && !kept => {
                put.token = Some(t.to_vec());
                put.token_status = status.to_vec();
                self.put_checkout(put)?;
            }
            _ if is(&k.token_status, SUPERSEDED)
                && !kept
                && !(guarded && is(status, SUPERSEDED)) =>
            {
                put.token_status = status.to_vec();
                self.put_checkout(put)?;
            }
            _ => {}
        }
        self.event(
            "token_reminted",
            vec![
                ("checkout", s(&k.id)),
                ("status", s(status)),
                ("written", PyValue::Bool(is(status, "written"))),
            ],
        )
    }

    /// `token_written(checkout_id, token, status)` (1648-1674): in one
    /// call, the token the caller wrote recorded, unless the claim was
    /// contended or the token found is foreign.
    pub fn token_written(
        &mut self,
        checkout_id: &[u32],
        token: Option<&[u32]>,
        status: &[u32],
    ) -> Result2<(), S::Error> {
        self.call(|m| {
            let k = m.checkout(checkout_id)?;
            let kept = is(status, CONTENDED) || foreign(&k, token, status)?;
            m.record_token(&k, token, status, kept, true)
        })
    }
}
