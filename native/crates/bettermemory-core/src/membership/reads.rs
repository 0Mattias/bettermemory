//! The evidence and checkout reads of the model (`membership.py`
//! 513-534, 589-636, 721-730 and 843-884): the witness rows a full object
//! id carries, the projects with a witness, a project file's binding, and
//! the checkouts with their views.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use super::{column, key_error, str_of, Error, Membership, MembershipStore, Query, Result2};
use crate::membership_records::records::{Checkout, View, UNRESOLVED};
use crate::membership_rows::{checkout_from, checkout_row, view_from};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::{cps, lower};

/// `_FULL_SHA.fullmatch(text)` (`membership_records.py` 115,
/// `[0-9a-f]{40}(?:[0-9a-f]{24})?`): a fixed class, matched by hand as
/// the regex decision's rule 3 allows. 40 or 64 lowercase hex digits.
pub fn full_sha(text: &[u32]) -> bool {
    (text.len() == 40 || text.len() == 64)
        && text.iter().all(|&c| matches!(c, 0x30..=0x39 | 0x61..=0x66))
}

fn s(v: &[u32]) -> PyValue {
    PyValue::Str(v.to_vec())
}

fn is_unresolved(k: &Checkout) -> bool {
    k.kind == cps(UNRESOLVED)
}

impl<S: MembershipStore> Membership<S> {
    /// `_witness_query(**query)` (589-609): the rows whose commit,
    /// lowercased, is a full object id.
    pub fn witness_query(&self, query: &Query) -> Result2<Vec<PyValue>, S::Error> {
        let mut out = Vec::new();
        for r in self.read("project_witnesses", query)? {
            if full_sha(&lower(&str_of(column(&r, "commit_id")?)?)) {
                out.push(r);
            }
        }
        Ok(out)
    }

    /// `_witness_rows(project_id)` (610-616): oldest first.
    pub fn witness_rows(&mut self, project_id: Option<&[u32]>) -> Result2<Vec<PyValue>, S::Error> {
        let column_in = match project_id {
            None => None,
            Some(pid) => Some((
                cps("project"),
                self.members(pid)?.into_iter().map(PyValue::Str).collect(),
            )),
        };
        self.witness_query(&Query {
            where_: None,
            column_in,
            order_by: vec![cps("arrival")],
        })
    }

    /// `witness_commits(project_id)` (617-623): in lowercase.
    pub fn witness_commits(&mut self, project_id: Option<&[u32]>) -> Result2<Vec<PyStr>, S::Error> {
        let mut out = Vec::new();
        for r in self.witness_rows(project_id)? {
            out.push(lower(&str_of(column(&r, "commit_id")?)?));
        }
        Ok(out)
    }

