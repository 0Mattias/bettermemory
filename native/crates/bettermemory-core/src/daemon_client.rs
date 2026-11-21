//! The hook budget of `_daemon_client.py` (lines 149-172): the client's
//! timeouts, the harness's timeout per hook and `hook_budget_seconds`.
//! The rest of the module is left for later units.

/// `_daemon_client.STORE_FILENAME`, which mirrors `config.STORE_FILENAME`
/// (tests/test_hook_client.py pins the two equal).
pub use crate::store::create::STORE_FILENAME;

/// `START_TIMEOUT_SECONDS`: the client's wait for a daemon to start.
pub const START_TIMEOUT_SECONDS: f64 = 8.0;
/// `HOOK_TIMEOUT_SECONDS`: the client's wait for the daemon's reply to a
/// hook.
pub const HOOK_TIMEOUT_SECONDS: f64 = 15.0;
/// `HOOK_HARNESS_TIMEOUT_SECONDS`: the timeout the plugin's hooks.json
/// gives each hook, in seconds, in the reference's order. It mirrors the
/// manifest, which tests/test_plugin.py pins to the reference's mapping.
pub const HOOK_HARNESS_TIMEOUT_SECONDS: &[(&str, f64)] =
    &[("session-start", 20.0), ("stop", 30.0), ("prompt", 10.0)];
/// `HOOK_MARGIN_SECONDS`: what a hook body leaves of its harness timeout
/// for the client's start and the reply's way back.
pub const HOOK_MARGIN_SECONDS: f64 = 2.0;

/// `HOOK_HARNESS_TIMEOUT_SECONDS.get(event)`.
pub fn harness_timeout(event: &str) -> Option<f64> {
    HOOK_HARNESS_TIMEOUT_SECONDS
        .iter()
        .find(|(name, _)| *name == event)
        .map(|(_, seconds)| *seconds)
}

/// `hook_budget_seconds`: the harness's timeout for the hook or
/// `HOOK_TIMEOUT_SECONDS`, the shorter, less the margin, at least zero.
pub fn hook_budget_seconds(event: &str) -> f64 {
    let harness = harness_timeout(event).unwrap_or(HOOK_TIMEOUT_SECONDS);
    // Python's `min` and `max` keep the first argument on a tie; no value
    // here is a NaN or a signed zero that would tell the two apart.
    let shorter = if HOOK_TIMEOUT_SECONDS < harness {
        HOOK_TIMEOUT_SECONDS
    } else {
        harness
    };
    let left = shorter - HOOK_MARGIN_SECONDS;
    if left > 0.0 {
        left
    } else {
        0.0
    }
}
