//! The wire shapes of a search hit and of `memory_show`.
//!
//! `_response.py`'s `ResponseBuilder.render_hit` and `render_show`, with
//! `_SHOW_DEFAULTS`, `_NATIVE_SEP`, `_slashed`, `_path_states`,
//! `_compact_origin` and `_grouped_links`.
//!
//! Rows and the show's state are `dict`s of any values, read as Python
//! reads them (`response_trust::Row`); what the reference raises on a value
//! it cannot read comes back as the exception's class ([`Fault`]). The path
//! drift report is [`PathDriftLists`], the fields `_path_states` reads, and
//! `claims.parse_claim` is a caller's function.

use std::collections::{HashMap, HashSet};

use crate::events::truthy;
use crate::location_witness::py_eq;
use crate::pyvalue::{PyStr, PyValue};
use crate::records::Memory;
use crate::response_reason::{verdict_and_reason, Raised};
use crate::response_trust::{get, hashable, set, Row};
use crate::unicode::cps;

/// The class of what the reference raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    TypeError,
    KeyError,
    AttributeError,
    IndexError,
    /// `zip(..., strict=True)` over lists of different lengths.
    ValueError,
}

impl From<Raised> for Fault {
    fn from(r: Raised) -> Fault {
        match r {
            Raised::TypeError => Fault::TypeError,
            Raised::KeyError => Fault::KeyError,
            Raised::AttributeError => Fault::AttributeError,
            Raised::IndexError => Fault::IndexError,
        }
    }
}

type R<T> = Result<T, Fault>;

/// `_SHOW_DEFAULTS`: `memory_show` fields left out at these values.
pub const SHOW_DEFAULTS: [(&str, &str); 4] = [
    ("confidence", "medium"),
    ("source", "explicit-statement"),
    ("category", "fact"),
    ("provenance", "local"),
];

/// `_NATIVE_SEP`: the platform's separator when it is not `/`.
pub const NATIVE_SEP: Option<char> = if cfg!(windows) { Some('\\') } else { None };

/// The fields of `verify.PathDriftReport` that `render_show` reads.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PathDriftLists {
    pub missing: Vec<PyStr>,
    pub claim_anchored_missing: Vec<PyStr>,
    pub verified: Vec<PyStr>,
    pub expected_absent: Vec<PyStr>,
    pub present_but_attested_absent: Vec<PyStr>,
    pub unconfirmed: Vec<PyStr>,
    pub unconfirmed_reasons: Vec<PyStr>,
    pub dropped_as_route: Vec<PyStr>,
    pub checked: Vec<PyStr>,
    pub anchor: Option<PyStr>,
}

/// `parse_claim` raised `ValueError`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ClaimRejected;

/// A parsed claim's `kind` and `rel_path`, or `Err(ClaimRejected)`
/// where `parse_claim` raises `ValueError`.
pub type ParsedClaim = Result<(PyStr, PyStr), ClaimRejected>;

/// `_path_states`' answer: each path with its findings, and the claims
/// that name more than a path.
pub type PathStates = (Vec<(PyStr, PyStr)>, Vec<PyStr>);

/// `_slashed(path)` under the separator `sep` (`_NATIVE_SEP`).
pub fn slashed(path: &[u32], sep: Option<char>) -> PyStr {
    match sep {
        None => path.to_vec(),
        Some(s) => path
            .iter()
            .map(|&c| if c == s as u32 { '/' as u32 } else { c })
            .collect(),
    }
}

fn is_str(v: &PyValue, text: &str) -> bool {
    matches!(v, PyValue::Str(s) if *s == cps(text))
}

/// `value[key]` for a `str` key.
fn index<'a>(value: &'a PyValue, key: &str) -> R<&'a PyValue> {
    match value {
        PyValue::Dict(_) => value.get(key).ok_or(Fault::KeyError),
        _ => Err(Fault::TypeError),
    }
}

