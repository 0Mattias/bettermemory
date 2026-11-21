//! What the Stop audit (`hook.py` 319-946) asks of the modules not yet
//! ported: one method of [`StopSide`] per call the reference makes, named
//! after it. `hook_stop` runs the audit's front half over it; the
//! settlement steps (`_settle_stop_turn`, `_record_stop_rows`,
//! `_count_stop_reviews`, `_settle_stop_interrupted`) call the methods
//! below the line "the settlement's calls" and reach the front half through
//! [`crate::hook_stop::StopSettle`]. No implementation is built yet.

use std::collections::BTreeSet;

/// `audit.REAUDIT_DEDUP_WINDOW_SECONDS` (`audit.py` 196): the widest window
/// the Stop reads (`_stop_window`) and the re-audit dedup's. Its home is
/// `audit`, which is not ported yet.
pub const REAUDIT_DEDUP_WINDOW_SECONDS: i64 = 3600;

/// `audit.ATTRIBUTION_LOOKBACK_SECONDS` (`audit.py` 185), the lookback
/// `no_owner_prompt_report` is given.
pub const ATTRIBUTION_LOOKBACK_SECONDS: i64 = 600;

/// The recall key's harness when the caller names none, or names `""`
/// (`harness or "claude-code"`).
pub const DEFAULT_HARNESS: &str = "claude-code";

/// The probe mode when the config's `search_mode` is None or `""`
/// (`cfg.behavior.search_mode or "hybrid"`).
pub const DEFAULT_SEARCH_MODE: &str = "hybrid";

/// `triggered_from` on every row and call of the Stop.
pub const TRIGGERED_FROM: &str = "stop_hook";

/// The hook event `run_audit` runs under (`@hook_body("stop")`).
pub const STOP_EVENT: &str = "stop";

/// What `_probe_message` is given (`recall_settle._probe_message`'s
/// keywords; `now` is the `utcnow()` read just before the call).
pub struct ProbeInput<'a, S: StopSide> {
    pub cfg: &'a S::Config,
    pub store: &'a S::Store,
    pub user_message: &'a str,
    pub session_id: &'a str,
    pub server_session: Option<&'a str>,
    pub admission: &'a S::Admission,
    pub excluded_scopes: &'a BTreeSet<String>,
    pub recent: &'a [S::Event],
    pub worktree_root: Option<&'a str>,
    pub now: &'a S::Time,
}

/// Every call the Stop audit makes outside `hook.py`, and the Python
/// objects it passes between them as associated types. An error is the
/// exception the reference raises there; it propagates out of the audit
/// as the reference's does.
pub trait StopSide: Sized {
    type Error;
    type Store;
    type Config;
    type Report;
    type Admission;
    /// The second half of `caller_admission`'s answer, which the Stop drops.
    type Resolution;
    type RecallState;
    type Origin;
    /// `transcript.stop_turn`'s turn, read by `audit_transcript` (hook-2c).
    type Turn;
    type Recorder;
    /// One row of `Store.events_since`.
    type Event;
    type Time: Clone + PartialOrd;
    type Path;
    /// What the hook `posted` (a mapping).
    type Posted;
    /// `MissReport.to_dict()`'s answer, `run_audit`'s.
    type Dict;
    /// A row's fields (`turn_audited_fields`, `search_miss_fields`).
    type Fields;

