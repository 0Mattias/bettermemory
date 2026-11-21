//! The repeats of `re._parser._parse` (CPython 3.13.13, `Lib/re/_parser.py`
//! 638 to 703): `?`, `*`, `+` and `{m,n}` over the item before them, the
//! lazy `?` after one, and the errors `re` raises there.
//!
//! [`repeat`] is called with the repeat character already taken, as
//! `_parse` holds it in `this`, and the items read so far. It replaces the
//! last item with the repeat over it, or appends a literal `{` where `re`
//! reads one so. A possessive `+` is refused where `re` would accept it,
//! after every check of the repeat, so an error `re` raises on the same
//! text comes first. "the repetition number is too large" is the
//! OverflowError of `re`, not `re.error`, and is [`BuildError::Overflow`];
//! a bound past `int`'s digit limit is its ValueError, [`BuildError::DigitLimit`].

use super::token::{Built, Tok, Tokenizer};
use super::tree::{BuildError, Feature, Node, SubPattern, Why, MAXREPEAT};

/// `while source.next in DIGITS: lo += sourceget()`: the ASCII digits
/// next, each a token without a backslash.
fn digits(source: &mut Tokenizer<'_>) -> Built<Vec<u32>> {
    source.getwhile(usize::MAX, |c| (0x30..=0x39).contains(&c))
}

/// The default of `sys.get_int_max_str_digits()`: `int` refuses a decimal
/// text of more digits than this (leading zeros included) with ValueError.
pub const INT_MAX_STR_DIGITS: usize = 4300;

/// `int(digits)` below `MAXREPEAT`, or the OverflowError `re` raises at
/// it or past it. Leading zeros count for nothing, as in `int`, but count
/// toward its limit: past [`INT_MAX_STR_DIGITS`] digits `int` raises
/// ValueError, [`BuildError::DigitLimit`], before any bound is checked.
fn count(digits: &[u32]) -> Built<u32> {
    if digits.len() > INT_MAX_STR_DIGITS {
        return Err(BuildError::DigitLimit);
    }
    let mut n: u64 = 0;
    for &c in digits {
        n = (n * 10 + u64::from(c - 0x30)).min(u64::from(MAXREPEAT));
    }
    u32::try_from(n)
        .ok()
        .filter(|&n| n < MAXREPEAT)
        .ok_or(BuildError::Overflow)
}

/// The bounds of `{m,n}` after its `{`, which ended at `here`. `None`
/// where `re` reads the `{` as a literal: before a `}`, or where no `}`
/// closes the digits, and then the tokenizer is back after the `{`.
fn braces(source: &mut Tokenizer<'_>, here: isize) -> Built<Option<(u32, u32)>> {
    if source.next.is_some_and(|next| next.is('}')) {
        return Ok(None);
    }
    let lo = digits(source)?;
    let hi = if source.matches(',')? {
        digits(source)?
    } else {
        lo.clone()
    };
    if !source.matches('}')? {
        source.seek(usize::try_from(here).unwrap_or(0))?;
        return Ok(None);
    }
    let (mut min, mut max) = (0, MAXREPEAT);
    if !lo.is_empty() {
        min = count(&lo)?;
    }
    if !hi.is_empty() {
        max = count(&hi)?;
        if max < min {
            let why = Why::MinGreaterThanMax;
            return Err(BuildError::Invalid { at: here, why });
        }
    }
    Ok(Some((min, max)))
}

/// The repeat branch of `_parse`: `this` is the repeat character just
/// taken, `items` the sub-pattern read so far. "nothing to repeat" and
/// "multiple repeat" are placed at the repeat character. A group without
/// a number or flags is repeated as its body, as `_parse` unpacks it.
pub fn repeat(source: &mut Tokenizer<'_>, this: Tok, items: &mut Vec<Node>) -> Built<()> {
    let here = source.tell();
    let (min, max) = match char::from_u32(this.c) {
        Some('?') => (0, 1),
        Some('*') => (0, MAXREPEAT),
        Some('+') => (1, MAXREPEAT),
        _ => match braces(source, here)? {
            Some(bounds) => bounds,
            None => {
                items.push(Node::Literal(this.c));
                return Ok(());
            }
        },
    };
    let at = here.saturating_sub(1);
    let why = match items.last() {
        None | Some(Node::At(_)) => Some(Why::NothingToRepeat),
        Some(Node::MaxRepeat(..) | Node::MinRepeat(..)) => Some(Why::MultipleRepeat),
        Some(_) => None,
    };
    if let Some(why) = why {
        return Err(BuildError::Invalid { at, why });
    }
    let body = match items.pop() {
        Some(Node::Subpattern {
            group: None,
            add_flags: 0,
            del_flags: 0,
            body,
        }) => body,
        last => SubPattern(last.into_iter().collect()),
    };
    let node = if source.matches('?')? {
        Node::MinRepeat(min, max, body)
    } else if source.matches('+')? {
        return Err(source.refuse(Feature::Possessive, 1));
    } else {
        Node::MaxRepeat(min, max, body)
    };
    items.push(node);
    Ok(())
}
