//! The case folding `_sre` uses under IGNORECASE on a `str` pattern, and the
//! test of one literal (unit pyre-1).
//!
//! `re` folds one code point at a time. `_compile` (`Lib/re/_compiler.py`
//! 57 to 84) asks `_sre.unicode_iscased` of a pattern literal; an uncased
//! one is compared as it stands; a cased one is replaced by its lower case
//! `lo`, and a text character matches when `_sre.unicode_tolower` of it is
//! `lo` or one of `re._casefix._EXTRA_CASES[lo]`. Nothing here is
//! `str.lower`, `str.casefold` or the Unicode property Cased:
//!
//! - one code point in, one out: U+0130 lowers to U+0069 (where
//!   `str.lower` gives two code points), so no fold takes two characters
//!   and U+00DF never matches `ss`;
//! - no context: U+03A3 lowers to U+03C3 wherever it stands;
//! - cased means "differs from its lower case or from its upper case",
//!   which U+00AA is not, though it has the property Cased
//!   (`unicode::is_cased`).
//!
//! The tables are the crate's own (`unicode`), generated from the
//! interpreter. A value past U+10FFFF is no code point: it lowers and
//! uppers to itself, is uncased and matches only itself.
//!
//! Two modes of `_compile` fold by other tables and are not reproduced:
//! IGNORECASE with LOCALE, and IGNORECASE without UNICODE (a bytes
//! pattern, or `re.ASCII`). [`Lit::new`] refuses a literal under either
//! ([`Unreproduced`]).

use crate::unicode;

/// `re.IGNORECASE` (`re._constants.SRE_FLAG_IGNORECASE`).
pub const IGNORECASE: u32 = 2;
/// `re.LOCALE`, which `re` allows on a bytes pattern only.
pub const LOCALE: u32 = 4;
/// `re.UNICODE`. `_parser` sets it on every `str` pattern not under
/// `re.ASCII`, and never on a bytes pattern.
pub const UNICODE: u32 = 32;
/// `re.ASCII`. `_compile` does not read it: a pattern under it lacks
/// UNICODE.
pub const ASCII: u32 = 256;

/// `_sre.unicode_tolower` (`_PyUnicode_ToLowercase`): the first code point
/// of the full lower-case mapping of `c`, or `c`.
pub fn sre_lower(c: u32) -> u32 {
    sre_lower_by(c).0
}

/// The way [`sre_lower`] answered for one code point.
#[doc(hidden)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lowering {
    /// Below U+0080, by arithmetic.
    Arithmetic,
    /// A code point of the BMP whose dense flags say it has no lower case.
    Flags,
    /// Past the lower-case table's last key.
    PastLastKey,
    /// The lower-case table was searched.
    Searched,
}

/// [`sre_lower`] and the way it answered, for the tests that show the
/// fast paths skip the table search. [`sre_lower`] runs this body.
#[doc(hidden)]
#[inline]
pub fn sre_lower_by(c: u32) -> (u32, Lowering) {
    if c < 0x80 {
        let lower = if (0x41..=0x5A).contains(&c) {
            c + 0x20
        } else {
            c
        };
        return (lower, Lowering::Arithmetic);
    }
    let (lower, by) = unicode::lower_first_by(c);
    let by = match by {
        unicode::LowerBy::Flags => Lowering::Flags,
        unicode::LowerBy::PastLastKey => Lowering::PastLastKey,
        unicode::LowerBy::Searched => Lowering::Searched,
    };
    (lower, by)
}

/// `_PyUnicode_ToUppercase`, which `_sre` calls `sre_upper_unicode`: the
/// first code point of the full upper-case mapping of `c`, or `c` (U+00DF
/// gives U+0053). The engine reads it for [`sre_iscased`] only.
pub fn sre_upper(c: u32) -> u32 {
    if c < 0x80 {
        return if (0x61..=0x7A).contains(&c) {
            c - 0x20
        } else {
            c
        };
    }
    unicode::upper_first(c)
}

/// `_sre.unicode_iscased`: `c` differs from its lower case or from its
/// upper case, each as one code point.
pub fn sre_iscased(c: u32) -> bool {
    c != sre_lower(c) || c != sre_upper(c)
}

/// `re._casefix._EXTRA_CASES.get(lower, ())`: the other lowered code points
/// `re` takes for the letter whose lower case is `lower`, in the
/// interpreter's order (U+0069 gives U+0131; U+03C3 gives U+03C2). Empty
/// for every code point that is not a key of the table.
pub fn extra_cases(lower: u32) -> &'static [u32] {
    let table = unicode::sre_extra_cases();
    match table.binary_search_by_key(&lower, |&(key, _)| key) {
        Ok(i) => table[i].1,
        Err(_) => &[],
    }
}

/// One literal of a pattern, as `_compile` emits it: LITERAL for
/// [`Lit::Exact`]; under IGNORECASE and for a cased code point
/// LITERAL_UNI_IGNORE, or IN_UNI_IGNORE over the lower case and its extra
/// cases, both of which are [`Lit::Fold`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lit {
    /// The text character must be this code point.
    Exact(u32),
    /// The text character's lower case must be `lower` or one of `extra`.
    Fold { lower: u32, extra: &'static [u32] },
}

/// A literal under IGNORECASE in a mode `_compile` folds by other tables
/// than the Unicode ones, which the engine does not reproduce. The unit
/// that builds a pattern refuses the pattern for it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unreproduced {
    /// LOCALE (`_compiler.py` 61-63): OP_LOCALE_IGNORE, which folds by the
    /// C library's locale when the text is matched.
    Locale,
    /// No UNICODE, on a bytes pattern or under `re.ASCII` (`_compiler.py`
    /// 53-55 and 69-71): `_sre.ascii_iscased` and `_sre.ascii_tolower`,
    /// which fold A to Z only.
    AsciiFold,
}

impl Lit {
    /// The literal `c` of a pattern, under `flags` as `_compile` reads them
    /// (`re.compile(...).flags`: a `str` pattern carries UNICODE). Without
    /// IGNORECASE it is exact; under IGNORECASE with UNICODE a code point
    /// that is not cased is exact and a cased one folds. Under IGNORECASE,
    /// LOCALE and the ASCII fold are refused, in the reference's order.
    pub fn new(c: u32, flags: u32) -> Result<Lit, Unreproduced> {
        if flags & IGNORECASE == 0 {
            return Ok(Lit::Exact(c));
        }
        if flags & LOCALE != 0 {
            return Err(Unreproduced::Locale);
        }
        if flags & UNICODE == 0 {
            return Err(Unreproduced::AsciiFold);
        }
        Ok(if sre_iscased(c) {
            let lower = sre_lower(c);
            Lit::Fold {
                lower,
                extra: extra_cases(lower),
            }
        } else {
            Lit::Exact(c)
        })
    }

    /// Whether the text character `c` matches. NOT_LITERAL is the negation
    /// of this, taken by the caller.
    pub fn matches(&self, c: u32) -> bool {
        match *self {
            Lit::Exact(p) => c == p,
            Lit::Fold { lower, extra } => {
                let folded = sre_lower(c);
                folded == lower || extra.contains(&folded)
            }
        }
    }
}
