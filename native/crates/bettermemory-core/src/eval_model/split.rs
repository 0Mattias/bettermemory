//! The eval family's model, unit eval_model-1b: the recall surfaces'
//! split on each show's rule (`surface_rule_split`, design 8.11) with its
//! membership delta, and `_with_rule_split`, which adds both to the
//! surfaces section.
//!
//! A show belongs to the current rule ([`RULE`]) exactly when its `rule`
//! is the `str` `"rs-v8"`; every other show, a missing rule, None, an
//! `int`, a `bool` or a list included, belongs to [`EARLIER_RULES`]. So a
//! show falls in exactly one group, and the two groups add up to the
//! section. Each group's tallies are `compute_surface_report` with the
//! group as its pick: the pick runs inside the report, so what the
//! conversation did after a show is still joined over every row whatever
//! its rule.

use std::collections::{BTreeSet, HashSet};

use super::{show_in_window, Answer, EvalModelError, EARLIER_RULES};
use crate::datetime::DateTime;
use crate::health_model::{dict, int};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::recall_counters::{compute_surface_report, is_surface_show, shown_ids, SURFACES};
use crate::recall_state::RULE;
use crate::unicode::cps;

/// `group_of(show)`: [`RULE`] when `show.get("rule") == RULE` (a `str`
/// of exactly those code points), else [`EARLIER_RULES`].
pub fn group_of(show: &PyValue) -> &'static str {
    match show.get("rule") {
        Some(PyValue::Str(rule)) if rule.iter().copied().eq(RULE.chars().map(u32::from)) => RULE,
        _ => EARLIER_RULES,
    }
}

/// `membership_delta`: the show rows under the current rule whose
/// `rc3_admitted` is a list, compared with the ids they delivered. Each
/// count is bounded by the rows times their ids, so `i64` holds it.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MembershipDelta {
    pub rows: i64,
    pub rows_differing: i64,
    pub delivered_not_rc3: i64,
    pub rc3_not_delivered: i64,
}

impl MembershipDelta {
    /// The dict in the reference's key order.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("rows", int(self.rows)),
            ("rows_differing", int(self.rows_differing)),
            ("delivered_not_rc3", int(self.delivered_not_rc3)),
            ("rc3_not_delivered", int(self.rc3_not_delivered)),
        ])
    }

    /// One show's part: `rc3` is the set of the list's `str` items (the
    /// empty one included, a `bool` or any other value left out),
    /// `delivered` the set of `shown_ids(show)` (non-empty `str`s only).
    fn add(&mut self, show: &PyValue, admitted: &[PyValue]) {
        let rc3: HashSet<&PyStr> = admitted
            .iter()
            .filter_map(|item| match item {
                PyValue::Str(s) => Some(s),
                _ => None,
            })
            .collect();
        let ids = shown_ids(show);
        let delivered: HashSet<&PyStr> = ids.iter().collect();
        let delivered_not_rc3 = delivered.iter().filter(|id| !rc3.contains(*id)).count();
        let rc3_not_delivered = rc3.iter().filter(|id| !delivered.contains(*id)).count();
        self.rows += 1;
        self.rows_differing += i64::from(delivered != rc3);
        self.delivered_not_rc3 += delivered_not_rc3 as i64;
        self.rc3_not_delivered += rc3_not_delivered as i64;
    }
}

