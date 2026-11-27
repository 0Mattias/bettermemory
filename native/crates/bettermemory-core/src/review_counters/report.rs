//! The review report (`review_counters.py` 613-780), unit
//! review_counters-2: `ReviewReport` with `to_dict`, and
//! `compute_review_report` over the chains of unit 1b.
//!
//! The chains are counted into the total and into one `_Tally` per client
//! and per model; the filings of each committed answer go under the ISO
//! week of its decision; the response sizes are read off each issued row
//! inside the window (a roster page and a pre-answered call left out),
//! under that row's own client and model; and the grades keep the latest
//! row per review_id, by `>=`, for samples written inside the window.
//! The dicts the reference builds keep their insertion order here
//! ([`Keyed`]); `to_dict` sorts them by key as the reference does, in
//! code point order. Times are the caller's and compared as instants.

use std::collections::HashMap;
use std::hash::Hash;

use super::chains::plus_window;
use super::{
    chains_in_window, in_window, is_review_row, iso_week, ChainState, CountersError, Counts,
    GradeRow, IssuedRow, Tally, ANSWER_WINDOW_US, DETECTORS, REVIEW_GRADE,
};
use crate::datetime::DateTime;
use crate::pyvalue::{PyStr, PyValue};
use crate::review::REVIEW_ISSUED;
use crate::unicode::cps;

type Answer<T> = Result<T, CountersError>;

/// The instant of a datetime in microseconds; a naive one read as UTC.
fn at(ts: &DateTime) -> i128 {
    ts.instant_us().unwrap_or_else(|| ts.wall_micros())
}

/// A `dict` in insertion order, filled by `setdefault`. Two are equal
/// when their items are, in order.
#[derive(Clone, Debug)]
pub struct Keyed<K, V> {
    pub items: Vec<(K, V)>,
    at: HashMap<K, usize>,
}

impl<K: PartialEq, V: PartialEq> PartialEq for Keyed<K, V> {
    fn eq(&self, other: &Self) -> bool {
        self.items == other.items
    }
}

impl<K, V> Default for Keyed<K, V> {
    fn default() -> Self {
        Keyed {
            items: Vec::new(),
            at: HashMap::new(),
        }
    }
}

impl<K: Clone + Eq + Hash + Ord, V> Keyed<K, V> {
    /// `d.setdefault(key, make())`: a new key goes last.
    pub fn setdefault(&mut self, key: &K, make: impl FnOnce() -> V) -> &mut V {
        let i = match self.at.get(key) {
            Some(&i) => i,
            None => {
                self.at.insert(key.clone(), self.items.len());
                self.items.push((key.clone(), make()));
                self.items.len() - 1
            }
        };
        &mut self.items[i].1
    }

    /// `d.get(key)`.
    pub fn get(&self, key: &K) -> Option<&V> {
        self.at.get(key).map(|&i| &self.items[i].1)
    }

    /// `sorted(d.items())`: by key (keys are distinct).
    pub fn sorted(&self) -> Vec<&(K, V)> {
        let mut out: Vec<&(K, V)> = self.items.iter().collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }
}

/// The `grades` dict: the latest grade per review_id, counted.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Grades {
    pub n: u64,
    pub right_both: u64,
    pub right_answers: u64,
    pub right_action: u64,
    pub pre_answered_n: u64,
}

/// `ReviewReport`: the `review` section of `bettermemory eval`.
#[derive(Clone, Debug, PartialEq)]
pub struct ReviewReport {
    pub start: Option<DateTime>,
    pub end: Option<DateTime>,
    pub judged_through: DateTime,
    pub total: Tally,
    pub by_client: Keyed<PyStr, Tally>,
    pub by_model: Keyed<PyStr, Tally>,
    pub filings_by_week: Keyed<String, Counts>,
    pub grades: Grades,
}

