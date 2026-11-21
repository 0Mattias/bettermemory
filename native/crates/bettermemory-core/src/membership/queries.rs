//! The read side of the membership model (`membership_queries.py` 69-277
//! and `membership.py` 1700-1729), unit mqueries: a record's home as
//! `relation` reads it (`home`, `homes`, `_home_from`), the prompt
//! recall's admission (`admit_recall`), and an open question as it is
//! shown (`restated`, `_written_afresh`, `_restatement`,
//! `filed_elsewhere`).
//!
//! Two effects are arguments. The declared scopes of global records are
//! read through [`ScopeReader`], a trait of the store beside
//! [`MembershipStore`]. The root project file of a question's checkout is
//! read through a [`ProjectFileId`] closure given the view's path as
//! stored: it joins `bettermemory.toml` as `Path` does, answers the id of a
//! well-formed project file, None for a missing file or one that names no
//! project, an error of kind `InvalidData` for what `project_file.parse`
//! raises as ValueError, and any other error for an OSError.

use std::collections::HashMap;
use std::io;

use super::{column, raise, str_of, Error, Membership, MembershipStore, Query, Result2};
use crate::membership_lines::{line_import, line_legacy, line_restated, line_unresolved};
use crate::membership_lines::{ImportCandidate, Named, Raised};
use crate::membership_records::paths::basename;
use crate::membership_records::records::{
    Proposal, FILE_CONTRADICTION, FIRST_SIGHT, IMPORT, LEGACY, OPEN, SUBPROJECT_CONTRADICTION,
    UNRESOLVED,
};
use crate::pyvalue::{py_int, repr_str, PyStr, PyValue};
use crate::store::caller::{
    admitted, relation, Home, Resolution, Surface, Val, GLOBAL, UNRESOLVED_STATES,
};
use crate::unicode::{cps, to_string};

/// `membership_records.UNRESOLVED_LOCATION`.
pub const UNRESOLVED_LOCATION: &str = "unresolved_location";
/// `membership_records.LEGACY_NO_LIVE_CHECKOUT`.
pub const LEGACY_NO_LIVE_CHECKOUT: &str = "legacy_no_live_checkout";
/// `_QUOTED_LINE` (138): how a line that quotes its path begins.
pub const QUOTED_LINE: &str = "bettermemory: \"";
/// The first sight's old separate answer (170-172).
const OLD_SEPARATE: &str = "separate: bettermemory project init --new (a fresh project file)";

/// `Store.declared_scopes(record_ids)`: `{id: scopes}` for the memories
/// and episodes asked, as written; an id that is neither is absent.
pub trait ScopeReader: MembershipStore {
    fn declared_scopes(
        &self,
        record_ids: &[PyStr],
    ) -> Result<HashMap<PyStr, Vec<PyStr>>, Self::Error>;
}

/// The root project file of a view's path: see the module text.
pub type ProjectFileId<'a> = &'a dyn Fn(&[u32]) -> Result<Option<PyStr>, io::Error>;

/// `membership_records.Home` as `_home_from` builds it: the row's own
/// columns, and the part `relation` reads (`store::caller::Home`).
#[derive(Clone, Debug)]
pub struct HomeRow {
    pub record_id: PyStr,
    pub kind: PyStr,
    pub checkout: PyValue,
    pub rule: PyStr,
    pub decided: PyStr,
    pub recorded_project: PyStr,
    pub home: Home,
}

/// `RecallAdmission`: the hits delivered, and each suppressed with why.
#[derive(Clone, Debug, PartialEq)]
pub struct RecallAdmission {
    pub delivered: Vec<PyStr>,
    pub suppressed: Vec<(PyStr, PyStr)>,
}

fn is(text: &[u32], word: &str) -> bool {
    text.len() == word.len() && word.chars().map(u32::from).eq(text.iter().copied())
}

/// `except (classes...)`: the raise of a listed class is None, anything
/// else passes through.
fn caught<T, E>(got: Result2<T, E>, classes: &[&str]) -> Result2<Option<T>, E> {
    match got {
        Ok(v) => Ok(Some(v)),
        Err(Error::Raised(r)) if classes.contains(&r.class) => Ok(None),
        Err(e) => Err(e),
    }
}

/// `ev[key]` on the evidence: KeyError when missing, TypeError when the
/// evidence is no mapping.
fn item<'a, E>(ev: &'a PyValue, key: &str) -> Result2<&'a PyValue, E> {
    match ev {
        PyValue::Dict(_) => column(ev, key),
        _ => raise("TypeError", String::new()),
    }
}

/// `ev.get(key)`: AttributeError when the evidence is no mapping.
fn get<'a, E>(ev: &'a PyValue, key: &str) -> Result2<Option<&'a PyValue>, E> {
    match ev {
        PyValue::Dict(_) => Ok(ev.get(key)),
        _ => raise("AttributeError", String::new()),
    }
}

