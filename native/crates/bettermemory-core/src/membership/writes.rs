//! The evidence, boundary and checkout writes of the model
//! (`membership.py` 637-720, 731-842 and 885-971): a witness commit with
//! its blobs and the bound that releases the oldest, the remote keys, a
//! project file bound, a boundary made or lifted, a checkout's object
//! read anew, a view put and a checkout registered.

use std::collections::BTreeSet;

use super::reads::full_sha;
use super::vocab::WITNESS_BOUND;
use super::{column, raise, str_of, Membership, MembershipStore, Query, Result2};
use crate::capture::location::{Location, ObjectKey, ProjectFileRef};
use crate::capture::paths::{inside, norm};
use crate::capture::Kind;
use crate::membership_lines::{line_boundary_added, line_boundary_dropped};
use crate::membership_records::records::{
    Checkout, HistoryReader, Project, View, PRIMARY, UNRESOLVED,
};
use crate::membership_records::same_number;
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::unicode::cps;

/// `KEEP_OLDEST` (`membership_records.py` 116): the first witnesses of a
/// project the bound never releases. Lane mrecid ports it with the rest
/// of its lines; the fold keeps one definition.
pub const KEEP_OLDEST: usize = 16;
/// `BLOBS_PER_WITNESS` (`membership_records.py` 117): at most this many
/// new blob ids a witness brings.
pub const BLOBS_PER_WITNESS: usize = 8;

fn s(v: &[u32]) -> PyValue {
    PyValue::Str(v.to_vec())
}

