//! `verify.py`, its first unit: the status leaves (`status`). The verdict
//! strings are re-exported from [`crate::verdict_signals`] as the
//! reference re-exports them; its other re-exports (`Anchoring`,
//! `PathDriftReport`, `VerificationStatus`, `compute_verification_status`,
//! `detect_path_drift`) are imported from `verify_status` and
//! `verify_anchoring` directly.

pub mod status;

pub use crate::verdict_signals::{VERDICT_FRESH, VERDICT_RECOMMENDED, VERDICT_REQUIRED};
