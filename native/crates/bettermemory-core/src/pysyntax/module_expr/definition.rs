//! The definitions and `try` of CPython 3.13.13's grammar
//! (`function_def`, `class_def`, decorators and `try_stmt`) as
//! [`super::compound`] dispatches them: declined (`statement`) until unit
//! claims-5c2b reads them here.

use super::compound::Open;
use super::*;

impl Parser<'_> {
    /// The head of the `def`, `async def`, `class`, `try` or decorated
    /// statement at the cursor, whose block [`super::compound`] would read:
    /// declined (`statement`).
    pub(super) fn definition(&mut self) -> Res<Open> {
        Err(ExprError::Declined("statement"))
    }
}