fn int(n: u64) -> PyValue {
    PyValue::int(i128::from(n))
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        pairs
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

/// The total's `to_dict` value under `key`.
fn field(total: &PyValue, key: &str) -> PyValue {
    total.get(key).cloned().expect("a key of _Tally.to_dict")
}

/// `{k: v.pre_answered_zero for k, v in sorted(m.items())}`.
fn zero_by(m: &Keyed<PyStr, Tally>) -> PyValue {
    PyValue::Dict(
        m.sorted()
            .into_iter()
            .map(|(k, v)| (PyValue::Str(k.clone()), int(v.pre_answered_zero)))
            .collect(),
    )
}

/// `{k: v.to_dict() for k, v in sorted(m.items())}`: the first raise in
/// key order.
fn tallies_by(m: &Keyed<PyStr, Tally>) -> Answer<PyValue> {
    let mut out = Vec::with_capacity(m.items.len());
    for (k, v) in m.sorted() {
        out.push((PyValue::Str(k.clone()), v.to_dict()?));
    }
    Ok(PyValue::Dict(out))
}

fn chains_calls(chains: u64, calls: u64) -> PyValue {
    dict(vec![("chains", int(chains)), ("calls", int(calls))])
}

impl ReviewReport {
    /// `to_dict`, in the reference's key order. OverflowError from a
    /// median: the total's first, then each client's tally in key order,
    /// then each model's, as the reference evaluates them.
    pub fn to_dict(&self) -> Answer<PyValue> {
        let total = self.total.to_dict()?;
        let f = |key: &str| field(&total, key);
        let weeks = PyValue::Dict(
            self.filings_by_week
                .sorted()
                .into_iter()
                .map(|(week, counts)| {
                    let held = counts
                        .items
                        .iter()
                        .map(|(k, n)| (PyValue::Str(k.clone()), int(*n)))
                        .collect();
                    (PyValue::str(week), PyValue::Dict(held))
                })
                .collect(),
        );
        let refused = i128::try_from(self.total.refused()).expect("three u64 counts fit");
        let by_client = tallies_by(&self.by_client)?;
        let by_model = tallies_by(&self.by_model)?;
        let g = &self.grades;
        Ok(dict(vec![
            (
                "answer_window_minutes",
                int((ANSWER_WINDOW_US / 60_000_000) as u64),
            ),
            (
                "judged_through",
                PyValue::str(&self.judged_through.isoformat_utc()),
            ),
            (
                "chains",
                dict(vec![
                    ("issued", f("issued")),
                    ("answered", f("answered")),
                    ("unanswered", f("unanswered")),
                    ("pending", f("pending")),
                    ("closed_by_call", f("closed_by_call")),
                ]),
            ),
            ("unanswered_share", f("unanswered_share")),
            ("pre_answered", f("pre_answered")),
            ("pre_answered_share", f("pre_answered_share")),
            (
                "pre_answered_zero",
                dict(vec![
                    ("total", f("pre_answered_zero")),
                    ("by_client", zero_by(&self.by_client)),
                    ("by_model", zero_by(&self.by_model)),
                ]),
            ),
            ("actions", f("actions")),
            ("links", f("links")),
            ("update_suggested", f("update_suggested")),
            ("filings", f("filings")),
            ("response_chars_p50", f("response_chars_p50")),
            ("response_chars_n", f("response_chars_n")),
            ("calls_per_committed_write", f("calls_per_committed_write")),
            (
                "committed_chains",
                chains_calls(self.total.committed(), self.total.committed_calls),
            ),
            (
                "refused_chains",
                dict(vec![
                    ("chains", PyValue::int(refused)),
                    ("calls", int(self.total.refused_calls)),
                ]),
            ),
            (
                "unanswered_chains",
                chains_calls(self.total.unanswered, self.total.unanswered_calls),
            ),
            ("filings_per_100_committed", f("filings_per_100_committed")),
            ("filings_by_week", weeks),
            ("by_client", by_client),
            ("by_model", by_model),
            (
                "grades",
                dict(vec![
                    ("n", int(g.n)),
                    ("right_both", int(g.right_both)),
                    ("right_answers", int(g.right_answers)),
                    ("right_action", int(g.right_action)),
                    ("pre_answered_n", int(g.pre_answered_n)),
                ]),
            ),
        ]))
    }
}

/// `event.get("kind") == kind` for a dict event.
fn kind_is(event: &PyValue, kind: &PyStr) -> bool {
    matches!(event.get("kind"), Some(PyValue::Str(k)) if k == kind)
}

/// `compute_review_report`: the review counters over chains issued in
/// `[start, end)`. Events that are not dicts are skipped, as
/// `isinstance(e, Mapping)` skips them. OverflowError where the reference
/// raises it: `end + ANSWER_WINDOW` past year 9999 (in `chains_in_window`
/// first), and the ISO week of a committed decision with filings whose
/// UTC date leaves years 1 to 9999.
pub fn compute_review_report(
    events: &[PyValue],
    start: Option<&DateTime>,
    end: Option<&DateTime>,
    now: &DateTime,
) -> Answer<ReviewReport> {
    let rows: Vec<PyValue> = events
        .iter()
        .filter(|e| matches!(e, PyValue::Dict(_)) && matches!(is_review_row(e), Ok(true)))
        .cloned()
        .collect();
    let chains = chains_in_window(&rows, start, end, now)?;
    let mut total = Tally::default();
    let mut by_client: Keyed<PyStr, Tally> = Keyed::default();
    let mut by_model: Keyed<PyStr, Tally> = Keyed::default();
    let mut filings_by_week: Keyed<String, Counts> = Keyed::default();
    for chain in &chains {
        total.add(chain);
        by_client
            .setdefault(chain.client(), Tally::default)
            .add(chain);
        by_model
            .setdefault(chain.model(), Tally::default)
            .add(chain);
        let Some(decided) = chain.decided.as_ref() else {
            continue;
        };
        if chain.state != ChainState::Answered || !decided.committed() {
            continue;
        }
        for (_, detector) in &decided.filings {
            filings_by_week
                .setdefault(&iso_week(&decided.ts)?, || Counts::from_keys(&DETECTORS))
                .add(detector, 1);
        }
    }

    let issued_kind = cps(REVIEW_ISSUED);
    for event in &rows {
        if !kind_is(event, &issued_kind) {
            continue;
        }
        let Some(row) = IssuedRow::from_event(event)? else {
            continue;
        };
        let Some(chars) = row.response_chars.as_ref() else {
            continue;
        };
        if row.pre_answered || !row.roster.is_empty() || !in_window(&row.ts, start, end) {
            continue;
        }
        total.response_chars.push(chars.clone());
        by_client
            .setdefault(&row.client, Tally::default)
            .response_chars
            .push(chars.clone());
        by_model
            .setdefault(&row.model, Tally::default)
            .response_chars
            .push(chars.clone());
    }

    let grade_kind = cps(REVIEW_GRADE);
    let mut latest: HashMap<PyStr, GradeRow> = HashMap::new();
    for event in &rows {
        if !kind_is(event, &grade_kind) {
            continue;
        }
        let Some(grade) = GradeRow::from_event(event)? else {
            continue;
        };
        match &grade.sample_ts {
            Some(sample) if in_window(sample, start, end) => {}
            _ => continue,
        }
        let replaces = latest
            .get(&grade.review_id)
            .is_none_or(|prior| at(&grade.ts) >= at(&prior.ts));
        if replaces {
            latest.insert(grade.review_id.clone(), grade);
        }
    }
    let mut grades = Grades::default();
    for g in latest.values() {
        grades.n += 1;
        grades.right_both += u64::from(g.right_answers && g.right_action);
        grades.right_answers += u64::from(g.right_answers);
        grades.right_action += u64::from(g.right_action);
        grades.pre_answered_n += u64::from(g.pre_answered);
    }

    let judged_through = match end {
        None => *now,
        Some(end) => {
            let limit = plus_window(end)?;
            if at(&limit) < at(now) {
                limit
            } else {
                *now
            }
        }
    };
    Ok(ReviewReport {
        start: start.copied(),
        end: end.copied(),
        judged_through,
        total,
        by_client,
        by_model,
        filings_by_week,
        grades,
    })
}
