//! The legacy questions, the shown proposals and the record counts of the
//! membership model, unit mlegacy: `pending` (`membership.py` 1366-1370),
//! `settle_legacy_questions` (1488-1526), `below_root` and
//! `_declared_below` (1559-1597), `_record_counts` and
//! `_records_by_project` (1675-1697), and `first_output`
//! (`membership_queries.py` 280-292).
//!
//! Two effects are arguments: the root project file a shown question
//! reads ([`ProjectFileId`], as `restated` takes it) and the view a
//! directory is read as (`location.view_path`, a closure that answers
//! None where the port declines). `_DIR_PATHS` is a [`Flavour`]: the
//! host's in `below_root`, any in `below_root_in`.

use std::collections::{BTreeSet, HashMap};

use super::queries::{ProjectFileId, LEGACY_NO_LIVE_CHECKOUT};
use super::{column, raise, str_of, Error, Membership, MembershipStore, Query, Result2};
use crate::events::truthy;
use crate::fsdata::Flavour;
use crate::membership_records::legacy_asks;
use crate::membership_records::paths::{dir_below, dir_key};
use crate::membership_records::records::{Checkout, Proposal, LEGACY};
use crate::pyvalue::{PyStr, PyValue};
use crate::store::caller::Resolution;
use crate::unicode::{cps, to_string};

/// `membership_records.LEGACY_SEPARATE`: who answers a legacy question
/// the rule settles.
pub const LEGACY_SEPARATE: &str = "rule:legacy_separate";

/// `_INITS`: the declarations that write a project file.
pub const INITS: [&str; 3] = ["init", "init_new", "init_rebind"];

/// `location.view_path(directory)`: the view a directory is read as;
/// None where the port declines (answered as NotPorted).
pub type ViewPath<'a> = &'a dyn Fn(&[u32]) -> Option<PyStr>;

/// `(memories, episodes)` by project, in the order of the reference's
/// dict as the port builds it.
pub type Counts = Vec<(PyStr, (i64, i64))>;

fn is(text: &[u32], word: &str) -> bool {
    text.iter().copied().eq(word.chars().map(u32::from))
}

fn lossy(text: &[u32]) -> String {
    to_string(text).unwrap_or_else(|| {
        text.iter()
            .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
            .collect()
    })
}

/// `mapping.get(key)` on a proposal's evidence: AttributeError for a
/// value that is not a dict.
fn ev_get<'a, E>(ev: &'a PyValue, key: &str) -> Result2<Option<&'a PyValue>, E> {
    match ev {
        PyValue::Dict(_) => Ok(ev.get(key)),
        _ => raise("AttributeError", String::new()),
    }
}

impl<S: MembershipStore> Membership<S> {
    /// `pending()` (1366-1370): every open proposal as it is shown.
    pub fn pending(&mut self, file: ProjectFileId) -> Result2<Vec<Proposal>, S::Error> {
        let open = self.open_proposals(None)?;
        open.iter().map(|pr| self.restated(pr, file)).collect()
    }

    /// `first_output(res)` (`membership_queries.py` 280-292): the
    /// resolution's lines, then each open question of its checkout as
    /// shown, a line already held not repeated.
    pub fn first_output(
        &mut self,
        res: &Resolution,
        file: ProjectFileId,
    ) -> Result2<Vec<PyStr>, S::Error> {
        let mut lines = res.lines.clone();
        if let Some(k) = &res.checkout {
            for pr in self.open_proposals(Some(k))? {
                let shown = self.restated(&pr, file)?.line;
                if !lines.contains(&shown) {
                    lines.push(shown);
                }
            }
        }
        Ok(lines)
    }

    /// `settle_legacy_questions()` (1488-1526): every open legacy question
    /// whose evidence decides nothing closed as separate, in the order
    /// the open questions are read; the questions returned as read.
    pub fn settle_legacy_questions(&mut self) -> Result2<Vec<Proposal>, S::Error> {
        self.call(|m| {
            let mut closed = Vec::new();
            for pr in m.open_proposals(None)? {
                if !is(&pr.kind, LEGACY) || !is(&pr.reason, LEGACY_NO_LIVE_CHECKOUT) {
                    continue;
                }
                let legacy = match ev_get(&pr.evidence, "unresolved_project")? {
                    Some(PyValue::Str(t)) => t.clone(),
                    _ => continue,
                };
                let Some(first) = pr.candidates.first() else {
                    continue;
                };
                let candidate = m.canon(first)?;
                let pair = m
                    .canon(&legacy)
                    .and_then(|l| m.project(&l))
                    .and_then(|lp| Ok((lp, m.project(&candidate)?)));
                let (lp, cp) = match pair {
                    Ok(pair) => pair,
                    Err(Error::Raised(r)) if r.class == "KeyError" => continue,
                    Err(e) => return Err(e),
                };
                let mut mine = m.remotes_of(&lp.id)?;
                for k in m.checkouts_of(&lp.id)? {
                    if k.legacy {
                        mine.extend(k.remotes);
                    }
                }
                let theirs = m.remotes_of(&cp.id)?;
                let compatible = mine.is_empty() || theirs.is_empty() || !mine.is_disjoint(&theirs);
                let rule = match ev_get(&pr.evidence, "rule")? {
                    Some(v) if truthy(v) => str_of(v)?,
                    _ => Vec::new(),
                };
                if legacy_asks(&rule, lp.name == cp.name, compatible) {
                    continue;
                }
                m.close(&pr, &cps("new"), &cps(LEGACY_SEPARATE), None)?;
                closed.push(pr);
            }
            Ok(closed)
        })
    }

