//! The reason a row asks for a check, and the verdict it carries: the text
//! a user reads beside a verdict other than ``fresh``.
//!
//! `_response.py`'s `check_reason` (with `_named`, `_REASON_PATHS` and
//! `_rejection_note`), `_UNTRUSTED_PROVENANCE`, `_untrusted`,
//! `verdict_and_reason`, `_row_reason` and `_claim_at_index`, and
//! `verdict.py`'s `_OBJECTED` and `with_objection`.
//!
//! Rows are `dict`s of any values, read as Python reads them: `x or ()`
//! by truthiness, a `str` iterated by code point, a `dict` by its keys,
//! membership in a `set` by hash and `==`. What the reference raises on a
//! value it cannot read comes back as the exception's class.

use crate::events::truthy;
use crate::location_witness::py_eq;
use crate::pyvalue::{py_str, PyStr, PyValue};
use crate::response_trust::{get, hashable, TRUST_UNAVAILABLE_RECOMMENDATION};
use crate::unicode::cps;

/// `verdict._OBJECTED`.
pub const OBJECTED: &str = "spot_check_recommended";
/// `_REASON_PATHS`.
pub const REASON_PATHS: usize = 3;
/// `NEGATIVE_OUTCOME_WINDOW_DAYS`, as `_rejection_note` prints it.
pub const NEGATIVE_OUTCOME_WINDOW_DAYS: i64 = 30;
/// `_UNTRUSTED_PROVENANCE["unaccounted"]`.
pub const UNTRUSTED_UNACCOUNTED: &str = "It entered the store without a log record on this host, \
so nothing says who wrote it; memory_verify does not clear that. Remove it, or restore it after \
a memory_remove if you recognise it.";
/// `_UNTRUSTED_PROVENANCE["synced"]`.
pub const UNTRUSTED_SYNCED: &str =
    "It arrived by sync and has not been verified on this host; memory_verify here clears that.";

/// The class of what the reference raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Raised {
    TypeError,
    KeyError,
    AttributeError,
    IndexError,
}

type R<T> = Result<T, Raised>;

/// `verdict.with_objection(verdict, objected)`.
pub fn with_objection(verdict: &PyValue, objected: bool) -> PyValue {
    if objected && is_str(verdict, "fresh") {
        PyValue::str(OBJECTED)
    } else {
        verdict.clone()
    }
}

fn is_str(v: &PyValue, text: &str) -> bool {
    matches!(v, PyValue::Str(s) if *s == cps(text))
}

/// `iter(value)` drained: a `str` by code point, a `dict` by key.
fn items(value: &PyValue) -> R<Vec<PyValue>> {
    match value {
        PyValue::Str(s) => Ok(s.iter().map(|&c| PyValue::Str(vec![c])).collect()),
        PyValue::List(v) | PyValue::Tuple(v) => Ok(v.clone()),
        PyValue::Dict(d) => Ok(d.iter().map(|(k, _)| k.clone()).collect()),
        _ => Err(Raised::TypeError),
    }
}

/// `set(values)` then `x in` it: hashing every value, then `x`.
fn in_set(x: &PyValue, values: &[PyValue]) -> R<bool> {
    if !values.iter().all(hashable) || !hashable(x) {
        return Err(Raised::TypeError);
    }
    Ok(values.iter().any(|v| py_eq(x, v)))
}

/// `_named(items)` over a list.
fn named(items: &[PyValue]) -> R<PyStr> {
    named_counted(&items[..items.len().min(REASON_PATHS)], items.len())
}

/// `_named` given `value[:3]` as `head` and `len(value)` as `total`.
fn named_counted(head: &[PyValue], total: usize) -> R<PyStr> {
    let items = head;
    let mut out = Vec::new();
    for (i, item) in items.iter().take(REASON_PATHS).enumerate() {
        let PyValue::Str(s) = item else {
            return Err(Raised::TypeError);
        };
        if i > 0 {
            out.extend(cps(", "));
        }
        out.extend(s);
    }
    if total > REASON_PATHS {
        out.extend(cps(&format!(" and {} more", total - REASON_PATHS)));
    }
    Ok(out)
}

