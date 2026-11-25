//! Unit health_model-2 of `health_model.py` (lines 662-1241 less
//! `explicit_ratio`): the actor slices, the verification, commit-drift and
//! cross-repo debt rollups, the cold-endorsement predicate and bucket, the
//! silent-miss report types, and the caps they are read under.
//!
//! As in unit 1, the types hold Rust values and `to_dict` answers a
//! [`PyValue::Dict`] in the reference's key order. A rollup holding
//! `MemoryStats` rows answers [`Answer`]: the reference builds its scalars
//! first and then each row list in turn, so the first row whose `to_dict`
//! raises TypeError is the answer.

use super::{dict, int, text, ActorSlice, Answer, MemoryStats};
use crate::datetime::DateTime;
use crate::origin_history::HistoryUnread;
use crate::pyvalue::{PyStr, PyValue};
use crate::time_utils::isoformat_utc_optional;
use crate::unicode;
use crate::verify_basis::BASIS_AUTHOR_DATE;

/// `_VERIFICATION_DEBT_CAP`: the rows `VerificationDebt` lists inline.
pub const VERIFICATION_DEBT_CAP: usize = 20;
/// `_COLD_ENDORSEMENT_MIN_RETRIEVALS`: the retrieval floor of the
/// cold-endorsement bucket.
pub const COLD_ENDORSEMENT_MIN_RETRIEVALS: i64 = 30;
/// `_COLD_ENDORSEMENT_CAP`: the rows `ColdEndorsementMemories` lists.
pub const COLD_ENDORSEMENT_CAP: usize = 20;
/// `_CROSS_REPO_MAX_ROOTS`: estate roots one daemon health call walks.
pub const CROSS_REPO_MAX_ROOTS: usize = 8;
/// `_COMMIT_DRIFT_DEBT_CAP`: the drift rows and unread ids listed inline.
pub const COMMIT_DRIFT_DEBT_CAP: usize = 20;
/// `_RECENT_SILENT_MISSES_CAP`: the silent misses listed inline.
pub const RECENT_SILENT_MISSES_CAP: usize = 10;

fn opt_text(s: Option<&[u32]>) -> PyValue {
    s.map_or(PyValue::None, text)
}

fn texts(items: &[PyStr]) -> PyValue {
    PyValue::List(items.iter().map(|s| text(s)).collect())
}

/// Whether no two of `keys` are equal: the reference's causes and the
/// ids `unread_fields` reads are a dict's keys, which are distinct.
fn distinct<'a>(keys: impl IntoIterator<Item = &'a PyStr>) -> bool {
    let mut seen = std::collections::HashSet::new();
    keys.into_iter().all(|k| seen.insert(k))
}

/// `dict(causes)` of causes whose keys are distinct, as a dict's are: each
/// pair is written as given, in the order given. A repeated key is refused
/// in debug builds; a release build writes both pairs, which no value of
/// the reference can hold.
fn tally(items: &[(PyStr, i64)]) -> PyValue {
    debug_assert!(
        distinct(items.iter().map(|(k, _)| k)),
        "unread_causes holds a repeated key; a dict's keys are distinct"
    );
    PyValue::Dict(items.iter().map(|(k, n)| (text(k), int(*n))).collect())
}

/// `[s.to_dict() for s in rows]`: the first row that raises is the answer.
fn stats_list(rows: &[MemoryStats]) -> Answer<PyValue> {
    rows.iter()
        .map(MemoryStats::to_dict)
        .collect::<Answer<Vec<_>>>()
        .map(PyValue::List)
}

fn opt_iso(dt: Option<&DateTime>) -> PyValue {
    isoformat_utc_optional(dt).map_or(PyValue::None, |s| PyValue::str(&s))
}

/// `ActorSlices`: the declared actors and the undeclared bucket, which is
/// always present (`ActorSlice(client=None)` by default).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ActorSlices {
    pub declared: Vec<ActorSlice>,
    pub undeclared: ActorSlice,
}

impl ActorSlices {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            (
                "declared",
                PyValue::List(self.declared.iter().map(ActorSlice::to_dict).collect()),
            ),
            ("undeclared", self.undeclared.to_dict()),
        ])
    }
}

