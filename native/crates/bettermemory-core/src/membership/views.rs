//! Unit membership-3 of `membership.py`: the views and their liveness
//! (`view_state`, `live_view`, `_live_state`, `liveness_held`, 972-1028),
//! witness O through `location.continuity` (`_continuity`, `_confirms`,
//! 1079-1134) and the checkout of an object store (`_by_common`,
//! `_by_pointer`, 1135-1207).
//!
//! `_shared_remint` (`membership_shared`) is not named here: the caller
//! passes it as `remint`. The host's `os.path`, which splits a linked
//! worktree's git directory, is an argument of [`Membership::by_pointer_in`].

use std::collections::{BTreeMap, HashMap};

use super::vocab::{LIVE_STATES, OTHER_MACHINE, UNREACHABLE};
use super::{Error, Membership, MembershipStore, Result2};
use crate::capture::location::{Location, Sameness};
use crate::capture::Kind;
use crate::checkout_token;
use crate::fsdata::Flavour;
use crate::location_continuity::{continuity, Readers, Witness};
use crate::membership_lines::Raised;
use crate::membership_records::paths::{common_by_pointer, path_under, same_path};
use crate::membership_records::records::{
    found_token, live_key, Checkout, HistoryReader, LiveKey, View, BARE, FOLDER, LINKED, PRIMARY,
    UNRESOLVED,
};
use crate::membership_records::same_number;
use crate::membership_rows::root_file;
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// `_shared_remint(k, loc, token)`, which the caller supplies.
pub type Remint<'a, E> =
    &'a mut dyn FnMut(&Checkout, &Location, Option<&PyStr>) -> Result2<bool, E>;

pub(crate) fn is(text: &[u32], word: &str) -> bool {
    text.iter().copied().eq(word.chars().map(u32::from))
}

fn kind_text(kind: Option<Kind>) -> Option<PyStr> {
    kind.map(|k| cps(k.as_str()))
}

impl<S: MembershipStore> Membership<S> {
    /// `view_state(k, view)` (972-985).
    pub fn view_state(&mut self, k: &Checkout, view: Option<&View>) -> PyStr {
        if is(&k.kind, UNRESOLVED) {
            return cps(UNREACHABLE);
        }
        let views = match view {
            Some(v) => vec![v],
            None => k.views_on(&self.machine),
        };
        // `max` keeps the first of equal orders.
        let mut newest: Option<&View> = None;
        for v in views {
            if newest.is_none_or(|n| v.order > n.order) {
                newest = Some(v);
            }
        }
        let Some(newest) = newest else {
            return cps(OTHER_MACHINE);
        };
        if newest.machine != self.machine {
            return cps(OTHER_MACHINE);
        }
        let newest = newest.clone();
        self.live_state(k, &newest)
    }

    /// `live_view(k)` (986-996): the newest view here that is live.
    pub fn live_view(&mut self, k: &Checkout) -> Option<PyStr> {
        if is(&k.kind, UNRESOLVED) {
            return None;
        }
        let mut views: Vec<View> = k.views_on(&self.machine).into_iter().cloned().collect();
        views.sort_by_key(|v| std::cmp::Reverse(v.order));
        for v in views {
            let state = self.live_state(k, &v);
            if LIVE_STATES.iter().any(|s| is(&state, s)) {
                return Some(v.path);
            }
        }
        None
    }

    /// `_live_state(k, v)` (997-1014): the value kept in a
    /// `liveness_held` span, else read now.
    pub fn live_state(&mut self, k: &Checkout, v: &View) -> PyStr {
        let key = live_key(k, v);
        if let Some(hit) = self.live.as_ref().and_then(|live| live.get(&key)) {
            return hit.clone();
        }
        let state = (self.liveness)(k, v);
        if let Some(live) = &mut self.live {
            live.insert(key, state.clone());
        }
        state
    }