/// `int(value)`, within the lines' i64; past it the reference answers a
/// line this port does not write (NotPorted).
fn int<E>(value: &PyValue) -> Result2<i64, E> {
    match &py_int(value) {
        Ok(PyValue::Int(text)) => match text.parse::<i64>() {
            Ok(n) => Ok(n),
            Err(_) => raise("NotPorted", "an int past i64".into()),
        },
        Ok(_) => raise("TypeError", String::new()),
        Err(e) => raise(e.name(), String::new()),
    }
}

/// `int(ev.get(key) or 0)`.
fn int_or_zero<E>(value: Option<&PyValue>) -> Result2<i64, E> {
    let falsy = match value {
        None | Some(PyValue::None) | Some(PyValue::Bool(false)) => true,
        Some(PyValue::Int(t)) => t == "0",
        Some(PyValue::Float(f)) => *f == 0.0,
        Some(PyValue::Str(s) | PyValue::Path(s)) => s.is_empty(),
        Some(PyValue::List(v) | PyValue::Tuple(v)) => v.is_empty(),
        Some(PyValue::Dict(v)) => v.is_empty(),
        Some(_) => false,
    };
    match value {
        Some(v) if !falsy => int(v),
        _ => Ok(0),
    }
}

/// `'+'.join(sorted(codes))` takes an iterable of str: a list of str, a
/// str's characters or a dict's keys; anything else is a TypeError.
fn codes_of<E>(codes: &PyValue) -> Result2<Vec<PyStr>, E> {
    let strs = |items: Vec<&PyValue>| -> Result2<Vec<PyStr>, E> {
        items
            .into_iter()
            .map(|v| match v {
                PyValue::Str(s) => Ok(s.clone()),
                _ => raise("TypeError", String::new()),
            })
            .collect()
    };
    match codes {
        PyValue::List(v) | PyValue::Tuple(v) => strs(v.iter().collect()),
        PyValue::Dict(v) => strs(v.iter().map(|(k, _)| k).collect()),
        PyValue::Str(s) => Ok(s.iter().map(|&c| vec![c]).collect()),
        _ => raise("TypeError", String::new()),
    }
}

impl<S: ScopeReader> Membership<S> {
    /// `home(record_id)` (69-72).
    pub fn home(&mut self, record_id: &[u32]) -> Result2<Option<HomeRow>, S::Error> {
        let mut all = self.homes(&[record_id.to_vec()])?;
        let at = all.iter().position(|(k, _)| k == record_id);
        Ok(at.map(|i| all.swap_remove(i).1))
    }

    /// `homes(record_ids)` (75-86): one read of the rows, one of the
    /// global ones' scopes, in the order of a dict keyed by record id.
    pub fn homes(&mut self, record_ids: &[PyStr]) -> Result2<Vec<(PyStr, HomeRow)>, S::Error> {
        self.forget_projects();
        let ids = record_ids.iter().map(|r| PyValue::Str(r.clone())).collect();
        let query = Query {
            column_in: Some((cps("record_id"), ids)),
            ..Query::default()
        };
        let rows = self.read("homes", &query)?;
        let mut global = Vec::new();
        for r in &rows {
            if is(&str_of(column(r, "project")?)?, GLOBAL) {
                global.push(str_of(column(r, "record_id")?)?);
            }
        }
        let scopes = self.store.declared_scopes(&global).map_err(Error::Store)?;
        let mut out: Vec<(PyStr, HomeRow)> = Vec::new();
        let mut at: std::collections::HashMap<PyStr, usize> = std::collections::HashMap::new();
        for r in &rows {
            let key = str_of(column(r, "record_id")?)?;
            let home = self.home_from(r, scopes.get(&key).map_or(&[][..], Vec::as_slice))?;
            match at.get(&key) {
                Some(&i) => out[i].1 = home,
                None => {
                    at.insert(key.clone(), out.len());
                    out.push((key, home));
                }
            }
        }
        Ok(out)
    }

