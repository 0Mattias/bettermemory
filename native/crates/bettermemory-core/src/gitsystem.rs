//! What the git on PATH reads before the global config: the port of
//! `src/bettermemory/gitsystem.py` (see the README's "The git on PATH and
//! what it reads", which lists every ambient thing the module touches and
//! how the port takes each).
//!
//! - [`record`]: the answers recorded in the state directory and whether
//!   one stands (`_key`, `_developer`, `_current`, `_kept`, `_entries`,
//!   `_text`, `_recorded`, `_recorded_version`, `_record`): built.
//!
//! The questions asked of git, the probe repository and `sources` itself
//! are the next units'.

pub mod record;
