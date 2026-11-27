//! `match_stmt` of CPython 3.13.13's grammar (a `match` line ending in
//! `:`, its `case` blocks and their patterns) as [`super::compound`]
//! dispatches it: declined (`statement`) until unit claims-5c2c reads it
//! here.

use super::compound::Open;
use super::*;

impl Parser<'_> {
    /// The head of the `match` statement at the cursor, whose `case`
    /// blocks [`super::compound`] would read: declined (`statement`).
    pub(super) fn match_stmt(&mut self) -> Res<Open> {
        Err(ExprError::Declined("statement"))
    }
}