/// `surface_rule_split(rows, start=start, end=end)`: `(by_rule,
/// membership_delta)`.
///
/// The shows are the rows that are dicts, are surface shows and fall in
/// `[start, end)` (`_show_in_window`, which raises TypeError for a naive
/// bound against a readable stamp, before any report is made). Then for
/// [`RULE`] and [`EARLIER_RULES`], in that order, `compute_surface_report`
/// over every row with the group as its pick (a raise in the first stops
/// before the second), and the group's entry: `rules`, the non-empty
/// `str` rules its shows named, sorted by code point, then each surface's
/// part of the report, None for a report that is None.
///
/// `compute_surface_report` skips a value that is not a dict itself and
/// counts positions among the dict surface rows, so the rows go to it
/// unfiltered, as the reference's `surface_rows` would read.
pub fn surface_rule_split(
    rows: &[PyValue],
    start: Option<&DateTime>,
    end: Option<&DateTime>,
) -> Answer<(PyValue, MembershipDelta)> {
    let mut shows = Vec::new();
    for row in rows {
        if matches!(row, PyValue::Dict(_))
            && is_surface_show(row)
            && show_in_window(row, start, end)?
        {
            shows.push(row);
        }
    }
    let mut by_rule = Vec::with_capacity(2);
    for group in [RULE, EARLIER_RULES] {
        let mut pick = |show: &PyValue| Ok::<_, EvalModelError>(group_of(show) == group);
        let report = compute_surface_report(rows, start.copied(), end.copied(), Some(&mut pick))?;
        let named: BTreeSet<&PyStr> = shows
            .iter()
            .filter(|show| group_of(show) == group)
            .filter_map(|show| match show.get("rule") {
                Some(PyValue::Str(rule)) if !rule.is_empty() => Some(rule),
                _ => None,
            })
            .collect();
        let named = named.into_iter().map(|r| PyValue::Str(r.clone()));
        let mut entry = vec![("rules", PyValue::List(named.collect()))];
        for surface in SURFACES {
            entry.push((surface, surface_part(report.as_ref(), surface)));
        }
        by_rule.push((PyValue::Str(cps(group)), dict(entry)));
    }
    let mut delta = MembershipDelta::default();
    for show in shows {
        if group_of(show) != RULE {
            continue;
        }
        if let Some(PyValue::List(admitted)) = show.get("rc3_admitted") {
            delta.add(show, admitted);
        }
    }
    Ok((PyValue::Dict(by_rule), delta))
}

/// `report[surface] if report is not None else None`. Every report
/// `compute_surface_report` answers holds both surfaces, so the
/// reference's KeyError for a missing one is unreachable here.
fn surface_part(report: Option<&PyValue>, surface: &str) -> PyValue {
    match report {
        None => PyValue::None,
        Some(report) => report
            .get(surface)
            .cloned()
            .expect("compute_surface_report answers every surface"),
    }
}

/// `_with_rule_split(surfaces, rows, start=start, end=end)`: None (a
/// missing section or Python's None) stays None and the split is not
/// made; else `{**surfaces, "by_rule": ..., "membership_delta": ...}`,
/// where a key already present keeps its place and takes the new value
/// and a new one goes last. A section that is not a dict is TypeError
/// (`{**x}` of a non-mapping); no caller passes one.
pub fn with_rule_split(
    surfaces: Option<&PyValue>,
    rows: &[PyValue],
    start: Option<&DateTime>,
    end: Option<&DateTime>,
) -> Answer<Option<PyValue>> {
    let items = match surfaces {
        None | Some(PyValue::None) => return Ok(None),
        Some(PyValue::Dict(items)) => items,
        Some(_) => return Err(EvalModelError::Raised(PyError::TypeError)),
    };
    let (by_rule, delta) = surface_rule_split(rows, start, end)?;
    let mut out = items.clone();
    for (key, value) in [("by_rule", by_rule), ("membership_delta", delta.to_dict())] {
        set_key(&mut out, key, value);
    }
    Ok(Some(PyValue::Dict(out)))
}

/// `d[key] = value` on a dict as pairs: every pair under the `str` key
/// takes the value in its place; with none, the pair goes last.
fn set_key(items: &mut Vec<(PyValue, PyValue)>, key: &str, value: PyValue) {
    let name = cps(key);
    let mut found = false;
    for (k, v) in items.iter_mut() {
        if matches!(k, PyValue::Str(s) if *s == name) {
            *v = value.clone();
            found = true;
        }
    }
    if !found {
        items.push((PyValue::Str(name), value));
    }
}
