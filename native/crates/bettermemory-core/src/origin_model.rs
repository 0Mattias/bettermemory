//! `origin_model.py`: `Origin` as a write's caller captures it. The
//! stored half is [`records::Origin`]; this module holds the private
//! state `origin.capture` sets beside it.

use crate::pyvalue::PyStr;
use crate::records;

/// `Origin` with its three private attributes (`_repo_url_alternates`,
/// `_git_indeterminate`, `_git_refusal`). The three are never
/// serialized: `dump` is `origin.dump`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CapturedOrigin {
    pub origin: records::Origin,
    pub repo_url_alternates: Vec<PyStr>,
    pub git_indeterminate: bool,
    pub git_refusal: Option<PyStr>,
}

impl CapturedOrigin {
    /// The stored origin with the private state at its defaults (empty,
    /// False, None).
    pub fn new(origin: records::Origin) -> CapturedOrigin {
        CapturedOrigin {
            origin,
            ..CapturedOrigin::default()
        }
    }

    /// `git_indeterminate`.
    pub fn git_indeterminate(&self) -> bool {
        self.git_indeterminate
    }

    /// `git_refusal`.
    pub fn git_refusal(&self) -> Option<&PyStr> {
        self.git_refusal.as_ref()
    }
}
