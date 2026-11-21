//! The projects of the model (`membership.py` 425-578): the rows read
//! once a call, their merges followed, the members index, the names a
//! record's scopes give, and a project put or minted.

use std::collections::{BTreeMap, BTreeSet};

use super::vocab::{GLOBAL, PROJECT_SCOPE};
use super::{
    column, key_error, str_of, Error, Membership, MembershipStore, ProjectMap, Query, Result2,
};
use crate::membership_lines::named as line_named;
use crate::membership_records::records::Project;
use crate::membership_rows::{project_from, project_row};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::{casefold, cps};

fn is_global(id: &[u32]) -> bool {
    id.iter().copied().eq(GLOBAL.chars().map(u32::from))
}

impl<S: MembershipStore> Membership<S> {
    /// `_all_projects()` (425-432): read once until the next call.
    pub fn all_projects(&mut self) -> Result2<&mut ProjectMap, S::Error> {
        if self.projects.is_none() {
            let mut map = ProjectMap::default();
            for r in self.read("projects", &Query::default())? {
                let id = str_of(column(&r, "id")?)?;
                let p = project_from(&r).map_err(Error::Raised)?;
                map.insert(id, p);
            }
            self.projects = Some(map);
            self.generation += 1;
        }
        Ok(self.projects.as_mut().expect("read above"))
    }

    /// `project(project_id)` (433-439): as recorded; KeyError when absent.
    pub fn project(&mut self, project_id: &[u32]) -> Result2<Project, S::Error> {
        match self.all_projects()?.get(project_id) {
            Some(p) => Ok(p.clone()),
            None => key_error(project_id),
        }
    }

    /// `projects()` (440-446): every project not merged, by id.
    pub fn projects(&mut self) -> Result2<Vec<Project>, S::Error> {
        let mut out: Vec<Project> = self
            .all_projects()?
            .values()
            .filter(|p| p.merged_into.is_none())
            .cloned()
            .collect();
        out.sort_by(|a, b| a.id.cmp(&b.id));
        Ok(out)
    }

    /// `canonical(project_id)` (447-460): the merges followed; a cycle
    /// stops at the first id seen twice.
    pub fn canonical(&mut self, project_id: Option<&[u32]>) -> Result2<Option<PyStr>, S::Error> {
        let projects = self.all_projects()?;
        let mut seen: BTreeSet<PyStr> = BTreeSet::new();
        let mut pid = project_id.map(<[u32]>::to_vec);
        while let Some(id) = pid.clone() {
            if is_global(&id) {
                break;
            }
            let Some(p) = projects.get(&id) else { break };
            match &p.merged_into {
                Some(next) if !seen.contains(&id) => {
                    seen.insert(id);
                    pid = Some(next.clone());
                }
                _ => return Ok(Some(id)),
            }
        }
        Ok(pid)
    }

    /// `_canon(project_id)` (461-464).
    pub fn canon(&mut self, project_id: &[u32]) -> Result2<PyStr, S::Error> {
        Ok(self
            .canonical(Some(project_id))?
            .unwrap_or_else(|| project_id.to_vec()))
    }

    /// `named(scopes)` (465-483): the projects (merges followed) whose
    /// name, case-folded, a `projects:<name>` scope gives.
    pub fn named(&mut self, scopes: &[PyStr]) -> Result2<BTreeSet<PyStr>, S::Error> {
        let prefix = cps(PROJECT_SCOPE);
        let wanted: BTreeSet<PyStr> = scopes
            .iter()
            .filter(|s| s.starts_with(&prefix) && s.len() > prefix.len())
            .map(|s| casefold(&s[prefix.len()..]))
            .collect();
        if wanted.is_empty() {
            return Ok(BTreeSet::new());
        }
        let hits: Vec<PyStr> = self
            .all_projects()?
            .values()
            .filter(|p| wanted.contains(&casefold(&p.name)))
            .map(|p| p.id.clone())
            .collect();
        let mut out = BTreeSet::new();
        for id in hits {
            out.insert(self.canon(&id)?);
        }
        Ok(out)
    }

    /// `chain_of(project_id)` (484-493): the project and its parents.
    pub fn chain_of(&mut self, project_id: Option<&[u32]>) -> Result2<Vec<PyStr>, S::Error> {
        let mut out: Vec<PyStr> = Vec::new();
        let mut pid = self.canonical(project_id)?;
        while let Some(id) = pid {
            if is_global(&id) || out.contains(&id) {
                break;
            }
            let Some(parent) = self.all_projects()?.get(&id).map(|p| p.parent.clone()) else {
                break;
            };
            out.push(id);
            pid = self.canonical(parent.as_deref())?;
        }
        Ok(out)
    }

    /// `_members(project_id)` (494-498).
    pub fn members(&mut self, project_id: &[u32]) -> Result2<Vec<PyStr>, S::Error> {
        let canon = self.canon(project_id)?;
        Ok(self
            .members_index()?
            .get(&canon)
            .cloned()
            .unwrap_or_default())
    }

    /// `_members_index()` (499-512): kept while the projects are the ones
    /// it was built from.
    pub fn members_index(&mut self) -> Result2<&BTreeMap<PyStr, Vec<PyStr>>, S::Error> {
        let ids = self.all_projects()?.ids().to_vec();
        let fresh = matches!(&self.index, Some((g, _)) if *g == self.generation());
        if !fresh {
            let mut groups: BTreeMap<PyStr, Vec<PyStr>> = BTreeMap::new();
            for pid in ids {
                let canon = self.canon(&pid)?;
                groups.entry(canon).or_default().push(pid);
            }
            for v in groups.values_mut() {
                v.sort();
            }
            self.index = Some((self.generation(), groups));
        }
        Ok(&self.index.as_ref().expect("built above").1)
    }

    /// `_name(project_id)` (535-538).
    pub fn name(&mut self, project_id: &[u32]) -> Result2<PyStr, S::Error> {
        let pid = self.canon(project_id)?;
        let p = self.project(&pid)?;
        Ok(line_named(&p.name, &pid))
    }

    /// `_put_project(p)` (539-543).
    pub fn put_project(&mut self, p: Project) -> Result2<(), S::Error> {
        self.put("project", &project_row(&p))?;
        self.all_projects()?.insert(p.id.clone(), p);
        self.index = None;
        Ok(())
    }

    /// `_mint_project(name, ...)` (544-578).
    #[allow(clippy::too_many_arguments)]
    pub fn mint_project(
        &mut self,
        name: &[u32],
        reason: &[u32],
        provisional: Option<PyStr>,
        parent: Option<PyStr>,
        tenant: Option<PyStr>,
        folder: bool,
        boundary: bool,
    ) -> Result2<Project, S::Error> {
        let pid = self
            .store
            .next_membership_id("projects", "P")
            .map_err(Error::Store)?;
        let p = Project {
            id: pid.clone(),
            name: name.to_vec(),
            merged_into: None,
            provisional: provisional.clone(),
            parent: parent.clone(),
            tenant: if boundary { Some(pid) } else { tenant },
            folder,
            boundary,
            minted: self.stamp()?,
        };
        self.put_project(p.clone())?;
        let opt = |v: &Option<PyStr>| v.clone().map_or(PyValue::None, PyValue::Str);
        self.event(
            "project_minted",
            vec![
                ("project", PyValue::Str(p.id.clone())),
                ("name", PyValue::Str(p.name.clone())),
                ("reason", PyValue::Str(reason.to_vec())),
                ("provisional", opt(&provisional)),
                ("parent", opt(&parent)),
                ("tenant", opt(&p.tenant)),
            ],
        )?;
        Ok(p)
    }
}