    /// `events.within_hook_budget(event, store)` around `body`
    /// (`hook_body`): the store is handed back to the body.
    fn within_hook_budget<R, F>(
        &mut self,
        event: &str,
        store: Option<Self::Store>,
        body: F,
    ) -> Result<R, Self::Error>
    where
        F: FnOnce(&mut Self, Option<Self::Store>) -> Result<R, Self::Error>;
    /// `with events.suppressed(): body`.
    fn suppressed<R, F>(&mut self, body: F) -> Result<R, Self::Error>
    where
        F: FnOnce(&mut Self) -> Result<R, Self::Error>;
    /// `config.load_config(None)`.
    fn load_config(&mut self) -> Result<Self::Config, Self::Error>;
    /// `cfg.resolved_directory()`.
    fn resolved_directory(&mut self, cfg: &Self::Config) -> Result<Self::Path, Self::Error>;
    /// `root / STORE_FILENAME`.
    fn store_file(&mut self, root: &Self::Path) -> Self::Path;
    /// `Store.open_or_create(path)`.
    fn open_or_create(&mut self, path: &Self::Path) -> Result<Self::Store, Self::Error>;
    /// `hook._utcnow()`.
    fn hook_utcnow(&mut self) -> Result<Self::Time, Self::Error>;
    /// `time - timedelta(seconds=seconds)`.
    fn seconds_before(&mut self, time: &Self::Time, seconds: i64) -> Self::Time;
    /// `store.events_since(since)`.
    fn events_since(
        &mut self,
        store: &Self::Store,
        since: &Self::Time,
    ) -> Result<Vec<Self::Event>, Self::Error>;
    /// `identity.bind_transcript(session_id=, model=)`.
    fn bind_transcript(&mut self, session_id: &str, model: Option<&str>)
        -> Result<(), Self::Error>;
    /// `hook._origin_for(cwd)` (hook-2c).
    fn origin_for(&mut self, cwd: Option<&Self::Path>) -> Result<Self::Origin, Self::Error>;
    /// `origin.worktree_root`.
    fn worktree_root(&self, origin: &Self::Origin) -> Option<String>;
    /// `sessions.bridged_session(recent, harness_session=, worktree_root=)`.
    fn bridged_session(
        &mut self,
        recent: &[Self::Event],
        harness_session: &str,
        worktree_root: Option<&str>,
    ) -> Result<Option<String>, Self::Error>;
    /// `recall.caller_admission(POOL_PREFILTER, store=, cfg=, cwd=, origin=,
    /// posted=, session_id=)`.
    #[allow(clippy::too_many_arguments)]
    fn caller_admission(
        &mut self,
        store: &Self::Store,
        cfg: &Self::Config,
        cwd: Option<&Self::Path>,
        origin: &Self::Origin,
        posted: Option<&Self::Posted>,
        session_id: &str,
    ) -> Result<(Self::Admission, Self::Resolution), Self::Error>;
    /// `sessions._disabled_scopes_from_events(recent, worktree_root=,
    /// server_session=)`.
    fn disabled_scopes_from_events(
        &mut self,
        recent: &[Self::Event],
        worktree_root: Option<&str>,
        server_session: Option<&str>,
    ) -> Result<BTreeSet<String>, Self::Error>;
    /// `models.utcnow()`.
    fn utcnow(&mut self) -> Result<Self::Time, Self::Error>;
    /// `audit.no_owner_prompt_report(session_id=, now=, lookback_seconds=)`.
    fn no_owner_prompt_report(
        &mut self,
        session_id: &str,
        now: &Self::Time,
        lookback_seconds: i64,
    ) -> Result<Self::Report, Self::Error>;
    /// `recall_settle._probe_message(...)`.
    fn probe_message(&mut self, input: ProbeInput<'_, Self>) -> Result<Self::Report, Self::Error>;
    /// `cfg.telemetry.enabled`.
    fn telemetry_enabled(&self, cfg: &Self::Config) -> bool;
    /// `cfg.behavior.search_mode`.
    fn search_mode(&self, cfg: &Self::Config) -> Option<String>;
    /// `Recorder(store=, session_id=, enabled=, worktree_root=)`.
    fn new_recorder(
        &mut self,
        store: &Self::Store,
        session_id: &str,
        enabled: bool,
        worktree_root: Option<&str>,
    ) -> Result<Self::Recorder, Self::Error>;
    /// `recall.RecallState()`.
    fn new_recall_state(&mut self) -> Result<Self::RecallState, Self::Error>;
    /// `recall.confirm_compaction(store=, state=, key=, transcript_path=,
    /// cfg=, triggered_from=, now=, origin=)`.
    #[allow(clippy::too_many_arguments)]
    fn confirm_compaction(
        &mut self,
        store: &Self::Store,
        state: &mut Self::RecallState,
        key: (&str, &str),
        transcript_path: Option<&str>,
        cfg: &Self::Config,
        triggered_from: &str,
        now: &Self::Time,
        origin: &Self::Origin,
    ) -> Result<(), Self::Error>;
    /// `recall.previous_audit_at(store, session_id, now=)`.
    fn previous_audit_at(
        &mut self,
        store: &Self::Store,
        session_id: &str,
        now: &Self::Time,
    ) -> Result<Option<Self::Time>, Self::Error>;
    /// `events.redact_query(text)["hash"]`.
    fn redact_query_hash(&mut self, text: &str) -> Result<String, Self::Error>;
    /// `audit.is_duplicate_audit(recent, session_id=, probe_query_hash=,
    /// probe_query_text=, now=)`.
    fn is_duplicate_audit(
        &mut self,
        recent: &[Self::Event],
        session_id: &str,
        probe_query_hash: &str,
        probe_query_text: &str,
        now: &Self::Time,
    ) -> Result<bool, Self::Error>;
    /// `report.to_dict()`.
    fn report_to_dict(&mut self, report: &Self::Report) -> Result<Self::Dict, Self::Error>;

    // The settlement's calls (`hook.py` 728-946, lane hookstopb).

    /// `sessions.joined_sessions(recent, harness_session=, worktree_root=)`.
    fn joined_sessions(
        &mut self,
        recent: &[Self::Event],
        harness_session: &str,
        worktree_root: Option<&str>,
    ) -> Result<BTreeSet<String>, Self::Error>;
    /// `hook._emit_hook_attributions(store=, recorder=, recent_events=,
    /// session_id=, assistant_response=, worktree_root=, client_model=,
    /// server_session=)`: the ids it settled.
    #[allow(clippy::too_many_arguments)]
    fn emit_hook_attributions(
        &mut self,
        store: &Self::Store,
        recorder: &mut Self::Recorder,
        recent_events: &[Self::Event],
        session_id: &str,
        assistant_response: &str,
        worktree_root: Option<&str>,
        client_model: Option<&str>,
        server_session: Option<&str>,
    ) -> Result<Vec<String>, Self::Error>;
    /// `recall.settle_shown(store=, recorder=, state=, session_id=, harness=,
    /// assistant_response=, sessions=, exclude=, prompt_id=, client_model=,
    /// now=)`: the ids it settled.
    #[allow(clippy::too_many_arguments)]
    fn settle_shown(
        &mut self,
        store: &Self::Store,
        recorder: &mut Self::Recorder,
        state: &mut Self::RecallState,
        session_id: &str,
        harness: Option<&str>,
        assistant_response: &str,
        sessions: &BTreeSet<String>,
        exclude: &[String],
        prompt_id: Option<&str>,
        client_model: Option<&str>,
        now: &Self::Time,
    ) -> Result<Vec<String>, Self::Error>;
    /// `recall.RULE`.
    fn rule(&self) -> String;
    /// `audit.turn_audited_fields(report, session_id=, probe_mode=,
    /// assistant_present=, triggered_from=, repeat=, client_model=,
    /// prompt_id=, reply_source=, rule=, settled_count=)`.
    #[allow(clippy::too_many_arguments)]
    fn turn_audited_fields(
        &mut self,
        report: &Self::Report,
        session_id: &str,
        probe_mode: &str,
        assistant_present: bool,
        triggered_from: &str,
        repeat: bool,
        client_model: Option<&str>,
        prompt_id: Option<&str>,
        reply_source: Option<&str>,
        rule: &str,
        settled_count: i64,
    ) -> Result<Self::Fields, Self::Error>;
    /// `audit.search_miss_fields(report, session_id=, triggered_from=,
    /// client_model=, rule=)`.
    fn search_miss_fields(
        &mut self,
        report: &Self::Report,
        session_id: &str,
        triggered_from: &str,
        client_model: Option<&str>,
        rule: &str,
    ) -> Result<Self::Fields, Self::Error>;
    /// `recorder.record(kind, **fields)`.
    fn record(
        &mut self,
        recorder: &mut Self::Recorder,
        kind: &str,
        fields: Self::Fields,
    ) -> Result<(), Self::Error>;
    /// `report.is_miss`.
    fn report_is_miss(&self, report: &Self::Report) -> bool;
    /// `state.segment(store, key, now=).started_at`.
    fn segment_started_at(
        &mut self,
        state: &mut Self::RecallState,
        store: &Self::Store,
        key: (&str, &str),
        now: &Self::Time,
    ) -> Result<Self::Time, Self::Error>;
    /// `store.events_of_kinds(HARNESS_STAMPED_KINDS, since=)`.
    fn events_of_harness_stamped_kinds(
        &mut self,
        store: &Self::Store,
        since: &Self::Time,
    ) -> Result<Vec<Self::Event>, Self::Error>;
    /// `sessions.harness_joined_sessions(events, session_id)`.
    fn harness_joined_sessions(
        &mut self,
        events: &[Self::Event],
        session_id: &str,
    ) -> Result<BTreeSet<String>, Self::Error>;
    /// `recall.review_unanswered(store=, recorder=, state=, session_id=,
    /// harness=, sessions=, since=, now=)`.
    #[allow(clippy::too_many_arguments)]
    fn review_unanswered(
        &mut self,
        store: &Self::Store,
        recorder: &mut Self::Recorder,
        state: &mut Self::RecallState,
        session_id: &str,
        harness: Option<&str>,
        sessions: &BTreeSet<String>,
        since: &Self::Time,
        now: &Self::Time,
    ) -> Result<(), Self::Error>;
    /// `recall.settle_interrupted(...)` with `triggered_from="stop_hook"`
    /// and `exclude_prompt_id=prompt_id`; the arguments are the Stop's.
    fn settle_interrupted(
        &mut self,
        stop: &crate::hook_stop::Stop<Self>,
    ) -> Result<(), Self::Error>;
    /// True when `except Exception` catches `error` (not a
    /// `BaseException` alone).
    fn is_exception(&self, error: &Self::Error) -> bool;
    /// `error.__class__.__name__` and `str(error)`.
    fn error_parts(&self, error: &Self::Error) -> (String, String);
    /// `log.warning(message)` on `bettermemory.hook`'s logger.
    fn log_warning(&mut self, message: &str);
}