fn opt(v: Option<&[u32]>) -> PyValue {
    v.map_or(PyValue::None, s)
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

/// `dataclasses.asdict(key)`.
fn key_dict(k: &ObjectKey) -> PyValue {
    dict(vec![
        ("device", s(&k.device)),
        ("inode", s(&k.inode)),
        ("birth", opt(k.birth.as_deref())),
    ])
}

fn where_(items: Vec<(&str, PyValue)>) -> Query {
    Query {
        where_: Some(items.into_iter().map(|(k, v)| (cps(k), v)).collect()),
        ..Query::default()
    }
}

impl<S: MembershipStore> Membership<S> {
    /// `add_witness(project_id, commit, source=, history=)` (637-653):
    /// `_add_witness` in one call.
    pub fn add_witness(
        &mut self,
        project_id: &[u32],
        commit: &[u32],
        source: &[u32],
        history: Option<&dyn HistoryReader>,
    ) -> Result2<(), S::Error> {
        self.call(|m| m.add_witness_in(project_id, commit, source, history))
    }

    /// `_add_witness(...)` (654-707): the one writer of a witness row.
    pub fn add_witness_in(
        &mut self,
        project_id: &[u32],
        commit: &[u32],
        source: &[u32],
        history: Option<&dyn HistoryReader>,
    ) -> Result2<(), S::Error> {
        if !full_sha(commit) {
            let shown = repr_str(&commit[..commit.len().min(72)]);
            let shown = crate::unicode::to_string(&shown).unwrap_or_default();
            return raise(
                "ValueError",
                format!("a witness {shown} is not a full commit id"),
            );
        }
        let pid = self.canon(project_id)?;
        if self.has_witness(&pid, commit)? {
            return Ok(());
        }
        let fingerprint = history.and_then(|h| h.fingerprint(commit));
        let blobs: BTreeSet<PyStr> = history
            .map(|h| h.blobs(commit))
            .unwrap_or_default()
            .into_iter()
            .collect();
        let arrival = self
            .store
            .next_membership_number("project_witnesses", "arrival")
            .map_err(super::Error::Store)?;
        self.put(
            "project_witness",
            &dict(vec![
                ("project", s(&pid)),
                ("commit_id", s(commit)),
                ("arrival", PyValue::int(i128::from(arrival))),
                ("source", s(source)),
                ("fingerprint", opt(fingerprint.as_deref())),
            ]),
        )?;
        let mut kept = BTreeSet::new();
        for r in self.read("project_blobs", &where_(vec![("project", s(&pid))]))? {
            kept.insert(str_of(column(&r, "blob")?)?);
        }
        let fresh: Vec<PyStr> = blobs
            .into_iter()
            .filter(|b| !kept.contains(b))
            .take(BLOBS_PER_WITNESS)
            .collect();
        for blob in fresh {
            self.put(
                "project_blob",
                &dict(vec![
                    ("project", s(&pid)),
                    ("blob", PyValue::Str(blob)),
                    ("witness", s(commit)),
                ]),
            )?;
        }
        self.event(
            "witness_put",
            vec![
                ("project", s(&pid)),
                ("commit", s(commit)),
                ("source", s(source)),
            ],
        )?;
        let own = self.witness_query(&Query {
            order_by: vec![cps("arrival")],
            ..where_(vec![("project", s(&pid))])
        })?;
        if own.len() > WITNESS_BOUND {
            let drop = str_of(column(&own[KEEP_OLDEST], "commit_id")?)?;
            self.delete(
                "project_witness",
                &dict(vec![("project", s(&pid)), ("commit_id", s(&drop))]),
            )?;
            let query = where_(vec![("project", s(&pid)), ("witness", s(&drop))]);
            for row in self.read("project_blobs", &query)? {
                let blob = column(&row, "blob")?.clone();
                self.delete(
                    "project_blob",
                    &dict(vec![("project", s(&pid)), ("blob", blob)]),
                )?;
            }
            self.event(
                "witness_released",
                vec![
                    ("project", s(&pid)),
                    ("commit", s(&drop)),
                    ("bound", PyValue::int(WITNESS_BOUND as i128)),
                ],
            )?;
        }
        Ok(())
    }

    /// `_remotes_of(project_id)` (708-715): merges followed.
    pub fn remotes_of(&mut self, project_id: &[u32]) -> Result2<BTreeSet<PyStr>, S::Error> {
        let members = self
            .members(project_id)?
            .into_iter()
            .map(PyValue::Str)
            .collect();
        let query = Query {
            column_in: Some((cps("project"), members)),
            ..Query::default()
        };
        let mut out = BTreeSet::new();
        for r in self.read("project_remotes", &query)? {
            out.insert(str_of(column(&r, "remote_key")?)?);
        }
        Ok(out)
    }

    /// `_add_remotes(project_id, keys)` (716-720): the new keys, sorted.
    pub fn add_remotes(&mut self, project_id: &[u32], keys: &[PyStr]) -> Result2<(), S::Error> {
        let pid = self.canon(project_id)?;
        let known = self.remotes_of(&pid)?;
        let new: BTreeSet<&PyStr> = keys.iter().filter(|k| !known.contains(*k)).collect();
        for key in new {
            self.put(
                "project_remote",
                &dict(vec![("project", s(&pid)), ("remote_key", s(key))]),
            )?;
        }
        Ok(())
    }

    /// `_bind_file(file_id, project_id, evidence=, boundary=, checkout=,
    /// directory=)` (731-764).
    pub fn bind_file(
        &mut self,
        file_id: &[u32],
        project_id: &[u32],
        evidence: &[u32],
        boundary: bool,
        checkout: Option<&[u32]>,
        directory: Option<&[u32]>,
    ) -> Result2<(), S::Error> {
        let pid = self.canon(project_id)?;
        self.put(
            "project_file",
            &dict(vec![
                ("file_id", s(file_id)),
                ("project", s(&pid)),
                ("evidence", s(evidence)),
            ]),
        )?;
        let mut payload = vec![
            ("file_id", s(file_id)),
            ("project", s(&pid)),
            ("evidence", s(evidence)),
        ];
        if let Some(c) = checkout {
            payload.push(("checkout", s(c)));
        }
        if let Some(d) = directory {
            payload.push(("dir", s(d)));
        }
        self.event("project_file_bound", payload)?;
        if boundary {
            self.make_boundary(&pid, directory)?;
        }
        Ok(())
    }

    /// `_file_flag(f, project_id)` (765-780): B12's lines.
    pub fn file_flag(
        &mut self,
        f: &ProjectFileRef,
        project_id: &[u32],
    ) -> Result2<Vec<PyStr>, S::Error> {
        let pid = self.canon(project_id)?;
        let p = self.project(&pid)?;
        if f.boundary && !p.boundary {
            self.make_boundary(&pid, Some(&f.dir))?;
            return Ok(vec![line_boundary_added(&f.path, &p.name, &pid)]);
        }
        if p.boundary && !f.boundary {
            return Ok(vec![line_boundary_dropped(
                &f.path, &p.name, &pid, &f.dir, &f.id,
            )]);
        }
        Ok(Vec::new())
    }

    fn snapshot(&mut self) -> Result2<Vec<Project>, S::Error> {
        Ok(self.all_projects()?.values().cloned().collect())
    }

    /// `_lift_boundary(project_id, outer)` (781-797).
    pub fn lift_boundary(
        &mut self,
        project_id: &[u32],
        outer: Option<&[u32]>,
    ) -> Result2<(), S::Error> {
        let pid = self.canon(project_id)?;
        let p = self.project(&pid)?;
        if !p.boundary {
            return Ok(());
        }
        let outer = match &p.parent {
            Some(parent) => {
                let up = self.canon(parent)?;
                self.project(&up)?.tenant
            }
            None => outer.map(<[u32]>::to_vec),
        };
        self.put_project(Project {
            boundary: false,
            tenant: outer.clone(),
            ..p
        })?;
        for q in self.snapshot()? {
            if q.merged_into.is_none() && q.id != pid && q.tenant.as_deref() == Some(&pid[..]) {
                self.put_project(Project {
                    tenant: outer.clone(),
                    ..q
                })?;
            }
        }
        Ok(())
    }

    /// `_make_boundary(project_id, around=)` (798-842).
    pub fn make_boundary(
        &mut self,
        project_id: &[u32],
        around: Option<&[u32]>,
    ) -> Result2<(), S::Error> {
        let pid = self.canon(project_id)?;
        let p = self.project(&pid)?;
        if p.boundary {
            return Ok(());
        }
        let previous_tenant = p.tenant.clone();
        self.put_project(Project {
            boundary: true,
            tenant: Some(pid.clone()),
            ..p
        })?;
        let mut enclosed: BTreeSet<PyStr> = BTreeSet::new();
        if let Some(around) = around {
            let machine = self.machine.clone();
            for checkout in self.checkouts(None)? {
                let unresolved = checkout.kind == cps(UNRESOLVED);
                let last = checkout.views_on(&machine).last().map(|v| v.path.clone());
                if let Some(path) = last {
                    if !unresolved && inside(&path, around) {
                        enclosed.insert(self.canon(&checkout.project)?);
                    }
                }
            }
        }
        for q in self.snapshot()? {
            if q.merged_into.is_some() || q.id == pid {
                continue;
            }
            let chain = self.chain_of(Some(&q.id))?;
            let inner = match chain.iter().position(|x| *x == pid) {
                Some(i) => &chain[..i],
                None => match chain.iter().position(|x| enclosed.contains(x)) {
                    Some(at) if q.tenant == previous_tenant => &chain[..=at],
                    _ => continue,
                },
            };
            let mut walled = false;
            for x in inner {
                if self.project(x)?.boundary {
                    walled = true;
                    break;
                }
            }
            if walled || q.tenant.as_deref() == Some(&pid[..]) {
                continue;
            }
            self.put_project(Project {
                tenant: Some(pid.clone()),
                ..q
            })?;
        }
        Ok(())
    }

    /// `_refresh_object(k, loc)` (885-910): the object keys read anew
    /// (decision B20).
    pub fn refresh_object(&mut self, k: Checkout, loc: &Location) -> Result2<Checkout, S::Error> {
        let (Some(new), Some(old)) = (loc.object_key.clone(), k.object_key.clone()) else {
            return Ok(k);
        };
        if old == new {
            return Ok(k);
        }
        let mut common = k.common_object_key.clone();
        if common.is_some() && same_number(common.as_ref(), Some(&old)) {
            common = loc.common_object_key.clone().or(common);
        }
        let k = self.put_checkout(Checkout {
            object_key: Some(new.clone()),
            common_object_key: common,
            ..k
        })?;
        self.event(
            "checkout_object_refreshed",
            vec![
                ("checkout", s(&k.id)),
                ("path", opt(loc.root.as_deref())),
                ("old_object", key_dict(&old)),
                ("new_object", key_dict(&new)),
            ],
        )?;
        Ok(k)
    }

    /// `_put_view(v)` (911-923).
    pub fn put_view(&mut self, v: &View) -> Result2<(), S::Error> {
        self.put(
            "checkout_view",
            &dict(vec![
                ("checkout", s(&v.checkout)),
                ("machine", s(&v.machine)),
                ("path", s(&v.path)),
                ("first_seen", s(&v.first_seen)),
                ("last_seen", s(&v.last_seen)),
                ("seen_order", PyValue::int(i128::from(v.order))),
            ]),
        )
    }

    /// `_register(loc, project_id, rule)` (924-971): the id before the
    /// view's number, then the checkout, its view and the event.
    pub fn register(
        &mut self,
        loc: &Location,
        project_id: &[u32],
        rule: &[u32],
    ) -> Result2<Checkout, S::Error> {
        let kid = self
            .store
            .next_membership_id("checkouts", "K")
            .map_err(super::Error::Store)?;
        let stamp = self.stamp()?;
        let token = match &loc.token {
            Some(t) if !t.is_empty() => Some(t.clone()),
            _ => loc.token_written.clone(),
        };
        let at = match &loc.root {
            Some(r) if !r.is_empty() => r,
            _ => &loc.cwd,
        };
        let order = self
            .store
            .next_membership_number("checkout_views", "seen_order")
            .map_err(super::Error::Store)?;
        let view = View {
            checkout: kid.clone(),
            machine: loc.machine.clone(),
            path: norm(at),
            first_seen: stamp.clone(),
            last_seen: stamp.clone(),
            order,
        };
        let no_token = token.as_ref().is_none_or(Vec::is_empty);
        let keep_manifest = (!loc.born() && no_token) || loc.kind == Some(Kind::Folder);
        let k = Checkout {
            id: kid.clone(),
            project: project_id.to_vec(),
            machine: loc.machine.clone(),
            kind: cps(loc.kind.map_or(PRIMARY, Kind::as_str)),
            object_key: loc.object_key.clone(),
            common_object_key: loc.common_object_key.clone(),
            git_dir: loc.git_dir.clone(),
            token,
            token_status: loc.token_status.clone(),
            stamps: loc.stamps.clone(),
            last_head: loc.head.clone(),
            branch: loc.branch.clone(),
            remotes: loc.remote_keys.iter().cloned().collect(),
            manifest: if keep_manifest {
                loc.manifest.clone()
            } else {
                None
            },
            legacy: false,
            retired: false,
            registered: stamp,
            views: vec![view.clone()],
            sessions: Vec::new(),
            replaced_by: None,
            ticks: loc.ticks,
        };
        let k = self.put_checkout(k)?;
        self.put_view(&view)?;
        self.event(
            "checkout_registered",
            vec![
                ("checkout", s(&kid)),
                ("project", s(project_id)),
                ("path", s(&view.path)),
                ("machine", s(&loc.machine)),
                ("checkout_kind", s(&k.kind)),
                ("rule", s(rule)),
                ("token", s(&loc.token_status)),
            ],
        )?;
        Ok(k)
    }
}