/// `VerificationDebt`: capped row lists and uncapped totals.
#[derive(Clone, Debug, PartialEq)]
pub struct VerificationDebt {
    pub stale_after_days: i64,
    pub never_verified: Vec<MemoryStats>,
    pub never_verified_total: i64,
    pub stale: Vec<MemoryStats>,
    pub stale_total: i64,
    pub fresh_count: i64,
    pub never_verified_checkable: i64,
    pub stale_checkable: i64,
}

impl VerificationDebt {
    /// The dataclass's constructor with every defaulted field at its
    /// default (empty lists, zero counts).
    pub fn new(stale_after_days: i64) -> Self {
        VerificationDebt {
            stale_after_days,
            never_verified: Vec::new(),
            never_verified_total: 0,
            stale: Vec::new(),
            stale_total: 0,
            fresh_count: 0,
            never_verified_checkable: 0,
            stale_checkable: 0,
        }
    }

    /// `to_dict`: the scalars, then `never_verified`'s rows, then
    /// `stale`'s; TypeError from the first row that raises.
    pub fn to_dict(&self) -> Answer<PyValue> {
        Ok(dict(vec![
            ("stale_after_days", int(self.stale_after_days)),
            ("never_verified_total", int(self.never_verified_total)),
            (
                "never_verified_checkable",
                int(self.never_verified_checkable),
            ),
            ("stale_total", int(self.stale_total)),
            ("stale_checkable", int(self.stale_checkable)),
            ("fresh_count", int(self.fresh_count)),
            ("never_verified", stats_list(&self.never_verified)?),
            ("stale", stats_list(&self.stale)?),
        ]))
    }
}

/// `clamp_ratio_threshold`: NaN is 0.0; otherwise `min(1.0, max(0.0,
/// value))` as Python's `max` and `min` choose, keeping the first argument
/// unless the second compares strictly beyond it (`-0.0` reads 0.0).
pub fn clamp_ratio_threshold(value: f64) -> f64 {
    if value.is_nan() {
        return 0.0;
    }
    let low = if value > 0.0 { value } else { 0.0 };
    if low < 1.0 {
        low
    } else {
        1.0
    }
}

/// `weakly_endorsed`: applied at least once (`explicit_ratio` is not
/// None), and never explicitly or, with a threshold above 0, explicitly
/// less than that share of the time. A NaN threshold is not `<= 0.0` and
/// fails `ratio < NaN`.
pub fn weakly_endorsed(applied: i64, explicit_applied: i64, ratio_threshold: f64) -> bool {
    let Some(ratio) = super::explicit_ratio(applied, explicit_applied) else {
        return false;
    };
    if explicit_applied == 0 {
        return true;
    }
    if ratio_threshold <= 0.0 {
        return false;
    }
    ratio < ratio_threshold
}

/// `_is_weakly_endorsed`: `weakly_endorsed` of the row's applied and
/// explicit-applied counts.
pub fn is_weakly_endorsed(stats: &MemoryStats, ratio_threshold: f64) -> bool {
    weakly_endorsed(
        stats.applied_count,
        stats.explicit_applied_count,
        ratio_threshold,
    )
}

/// `CommitDriftRow`: one memory whose verification anchor is behind HEAD.
#[derive(Clone, Debug, PartialEq)]
pub struct CommitDriftRow {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub summary: PyStr,
    pub last_verified_at: Option<DateTime>,
    pub commits_since_verify: i64,
    pub basis: PyStr,
}

impl CommitDriftRow {
    /// The dataclass's constructor, `basis` at its default
    /// (`BASIS_AUTHOR_DATE`).
    pub fn new(
        id: PyStr,
        scopes: Vec<PyStr>,
        summary: PyStr,
        last_verified_at: Option<DateTime>,
        commits_since_verify: i64,
    ) -> Self {
        CommitDriftRow {
            id,
            scopes,
            summary,
            last_verified_at,
            commits_since_verify,
            basis: unicode::cps(BASIS_AUTHOR_DATE),
        }
    }

    /// `to_dict`; `_iso(x) if x else None` is None only for None, since a
    /// datetime is never falsy.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("id", text(&self.id)),
            ("scopes", texts(&self.scopes)),
            ("summary", text(&self.summary)),
            ("last_verified_at", opt_iso(self.last_verified_at.as_ref())),
            ("commits_since_verify", int(self.commits_since_verify)),
            ("basis", text(&self.basis)),
        ])
    }
}

