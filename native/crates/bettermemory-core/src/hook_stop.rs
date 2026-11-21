//! The Stop audit's front half (`hook.py` 319-725): `run_audit` with its
//! `@hook_body("stop")`, `_audit_turn`, `_stop_window`, `_stop_pool`,
//! `_stop_probe`, `_stop_recorder` and `_is_repeat_audit`, as one
//! sequence over [`StopSide`]. The settlement steps are [`StopSettle`]'s.

use std::collections::BTreeSet;

pub use crate::hook_stop_side::{
    ProbeInput, StopSide, ATTRIBUTION_LOOKBACK_SECONDS, DEFAULT_HARNESS, DEFAULT_SEARCH_MODE,
    REAUDIT_DEDUP_WINDOW_SECONDS, STOP_EVENT, TRIGGERED_FROM,
};

/// `run_audit`'s keyword arguments.
pub struct AuditInput<S: StopSide> {
    pub user_message: Option<String>,
    pub assistant_response: Option<String>,
    pub session_id: String,
    pub client_model: Option<String>,
    pub config: Option<S::Config>,
    pub dry_run: bool,
    pub store: Option<S::Store>,
    pub cwd: Option<S::Path>,
    pub harness: Option<String>,
    pub prompt_id: Option<String>,
    pub reply_source: Option<String>,
    pub transcript_path: Option<String>,
    pub state: Option<S::RecallState>,
    pub posted: Option<S::Posted>,
}

/// What `_audit_turn` holds when it reaches the settlement steps: each
/// field is the local of the same name there (`store` and `cfg` resolved,
/// `recall_state` made, `worktree_root` the caller origin's).
pub struct Stop<S: StopSide> {
    pub store: S::Store,
    pub cfg: S::Config,
    pub recorder: S::Recorder,
    pub recent: Vec<S::Event>,
    pub session_id: String,
    pub user_message: Option<String>,
    pub assistant_response: Option<String>,
    pub worktree_root: Option<String>,
    pub client_model: Option<String>,
    pub server_session: Option<String>,
    pub recall_state: S::RecallState,
    pub harness: Option<String>,
    pub prompt_id: Option<String>,
    pub reply_source: Option<String>,
    pub transcript_path: Option<String>,
    pub now: S::Time,
    pub previous_audit: Option<S::Time>,
    pub admission: S::Admission,
    pub excluded_scopes: BTreeSet<String>,
    pub report: S::Report,
    pub probe_mode: String,
    pub repeat: bool,
}

/// The settlement steps `_audit_turn` calls in order (`hook.py` 728-946,
/// lane hookstopb).
pub trait StopSettle<S: StopSide> {
    /// `_settle_stop_turn`: how many memories the turn settled.
    fn settle_stop_turn(&mut self, side: &mut S, stop: &mut Stop<S>) -> Result<i64, S::Error>;
    /// `_record_stop_rows(recorder, report, ..., settled_count=)`.
    fn record_stop_rows(
        &mut self,
        side: &mut S,
        stop: &mut Stop<S>,
        settled_count: i64,
    ) -> Result<(), S::Error>;
    /// `_count_stop_reviews`.
    fn count_stop_reviews(&mut self, side: &mut S, stop: &mut Stop<S>) -> Result<(), S::Error>;
    /// `_settle_stop_interrupted`.
    fn settle_stop_interrupted(&mut self, side: &mut S, stop: &mut Stop<S>)
        -> Result<(), S::Error>;
}

/// `run_audit`: under the Stop's hook budget, a dry run runs itself once
/// more inside `suppressed()` with `state` None; otherwise `_audit_turn`.
pub fn run_audit<S: StopSide, T: StopSettle<S>>(
    side: &mut S,
    settle: &mut T,
    mut input: AuditInput<S>,
) -> Result<S::Dict, S::Error> {
    let store = input.store.take();
    side.within_hook_budget(STOP_EVENT, store, |side, store| {
        input.store = store;
        if input.dry_run {
            input.dry_run = false;
            input.state = None;
            return side.suppressed(|side| run_audit(side, settle, input));
        }
        audit_turn(side, settle, input)
    })
}

