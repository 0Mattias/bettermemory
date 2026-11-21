//! The process entry (`_entry.py`): the hook words are routed before
//! anything else, everything else goes to the CLI. Both callees are
//! arguments.

use crate::pyvalue::PyStr;

/// `_daemon_client.HOOK_WORDS` (`_daemon_client.py` 136-138): ``hook`` and
/// its three 8.x aliases. Its home is `_daemon_client`; it sits here until
/// the unit that ports that module moves it.
pub const HOOK_WORDS: [&str; 4] = ["hook", "session-start", "audit-turn", "prompt-recall"];

/// Whether `word` is one of `HOOK_WORDS` (an exact match of code points).
pub fn is_hook_word(word: &[u32]) -> bool {
    HOOK_WORDS
        .iter()
        .any(|w| w.len() == word.len() && w.bytes().zip(word).all(|(a, &b)| u32::from(a) == b))
}

/// `route_hook_word(argv)` (`_entry.py` 17-28): `Some(code)` where the
/// reference exits with `hook_main(argv)`'s code, `None` where it returns.
pub fn route_hook_word(argv: &[PyStr], hook_main: impl FnOnce(&[PyStr]) -> i32) -> Option<i32> {
    let first = argv.first()?;
    if is_hook_word(first) {
        return Some(hook_main(argv));
    }
    None
}

/// What `main` ended in: the hook's exit code, or the CLI's answer.
#[derive(Debug, PartialEq, Eq)]
pub enum Routed<R> {
    Hook(i32),
    Cli(R),
}

/// `main()` (`_entry.py` 31-35) over `argv`, which is `sys.argv[1:]`.
pub fn main<R>(
    argv: &[PyStr],
    hook_main: impl FnOnce(&[PyStr]) -> i32,
    cli_main: impl FnOnce() -> R,
) -> Routed<R> {
    match route_hook_word(argv, hook_main) {
        Some(code) => Routed::Hook(code),
        None => Routed::Cli(cli_main()),
    }
}
