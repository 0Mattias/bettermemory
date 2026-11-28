//! A record's provenance, unit membership-4: `Membership.why`
//! (`membership.py` 1743-1814), the record's home and every membership log
//! row that decided it, oldest first, and `_decides` (1846-1889), the test
//! of one row.
//!
//! The store is read through [`ScopeReader`] (for `home`) and
//! [`MembershipLog`] (for `_folded` and the log itself); nothing is
//! written. A log row's payload that `json.loads` refuses with ValueError
//! is passed over, and RecursionError propagates, as the reference's
//! `except ValueError` lets it. The log is read whole, as
//! [`MembershipLog`] answers a list, where the reference tests each row as
//! the store's generator yields it: the two differ only in which error
//! comes first when a row raises in `_decides` and a later row cannot be
//! read (the store's error here, TypeError there). That is a gap of the
//! trait, closed when the store's `MembershipLog` lands.
//!
//! `in` over a set of str (the proposals, the projects) hashes its value
//! first, so a list or a dict raises TypeError there; `in` over the
//! checkouts' list compares and never raises. A str equals only a str, so
//! a value of any other type is never in either.

use std::collections::{BTreeSet, HashSet};

use super::queries::ScopeReader;
use super::shared_heal::MembershipLog;
use super::vocab::GLOBAL;
use super::{column, key_error, raise, str_of, Error, Membership, Query, Result2};
use crate::events::truthy;
use crate::json;
use crate::log::LogRow;
use crate::membership_lines::named;
use crate::membership_records::outcomes::Why;
use crate::membership_records::records::IMPORT;
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::response_trust::hashable;
use crate::store::caller::Val;
use crate::unicode::cps;

/// The project kinds `_decides` reads by `project`, `into` and, for
/// `project_unmerged`, `was_into`.
pub const PROJECT_KINDS: [&str; 6] = [
    "project_minted",
    "project_merged",
    "project_absorbed",
    "project_unmerged",
    "project_file_bound",
    "project_reparented",
];

/// The kinds `_decides` never keeps.
pub const NEVER_KINDS: [&str; 3] = ["recall_suppressed", "witness_put", "witness_released"];

/// `value in s` for a set of str: TypeError for an unhashable value, else
/// whether it is a str the set holds. A missing key reads as None.
fn in_set<E>(value: Option<&PyValue>, s: &HashSet<PyStr>) -> Result2<bool, E> {
    match value {
        Some(v) if !hashable(v) => raise("TypeError", "unhashable type".into()),
        Some(PyValue::Str(t)) => Ok(s.contains(t)),
        _ => Ok(false),
    }
}

/// `value in checkouts` for the sorted list of str: compares, never raises.
fn in_list(value: Option<&PyValue>, list: &[PyStr]) -> bool {
    matches!(value, Some(PyValue::Str(t)) if list.binary_search(t).is_ok())
}

/// `value == text`, a missing key read as None.
fn equals(value: Option<&PyValue>, text: Option<&[u32]>) -> bool {
    match (value, text) {
        (None | Some(PyValue::None), None) => true,
        (Some(PyValue::Str(v)), Some(t)) => v == t,
        _ => false,
    }
}

/// `_decides(kind, payload, record_id, checkouts, proposals, projects)`:
/// whether a membership log row is one `why` lists for a record.
///
/// Precondition: `checkouts` is sorted in code point order, as `why`
/// builds it (`sorted(folded | {cid})`). `in` over it is a bisection, so
/// an unsorted list may answer false for a checkout it holds where the
/// reference's `in` answers true. A debug build asserts the order.
pub fn decides<E>(
    kind: &str,
    payload: &PyValue,
    record_id: &[u32],
    checkouts: &[PyStr],
    proposals: &HashSet<PyStr>,
    projects: &HashSet<PyStr>,
) -> Result2<bool, E> {
    debug_assert!(
        checkouts.is_sorted(),
        "decides: the checkouts are not sorted in code point order"
    );
    decides_sorted(kind, payload, record_id, checkouts, proposals, projects)
}