    /// `liveness_held()` (1015-1028): `f` runs with the liveness it reads
    /// kept; a span inside another keeps the outer one's.
    pub fn liveness_held<T>(&mut self, f: impl FnOnce(&mut Self) -> T) -> T {
        if self.live.is_some() {
            return f(self);
        }
        self.live = Some(HashMap::new());
        // The reference clears the span in a `finally`: an unwind out of
        // `f` clears it too before it goes on.
        let out = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f(self)));
        self.live = None;
        match out {
            Ok(out) => out,
            Err(unwound) => std::panic::resume_unwind(unwound),
        }
    }

    /// The liveness values a span holds.
    pub fn live_held(&self) -> Option<&HashMap<LiveKey, PyStr>> {
        self.live.as_ref()
    }

    /// `_continuity(k, loc)` (1079-1114). `history` is `loc.history`.
    pub fn continuity(
        &mut self,
        k: &Checkout,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        remint: Remint<'_, S::Error>,
    ) -> Result2<(bool, Witness), S::Error> {
        let superseded = k.token_superseded();
        let here = found_token(k, loc);
        let mut holds = |c: &[u32]| Ok(!Self::holds(history, &[c.to_vec()]).is_empty());
        let mut confirms = || self.confirms(k, loc, history);
        let mut reminted = || {
            let token = here.map_or(PyValue::None, |t| PyValue::Str(t.clone()));
            let ok = checkout_token::succeeds(&token, k.lineage().as_ref()).map_err(|e| {
                Error::Raised(Raised {
                    class: e.class(),
                    message: String::new(),
                })
            })?;
            Ok(ok || remint(k, loc, here)?)
        };
        continuity(
            k.token.as_deref(),
            k.last_head.as_deref(),
            &k.stamps,
            here.map(Vec::as_slice),
            loc.head.as_deref(),
            &loc.stamps,
            superseded,
            Readers {
                holds: &mut holds,
                confirms: Some(&mut confirms),
                reminted: Some(&mut reminted),
            },
        )
    }

    /// `_confirms(k, loc)` (1115-1134): F, S or H.
    pub fn confirms(
        &mut self,
        k: &Checkout,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
    ) -> Result2<bool, S::Error> {
        let pid = self.canon(&k.project)?;
        if is(&k.kind, FOLDER) {
            return Ok(match root_file(loc) {
                Some(rf) => self.file_binding_read(&rf.id)? == Some(pid),
                None => false,
            });
        }
        let common = loc.common_object_key.as_ref();
        if common.is_some()
            && !same_number(common, loc.object_key.as_ref())
            && same_number(common, k.common_object_key.as_ref())
        {
            return Ok(true);
        }
        let commits = self.witness_commits(Some(&pid))?;
        Ok(!commits.is_empty() && !Self::holds(history, &commits).is_empty())
    }

    /// `_by_common(loc)` (1135-1173): a checkout of this object store and
    /// the rule that proved it, or None and "".
    pub fn by_common(
        &mut self,
        loc: &Location,
        history: Option<&dyn HistoryReader>,
        remint: Remint<'_, S::Error>,
    ) -> Result2<(Option<Checkout>, &'static str), S::Error> {
        let Some(key) = loc.common_object_key.clone() else {
            return Ok((None, ""));
        };
        if loc.kind == Some(Kind::Folder) {
            return Ok((None, ""));
        }
        let mut found: BTreeMap<PyStr, Checkout> = BTreeMap::new();
        for column in ["common", "object"] {
            let where_ = vec![
                (cps("machine"), PyValue::Str(loc.machine.clone())),
                (
                    cps(&format!("{column}_device")),
                    PyValue::Str(key.device.clone()),
                ),
                (
                    cps(&format!("{column}_inode")),
                    PyValue::Str(key.inode.clone()),
                ),
            ];
            for k in self.checkouts_where(Some(where_), None)? {
                found.insert(k.id.clone(), k);
            }
        }
        for k in found.into_values() {
            let verdicts: Vec<Sameness> = [&k.common_object_key, &k.object_key]
                .into_iter()
                .flatten()
                .map(|theirs| theirs.compare(Some(&key)))
                .collect();
            if verdicts.contains(&Sameness::Same) {
                return Ok((Some(k), "common_object"));
            }
            if verdicts.contains(&Sameness::Unproven) {
                if self.continuity(&k, loc, history, &mut *remint)?.0 {
                    return Ok((Some(k), "common_object"));
                }
                if self.by_pointer(&k, loc) {
                    return Ok((Some(k), "common_object+pointer"));
                }
            }
        }
        Ok((None, ""))
    }

    /// `_by_pointer(k, loc)` (1174-1207) with the host's `os.path`.
    pub fn by_pointer(&mut self, k: &Checkout, loc: &Location) -> bool {
        self.by_pointer_in(Flavour::host(), k, loc)
    }

    /// `_by_pointer(k, loc)` with `os_path` as the host's `os.path`.
    pub fn by_pointer_in(&mut self, os_path: Flavour, k: &Checkout, loc: &Location) -> bool {
        let loc_kind = kind_text(loc.kind);
        if !(loc_kind.as_deref().is_some_and(|t| is(t, LINKED)) || is(&k.kind, LINKED)) {
            return false;
        }
        let Some(mine) = common_by_pointer(os_path, loc_kind.as_deref(), loc.git_dir.as_deref())
        else {
            return false;
        };
        let theirs = k.common_object_key.as_ref().or(k.object_key.as_ref());
        if !same_number(loc.common_object_key.as_ref(), theirs) {
            return false;
        }
        let Some(live) = self.live_view(k) else {
            return false;
        };
        let mut recorded = vec![common_by_pointer(
            os_path,
            Some(&k.kind),
            k.git_dir.as_deref(),
        )];
        if is(&k.kind, PRIMARY) {
            recorded.push(Some(path_under(&live, &[&cps(".git")])));
        } else if is(&k.kind, BARE) {
            recorded.push(Some(live));
        }
        recorded.iter().flatten().any(|r| same_path(r, &mine))
    }
}
