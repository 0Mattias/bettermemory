//! [`LiteralLines`] over the built literal reader of `claims.py`
//! ([`crate::claims::literal`]). The reference catches every exception at
//! each call, so a text that is not a literal is None or nothing; a text
//! whose reading needs a table the crate does not hold yet has no answer,
//! and the first such gap is kept for the caller to read with
//! [`LiteralReader::gap`] after the index and the scoring have run.

use std::cell::Cell;

use crate::claim_drift::LiteralLines;
use crate::claims::literal;
use crate::pysyntax::literal::{literal_eval, Literal, LiteralError};
use crate::pyvalue::PyStr;

/// The built literal reader, with the first gap it met.
#[derive(Default)]
pub struct LiteralReader {
    gap: Cell<Option<LiteralError>>,
}

impl LiteralReader {
    /// The first reading that needed a table the crate does not hold.
    pub fn gap(&self) -> Option<LiteralError> {
        let gap = self.gap.take();
        self.gap.set(gap);
        gap
    }

    fn keep<T: Default>(&self, read: Result<T, LiteralError>) -> T {
        match read {
            Ok(v) => v,
            Err(LiteralError::Invalid(_)) => T::default(),
            Err(gap) => {
                let first = self.gap.take();
                self.gap.set(first.or(Some(gap)));
                T::default()
            }
        }
    }
}

impl LiteralLines for LiteralReader {
    fn string_fragment(&self, content: &[u32]) -> Option<PyStr> {
        self.keep(literal::string_fragment(content))
    }
    fn rhs_repr(&self, content: &[u32]) -> Option<PyStr> {
        self.keep(literal::rhs_repr(content))
    }
    fn anchors_from_value(&self, value: &[u32]) -> Vec<PyStr> {
        self.keep(literal::anchors_from_value(value))
    }
    fn claimed_str(&self, value: &[u32]) -> Option<PyStr> {
        self.keep(literal_eval(value).map(|v| match v {
            Literal::Str(text) => Some(text),
            _ => None,
        }))
    }
}
