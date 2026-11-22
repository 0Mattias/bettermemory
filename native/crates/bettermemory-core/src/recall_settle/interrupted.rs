//! `recall_settle.py`'s second unit: `_probe_message` (48-153), the one
//! probe both hooks run, and `settle_interrupted` (368-463), the
//! `turn_audited` rows of the turns the owner interrupted.
//!
//! `audit.probe_for_miss`, `handlers.search.resolve_search_pool` and
//! `transcript.interrupted_turns` are closures; `Path.expanduser` is a
//! closure answering `str(Path(p).expanduser())` as the host spells it,
//! and `is_file` asks the file system (`fsdata::Fs::stat`). The four
//! settings the reference reads off the configuration are a plain
//! struct.

use std::collections::BTreeSet;

use super::{
    either, field, seconds_before, Recorder, RecorderSpec, SettleStore, RULE, STATE_IDLE_SECONDS,
};
use crate::audit::report::{turn_audited_fields, AuditError, MissReport, TurnAuditedOptions};
use crate::datetime::DateTime;
use crate::fsdata::{Fs, Kind, OsError, PyErr};
use crate::hook::ATTRIBUTION_LOOKBACK_SECONDS;
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::transcript::InterruptedTurn;
use crate::unicode::cps;

/// The configuration fields this unit reads: `behavior.search_mode`,
/// `behavior.recency_boost_half_life_days`, `behavior.conversational`
/// and `telemetry.enabled`.
#[derive(Clone, Debug, PartialEq)]
pub struct Settings {
    pub search_mode: PyStr,
    pub half_life_days: f64,
    pub conversational: bool,
    pub telemetry_enabled: bool,
}

impl Settings {
    /// `cfg.behavior.search_mode or "hybrid"`.
    pub fn probe_mode(&self) -> PyStr {
        if self.search_mode.is_empty() {
            cps("hybrid")
        } else {
            self.search_mode.clone()
        }
    }
}

/// `handlers.search.SearchPool`: the pool's records and its statistics
/// provider, both handed to the probe as they are.
pub struct SearchPool<M, C> {
    pub memories: Vec<M>,
    pub corpus_stats_provider: C,
}

/// The arguments `_probe_message` hands `probe_for_miss`, in its order.
pub struct ProbeCall<'a, M, C, A> {
    pub memories: Vec<M>,
    pub user_message: &'a [u32],
    pub recent_events: &'a [PyValue],
    pub session_id: &'a [u32],
    pub retrieval_session_id: Option<&'a [u32]>,
    pub now: &'a DateTime,
    pub lookback_seconds: i64,
    pub admission: &'a A,
    pub worktree_root: Option<&'a [u32]>,
    pub excluded_scopes: &'a BTreeSet<PyStr>,
    pub mode: PyStr,
    pub half_life_days: f64,
    pub conversational: bool,
    pub corpus_stats_provider: C,
    pub floor: bool,
    pub searched: bool,
}

/// `resolve_search_pool(store, query, excluded_scopes=, admission=)`.
pub type PoolFn<'f, S, A, M, C, E> =
    dyn FnMut(&mut S, &[u32], &BTreeSet<PyStr>, &A) -> Result<SearchPool<M, C>, E> + 'f;

/// `audit.probe_for_miss(...)`.
pub type ProbeFn<'f, M, C, A, E> = dyn FnMut(ProbeCall<'_, M, C, A>) -> Result<MissReport, E> + 'f;

/// The two calls `_probe_message` makes.
pub struct Prober<'f, S, A, M, C, E> {
    pub resolve_pool: &'f mut PoolFn<'f, S, A, M, C, E>,
    pub probe: &'f mut ProbeFn<'f, M, C, A, E>,
}

/// What this unit raises: what a closure raised, the store declining
/// `has_memories`, `now - timedelta` before year 1, the `ValueError` of
/// `turn_audited_fields`, what the transcript read raised, the OSError
/// `Path.is_file` re-raises (any errno but ENOENT, ENOTDIR, EBADF and
/// ELOOP), or a path the Windows flavour does not port.
#[derive(Clone, Debug, PartialEq)]
pub enum Fault<E> {
    Raised(E),
    NotOffered,
    OverflowError,
    Audit(AuditError),
    Transcript(PyError),
    Os(OsError),
    NotPorted,
}

/// The keyword arguments of `_probe_message` past the store and the
/// configuration.
pub struct Message<'a, A> {
    pub user_message: &'a [u32],
    pub session_id: &'a [u32],
    pub server_session: Option<&'a [u32]>,
    pub admission: &'a A,
    pub excluded_scopes: &'a BTreeSet<PyStr>,
    pub recent: &'a [PyValue],
    pub worktree_root: Option<&'a [u32]>,
    pub now: &'a DateTime,
    pub floor: bool,
}

