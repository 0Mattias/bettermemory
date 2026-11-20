//! The vocabulary and records of the membership model: the port of
//! `src/bettermemory/membership_records.py` (see the README's "The
//! membership records", which maps the module by function and gives the
//! cut).
//!
//! - unit 1, built: the legacy ask rule and the comparison of two object
//!   numbers (here), and [`paths`], the helpers that compare, join and
//!   rebase recorded paths in either spelling.
//!
//! Parts of the module other units ported live where they are read:
//! `ObjectKey`, `ProjectFileRef` and `Location` in `capture::location`,
//! `_paths` in `capture::paths`, `relation`, `admitted`, `Home` and
//! `Resolution` in `store::caller`, `WORDS` in `membership_lines`. The
//! manifest, the portable identity and the remaining records are later
//! units.

pub mod paths;

use crate::capture::location::ObjectKey;

/// `legacy_asks(rule=, same_name=, compatible=)`: whether a migration's
/// legacy question decides anything. `rule` is compared as text: only
/// `ambiguous` and `none` are read.
pub fn legacy_asks(rule: &[u32], same_name: bool, compatible: bool) -> bool {
    let is = |word: &str| rule.iter().copied().eq(word.chars().map(u32::from));
    is("ambiguous") || (is("none") && same_name && compatible)
}

/// `_same_number`: two objects with one device and inode number, the
/// birth time aside. A missing one is never the same.
pub fn same_number(a: Option<&ObjectKey>, b: Option<&ObjectKey>) -> bool {
    match (a, b) {
        (Some(a), Some(b)) => (&a.device, &a.inode) == (&b.device, &b.inode),
        _ => false,
    }
}