    /// `below_root(k, directory, canonical=)` (1559-1583) with the host's
    /// `_DIR_PATHS`. `view` is None for `canonical=True`.
    pub fn below_root(
        &self,
        k: &Checkout,
        directory: &[u32],
        view: Option<ViewPath>,
    ) -> Result2<Option<bool>, S::Error> {
        self.below_root_in(Flavour::host(), k, directory, view)
    }

    /// `below_root` with `dir_paths` as `_DIR_PATHS`: False at the root
    /// of the newest view here, True below it, None outside it or with no
    /// view here.
    pub fn below_root_in(
        &self,
        dir_paths: Flavour,
        k: &Checkout,
        directory: &[u32],
        view: Option<ViewPath>,
    ) -> Result2<Option<bool>, S::Error> {
        let views = k.views_on(&self.machine);
        let Some(newest) = views.last() else {
            return Ok(None);
        };
        let read = match view {
            None => directory.to_vec(),
            Some(f) => match f(directory) {
                Some(v) => v,
                None => return raise("NotPorted", "view_path declined".into()),
            },
        };
        let here = dir_key(dir_paths, &read);
        let root = dir_key(dir_paths, &newest.path);
        if here == root {
            return Ok(Some(false));
        }
        if dir_below(dir_paths, &here, &root) {
            return Ok(Some(true));
        }
        Ok(None)
    }

    /// `_declared_below(decl, k)` (1584-1597) with the host's
    /// `_DIR_PATHS`: `action` and `directory` are the declaration's.
    pub fn declared_below(
        &self,
        action: &[u32],
        directory: Option<&[u32]>,
        k: &Checkout,
    ) -> Result2<bool, S::Error> {
        self.declared_below_in(Flavour::host(), action, directory, k)
    }

    /// `_declared_below` with `dir_paths` as `_DIR_PATHS`: ValueError
    /// where the directory stands outside the checkout's newest view.
    pub fn declared_below_in(
        &self,
        dir_paths: Flavour,
        action: &[u32],
        directory: Option<&[u32]>,
        k: &Checkout,
    ) -> Result2<bool, S::Error> {
        let Some(directory) = directory.filter(|_| INITS.iter().any(|w| is(action, w))) else {
            return Ok(false);
        };
        match self.below_root_in(dir_paths, k, directory, None)? {
            Some(below) => Ok(below),
            None => {
                let seen = k.newest_view().map_or(&k.id, |v| &v.path);
                raise(
                    "ValueError",
                    format!(
                        "cannot tell where {} stands in checkout {} (seen at {})",
                        lossy(directory),
                        lossy(&k.id),
                        lossy(seen)
                    ),
                )
            }
        }
    }

    /// `_record_counts(project_id)` (1675-1680).
    pub fn record_counts(&mut self, project_id: &[u32]) -> Result2<(i64, i64), S::Error> {
        let counts = self.records_by_project(&[project_id.to_vec()])?;
        let pid = self.canon(project_id)?;
        Ok(counts
            .into_iter()
            .find(|(k, _)| *k == pid)
            .map_or((0, 0), |(_, c)| c))
    }

    /// `_records_by_project(project_ids)` (1681-1697): the wanted
    /// projects (canonical) first, then any project a home row names that
    /// was not wanted, in the order read. The reference walks a set of
    /// str, whose order follows the hash seed; the port walks it sorted,
    /// an order the reference can produce, and asks the homes of the
    /// members in that order.
    pub fn records_by_project(&mut self, project_ids: &[PyStr]) -> Result2<Counts, S::Error> {
        self.members_index()?;
        let mut wanted = BTreeSet::new();
        for pid in project_ids {
            wanted.insert(self.canon(pid)?);
        }
        let index = &self.index.as_ref().expect("built above").1;
        let members: Vec<PyValue> = wanted
            .iter()
            .flat_map(|pid| index.get(pid).into_iter().flatten())
            .map(|m| PyValue::Str(m.clone()))
            .collect();
        let mut counts: Vec<(PyStr, [i64; 2])> =
            wanted.into_iter().map(|pid| (pid, [0, 0])).collect();
        let mut at: HashMap<PyStr, usize> = counts
            .iter()
            .enumerate()
            .map(|(i, (k, _))| (k.clone(), i))
            .collect();
        if !members.is_empty() {
            let query = Query {
                column_in: Some((cps("project"), members)),
                ..Query::default()
            };
            for r in self.read("homes", &query)? {
                let pid = self.canon(&str_of(column(&r, "project")?)?)?;
                let slot = *at.entry(pid.clone()).or_insert_with(|| {
                    counts.push((pid, [0, 0]));
                    counts.len() - 1
                });
                let kind = column(&r, "kind")?;
                let memory = matches!(kind, PyValue::Str(t) if is(t, "memory"));
                counts[slot].1[usize::from(!memory)] += 1;
            }
        }
        Ok(counts.into_iter().map(|(k, [m, e])| (k, (m, e))).collect())
    }
}