    /// `admit_recall(caller, hits, admission=...)` (92-131), in one call.
    pub fn admit_recall(
        &mut self,
        caller: &Resolution,
        hits: &[PyStr],
        admission: &[u32],
    ) -> Result2<RecallAdmission, S::Error> {
        self.call(|m| {
            let hit_homes = m.homes(hits)?;
            let by_id: std::collections::HashMap<&PyStr, &HomeRow> =
                hit_homes.iter().map(|(k, h)| (k, h)).collect();
            let mut delivered = Vec::new();
            let mut suppressed: Vec<(PyStr, PyStr)> = Vec::new();
            for rid in hits {
                let Some(h) = by_id.get(rid) else {
                    suppressed.push((rid.clone(), cps("no_home")));
                    continue;
                };
                let Some(surface) = to_string(admission).as_deref().and_then(Surface::parse) else {
                    let shown = to_string(&repr_str(admission)).unwrap_or_default();
                    return raise("ValueError", format!("unknown surface {shown}"));
                };
                if admitted(surface, &h.home, caller) {
                    delivered.push(rid.clone());
                } else if h.home.unresolved {
                    suppressed.push((rid.clone(), cps(UNRESOLVED)));
                } else {
                    suppressed.push((rid.clone(), cps(relation(&h.home, caller).name())));
                }
            }
            if !suppressed.is_empty() && delivered.is_empty() {
                let opt = |v: &Option<PyStr>| v.clone().map_or(PyValue::None, PyValue::Str);
                let listed = suppressed
                    .iter()
                    .map(|(rid, why)| {
                        PyValue::Dict(vec![
                            (PyValue::str("id"), PyValue::Str(rid.clone())),
                            (PyValue::str("reason"), PyValue::Str(why.clone())),
                        ])
                    })
                    .collect();
                m.event(
                    "recall_suppressed",
                    vec![
                        ("surface", PyValue::str(Surface::PromptRecall.name())),
                        ("admission", PyValue::Str(admission.to_vec())),
                        ("checkout", opt(&caller.checkout)),
                        ("caller_rule", PyValue::Str(caller.rule.clone())),
                        ("suppressed", PyValue::List(listed)),
                    ],
                )?;
            }
            Ok(RecallAdmission {
                delivered,
                suppressed,
            })
        })
    }
}

impl<S: MembershipStore> Membership<S> {
    /// `_home_from(row, scopes)` (`membership.py` 1700-1729).
    pub fn home_from(&mut self, row: &PyValue, scopes: &[PyStr]) -> Result2<HomeRow, S::Error> {
        let recorded = str_of(column(row, "project")?)?;
        let mut chain = Vec::new();
        let mut tenant = Val::Null;
        let mut unresolved = false;
        let mut named = Vec::new();
        let project = if is(&recorded, GLOBAL) {
            named = self.named(scopes)?.into_iter().map(Val::Text).collect();
            recorded.clone()
        } else {
            let project = self.canon(&recorded)?;
            chain = self.chain_of(Some(&project))?;
            if let Some(known) = self.all_projects()?.get(&project) {
                tenant = known.tenant.clone().map_or(Val::Null, Val::Text);
                unresolved = known
                    .provisional
                    .as_ref()
                    .is_some_and(|p| UNRESOLVED_STATES.iter().any(|s| is(p, s)));
            }
            project
        };
        Ok(HomeRow {
            record_id: str_of(column(row, "record_id")?)?,
            kind: str_of(column(row, "kind")?)?,
            checkout: column(row, "checkout")?.clone(),
            rule: str_of(column(row, "rule")?)?,
            decided: str_of(column(row, "decided")?)?,
            recorded_project: recorded,
            home: Home {
                project: Val::Text(project),
                chain,
                tenant,
                unresolved,
                named,
            },
        })
    }

    /// `canonical(c) or c`.
    fn canon_or(&mut self, c: &[u32]) -> Result2<PyStr, S::Error> {
        Ok(self
            .canonical(Some(c))?
            .filter(|k| !k.is_empty())
            .unwrap_or_else(|| c.to_vec()))
    }

    /// `restated(pr)` (141-184): an open question as it is shown.
    pub fn restated(&mut self, pr: &Proposal, file: ProjectFileId) -> Result2<Proposal, S::Error> {
        if !is(&pr.state, OPEN) {
            return Ok(pr.clone());
        }
        let mut line = None;
        if is(&pr.kind, IMPORT) || is(&pr.kind, LEGACY) {
            let afresh = self.written_afresh(pr);
            let classes = [
                "KeyError",
                "ValueError",
                "TypeError",
                "IndexError",
                "AttributeError",
            ];
            line = caught(afresh, &classes)?.flatten();
        } else {
            let old_first = is(&pr.kind, FIRST_SIGHT) && pr.line.ends_with(&cps(OLD_SEPARATE));
            let restate = !pr.line.starts_with(&cps(QUOTED_LINE))
                || is(&pr.kind, FILE_CONTRADICTION)
                || is(&pr.kind, SUBPROJECT_CONTRADICTION)
                || old_first;
            if !restate {
                return Ok(pr.clone());
            }
        }
        let line = match line {
            Some(l) => l,
            None => self.restatement(pr, None, file)?,
        };
        Ok(Proposal { line, ..pr.clone() })
    }