/// `_named(value)` over a value as given: `value[:3]` and `len(value)`.
fn named_sliced(value: &PyValue) -> R<PyStr> {
    match value {
        PyValue::Str(s) => {
            let head: Vec<PyValue> = s
                .iter()
                .take(REASON_PATHS)
                .map(|&c| PyValue::Str(vec![c]))
                .collect();
            named_counted(&head, s.len())
        }
        PyValue::List(v) | PyValue::Tuple(v) => {
            named_counted(&v[..v.len().min(REASON_PATHS)], v.len())
        }
        // A slice is hashable from 3.12 on: `dict[slice]` misses. On 3.11
        // the reference raises `TypeError` instead; the port follows 3.12
        // and later.
        PyValue::Dict(_) => Err(Raised::KeyError),
        _ => Err(Raised::TypeError),
    }
}

fn text(v: &PyValue) -> R<PyStr> {
    py_str(v).map_err(|_| Raised::TypeError)
}

fn entry_get<'a>(entry: &'a PyValue, key: &str) -> R<&'a PyValue> {
    match entry {
        PyValue::Dict(_) => entry.get(key).ok_or(Raised::KeyError),
        _ => Err(Raised::TypeError),
    }
}

/// `_rejection_note(entries)`.
pub fn rejection_note(entries: &PyValue) -> R<PyStr> {
    let mut parts: Vec<PyStr> = Vec::new();
    for entry in items(entries)? {
        let count = entry_get(&entry, "count_in_window")?;
        let times = if py_eq(count, &PyValue::int(1)) {
            Vec::new()
        } else {
            let mut t = cps(" ");
            t.extend(text(count)?);
            t.extend(cps(&format!(
                " times in the last {NEGATIVE_OUTCOME_WINDOW_DAYS} days, last"
            )));
            t
        };
        let day: PyStr = text(entry_get(&entry, "most_recent_ts")?)?
            .into_iter()
            .take(10)
            .collect();
        let mut part = cps("A use marked it ");
        part.extend(text(entry_get(&entry, "outcome")?)?);
        part.extend(times);
        part.extend(cps(" on "));
        part.extend(day);
        part.push(u32::from('.'));
        parts.push(part);
    }
    parts.push(cps("It clears once a later use applies it."));
    Ok(parts.join(&u32::from(' ')))
}

/// The keyword arguments of `check_reason`, each as the caller passes it.
#[derive(Debug, Clone, PartialEq)]
pub struct CheckReason {
    pub missing: PyValue,
    pub anchored_missing: PyValue,
    pub attested_absent_present: PyValue,
    pub commits: PyValue,
    pub claims_drifted: PyValue,
    pub leg_note: PyValue,
    pub remote: bool,
    pub trust_unavailable: bool,
    pub untrusted: Option<String>,
    pub contradicted_by: PyValue,
    pub rejections: PyValue,
}

impl Default for CheckReason {
    fn default() -> Self {
        let empty = || PyValue::Tuple(Vec::new());
        CheckReason {
            missing: empty(),
            anchored_missing: empty(),
            attested_absent_present: empty(),
            commits: PyValue::None,
            claims_drifted: empty(),
            leg_note: PyValue::None,
            remote: false,
            trust_unavailable: false,
            untrusted: None,
            contradicted_by: empty(),
            rejections: empty(),
        }
    }
}

