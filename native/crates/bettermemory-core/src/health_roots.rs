//! `health_roots.py` 21-73: the process-local cursor of bounded estate
//! reads (ENG-238).
//!
//! The reference guards an `EstateProgress` with an `RLock`; here the
//! owner holds it in a `Mutex`. `progress_for_store` keeps one per open
//! Store in a weak map; here the open Store owns its `EstateProgress`
//! (made with `EstateProgress::default()`) and hands it to the caller.

use std::collections::VecDeque;

use crate::datetime::DateTime;
use crate::pyvalue::PyStr;

/// `_LAST_CHECKED_CAP`: a metadata bound, not a coverage bound.
pub const LAST_CHECKED_CAP: usize = 1024;

/// `EstateProgress`: a rotating root cursor and the most recent known
/// attempt times, oldest first.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EstateProgress {
    pub cursor: Option<PyStr>,
    pub checked: VecDeque<(PyStr, DateTime)>,
    pub caller_first: bool,
    pub retry_root: Option<PyStr>,
}

impl Default for EstateProgress {
    fn default() -> Self {
        EstateProgress {
            cursor: None,
            checked: VecDeque::new(),
            caller_first: true,
            retry_root: None,
        }
    }
}

impl EstateProgress {
    /// `ordered`: the distinct roots in code point order, starting at the
    /// retry root when it is one of them, else after the cursor.
    pub fn ordered(&self, roots: &[PyStr]) -> Vec<PyStr> {
        let mut ordered: Vec<PyStr> = roots.to_vec();
        ordered.sort();
        ordered.dedup();
        let start = match &self.retry_root {
            Some(retry) if ordered.binary_search(retry).is_ok() => {
                ordered.partition_point(|r| r < retry)
            }
            _ => match &self.cursor {
                Some(cursor) => ordered.partition_point(|r| r <= cursor),
                None => 0,
            },
        };
        ordered.rotate_left(start);
        ordered
    }

    /// `attempt`: advance the cursor, clear the retry root, and record the
    /// time last, evicting the oldest beyond the cap.
    pub fn attempt(&mut self, root: PyStr, when: DateTime) {
        self.cursor = Some(root.clone());
        self.retry_root = None;
        if let Some(at) = self.checked.iter().position(|(r, _)| *r == root) {
            self.checked.remove(at);
        }
        self.checked.push_back((root, when));
        while self.checked.len() > LAST_CHECKED_CAP {
            self.checked.pop_front();
        }
    }

    /// `checked.get(root)`.
    pub fn checked_at(&self, root: &[u32]) -> Option<&DateTime> {
        self.checked
            .iter()
            .find(|(r, _)| r.as_slice() == root)
            .map(|(_, when)| when)
    }
}
