//! `admission.py` 59-127: the vocabulary around one request's admission
//! that `store::caller` does not already hold. `NO_RESOLUTION` and
//! `Admission` (with `admits_row`) live in `store::caller` and are
//! re-exported here, not redefined.

use std::collections::HashSet;

use crate::in_force::RecordView;
use crate::pyvalue::PyStr;
use crate::store::caller::ModelError;

pub use crate::store::caller::NO_RESOLUTION;

/// `admission.DriftScope`: which records' commit drift counts in the
/// caller's repository.
pub trait DriftScope {
    fn prime(&mut self, ids: &[String]);
    fn drift_counts(&mut self, id: &str) -> bool;
}

/// `admission.CountedRecords`: the records whose commit drift counts,
/// decided once.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CountedRecords {
    pub ids: HashSet<String>,
}

impl DriftScope for CountedRecords {
    /// Nothing to read: the records were decided.
    fn prime(&mut self, _ids: &[String]) {}

    fn drift_counts(&mut self, id: &str) -> bool {
        self.ids.contains(id)
    }
}

/// `admission.EveryRecord`: every record admitted.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct EveryRecord;

impl EveryRecord {
    /// `EveryRecord.admits_row`, with the signature of
    /// `store::caller::Admission::admits_row`. The crate's `RecordView`
    /// has no `admits_row` yet, so this stays inherent until a reader of
    /// it is ported.
    pub fn admits_row(
        &self,
        _scopes: &[PyStr],
        _recorded: Option<&[u32]>,
    ) -> Result<bool, ModelError> {
        Ok(true)
    }
}

impl RecordView for EveryRecord {
    /// Nothing to read: every record is admitted.
    fn prime(&mut self, _ids: &[String]) {}

    fn admits_id(&mut self, _id: &str) -> bool {
        true
    }
}

/// `admission.EVERY_RECORD`.
pub const EVERY_RECORD: EveryRecord = EveryRecord;

/// `admission.admitted_nothing`: the text a row carries when its caller
/// was admitted nothing.
pub fn admitted_nothing(outcome: &str) -> String {
    format!("admitted nothing: {outcome}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vocabulary() {
        assert_eq!(NO_RESOLUTION, "no_resolution");
        assert_eq!(
            admitted_nothing(NO_RESOLUTION),
            "admitted nothing: no_resolution"
        );
        assert_eq!(admitted_nothing(""), "admitted nothing: ");
        let mut every = EVERY_RECORD;
        every.prime(&[]);
        assert!(every.admits_id(""));
        assert_eq!(every.admits_row(&[], None), Ok(true));
        assert_eq!(every.admits_row(&[vec![0xD800]], Some(&[0x67])), Ok(true));
        let mut counted = CountedRecords {
            ids: ["a".to_string()].into_iter().collect(),
        };
        counted.prime(&["b".to_string()]);
        assert!(counted.drift_counts("a"));
        assert!(!counted.drift_counts("b"));
        assert!(!counted.drift_counts("A"));
    }
}