/// [`decides`] without its assertion: `why` calls it once per log row
/// with a list sorted by construction, where checking the order on each
/// row would make a debug build quadratic in the folded checkouts.
fn decides_sorted<E>(
    kind: &str,
    payload: &PyValue,
    record_id: &[u32],
    checkouts: &[PyStr],
    proposals: &HashSet<PyStr>,
    projects: &HashSet<PyStr>,
) -> Result2<bool, E> {
    let body = kind
        .strip_suffix("_put")
        .and_then(|table| payload.get(table));
    if let Some(body @ PyValue::Dict(_)) = body {
        return match kind {
            "home_put" => Ok(equals(body.get("record_id"), Some(record_id))),
            "checkout_put" | "proposal_put" => {
                let id = body.get("id");
                Ok(in_list(id, checkouts) || in_set(id, proposals)?)
            }
            "checkout_view_put" => Ok(in_list(body.get("checkout"), checkouts)),
            "project_put" => in_set(body.get("id"), projects),
            "project_file_put" => in_set(body.get("project"), projects),
            _ => Ok(false),
        };
    }
    if NEVER_KINDS.contains(&kind) {
        return Ok(false);
    }
    if kind == "import_homed" {
        return Ok(equals(payload.get("record"), Some(record_id)));
    }
    if PROJECT_KINDS.contains(&kind) {
        return Ok(in_set(payload.get("project"), projects)?
            || in_set(payload.get("into"), projects)?
            || (kind == "project_unmerged" && in_set(payload.get("was_into"), projects)?));
    }
    if in_list(payload.get("checkout"), checkouts) {
        return Ok(true);
    }
    in_set(payload.get("proposal"), proposals)
}

fn push(out: &mut PyStr, text: &str) {
    out.extend(text.chars().map(u32::from));
}

fn is_global(id: &[u32]) -> bool {
    id.iter().copied().eq(GLOBAL.chars().map(u32::from))
}

/// `json.loads(row.payload)`: None where it raises ValueError.
fn loaded<E>(row: &LogRow) -> Result2<Option<PyValue>, E> {
    match json::loads(&cps(&row.payload)) {
        Ok(p) => Ok(Some(p)),
        Err(PyError::ValueError) => Ok(None),
        Err(e) => raise(e.name(), String::new()),
    }
}

/// `str(value)` of a checkout that is a str or None.
fn spelled_id(id: Option<&[u32]>) -> PyStr {
    id.map_or_else(|| cps("None"), <[u32]>::to_vec)
}

impl<S: ScopeReader + MembershipLog> Membership<S> {
    /// `why(record_id)` (1743-1814): the record's home, the log rows that
    /// decided it in log order, and the sentence `project why` prints.
    ///
    /// A home whose checkout is neither a str nor None declines
    /// (NotPorted) where the reference goes on with the value: `Why`
    /// holds a str there. The homes table's `checkout` is a TEXT column,
    /// which turns an integer or a real into text, so through the store
    /// only a BLOB could reach it, and no writer puts one.
    pub fn why(&mut self, record_id: &[u32]) -> Result2<Why, S::Error> {
        self.forget_projects();
        let Some(home) = self.home(record_id)? else {
            let mut text = record_id.to_vec();
            push(&mut text, ": no home recorded");
            return Ok(Why {
                record_id: record_id.to_vec(),
                project: None,
                checkout: None,
                rule: None,
                rows: Vec::new(),
                text,
            });
        };
        let cid: Option<PyStr> = match &home.checkout {
            PyValue::None => None,
            PyValue::Str(c) => Some(c.clone()),
            _ => return raise("NotPorted", "a home's checkout that is not a str".into()),
        };
        let Val::Text(project) = home.home.project.clone() else {
            return raise("NotPorted", "a home's project that is not a str".into());
        };
        let mut projects: HashSet<PyStr> = home.home.chain.iter().cloned().collect();
        projects.insert(home.recorded_project.clone());
        projects.insert(project.clone());
        let mut members = Vec::new();
        for p in self.all_projects()?.ids().to_vec() {
            if self
                .canonical(Some(&p))?
                .is_some_and(|c| projects.contains(&c))
            {
                members.push(p);
            }
        }
        projects.extend(members);
        let folded: BTreeSet<PyStr> = match &cid {
            Some(c) => self.folded(c)?,
            None => BTreeSet::new(),
        };
        let checkouts: Vec<PyStr> = match &cid {
            Some(c) => {
                let mut all = folded.clone();
                all.insert(c.clone());
                all.into_iter().collect()
            }
            None => Vec::new(),
        };
        let proposals = self.proposals(&checkouts, &projects)?;
        let mut rows: Vec<(LogRow, PyValue)> = Vec::new();
        for row in self
            .store()
            .membership_log(None, None)
            .map_err(Error::Store)?
        {
            let Some(payload @ PyValue::Dict(_)) = loaded(&row)? else {
                continue;
            };
            if decides_sorted(
                &row.kind, &payload, record_id, &checkouts, &proposals, &projects,
            )? {
                rows.push((row, payload));
            }
        }
        let text = if is_global(&project) {
            let mut t = record_id.to_vec();
            push(
                &mut t,
                ": global (written outside any repository and any project file); rule ",
            );
            t.extend(&home.rule);
            t
        } else {
            self.text(
                record_id,
                &project,
                &home.rule,
                cid.as_deref(),
                &folded,
                &rows,
            )?
        };
        Ok(Why {
            record_id: record_id.to_vec(),
            project: Some(project),
            checkout: cid,
            rule: Some(home.rule),
            rows: rows.into_iter().map(|(row, _)| row).collect(),
            text,
        })
    }