/// `row[key]`.
fn need<'a>(row: &'a [(PyValue, PyValue)], key: &str) -> R<&'a PyValue> {
    get(row, key).ok_or(Fault::KeyError)
}

/// `list(value)`: a `str` by code point, a `dict` by key.
fn iterate(value: &PyValue) -> R<Vec<PyValue>> {
    match value {
        PyValue::Str(s) => Ok(s.iter().map(|&c| PyValue::Str(vec![c])).collect()),
        PyValue::List(v) | PyValue::Tuple(v) => Ok(v.clone()),
        PyValue::Dict(d) => Ok(d.iter().map(|(k, _)| k.clone()).collect()),
        _ => Err(Fault::TypeError),
    }
}

/// `[entry["id"] for entry in value or ()]`.
fn ids_of(value: Option<&PyValue>) -> R<Vec<PyValue>> {
    let Some(v) = value.filter(|v| truthy(v)) else {
        return Ok(Vec::new());
    };
    iterate(v)?
        .iter()
        .map(|e| index(e, "id").cloned())
        .collect()
}

/// `ResponseBuilder.render_hit(row)`.
pub fn render_hit(row: &[(PyValue, PyValue)]) -> R<Row> {
    let contradicts = match get(row, "_contradicted_by") {
        Some(v) => iterate(v)?,
        None => ids_of(get(row, "contradicts"))?,
    };
    let (verdict, reason) = verdict_and_reason(row)?;
    let mut out: Row = Vec::new();
    set(&mut out, "id", need(row, "id")?.clone());
    set(&mut out, "snippet", need(row, "snippet")?.clone());
    set(&mut out, "relevance", need(row, "relevance")?.clone());
    set(&mut out, "staleness_verdict", verdict);
    if let Some(r) = reason {
        set(&mut out, "reason", PyValue::Str(r));
    }
    if get(row, "trust_unavailable").is_some_and(truthy) {
        set(&mut out, "trust_unavailable", PyValue::Bool(true));
    } else {
        let provenance = get(row, "provenance").cloned().unwrap_or(PyValue::None);
        if !is_str(&provenance, "local") {
            set(&mut out, "provenance", provenance);
        }
    }
    let superseded_by = ids_of(get(row, "superseded_by"))?;
    if !superseded_by.is_empty() {
        set(&mut out, "superseded_by", PyValue::List(superseded_by));
    }
    if !contradicts.is_empty() {
        set(&mut out, "contradicts", PyValue::List(contradicts));
    }
    if get(row, "held").is_some_and(truthy) {
        set(&mut out, "held", PyValue::Bool(true));
    }
    for key in ["body", "use_token"] {
        if let Some(v) = get(row, key) {
            set(&mut out, key, v.clone());
        }
    }
    Ok(out)
}

/// `ResponseBuilder.render_show(state, memory, path_drift)`, with
/// `parse_claim` the caller's and the platform's separator.
pub fn render_show(
    state: &[(PyValue, PyValue)],
    memory: &Memory,
    drift: &PathDriftLists,
    parse_claim: &mut dyn FnMut(&[u32]) -> ParsedClaim,
) -> R<Row> {
    render_show_sep(state, memory, drift, parse_claim, NATIVE_SEP)
}