    /// `_has_witness(project_id, commit)` (624-633): compared in lowercase.
    pub fn has_witness(&mut self, project_id: &[u32], commit: &[u32]) -> Result2<bool, S::Error> {
        let want = lower(commit);
        for r in self.witness_rows(Some(project_id))? {
            if lower(&str_of(column(&r, "commit_id")?)?) == want {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// `_has_witnesses(project_id)` (634-636).
    pub fn has_witnesses(&mut self, project_id: &[u32]) -> Result2<bool, S::Error> {
        Ok(!self.witness_rows(Some(project_id))?.is_empty())
    }

    /// `_witnessed()` (513-517): the projects (merges followed) with a
    /// witness commit.
    pub fn witnessed(&mut self) -> Result2<BTreeSet<PyStr>, S::Error> {
        let mut out = BTreeSet::new();
        for r in self.witness_query(&Query::default())? {
            let pid = str_of(column(&r, "project")?)?;
            out.insert(self.canon(&pid)?);
        }
        Ok(out)
    }

    /// `file_binding(file_id)` (721-726): the projects read afresh.
    pub fn file_binding(&mut self, file_id: &[u32]) -> Result2<Option<PyStr>, S::Error> {
        self.forget_projects();
        self.file_binding_read(file_id)
    }

    /// `_file_binding(file_id)` (727-730).
    pub fn file_binding_read(&mut self, file_id: &[u32]) -> Result2<Option<PyStr>, S::Error> {
        let rows = self.read(
            "project_files",
            &Query {
                where_: Some(vec![(cps("file_id"), s(file_id))]),
                ..Query::default()
            },
        )?;
        match rows.first() {
            None => Ok(None),
            Some(r) => {
                let pid = str_of(column(r, "project")?)?;
                self.canonical(Some(&pid))
            }
        }
    }

    /// `_checkouts(where=..., column_in=...)` (843-860): the rows with
    /// their views, in one read of each table.
    pub fn checkouts_where(
        &self,
        where_: Option<Vec<(PyStr, PyValue)>>,
        column_in: Option<(PyStr, Vec<PyValue>)>,
    ) -> Result2<Vec<Checkout>, S::Error> {
        let rows = self.read(
            "checkouts",
            &Query {
                where_,
                column_in,
                order_by: Vec::new(),
            },
        )?;
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        let mut ids = Vec::with_capacity(rows.len());
        for r in &rows {
            ids.push(column(r, "id")?.clone());
        }
        let query = Query {
            column_in: Some((cps("checkout"), ids)),
            ..Query::default()
        };
        let mut views: HashMap<PyStr, Vec<View>> = HashMap::new();
        for v in self.read("checkout_views", &query)? {
            let key = str_of(column(&v, "checkout")?)?;
            let view = view_from(&v).map_err(Error::Raised)?;
            views.entry(key).or_default().push(view);
        }
        let mut out = Vec::with_capacity(rows.len());
        for r in &rows {
            let key = str_of(column(r, "id")?)?;
            let mine = views.get(&key).map_or(&[][..], Vec::as_slice);
            out.push(checkout_from(r, mine).map_err(Error::Raised)?);
        }
        Ok(out)
    }

    /// `checkout(checkout_id)` (861-866): KeyError when absent.
    pub fn checkout(&self, checkout_id: &[u32]) -> Result2<Checkout, S::Error> {
        let found = self.checkouts_where(Some(vec![(cps("id"), s(checkout_id))]), None)?;
        match found.into_iter().next() {
            Some(k) => Ok(k),
            None => key_error(checkout_id),
        }
    }

    /// `checkouts(token=...)` (867-871).
    pub fn checkouts(&self, token: Option<&[u32]>) -> Result2<Vec<Checkout>, S::Error> {
        self.checkouts_where(token.map(|t| vec![(cps("token"), s(t))]), None)
    }

    /// `_checkouts_of(project_id)` (872-880): merges followed, the
    /// unresolved paths left out.
    pub fn checkouts_of(&mut self, project_id: &[u32]) -> Result2<Vec<Checkout>, S::Error> {
        let members = self
            .members(project_id)?
            .into_iter()
            .map(PyValue::Str)
            .collect();
        let all = self.checkouts_where(None, Some((cps("project"), members)))?;
        Ok(all.into_iter().filter(|k| !is_unresolved(k)).collect())
    }

    /// `_checkouts_by_project(project_ids, machine=...)` (518-534): one
    /// read for every project. A map by id: the reference's dict is built
    /// in the order of a set, which no caller iterates unsorted.
    pub fn checkouts_by_project(
        &mut self,
        project_ids: &[PyStr],
        machine: Option<&[u32]>,
    ) -> Result2<BTreeMap<PyStr, Vec<Checkout>>, S::Error> {
        let index = self.members_index()?.clone();
        let mut wanted = BTreeSet::new();
        for pid in project_ids {
            wanted.insert(self.canon(pid)?);
        }
        let members: Vec<PyValue> = wanted
            .iter()
            .flat_map(|pid| index.get(pid).into_iter().flatten())
            .map(|m| PyValue::Str(m.clone()))
            .collect();
        let mut out: BTreeMap<PyStr, Vec<Checkout>> =
            wanted.iter().map(|pid| (pid.clone(), Vec::new())).collect();
        if members.is_empty() {
            return Ok(out);
        }
        for k in self.checkouts_where(None, Some((cps("project"), members)))? {
            if is_unresolved(&k) || machine.is_some_and(|m| k.machine != m) {
                continue;
            }
            let canon = self.canon(&k.project)?;
            out.entry(canon).or_default().push(k);
        }
        Ok(out)
    }

    /// `_put_checkout(k)` (881-884).
    pub fn put_checkout(&mut self, k: Checkout) -> Result2<Checkout, S::Error> {
        self.put("checkout", &checkout_row(&k))?;
        Ok(k)
    }
}