    /// `_written_afresh(pr)` (187-220): an import or legacy question's
    /// line by its own writer; None for a reason it does not hold.
    pub fn written_afresh(&mut self, pr: &Proposal) -> Result2<Option<PyStr>, S::Error> {
        let ev = &pr.evidence;
        let Some(cand) = pr.candidates.first() else {
            return raise("IndexError", "tuple index out of range".into());
        };
        let q = Some(pr.id.as_slice());
        if is(&pr.kind, IMPORT) {
            let empty = PyValue::Dict(Vec::new());
            let codes = get(ev, "codes")?.unwrap_or(&empty);
            let codes = match codes {
                PyValue::Dict(items) => items
                    .iter()
                    .rev()
                    .find(|(k, _)| matches!(k, PyValue::Str(s) if s == cand))
                    .map_or(PyValue::List(Vec::new()), |(_, v)| v.clone()),
                _ => return raise("AttributeError", String::new()),
            };
            let imported = str_of(item(ev, "imported_project")?)?;
            let name = self.project(&imported)?.name;
            let cand_name = self.project(cand)?.name;
            let source = str_of(item(ev, "source")?)?;
            let candidates = [ImportCandidate {
                name: cand_name,
                id: cand.clone(),
                codes: codes_of(&codes)?,
            }];
            return Ok(Some(line_import(&name, &source, &candidates, q)));
        }
        if is(&pr.reason, UNRESOLVED_LOCATION) {
            let path = str_of(item(ev, "path")?)?;
            let memories = int(item(ev, "memories")?)?;
            let episodes = int(item(ev, "episodes")?)?;
            let name = self.project(cand)?.name;
            return Ok(Some(line_unresolved(
                &path, memories, episodes, &name, cand, q,
            )));
        }
        if is(&pr.reason, LEGACY_NO_LIVE_CHECKOUT) {
            let path = str_of(item(ev, "path")?)?;
            let base = basename(&path);
            let memories = int(item(ev, "memories")?)?;
            let episodes = int(item(ev, "episodes")?)?;
            let held = int_or_zero(get(ev, "held")?)?;
            let canon = self.canon_or(cand)?;
            let name = self.project(&canon)?.name;
            let recorded = int_or_zero(get(ev, "recorded")?)?;
            return Ok(Some(line_legacy(
                &base,
                memories,
                episodes,
                &path,
                held,
                &name,
                cand,
                Some(recorded),
                q,
            )));
        }
        Ok(None)
    }

    /// `_restatement(pr, at)` (223-252): the question from what its row
    /// stores alone.
    pub fn restatement(
        &mut self,
        pr: &Proposal,
        at: Option<&[u32]>,
        file: ProjectFileId,
    ) -> Result2<PyStr, S::Error> {
        let mut named = Vec::new();
        for c in &pr.candidates {
            let canon = self.canon_or(c)?;
            let name = caught(self.project(&canon), &["KeyError"])?.map(|p| p.name);
            named.push(Named { name, id: canon });
        }
        let mut filed = false;
        if at.is_none() && is(&pr.kind, FIRST_SIGHT) {
            let got = self.filed_elsewhere(pr, file);
            filed = caught(got, &["KeyError", "OSError", "ValueError"])?.unwrap_or(false);
        }
        let mut folder = None;
        if is(&pr.kind, SUBPROJECT_CONTRADICTION) {
            if let Some(PyValue::Str(d)) = get(&pr.evidence, "dir")? {
                folder = Some(d.clone()).filter(|d| !d.is_empty());
            }
        }
        Ok(line_restated(
            &pr.id,
            &pr.kind,
            &pr.reason,
            &named,
            filed,
            folder.as_deref(),
            at,
        ))
    }

    /// `filed_elsewhere(pr)` (255-277): whether the root project file of
    /// the question's checkout, on this machine, is bound to a project
    /// other than the checkout's own.
    pub fn filed_elsewhere(
        &mut self,
        pr: &Proposal,
        file: ProjectFileId,
    ) -> Result2<bool, S::Error> {
        let Some(kid) = &pr.checkout else {
            return Ok(false);
        };
        let k = self.checkout(kid)?;
        let Some(view) = k.views_on(&self.machine).last().copied() else {
            return Ok(false);
        };
        let found = match file(&view.path) {
            Ok(Some(id)) => id,
            Ok(None) => return Ok(false),
            Err(e) if e.kind() == io::ErrorKind::InvalidData => {
                return Err(Error::Raised(Raised {
                    class: "ValueError",
                    message: e.to_string(),
                }))
            }
            Err(_) => return Ok(false),
        };
        let Some(bound) = self.file_binding(&found)? else {
            return Ok(false);
        };
        Ok(bound != self.canon_or(&k.project)?)
    }
}
