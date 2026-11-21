//! The vocabulary of `membership_records.py` the model reads that
//! `membership_records::records` does not hold yet: the outcomes, the
//! witness codes, the relations, the project scope and the liveness
//! states. `SUPERSEDED` and the checkout kinds are in `records`.

pub use crate::membership_records::records::SUPERSEDED;

/// The project of a record written outside any repository and any
/// project file, and the caller's project there (46).
pub const GLOBAL: &str = "global";

// The outcomes (48-53).
pub const SILENT: &str = "silent";
pub const ASK: &str = "ask";
pub const NEW: &str = "new";
pub const GLOBAL_OUTCOME: &str = "global";
pub const INDETERMINATE: &str = "indeterminate";
pub const DEFERRED: &str = "deferred";

// The witnesses (81-91) and the three that are one history witness (92).
pub const O: &str = "O";
pub const S: &str = "S";
pub const H: &str = "H";
pub const HF: &str = "Hf";
pub const HB: &str = "Hb";
pub const R: &str = "R";
pub const D: &str = "D";
pub const F: &str = "F";
pub const T: &str = "T";
pub const B: &str = "B";
pub const C: &str = "C";
pub const HISTORY: [&str; 3] = [H, HF, HB];

// The relations of design 3.5 (206-216).
pub const SAME_PROJECT: &str = "same_project";
pub const SUB_PROJECT: &str = "sub_project";
pub const GLOBAL_RELATION: &str = "global";
pub const GLOBAL_NAMED: &str = "global_named";
pub const OTHER_PROJECT: &str = "other_project";
pub const OTHER_TENANT: &str = "other_tenant";
pub const UNKNOWN: &str = "unknown";

/// The scope prefix a record names a project with (238).
pub const PROJECT_SCOPE: &str = "projects:";
/// `_AUTOMATIC` (240).
pub const AUTOMATIC: [&str; 2] = [SAME_PROJECT, GLOBAL_NAMED];

// What a view's path holds now (266-272).
pub const LIVE: &str = "live";
pub const LIVE_LEGACY: &str = "live_legacy";
pub const GONE: &str = "gone";
pub const UNREACHABLE: &str = "unreachable";
pub const REPLACED: &str = "replaced";
pub const OTHER_MACHINE: &str = "other_machine";
pub const LIVE_STATES: [&str; 2] = [LIVE, LIVE_LEGACY];

/// `WITNESS_BOUND` (`membership_records.py` 110): at most this many
/// witness commits a project. Lane mrecid ports it with the rest of its
/// lines; the fold keeps one definition.
pub const WITNESS_BOUND: usize = 64;
