//! The Stop audit's settlement half (`hook.py` 728-946, unit hook-2b):
//! `_settle_stop_turn`, `_record_stop_rows`, `_count_stop_reviews` and
//! `_settle_stop_interrupted`, as [`Settlement`], the
//! [`StopSettle`] that `hook_stop::audit_turn` runs over any [`StopSide`].

use std::collections::BTreeSet;

use crate::hook_stop::{
    Stop, StopSettle, StopSide, DEFAULT_HARNESS, REAUDIT_DEDUP_WINDOW_SECONDS, TRIGGERED_FROM,
};

/// The warning `_settle_stop_interrupted` logs for an `Exception` it
/// swallows: `"%s: %s" % (class name, str(exc))` after the prefix.
pub const INTERRUPTED_WARNING: &str = "interrupted turns not settled at the Stop: ";

/// The four settlement steps of `hook.py`, in the order `_audit_turn`
/// calls them.
#[derive(Debug, Default, Clone, Copy)]
pub struct Settlement;

impl<S: StopSide> StopSettle<S> for Settlement {
    fn settle_stop_turn(&mut self, side: &mut S, stop: &mut Stop<S>) -> Result<i64, S::Error> {
        settle_stop_turn(side, stop)
    }
    fn record_stop_rows(
        &mut self,
        side: &mut S,
        stop: &mut Stop<S>,
        settled_count: i64,
    ) -> Result<(), S::Error> {
        record_stop_rows(side, stop, settled_count)
    }
    fn count_stop_reviews(&mut self, side: &mut S, stop: &mut Stop<S>) -> Result<(), S::Error> {
        count_stop_reviews(side, stop)
    }
    fn settle_stop_interrupted(
        &mut self,
        side: &mut S,
        stop: &mut Stop<S>,
    ) -> Result<(), S::Error> {
        settle_stop_interrupted(side, stop)
    }
}

/// `_settle_stop_turn`: the sessions are joined first, always; a turn
/// settles only with an owner prompt (even `""`) and a truthy reply, and
/// the count is the attributions' plus the shown pointers'.
pub fn settle_stop_turn<S: StopSide>(side: &mut S, stop: &mut Stop<S>) -> Result<i64, S::Error> {
    let sessions = side.joined_sessions(
        &stop.recent,
        &stop.session_id,
        stop.worktree_root.as_deref(),
    )?;
    let reply = match (&stop.user_message, stop.assistant_response.as_deref()) {
        (Some(_), Some(reply)) if !reply.is_empty() => reply,
        _ => return Ok(0),
    };
    let settled = side.emit_hook_attributions(
        &stop.store,
        &mut stop.recorder,
        &stop.recent,
        &stop.session_id,
        reply,
        stop.worktree_root.as_deref(),
        stop.client_model.as_deref(),
        stop.server_session.as_deref(),
    )?;
    let shown = side.settle_shown(
        &stop.store,
        &mut stop.recorder,
        &mut stop.recall_state,
        &stop.session_id,
        stop.harness.as_deref(),
        reply,
        &sessions,
        &settled,
        stop.prompt_id.as_deref(),
        stop.client_model.as_deref(),
        &stop.now,
    )?;
    Ok(len_i64(settled.len()) + len_i64(shown.len()))
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `_record_stop_rows`: `turn_audited` always (its fields built before the
/// row is recorded), then `search_miss` for a miss that is not a repeat.
pub fn record_stop_rows<S: StopSide>(
    side: &mut S,
    stop: &mut Stop<S>,
    settled_count: i64,
) -> Result<(), S::Error> {
    let rule = side.rule();
    let fields = side.turn_audited_fields(
        &stop.report,
        &stop.session_id,
        &stop.probe_mode,
        stop.assistant_response.is_some(),
        TRIGGERED_FROM,
        stop.repeat,
        stop.client_model.as_deref(),
        stop.prompt_id.as_deref(),
        stop.reply_source.as_deref(),
        &rule,
        settled_count,
    )?;
    side.record(&mut stop.recorder, "turn_audited", fields)?;
    if side.report_is_miss(&stop.report) && !stop.repeat {
        let fields = side.search_miss_fields(
            &stop.report,
            &stop.session_id,
            TRIGGERED_FROM,
            stop.client_model.as_deref(),
            &rule,
        )?;
        side.record(&mut stop.recorder, "search_miss", fields)?;
    }
    Ok(())
}

/// `_count_stop_reviews`: the window opens at the previous audit, else at
/// `now` less the re-audit window, raised to the segment's start when that
/// start lies strictly between the two; the sessions fall back to the
/// server session alone (or none) when the harness joins none.
pub fn count_stop_reviews<S: StopSide>(side: &mut S, stop: &mut Stop<S>) -> Result<(), S::Error> {
    let since = match &stop.previous_audit {
        Some(previous) => previous.clone(),
        None => {
            let since = side.seconds_before(&stop.now, REAUDIT_DEDUP_WINDOW_SECONDS);
            let harness = match stop.harness.as_deref() {
                Some(h) if !h.is_empty() => h,
                _ => DEFAULT_HARNESS,
            };
            let first_seen = side.segment_started_at(
                &mut stop.recall_state,
                &stop.store,
                (harness, &stop.session_id),
                &stop.now,
            )?;
            if since < first_seen && first_seen < stop.now {
                first_seen
            } else {
                since
            }
        }
    };
    let events = side.events_of_harness_stamped_kinds(&stop.store, &since)?;
    let mut sessions = side.harness_joined_sessions(&events, &stop.session_id)?;
    if sessions.is_empty() {
        sessions = stop.server_session.iter().cloned().collect::<BTreeSet<_>>();
    }
    side.review_unanswered(
        &stop.store,
        &mut stop.recorder,
        &mut stop.recall_state,
        &stop.session_id,
        stop.harness.as_deref(),
        &sessions,
        &since,
        &stop.now,
    )
}

/// `_settle_stop_interrupted`: an `Exception` the walk raises becomes one
/// warning naming its class and text; anything else propagates.
pub fn settle_stop_interrupted<S: StopSide>(
    side: &mut S,
    stop: &mut Stop<S>,
) -> Result<(), S::Error> {
    match side.settle_interrupted(stop) {
        Ok(()) => Ok(()),
        Err(error) if side.is_exception(&error) => {
            let (class, text) = side.error_parts(&error);
            side.log_warning(&format!("{INTERRUPTED_WARNING}{class}: {text}"));
            Ok(())
        }
        Err(error) => Err(error),
    }
}