/// [`render_show`] under the separator `sep` (`_NATIVE_SEP`).
pub fn render_show_sep(
    state: &[(PyValue, PyValue)],
    memory: &Memory,
    drift: &PathDriftLists,
    parse_claim: &mut dyn FnMut(&[u32]) -> ParsedClaim,
    sep: Option<char>,
) -> R<Row> {
    let (verdict, reason) = verdict_and_reason(state)?;
    let mut out: Row = Vec::new();
    set(&mut out, "id", need(state, "id")?.clone());
    set(&mut out, "staleness_verdict", verdict);
    if let Some(r) = reason {
        set(&mut out, "reason", PyValue::Str(r));
    }
    for key in ["use_token", "scopes", "body", "created"] {
        set(&mut out, key, need(state, key)?.clone());
    }
    let updated = need(state, "updated")?;
    if !py_eq(updated, need(state, "created")?) {
        set(&mut out, "updated", updated.clone());
    }
    if let Some(v) = get(state, "last_verified_at").filter(|v| !matches!(v, PyValue::None)) {
        set(&mut out, "last_verified_at", v.clone());
    }
    let verification = need(state, "verification")?;
    let PyValue::Dict(_) = verification else {
        return Err(Fault::AttributeError);
    };
    let mut shown: Row = Vec::new();
    for key in ["status", "age_days"] {
        if let Some(v) = verification
            .get(key)
            .filter(|v| !matches!(v, PyValue::None))
        {
            set(&mut shown, key, v.clone());
        }
    }
    set(&mut out, "verification", PyValue::Dict(shown));
    for (key, default) in SHOW_DEFAULTS {
        if let Some(v) = get(state, key) {
            if !matches!(v, PyValue::None) && !is_str(v, default) {
                set(&mut out, key, v.clone());
            }
        }
    }
    if get(state, "provenance").is_none_or(|v| matches!(v, PyValue::None))
        && !get(state, "trust_unavailable").is_some_and(truthy)
    {
        set(&mut out, "provenance", PyValue::None);
    }
    if let Some(origin) = compact_origin(get(state, "origin")) {
        set(&mut out, "origin", PyValue::Dict(origin));
    }
    if let Some(PyValue::Dict(actor)) = get(state, "actor") {
        let declared: Row = actor
            .iter()
            .filter(|(_, v)| !matches!(v, PyValue::None))
            .cloned()
            .collect();
        if !declared.is_empty() {
            set(&mut out, "actor", PyValue::Dict(declared));
        }
    }
    let (paths, claims) = path_states(memory, drift, parse_claim, sep)?;
    if !paths.is_empty() {
        let map = paths
            .into_iter()
            .map(|(k, v)| (PyValue::Str(k), PyValue::Str(v)))
            .collect();
        set(&mut out, "paths", PyValue::Dict(map));
        if let Some(anchor) = &drift.anchor {
            set(&mut out, "paths_read_at", PyValue::Str(anchor.clone()));
        }
    }
    if let Some(commit @ PyValue::Dict(_)) = get(state, "commit_drift") {
        let mut leg: Row = Vec::new();
        for key in ["commits_since_verify", "basis"] {
            set(&mut leg, key, index(commit, key)?.clone());
        }
        if let Some(v) = commit.get("claim_drift") {
            set(&mut leg, "claim_drift", v.clone());
        }
        set(&mut out, "commit_drift", PyValue::Dict(leg));
    }
    for flag in ["commit_drift_unread", "commit_drift_unchecked"] {
        if get(state, flag).is_some_and(truthy) {
            set(&mut out, flag, PyValue::Bool(true));
        }
    }
    let f = &memory.fields;
    for (key, values) in [
        ("verified_commits", &f.verified_commits),
        ("verified_versions", &f.verified_versions),
    ] {
        if !values.is_empty() {
            set(&mut out, key, strs(values));
        }
    }
    if let Some(head) = &f.verified_head {
        set(&mut out, "verified_head", PyValue::Str(head.clone()));
    }
    if !claims.is_empty() {
        set(&mut out, "claims", strs(&claims));
    }
    if memory.corroborations != "0" {
        set(
            &mut out,
            "corroborations",
            PyValue::Int(memory.corroborations.clone()),
        );
        let last = memory
            .last_corroborated
            .as_ref()
            .map_or(PyValue::None, |d| PyValue::str(&d.isoformat_utc()));
        set(&mut out, "last_corroborated", last);
    }
    for (key, end) in [("links", "target_id"), ("reverse_links", "source_id")] {
        if let Some(entries) = get(state, key).filter(|v| truthy(v)) {
            set(&mut out, key, grouped_links(entries, end)?);
        }
    }
    for key in [
        "trust_unavailable",
        "held",
        "held_hint",
        "standing_question",
    ] {
        if let Some(v) = get(state, key).filter(|v| truthy(v)) {
            set(&mut out, key, v.clone());
        }
    }
    Ok(out)
}