/// `check_reason(**args)`.
pub fn check_reason(a: &CheckReason) -> R<Option<PyStr>> {
    let mut parts: Vec<PyValue> = Vec::new();
    let sentence = |head: &str, body: PyStr, tail: &str| {
        let mut t = cps(head);
        t.extend(body);
        t.extend(cps(tail));
        PyValue::Str(t)
    };
    let missing = items(&a.missing)?;
    let mut anchored = Vec::new();
    for path in &missing {
        if in_set(path, &items(&a.anchored_missing)?)? {
            anchored.push(path.clone());
        }
    }
    // The comprehension runs before `+=`: each path is checked against
    // the list built from `missing` alone, so repeats in
    // `anchored_missing` stay.
    let from_missing = anchored.len();
    for path in items(&a.anchored_missing)? {
        if !anchored[..from_missing].iter().any(|p| py_eq(&path, p)) {
            anchored.push(path);
        }
    }
    let mut prose = Vec::new();
    for path in &missing {
        if !in_set(path, &anchored)? {
            prose.push(path.clone());
        }
    }
    if !anchored.is_empty() {
        parts.push(sentence(
            "Attested or anchored path missing: ",
            named(&anchored)?,
            ".",
        ));
    }
    if !prose.is_empty() {
        parts.push(sentence("Cited path missing: ", named(&prose)?, "."));
    }
    if truthy(&a.attested_absent_present) {
        let listed = named(&items(&a.attested_absent_present)?)?;
        parts.push(sentence("Attested absent but present: ", listed, "."));
    }
    if truthy(&a.commits) {
        let plural = if py_eq(&a.commits, &PyValue::int(1)) {
            ""
        } else {
            "s"
        };
        let mut head = text(&a.commits)?;
        head.extend(cps(&format!(
            " commit{plural} since the last verify touched the "
        )));
        if truthy(&a.claims_drifted) {
            head.extend(cps("claims "));
            head.extend(named(&items(&a.claims_drifted)?)?);
            head.push(u32::from('.'));
        } else {
            head.extend(cps("paths it cites."));
        }
        parts.push(PyValue::Str(head));
    }
    if truthy(&a.leg_note) {
        parts.push(a.leg_note.clone());
    }
    if a.remote {
        parts.push(PyValue::str(
            "Its verification stamp was made on another host.",
        ));
    }
    if a.trust_unavailable {
        parts.push(PyValue::str(&format!(
            "{TRUST_UNAVAILABLE_RECOMMENDATION} Nothing in these tools clears it; the store's owner restores the key."
        )));
    }
    if let Some(u) = a.untrusted.as_deref().filter(|u| !u.is_empty()) {
        parts.push(PyValue::str(u));
    }
    if truthy(&a.contradicted_by) {
        parts.push(sentence(
            "A recorded link says it contradicts ",
            named_sliced(&a.contradicted_by)?,
            ": read it, then fix whichever is wrong or remove the link; memory_verify does not clear it.",
        ));
    }
    if truthy(&a.rejections) {
        parts.push(PyValue::Str(rejection_note(&a.rejections)?));
    }
    let mut out: PyStr = Vec::new();
    for (i, part) in parts.iter().enumerate() {
        let PyValue::Str(s) = part else {
            return Err(Raised::TypeError);
        };
        if i > 0 {
            out.push(u32::from(' '));
        }
        out.extend(s);
    }
    Ok(if out.is_empty() { None } else { Some(out) })
}

/// `(value or {}).get(key)`: an `AttributeError` for a true value with no
/// `get`.
fn or_dict_get<'a>(value: Option<&'a PyValue>, key: &str) -> R<Option<&'a PyValue>> {
    match value {
        Some(v @ PyValue::Dict(_)) => Ok(v.get(key)),
        Some(v) if truthy(v) => Err(Raised::AttributeError),
        _ => Ok(None),
    }
}

/// `x or ()`.
fn or_empty(value: Option<&PyValue>) -> PyValue {
    match value {
        Some(v) if truthy(v) => v.clone(),
        _ => PyValue::Tuple(Vec::new()),
    }
}

/// `_untrusted(row)`.
pub fn untrusted(row: &[(PyValue, PyValue)]) -> R<Option<&'static str>> {
    let provenance = get(row, "provenance");
    let status = or_dict_get(get(row, "verification"), "status")?;
    if provenance.is_some_and(|p| is_str(p, "unaccounted")) {
        return Ok(Some(UNTRUSTED_UNACCOUNTED));
    }
    if provenance.is_some_and(|p| is_str(p, "synced")) && status.is_some_and(|s| is_str(s, "never"))
    {
        return Ok(Some(UNTRUSTED_SYNCED));
    }
    Ok(None)
}