fn drift_rows(rows: &[CommitDriftRow]) -> PyValue {
    PyValue::List(rows.iter().map(CommitDriftRow::to_dict).collect())
}

/// What `unread_fields` answers: the count, the count per cause and the
/// capped ids, as the rollups' `unread`, `unread_causes` and
/// `unread_ids`. The keys of `unread_causes` are distinct, as a dict's
/// are, and so are the ids.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct UnreadFields {
    pub unread: i64,
    pub unread_causes: Vec<(PyStr, i64)>,
    pub unread_ids: Vec<PyStr>,
}

impl UnreadFields {
    /// The dict the reference answers, in its key order.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("unread", int(self.unread)),
            ("unread_causes", tally(&self.unread_causes)),
            ("unread_ids", texts(&self.unread_ids)),
        ])
    }
}

/// `unread_fields(unread)` of a mapping given as its items in insertion
/// order (its keys, the ids, distinct, as a dict's are; a repeated id is
/// refused in debug builds): the count of entries, the count of each
/// cause's value sorted by that text in code point order, and the first
/// `COMMIT_DRIFT_DEBT_CAP` ids in insertion order.
pub fn unread_fields(unread: &[(PyStr, HistoryUnread)]) -> UnreadFields {
    debug_assert!(
        distinct(unread.iter().map(|(id, _)| id)),
        "unread_fields holds a repeated id; a mapping's keys are distinct"
    );
    let mut causes: Vec<(&'static str, i64)> = Vec::new();
    for (_, why) in unread {
        let value = why.value();
        match causes.iter_mut().find(|(k, _)| *k == value) {
            Some((_, n)) => *n += 1,
            None => causes.push((value, 1)),
        }
    }
    causes.sort_by(|a, b| a.0.cmp(b.0));
    UnreadFields {
        unread: unread.len() as i64,
        unread_causes: causes
            .into_iter()
            .map(|(k, n)| (unicode::cps(k), n))
            .collect(),
        unread_ids: unread
            .iter()
            .take(COMMIT_DRIFT_DEBT_CAP)
            .map(|(id, _)| id.clone())
            .collect(),
    }
}

/// `_unread_dict`: nothing where `unread` is zero; else the three keys,
/// the causes in the order given (`dict(causes)`, their keys distinct, as
/// a dict's are; a repeated key is refused in debug builds) and the ids as
/// given (`list(ids)`: a list, so a repeated id is copied as given).
pub fn unread_dict(
    unread: i64,
    causes: &[(PyStr, i64)],
    ids: &[PyStr],
) -> Vec<(&'static str, PyValue)> {
    if unread == 0 {
        return Vec::new();
    }
    vec![
        ("unread", int(unread)),
        ("unread_causes", tally(causes)),
        ("unread_ids", texts(ids)),
    ]
}

/// `CommitDriftDebt`: the drift rollup anchored to the caller's repo.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CommitDriftDebt {
    pub current_repo: Option<PyStr>,
    pub current_cwd: Option<PyStr>,
    pub rows: Vec<CommitDriftRow>,
    pub total_drifted: i64,
    pub unconfirmed: i64,
    pub unread: i64,
    /// The count per cause, its keys distinct as a dict's are.
    pub unread_causes: Vec<(PyStr, i64)>,
    /// The ids as the caller lists them (`list[str]`), copied as given.
    pub unread_ids: Vec<PyStr>,
}

impl CommitDriftDebt {
    /// The dataclass's constructor with every defaulted field at its
    /// default.
    pub fn new(current_repo: Option<PyStr>, current_cwd: Option<PyStr>) -> Self {
        CommitDriftDebt {
            current_repo,
            current_cwd,
            ..Self::default()
        }
    }

    /// `to_dict`: `unconfirmed` when it is not zero (a negative count
    /// too), then the unread keys when `unread` is not zero.
    pub fn to_dict(&self) -> PyValue {
        let mut out = vec![
            ("current_repo", opt_text(self.current_repo.as_deref())),
            ("current_cwd", opt_text(self.current_cwd.as_deref())),
            ("total_drifted", int(self.total_drifted)),
            ("rows", drift_rows(&self.rows)),
        ];
        if self.unconfirmed != 0 {
            out.push(("unconfirmed", int(self.unconfirmed)));
        }
        out.extend(unread_dict(
            self.unread,
            &self.unread_causes,
            &self.unread_ids,
        ));
        dict(out)
    }
}