fn strs(values: &[PyStr]) -> PyValue {
    PyValue::List(values.iter().cloned().map(PyValue::Str).collect())
}

/// The findings of each path, in first-seen order.
struct States {
    order: Vec<(PyStr, Vec<PyStr>)>,
    at: HashMap<PyStr, usize>,
}

impl States {
    fn add(&mut self, path: &[u32], finding: PyStr) {
        let i = *self.at.entry(path.to_vec()).or_insert_with(|| {
            self.order.push((path.to_vec(), Vec::new()));
            self.order.len() - 1
        });
        let found = &mut self.order[i].1;
        if !found.contains(&finding) {
            found.push(finding);
        }
    }

    /// `spelled(path)`: the key the read already holds for a spelling
    /// whose tail matches, else the spelling.
    fn spelled(&self, path: &[u32], sep: Option<char>) -> PyStr {
        if self.at.contains_key(path) {
            return path.to_vec();
        }
        let s = slashed(path, sep);
        let start = s
            .iter()
            .position(|&c| c != '.' as u32 && c != '/' as u32)
            .unwrap_or(s.len());
        let mut tail = vec!['/' as u32];
        tail.extend_from_slice(&s[start..]);
        self.order
            .iter()
            .find(|(key, _)| slashed(key, sep).ends_with(&tail))
            .map_or_else(|| path.to_vec(), |(key, _)| key.clone())
    }
}

/// `_path_states(memory, drift)`: each path with its findings joined by
/// `; `, and the claims that name more than a path.
pub fn path_states(
    memory: &Memory,
    drift: &PathDriftLists,
    parse_claim: &mut dyn FnMut(&[u32]) -> ParsedClaim,
    sep: Option<char>,
) -> R<PathStates> {
    let mut st = States {
        order: Vec::new(),
        at: HashMap::new(),
    };
    let anchored: HashSet<&PyStr> = drift.claim_anchored_missing.iter().collect();
    for path in &drift.missing {
        let finding = if anchored.contains(path) {
            "missing, attested or anchored"
        } else {
            "missing"
        };
        st.add(path, cps(finding));
    }
    for (paths, finding) in [
        (
            &drift.claim_anchored_missing,
            "missing, attested or anchored",
        ),
        (&drift.verified, "present, attested"),
        (&drift.expected_absent, "absent, as attested"),
        (
            &drift.present_but_attested_absent,
            "present, attested absent",
        ),
    ] {
        for path in paths {
            st.add(path, cps(finding));
        }
    }
    for (path, why) in drift.unconfirmed.iter().zip(&drift.unconfirmed_reasons) {
        let mut finding = cps("unconfirmed: ");
        finding.extend_from_slice(why);
        st.add(path, finding);
    }
    if drift.unconfirmed.len() != drift.unconfirmed_reasons.len() {
        return Err(Fault::ValueError);
    }
    for path in &drift.dropped_as_route {
        st.add(path, cps("read as a route, not checked"));
    }
    for path in &drift.checked {
        if !st.at.contains_key(path) {
            st.add(path, cps("present"));
        }
    }
    let f = &memory.fields;
    for (paths, finding) in [
        (&f.verified_paths, "attested"),
        (&f.verified_absent_paths, "attested absent"),
    ] {
        for path in paths {
            let key = st.spelled(path, sep);
            if !st.at.contains_key(&key) {
                st.add(&key, cps(finding));
            }
        }
    }
    for targets in [&f.verified_path_targets, &f.verified_absent_path_targets] {
        for (spelling, target) in targets.iter().flatten() {
            if let Some(t) = target.as_ref().filter(|t| *t != spelling) {
                let mut finding = cps("target ");
                finding.extend_from_slice(t);
                let key = st.spelled(spelling, sep);
                st.add(&key, finding);
            }
        }
    }
    let mut claims = Vec::new();
    for raw in &f.claims {
        let finding = match parse_claim(raw) {
            Ok((kind, rel)) if kind == cps("path") => Some((rel, "claimed present")),
            Ok((kind, rel)) if kind == cps("absent") => Some((rel, "claimed absent")),
            _ => None,
        };
        match finding {
            Some((rel, text)) => {
                let key = st.spelled(&rel, sep);
                st.add(&key, cps(text));
            }
            None => claims.push(raw.clone()),
        }
    }
    let glue = cps("; ");
    let joined = st
        .order
        .into_iter()
        .map(|(path, found)| (path, found.join(&glue[..])))
        .collect();
    Ok((joined, claims))
}

