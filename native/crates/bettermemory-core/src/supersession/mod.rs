//! Write-time supersession (`supersession.py`): does a new statement
//! replace a stored one? Unit supersession-1a holds the module's tables,
//! constants and patterns, the match and report models, and the per-body
//! features the detector and durability's anchored "the new" read
//! (reference lines 145-518, in [`features`]). The detector itself
//! (`_one_claim_replaced` to `_rule`, 520-797) is unit supersession-1b.
//!
//! Texts are code points (`&[u32]`); tokens are the tokenizer's `String`s.
//! Every pattern is the engine's [`Regex`](crate::pyre::regex::Regex)
//! built once from the reference's own text and flags, and called with
//! the method the reference calls. A pattern the engine does not build is
//! [`SupersessionError::Pattern`], never a panic.

mod detect;
mod features;

pub use detect::*;
pub use features::*;

pub use crate::store::digest::{claim_sized, strip_provenance};

use crate::pyre::regex::RegexError;
use crate::pyre::replace::ReplaceError;
use crate::pyvalue::{repr_str, PyStr};
use crate::unicode;

/// `MAX_CLAIM_TOKENS`, `MAX_CLAIM_SENTENCES`: the bounds `claim_sized`
/// (re-exported from `store::digest`) reads.
pub const MAX_CLAIM_TOKENS: usize = 80;
pub const MAX_CLAIM_SENTENCES: usize = 5;
/// `MIN_JACCARD`, `MIN_CONFLICT_JACCARD`, `MIN_CONFLICT_KIN_JACCARD`.
pub const MIN_JACCARD: f64 = 0.10;
pub const MIN_CONFLICT_JACCARD: f64 = 0.30;
pub const MIN_CONFLICT_KIN_JACCARD: f64 = 0.15;
/// `CONTEXT_WIDTH`, `MIN_CONTEXT_SHARED`, `MAX_LINKS_PER_WRITE`.
pub const CONTEXT_WIDTH: usize = 3;
pub const MIN_CONTEXT_SHARED: usize = 2;
pub const MAX_LINKS_PER_WRITE: usize = 5;

/// `CHANGE_CUES`, in the dict's insertion order: each cue, and whether
/// the new value follows it directly (true) or after a preposition.
pub const CHANGE_CUES: [(&str, bool); 44] = [
    ("no longer", false),
    ("cut over", false),
    ("cuts over", false),
    ("rolled back", false),
    ("instead of", false),
    ("used to", false),
    ("the old", false),
    ("the previous", false),
    ("the former", false),
    ("back on", false),
    ("once more", false),
    ("again", false),
    ("now", false),
    ("moved", false),
    ("moves", false),
    ("moving", false),
    ("switched", false),
    ("switches", false),
    ("switching", false),
    ("migrated", false),
    ("migrates", false),
    ("migrating", false),
    ("replaced", false),
    ("replaces", true),
    ("replacing", true),
    ("renamed", false),
    ("renames", false),
    ("changed", false),
    ("changes", false),
    ("upgraded", false),
    ("upgrades", false),
    ("downgraded", false),
    ("raised", false),
    ("lowered", false),
    ("bumped", false),
    ("adopted", true),
    ("adopts", true),
    ("retired", false),
    ("decommissioned", false),
    ("deprecated", false),
    ("withdrawn", false),
    ("reverted", false),
    ("previously", false),
    ("formerly", false),
];

/// `_VALUE_PREPS`.
pub const VALUE_PREPS: [&str; 10] = [
    "to", "through", "on", "in", "at", "with", "by", "via", "into", "onto",
];

/// The words `_CUE_TOKENS` adds to the cue phrases' tokens.
pub const EXTRA_CUE_TOKENS: [&str; 8] = [
    "old",
    "previous",
    "new",
    "former",
    "prior",
    "legacy",
    "replacement",
    "again",
];

/// The patterns' texts as the reference writes them, all compiled with
/// no flags. `_CUE_RE`'s is built at run time ([`cue_pattern`]).
pub const NUMERIC_TOKEN_PATTERN: &str = r"[a-z0-9][a-z0-9.\-_]*";
pub const ASCII_DIGIT_PATTERN: &str = r"[0-9]";
pub const WORD_PATTERN: &str = r"[A-Za-z0-9][A-Za-z0-9.\-_/]*";
pub const CLAUSE_PATTERN: &str = r"(?:[!?;:\n]|(?<!\d)\.|\.(?!\d))+";
pub const PROPER_NOUN_PATTERN: &str =
    r"^(?:[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*|[A-Z]{2,}[A-Za-z0-9]*)$";
pub const INTEGER_PATTERN: &str = r"\d+";
pub const DOTTED_PATTERN: &str = r"\d+(?:\.\d+)+";

/// What stops an answer. None is reachable from the reference's patterns
/// as the engine builds them; each is a fault inside the port.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SupersessionError {
    /// The engine did not build the pattern of this name.
    Pattern(&'static str, RegexError),
    /// A call of the pattern of this name answered no value.
    Call(&'static str, ReplaceError),
    /// `CHANGE_CUES[cue]` of a matched text that is no cue (`KeyError`).
    Cue(PyStr),
}

/// `SupersessionMatch`: one stored memory the new body replaces or
/// disagrees with.
#[derive(Clone, Debug, PartialEq)]
pub struct SupersessionMatch {
    pub memory_id: PyStr,
    pub outcome: PyStr,
    pub evidence: PyStr,
    pub cue: Option<PyStr>,
    pub new_value: PyStr,
    pub old_value: PyStr,
    pub similarity: f64,
    pub summary: PyStr,
}

impl SupersessionMatch {
    /// `note()`: `f"set at write time: {cue!r} in this memory,
    /// {new_value!r} against {old_value!r} ({evidence})"`.
    pub fn note(&self) -> PyStr {
        let mut out = unicode::cps("set at write time: ");
        match &self.cue {
            Some(cue) => out.extend(repr_str(cue)),
            None => out.extend(unicode::cps("None")),
        }
        out.extend(unicode::cps(" in this memory, "));
        out.extend(repr_str(&self.new_value));
        out.extend(unicode::cps(" against "));
        out.extend(repr_str(&self.old_value));
        out.extend(unicode::cps(" ("));
        out.extend_from_slice(&self.evidence);
        out.push(u32::from(b')'));
        out
    }

    /// `detector()`: `numeric` when both values, lowered, have a
    /// [`numeric_shape`], else `value`.
    pub fn detector(&self) -> Result<&'static str, SupersessionError> {
        let new = numeric_shape(&unicode::lower(&self.new_value))?;
        let old = numeric_shape(&unicode::lower(&self.old_value))?;
        Ok(if new.is_some() && old.is_some() {
            "numeric"
        } else {
            "value"
        })
    }
}

/// `SupersessionReport`: `eligible` is false when the new body is not
/// claim-sized, and then both lists are empty.
#[derive(Clone, Debug, PartialEq)]
pub struct SupersessionReport {
    pub eligible: bool,
    pub supersedes: Vec<SupersessionMatch>,
    pub conflicts: Vec<SupersessionMatch>,
}