    /// The ids of the questions asked at `checkouts`, and of the import
    /// questions about one of `projects` (design 8.7), in that order.
    fn proposals(
        &mut self,
        checkouts: &[PyStr],
        projects: &HashSet<PyStr>,
    ) -> Result2<HashSet<PyStr>, S::Error> {
        let at = self.read(
            "proposals",
            &Query {
                column_in: Some((
                    cps("checkout"),
                    checkouts.iter().cloned().map(PyValue::Str).collect(),
                )),
                ..Query::default()
            },
        )?;
        let mut out = HashSet::new();
        for r in &at {
            out.insert(str_of(column(r, "id")?)?);
        }
        let imports = self.read(
            "proposals",
            &Query {
                where_: Some(vec![(cps("kind"), PyValue::str(IMPORT))]),
                ..Query::default()
            },
        )?;
        let mut asked = Vec::new();
        for r in &imports {
            let evidence = column(r, "evidence_json")?;
            if matches!(evidence, PyValue::Dict(_))
                && in_set(evidence.get("imported_project"), projects)?
            {
                asked.push(str_of(column(r, "id")?)?);
            }
        }
        out.extend(asked);
        Ok(out)
    }

    /// The sentence of a record homed in a project: where it was written,
    /// the rule, the checkout's registration and the count of rows.
    fn text(
        &mut self,
        record_id: &[u32],
        project: &[u32],
        rule: &[u32],
        cid: Option<&[u32]>,
        folded: &BTreeSet<PyStr>,
        rows: &[(LogRow, PyValue)],
    ) -> Result2<PyStr, S::Error> {
        let registered = rows
            .iter()
            .find(|(row, p)| row.kind == "checkout_registered" && equals(p.get("checkout"), cid))
            .map(|(_, p)| p.get("rule").cloned().unwrap_or(PyValue::None));
        // `p["home"].get("checkout")` of every home_put kept: a home_put
        // kept by the checkout or the question its payload names, its
        // `home` missing or no mapping, raises here as in the reference.
        let mut first: Option<PyValue> = None;
        for (row, p) in rows {
            if row.kind != "home_put" {
                continue;
            }
            let at = match p.get("home") {
                None => return key_error(&cps("home")),
                Some(h @ PyValue::Dict(_)) => h.get("checkout").cloned().unwrap_or(PyValue::None),
                Some(_) => return raise("AttributeError", "no attribute 'get'".into()),
            };
            first.get_or_insert(at);
        }
        let at = match first {
            Some(v) if truthy(&v) => v,
            _ => cid.map_or(PyValue::None, |c| PyValue::Str(c.to_vec())),
        };
        let at = if equals(Some(&at), cid) {
            at
        } else {
            if !hashable(&at) {
                return raise("TypeError", "unhashable type".into());
            }
            let retired = matches!(&at, PyValue::Str(a) if folded.contains(a));
            let mut said = str_of(&at)?;
            push(
                &mut said,
                if retired {
                    " (retired into "
                } else {
                    " (homed now at "
                },
            );
            said.extend(spelled_id(cid));
            push(&mut said, ")");
            PyValue::Str(said)
        };
        let name = self.project(project)?.name;
        let mut t = record_id.to_vec();
        push(&mut t, ": project ");
        t.extend(named(&name, project));
        if truthy(&at) {
            push(&mut t, ", written in checkout ");
            t.extend(str_of(&at)?);
        } else {
            push(&mut t, ", no checkout recorded");
        }
        push(&mut t, "; rule ");
        t.extend(rule);
        if let Some(r) = registered.filter(truthy) {
            push(&mut t, "; the checkout joined by ");
            t.extend(str_of(&r)?);
        }
        push(&mut t, &format!("; {} rows", rows.len()));
        Ok(t)
    }
}