/// `_compact_origin(origin)`.
pub fn compact_origin(origin: Option<&PyValue>) -> Option<Row> {
    let Some(PyValue::Dict(items)) = origin else {
        return None;
    };
    let mut out: Row = items
        .iter()
        .filter(|(_, v)| !matches!(v, PyValue::None))
        .cloned()
        .collect();
    for (cwd, root) in [
        ("cwd", "worktree_root"),
        ("cwd_bytes_base64", "worktree_root_bytes_base64"),
    ] {
        if let Some(v) = get(&out, cwd) {
            let r = get(&out, root).cloned().unwrap_or(PyValue::None);
            if py_eq(v, &r) {
                out.retain(|(k, _)| !is_str(k, cwd));
            }
        }
    }
    (!out.is_empty()).then_some(out)
}

/// `_grouped_links(entries, end)`.
pub fn grouped_links(entries: &PyValue, end: &str) -> R<PyValue> {
    let mut groups: Vec<(PyValue, PyValue, Vec<PyValue>)> = Vec::new();
    let mut hashed: HashMap<(PyStr, Option<PyStr>), usize> = HashMap::new();
    let mut others: Vec<usize> = Vec::new();
    let none = PyValue::None;
    for entry in iterate(entries)? {
        let kind = index(&entry, "type")?;
        let note = entry.get("note").unwrap_or(&none);
        if !hashable(kind) || !hashable(note) {
            return Err(Fault::TypeError);
        }
        // A `str` equals only a `str` and `None` only `None`, so such a
        // key is found by hash; any other key by a scan of the others.
        let fast = match (kind, note) {
            (PyValue::Str(k), PyValue::None) => Some((k.clone(), None)),
            (PyValue::Str(k), PyValue::Str(n)) => Some((k.clone(), Some(n.clone()))),
            _ => None,
        };
        let found = match &fast {
            Some(key) => hashed.get(key).copied(),
            None => others
                .iter()
                .copied()
                .find(|&i| py_eq(&groups[i].0, kind) && py_eq(&groups[i].1, note)),
        };
        let at = found.unwrap_or_else(|| {
            groups.push((kind.clone(), note.clone(), Vec::new()));
            let i = groups.len() - 1;
            match fast {
                Some(key) => {
                    hashed.insert(key, i);
                }
                None => others.push(i),
            }
            i
        });
        let id = index(&entry, end)?.clone();
        groups[at].2.push(id);
    }
    Ok(PyValue::List(
        groups
            .into_iter()
            .map(|(kind, note, ids)| {
                let mut g: Row = vec![
                    (PyValue::str("type"), kind),
                    (PyValue::str("ids"), PyValue::List(ids)),
                ];
                if !matches!(note, PyValue::None) {
                    g.push((PyValue::str("note"), note));
                }
                PyValue::Dict(g)
            })
            .collect(),
    ))
}