/// `_audit_turn`.
pub fn audit_turn<S: StopSide, T: StopSettle<S>>(
    side: &mut S,
    settle: &mut T,
    input: AuditInput<S>,
) -> Result<S::Dict, S::Error> {
    let cfg = match input.config {
        Some(cfg) => cfg,
        None => side.load_config()?,
    };
    let store = match input.store {
        Some(store) => store,
        None => {
            let root = side.resolved_directory(&cfg)?;
            let path = side.store_file(&root);
            side.open_or_create(&path)?
        }
    };
    let recent = stop_window(side, &store)?;
    let session_id = input.session_id;
    side.bind_transcript(&session_id, input.client_model.as_deref())?;
    let origin = side.origin_for(input.cwd.as_ref())?;
    let worktree_root = side.worktree_root(&origin);
    let (server_session, admission, excluded_scopes) = stop_pool(
        side,
        &recent,
        &store,
        &cfg,
        input.cwd.as_ref(),
        &origin,
        input.posted.as_ref(),
        &session_id,
    )?;
    let user_message = input.user_message;
    let report = match &user_message {
        None => {
            let now = side.utcnow()?;
            side.no_owner_prompt_report(&session_id, &now, ATTRIBUTION_LOOKBACK_SECONDS)?
        }
        Some(message) => {
            let now = side.utcnow()?;
            side.probe_message(ProbeInput {
                cfg: &cfg,
                store: &store,
                user_message: message,
                session_id: &session_id,
                server_session: server_session.as_deref(),
                admission: &admission,
                excluded_scopes: &excluded_scopes,
                recent: &recent,
                worktree_root: worktree_root.as_deref(),
                now: &now,
            })?
        }
    };
    let enabled = side.telemetry_enabled(&cfg);
    let recorder = side.new_recorder(&store, &session_id, enabled, worktree_root.as_deref())?;
    let probe_mode = side
        .search_mode(&cfg)
        .filter(|mode| !mode.is_empty())
        .unwrap_or_else(|| DEFAULT_SEARCH_MODE.to_string());
    let now = side.utcnow()?;
    let mut recall_state = match input.state {
        Some(state) => state,
        None => side.new_recall_state()?,
    };
    let harness_key = match input.harness.as_deref() {
        Some(harness) if !harness.is_empty() => harness.to_string(),
        _ => DEFAULT_HARNESS.to_string(),
    };
    side.confirm_compaction(
        &store,
        &mut recall_state,
        (&harness_key, &session_id),
        input.transcript_path.as_deref(),
        &cfg,
        TRIGGERED_FROM,
        &now,
        &origin,
    )?;
    let previous_audit = side.previous_audit_at(&store, &session_id, &now)?;
    let repeat = is_repeat_audit(side, &recent, &session_id, user_message.as_deref())?;
    let mut stop = Stop {
        store,
        cfg,
        recorder,
        recent,
        session_id,
        user_message,
        assistant_response: input.assistant_response,
        worktree_root,
        client_model: input.client_model,
        server_session,
        recall_state,
        harness: input.harness,
        prompt_id: input.prompt_id,
        reply_source: input.reply_source,
        transcript_path: input.transcript_path,
        now,
        previous_audit,
        admission,
        excluded_scopes,
        report,
        probe_mode,
        repeat,
    };
    let settled_count = settle.settle_stop_turn(side, &mut stop)?;
    settle.record_stop_rows(side, &mut stop, settled_count)?;
    settle.count_stop_reviews(side, &mut stop)?;
    settle.settle_stop_interrupted(side, &mut stop)?;
    side.report_to_dict(&stop.report)
}

/// `_stop_window`: the store's events since `_utcnow()` less
/// `REAUDIT_DEDUP_WINDOW_SECONDS`.
pub fn stop_window<S: StopSide>(side: &mut S, store: &S::Store) -> Result<Vec<S::Event>, S::Error> {
    let now = side.hook_utcnow()?;
    let since = side.seconds_before(&now, REAUDIT_DEDUP_WINDOW_SECONDS);
    side.events_since(store, &since)
}

/// `_stop_pool`: (server session, admission, scopes off).
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn stop_pool<S: StopSide>(
    side: &mut S,
    recent: &[S::Event],
    store: &S::Store,
    cfg: &S::Config,
    cwd: Option<&S::Path>,
    origin: &S::Origin,
    posted: Option<&S::Posted>,
    session_id: &str,
) -> Result<(Option<String>, S::Admission, BTreeSet<String>), S::Error> {
    let worktree_root = side.worktree_root(origin);
    let server_session = side.bridged_session(recent, session_id, worktree_root.as_deref())?;
    let (admission, _resolution) =
        side.caller_admission(store, cfg, cwd, origin, posted, session_id)?;
    let excluded_scopes = side.disabled_scopes_from_events(
        recent,
        worktree_root.as_deref(),
        server_session.as_deref(),
    )?;
    Ok((server_session, admission, excluded_scopes))
}

/// `_is_repeat_audit`: never for a turn with no owner prompt; otherwise
/// the dedup's answer over the prompt's redacted hash.
pub fn is_repeat_audit<S: StopSide>(
    side: &mut S,
    recent: &[S::Event],
    session_id: &str,
    user_message: Option<&str>,
) -> Result<bool, S::Error> {
    let Some(message) = user_message else {
        return Ok(false);
    };
    let hash = side.redact_query_hash(message)?;
    let now = side.utcnow()?;
    side.is_duplicate_audit(recent, session_id, &hash, message, &now)
}
