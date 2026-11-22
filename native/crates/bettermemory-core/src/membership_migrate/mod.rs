//! `membership_migrate` (the migration of a store to the membership
//! model), its first unit: the module's constants (89-123), the report
//! types and their text (132-311) and the pure text helpers (464-517).
//! The run itself (`_Run`, `_Record`, `_Probe` and the helpers that read
//! the store and the machine) is left for later units.

pub mod helpers;
pub mod report;

pub use helpers::{
    basename, basename_on, id_order, json_list, origin, repository_name, shas, text, HOST_NT,
};
pub use report::{LegacyQuestion, MigrationReport, MigrationTotals, RootDecision, RootlessHome};

use crate::location_answer::PROBE_BUDGET as LOCATION_PROBE_BUDGET;
use crate::location_resolve::FETCH_BUDGET;

/// What a probe found at a recorded root: the root is there.
pub const LIVE: &str = "live";
/// The recorded root is gone.
pub const GONE: &str = "gone";
/// Another checkout stands at the recorded root.
pub const REPLACED: &str = "replaced";
/// The probe did not answer.
pub const UNREACHABLE: &str = "unreachable";
/// Every home rule the migration writes starts with this.
pub const RULE_PREFIX: &str = "migration:";
/// The reason of the migration's `legacy` questions.
pub const LEGACY_REASON: &str = crate::membership::queries::LEGACY_NO_LIVE_CHECKOUT;
/// The membership row the run ends with.
pub const MIGRATED: &str = "membership_migrated";
/// `_RECORD_TABLES`: each record table and the kind of its records.
pub const RECORD_TABLES: [(&str, &str); 3] = [
    ("memories", "memory"),
    ("tombstones", "memory"),
    ("episodes", "episode"),
];
/// `_RECORD_KINDS`: the log kinds that carry a record.
pub const RECORD_KINDS: [&str; 6] = [
    "memory_put",
    "memory_delete",
    "tombstone_put",
    "tombstone_delete",
    "episode_put",
    "episode_delete",
];
/// `_PROBE_BUDGET`: a liveness read's bound plus a fetch's, in seconds.
pub const PROBE_BUDGET: f64 = LOCATION_PROBE_BUDGET + FETCH_BUDGET;
/// `_PROBE_WORKERS`: probes run at once.
pub const PROBE_WORKERS: usize = 8;
