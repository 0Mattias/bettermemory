//! What the git on PATH reads before the global config: the port of
//! `src/bettermemory/gitsystem.py` (see the README's "The git on PATH and
//! what it reads", which lists every ambient thing the module touches and
//! how the port takes each).
//!
//! - [`record`]: the answers recorded in the state directory and whether
//!   one stands (`_key`, `_developer`, `_current`, `_kept`, `_entries`,
//!   `_text`, `_recorded`, `_recorded_version`, `_record`): built.
//!
//! - [`answers`]: what git's answers say (`_own_files`, `_variables`,
//!   `_REMOTE_ANSWERS`, the reading inside `_reads_remotes`, the parse
//!   inside `_older_system`, `_absolute`, `gitversion.parse`): built.
//!
//! - [`probe`]: the probe repository git is asked in and the folder it
//!   is asked in otherwise (`_probe_name`, `_where`, `_probe_repository`,
//!   `_sweep_probes`, `_probe_stamps`, `_probe_children`, `_probe_holds`,
//!   `_clear_probe`): built.
//!
//! - [`ask`]: the questions asked of the git on PATH (`_ask`,
//!   `_older_system`, `_version`, `_reads_remotes`, `_probe_ask`,
//!   `run_git`'s command shaping, `shutil.which`): built.
//!
//! - [`sources`]: `sources`, `reads_compat` and `is_at_least`, with the
//!   held answers (`_HELD`) as a cache the caller owns: built. The whole
//!   module is ported.

pub mod answers;
pub mod ask;
pub mod probe;
pub mod record;
pub mod sources;