/// `verdict_and_reason(row)`.
pub fn verdict_and_reason(row: &[(PyValue, PyValue)]) -> R<(PyValue, Option<PyStr>)> {
    let contradicted_by = or_empty(get(row, "_contradicted_by"));
    let untrusted = untrusted(row)?;
    let objected = truthy(&contradicted_by)
        || get(row, "recent_negative_outcomes").is_some_and(truthy)
        || get(row, "trust_unavailable").is_some_and(truthy)
        || untrusted.is_some();
    let mut verdict = with_objection(
        get(row, "staleness_verdict").ok_or(Raised::KeyError)?,
        objected,
    );
    let status = or_dict_get(get(row, "verification"), "status")?;
    if get(row, "provenance").is_some_and(|p| is_str(p, "unaccounted"))
        && !status.is_none_or(|s| matches!(s, PyValue::None) || is_str(s, "never"))
    {
        verdict = PyValue::str("spot_check_required");
    }
    if is_str(&verdict, "fresh") {
        return Ok((verdict, None));
    }
    let reason = row_reason(row, &contradicted_by, untrusted)?;
    Ok((verdict, reason))
}

/// `_row_reason(row, contradicted_by, untrusted)`.
pub fn row_reason(
    row: &[(PyValue, PyValue)],
    contradicted_by: &PyValue,
    untrusted: Option<&str>,
) -> R<Option<PyStr>> {
    let drift = get(row, "path_drift");
    let (commits, claims_drifted) = match get(row, "commit_drift") {
        Some(commit @ PyValue::Dict(_)) => (
            commit.get("commits_since_verify"),
            or_empty(or_dict_get(commit.get("claim_drift"), "drifted")?),
        ),
        _ => (
            get(row, "commit_drift_count"),
            or_empty(or_dict_get(get(row, "claim_drift"), "drifted")?),
        ),
    };
    let verification = get(row, "verification");
    let status = or_dict_get(verification, "status")?;
    let remote = status.is_some_and(|s| is_str(s, "remote"));
    let trust_unavailable = get(row, "trust_unavailable").is_some_and(truthy);
    let unread = get(row, "commit_drift_unread").is_some_and(truthy);
    let mut leg_note = PyValue::None;
    if unread || get(row, "commit_drift_unchecked").is_some_and(truthy) {
        leg_note = if !remote && !trust_unavailable {
            or_dict_get(verification, "recommendation")?
                .cloned()
                .unwrap_or(PyValue::None)
        } else if unread {
            PyValue::str("The commits since its stamp were not read.")
        } else {
            PyValue::str("No checkout here can read the commits since its stamp.")
        };
    }
    let commits = match commits {
        Some(c @ (PyValue::Int(_) | PyValue::Bool(_))) => c.clone(),
        _ => PyValue::None,
    };
    check_reason(&CheckReason {
        missing: or_empty(or_dict_get(drift, "missing")?),
        anchored_missing: or_empty(or_dict_get(drift, "claim_anchored_missing")?),
        attested_absent_present: or_empty(or_dict_get(drift, "present_but_attested_absent")?),
        commits,
        claims_drifted,
        leg_note,
        remote,
        trust_unavailable,
        untrusted: untrusted.map(str::to_owned),
        contradicted_by: contradicted_by.clone(),
        rejections: or_empty(get(row, "recent_negative_outcomes")),
    })
}

/// `_claim_at_index(event, index)`.
pub fn claim_at_index(event: &[(PyValue, PyValue)], index: i64) -> R<Option<PyStr>> {
    let Some(PyValue::List(excerpts)) = get(event, "claim_excerpts") else {
        return Ok(None);
    };
    let len = excerpts.len() as i64;
    if index >= len {
        return Ok(None);
    }
    let at = if index < 0 { index + len } else { index };
    if at < 0 {
        return Err(Raised::IndexError);
    }
    Ok(match &excerpts[at as usize] {
        PyValue::Str(s) => Some(s.clone()),
        _ => None,
    })
}