/// `_probe_message`: the production pool, then `probe_for_miss` over it
/// with the settings' ranker knobs; `store.has_memories()` is asked only
/// when the pool is empty (`bool(pool) or store.has_memories()`).
pub fn probe_message<S: SettleStore, A, M, C, E>(
    store: &mut S,
    settings: &Settings,
    prober: &mut Prober<'_, S, A, M, C, E>,
    m: &Message<'_, A>,
) -> Result<MissReport, Fault<E>> {
    let pool = (prober.resolve_pool)(store, m.user_message, m.excluded_scopes, m.admission)
        .map_err(Fault::Raised)?;
    let searched = !pool.memories.is_empty() || store.has_memories().ok_or(Fault::NotOffered)?;
    (prober.probe)(ProbeCall {
        memories: pool.memories,
        user_message: m.user_message,
        recent_events: m.recent,
        session_id: m.session_id,
        retrieval_session_id: m.server_session,
        now: m.now,
        lookback_seconds: ATTRIBUTION_LOOKBACK_SECONDS,
        admission: m.admission,
        worktree_root: m.worktree_root,
        excluded_scopes: m.excluded_scopes,
        mode: settings.probe_mode(),
        half_life_days: settings.half_life_days,
        conversational: settings.conversational,
        corpus_stats_provider: pool.corpus_stats_provider,
        floor: m.floor,
        searched,
    })
    .map_err(Fault::Raised)
}

/// The keyword arguments of `settle_interrupted` past the store and the
/// configuration.
pub struct Interrupted<'a, A> {
    pub session_id: &'a [u32],
    pub transcript_path: Option<&'a [u32]>,
    pub admission: &'a A,
    pub excluded_scopes: &'a BTreeSet<PyStr>,
    pub recent: &'a [PyValue],
    pub server_session: Option<&'a [u32]>,
    pub worktree_root: Option<&'a [u32]>,
    pub now: &'a DateTime,
    pub previous_audit: Option<&'a DateTime>,
    pub triggered_from: &'a [u32],
    pub exclude_prompt_id: Option<&'a [u32]>,
}

/// `transcript.interrupted_turns(path, since=)` over the path's bytes.
pub type TurnsFn<'f> = dyn FnMut(&[u8], &DateTime) -> Result<Vec<InterruptedTurn>, PyError> + 'f;

/// The host's side of the transcript: `str(Path(p).expanduser())`, the
/// file system `is_file` asks, and `transcript.interrupted_turns(path,
/// since=)`.
pub struct TranscriptHost<'f, F: Fs + ?Sized, E> {
    pub expanduser: &'f mut dyn FnMut(&[u32]) -> Result<PyStr, E>,
    pub fs: &'f F,
    pub turns: &'f mut TurnsFn<'f>,
}

/// Whether the host's `os.stat` takes the path as a wide string
/// (Windows) rather than as bytes.
pub const STAT_TAKES_WIDE: bool = cfg!(windows);

/// The path as the host's `os.stat` takes it: POSIX's `os.fsencode`
/// (surrogateescape; None for a lone surrogate it cannot encode), or,
/// where `wide`, UTF-8. On Windows the reference stats a path with a lone
/// surrogate through the wide string; the port declines there
/// (`NotPorted`), as the README section states.
pub fn host_bytes<E>(path: &[u32], wide: bool) -> Result<Option<Vec<u8>>, Fault<E>> {
    if wide {
        return path
            .iter()
            .map(|&c| char::from_u32(c))
            .collect::<Option<String>>()
            .map(|t| Some(t.into_bytes()))
            .ok_or(Fault::NotPorted);
    }
    Ok(crate::gitfile_rule::follow::fsencode(path))
}

/// The errnos `pathlib._abc._IGNORED_ERRNOS` names (3.13): ENOENT,
/// ENOTDIR, EBADF and ELOOP.
const IGNORED_ERRNOS: [&str; 4] = ["ENOENT", "ENOTDIR", "EBADF", "ELOOP"];

/// The Windows codes `pathlib._abc._IGNORED_WINERRORS` names (3.13):
/// ERROR_NOT_READY, ERROR_INVALID_NAME and ERROR_CANT_RESOLVE_FILENAME.
const IGNORED_WINERRORS: [u32; 3] = [21, 123, 1921];

/// `Path.is_file()` of 3.13: a regular file, links followed (a FIFO, a
/// socket, a device or a folder is not); False on ValueError and on an
/// OSError `_ignore_error` accepts (an errno of [`IGNORED_ERRNOS`] or a
/// Windows code of [`IGNORED_WINERRORS`]); any other OSError raised. An
/// error known by its class alone, not a missing path, declines.
fn is_file<F: Fs + ?Sized, E>(fs: &F, bytes: &[u8]) -> Result<bool, Fault<E>> {
    match fs.stat(bytes) {
        Ok(st) => Ok(st.kind == Kind::Regular),
        Err(e) => ignored(e).map(|()| false),
    }
}

