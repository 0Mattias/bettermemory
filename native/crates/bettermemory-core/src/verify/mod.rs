//! `verify.py`: the status leaves (`status`), the claim-level `weak` tier
//! and its git and spelling traits (`weak`), the two-leg narrowing and
//! `resolve_commit_drift` (`legs`), and the literal reader the claim index
//! reads through (`literal_lines`). The verdict
//! strings are re-exported from [`crate::verdict_signals`] as the
//! reference re-exports them; its other re-exports (`Anchoring`,
//! `PathDriftReport`, `VerificationStatus`, `compute_verification_status`,
//! `detect_path_drift`) are imported from `verify_status` and
//! `verify_anchoring` directly.

pub mod legs;
pub mod literal_lines;
pub mod status;
pub mod weak;

pub use crate::verdict_signals::{VERDICT_FRESH, VERDICT_RECOMMENDED, VERDICT_REQUIRED};