/// `CrossRepoDriftGroup`: one foreign origin's drift rollup.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CrossRepoDriftGroup {
    pub repo: PyStr,
    pub worktree_root: PyStr,
    pub rows: Vec<CommitDriftRow>,
    pub total_drifted: i64,
    pub candidates: i64,
    pub unread: i64,
    /// The count per cause, its keys distinct as a dict's are.
    pub unread_causes: Vec<(PyStr, i64)>,
    /// The ids as the caller lists them (`list[str]`), copied as given.
    pub unread_ids: Vec<PyStr>,
    pub last_checked_at: Option<DateTime>,
}

impl CrossRepoDriftGroup {
    /// The dataclass's constructor with every defaulted field at its
    /// default.
    pub fn new(repo: PyStr, worktree_root: PyStr) -> Self {
        CrossRepoDriftGroup {
            repo,
            worktree_root,
            ..Self::default()
        }
    }

    pub fn to_dict(&self) -> PyValue {
        let mut out = vec![
            ("repo", text(&self.repo)),
            ("worktree_root", text(&self.worktree_root)),
            ("total_drifted", int(self.total_drifted)),
            ("candidates", int(self.candidates)),
            ("rows", drift_rows(&self.rows)),
            ("last_checked_at", opt_iso(self.last_checked_at.as_ref())),
        ];
        out.extend(unread_dict(
            self.unread,
            &self.unread_causes,
            &self.unread_ids,
        ));
        dict(out)
    }
}

/// `CrossRepoDrift`: the foreign groups, the skipped entries (each a
/// `dict[str, Any]` copied as given) and the coverage counts.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CrossRepoDrift {
    pub groups: Vec<CrossRepoDriftGroup>,
    pub skipped: Vec<PyValue>,
    pub total_drifted: i64,
    pub unanchorable: i64,
}

impl CrossRepoDrift {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("total_drifted", int(self.total_drifted)),
            (
                "groups",
                PyValue::List(
                    self.groups
                        .iter()
                        .map(CrossRepoDriftGroup::to_dict)
                        .collect(),
                ),
            ),
            ("skipped", PyValue::List(self.skipped.clone())),
            ("unanchorable", int(self.unanchorable)),
        ])
    }
}

/// `ColdEndorsementMemories`: the capped rows and the uncapped total.
#[derive(Clone, Debug, PartialEq)]
pub struct ColdEndorsementMemories {
    pub min_retrievals: i64,
    pub rows: Vec<MemoryStats>,
    pub total: i64,
}

impl ColdEndorsementMemories {
    /// The dataclass's constructor with no rows and a zero total.
    pub fn new(min_retrievals: i64) -> Self {
        ColdEndorsementMemories {
            min_retrievals,
            rows: Vec::new(),
            total: 0,
        }
    }

    /// `to_dict`; TypeError from the first row that raises.
    pub fn to_dict(&self) -> Answer<PyValue> {
        Ok(dict(vec![
            ("min_retrievals", int(self.min_retrievals)),
            ("total", int(self.total)),
            ("rows", stats_list(&self.rows)?),
        ]))
    }
}

/// `SilentMissStats`: the audit and miss counts of the window.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SilentMissStats {
    pub audited_total: i64,
    pub miss_total: i64,
    pub unique_miss_memories: i64,
    pub no_signal_total: i64,
}

impl SilentMissStats {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("audited_total", int(self.audited_total)),
            ("miss_total", int(self.miss_total)),
            ("unique_miss_memories", int(self.unique_miss_memories)),
            ("no_signal_total", int(self.no_signal_total)),
        ])
    }
}

/// `RecentSilentMiss`: one unacknowledged miss for triage.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RecentSilentMiss {
    pub event_id: Option<PyStr>,
    pub top_hit_id: Option<PyStr>,
    pub query_preview: Option<PyStr>,
    pub ts: Option<PyStr>,
}

impl RecentSilentMiss {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("event_id", opt_text(self.event_id.as_deref())),
            ("top_hit_id", opt_text(self.top_hit_id.as_deref())),
            ("query_preview", opt_text(self.query_preview.as_deref())),
            ("ts", opt_text(self.ts.as_deref())),
        ])
    }
}