/// `except OSError: if not _ignore_error(e): raise` and `except
/// ValueError: return False`.
/// A NUL in the path is the `ValueError` every `Fs::stat` answers before
/// any system call.
fn ignored<E>(e: OsError) -> Result<(), Fault<E>> {
    if e.class == PyErr::Value {
        return Ok(());
    }
    if e.winerror.is_some_and(|w| IGNORED_WINERRORS.contains(&w)) {
        return Ok(());
    }
    match e.errno {
        Some(name) if IGNORED_ERRNOS.contains(&name) => Ok(()),
        Some(_) => Err(Fault::Os(e)),
        None if e.class.missing() => Ok(()),
        None => Err(Fault::NotPorted),
    }
}

/// `settle_interrupted`: nothing without a transcript file; otherwise the
/// prompt ids already audited since `previous_audit` (the idle window
/// back when None), the recorder built, and for each interrupted turn
/// with an id and a prompt, not excluded and not audited, the probe at
/// `now` (floored) and one `turn_audited` row. The prompt ids settled.
#[allow(clippy::too_many_arguments)]
pub fn settle_interrupted<S, A, M, C, E, F, R>(
    store: &mut S,
    settings: &Settings,
    prober: &mut Prober<'_, S, A, M, C, E>,
    host: &mut TranscriptHost<'_, F, E>,
    a: &Interrupted<'_, A>,
    make_recorder: impl FnOnce(&RecorderSpec<'_>) -> R,
) -> Result<Vec<PyStr>, Fault<E>>
where
    S: SettleStore,
    F: Fs + ?Sized,
    R: Recorder,
{
    let Some(text) = a.transcript_path.filter(|p| !p.is_empty()) else {
        return Ok(Vec::new());
    };
    let expanded = (host.expanduser)(text).map_err(Fault::Raised)?;
    let bytes = match host_bytes(&expanded, STAT_TAKES_WIDE)? {
        Some(b) => b,
        None => return Ok(Vec::new()),
    };
    if !is_file(host.fs, &bytes)? {
        return Ok(Vec::new());
    }
    let since = match a.previous_audit {
        Some(p) => *p,
        None => seconds_before(a.now, STATE_IDLE_SECONDS).map_err(|_| Fault::OverflowError)?,
    };
    let mut audited: BTreeSet<PyStr> = BTreeSet::new();
    for event in store.events_of_kinds(&["turn_audited"], &since) {
        if !matches!(either(&event, "session", "session_id"), PyValue::Str(x) if x == a.session_id)
        {
            continue;
        }
        if let PyValue::Str(named) = field(&event, "prompt_id") {
            audited.insert(named.clone());
        }
    }
    let mut recorder = make_recorder(&RecorderSpec {
        session_id: a.session_id,
        enabled: settings.telemetry_enabled,
        worktree_root: a.worktree_root,
        triggered_from: &[],
    });
    let probe_mode = settings.probe_mode();
    let mut settled = Vec::new();
    for turn in (host.turns)(&bytes, &since).map_err(Fault::Transcript)? {
        let (Some(prompt_id), Some(prompt)) = (&turn.prompt_id, &turn.prompt) else {
            continue;
        };
        if a.exclude_prompt_id == Some(prompt_id.as_slice()) || audited.contains(prompt_id) {
            continue;
        }
        let report = probe_message(
            store,
            settings,
            prober,
            &Message {
                user_message: prompt,
                session_id: a.session_id,
                server_session: a.server_session,
                admission: a.admission,
                excluded_scopes: a.excluded_scopes,
                recent: a.recent,
                worktree_root: a.worktree_root,
                now: a.now,
                floor: true,
            },
        )?;
        let fields = turn_audited_fields(
            &report,
            a.session_id,
            &probe_mode,
            turn.reply.is_some(),
            a.triggered_from,
            &TurnAuditedOptions {
                repeat: false,
                client_model: turn.model.clone(),
                prompt_id: Some(prompt_id.clone()),
                reply_source: Some(cps("interrupted")),
                rule: Some(cps(RULE)),
                settled_count: Some(PyValue::int(0)),
            },
        )
        .map_err(Fault::Audit)?;
        let PyValue::Dict(pairs) = &fields else {
            unreachable!("turn_audited_fields answers a dict")
        };
        recorder.record("turn_audited", pairs.clone());
        settled.push(prompt_id.clone());
    }
    Ok(settled)
}
