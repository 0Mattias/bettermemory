//! The atoms of `re._parser` (CPython 3.13.13, `Lib/re/_parser.py`):
//! `_class_escape` (311 to 369), `_escape` (371 to 446), `_uniq` (448 to
//! 449), the items of `_parse` that are neither a repeat nor a group (an
//! escape, a literal, a set, the dot, `^`, `$`: 522 to 636, 705 to 706,
//! 875 to 879), and `re.escape` (`Lib/re/__init__.py` 303 to 313).
//!
//! [`parse_flat`] stands in for `parse` until the parser's next unit
//! builds `_parse_sub`, `_parse` and `parse` over these functions. It reads
//! what they read and refuses by name what it does not: a repeat, a group
//! and an alternative where `_parse` would read one, and VERBOSE at the
//! first token the flag changes.
//!
//! Where `re` warns (FutureWarning, three places in a set) the engine
//! refuses (`Feature::SetWarning`): it cannot warn, and a refusal is never
//! another answer. `\N{...}` is read as far as the reference reads it
//! before it looks the name up, and refused there: the table of character
//! names is not carried.

use std::collections::HashSet;

use super::token::{Built, Tok, Tokenizer};
use super::tree::{At, BuildError, Category, Feature, Member, Node, State, SubPattern, Why};

const BACKSLASH: u32 = 0x5c;

/// The value of `c` as a digit of `radix` when it is an ASCII one:
/// `DIGITS`, `OCTDIGITS` and `HEXDIGITS` hold nothing else.
fn digit(c: u32, radix: u32) -> Option<u32> {
    char::from_u32(c)?.to_digit(radix)
}

/// `int(digits, radix)` for the digits `getwhile` returned: at most eight
/// of 16, which a `u32` holds.
fn number(digits: &[u32], radix: u32) -> u32 {
    digits
        .iter()
        .fold(0, |n, &c| n * radix + digit(c, radix).unwrap_or(0))
}

/// `ASCIILETTERS`.
fn ascii_letter(c: u32) -> bool {
    char::from_u32(c).is_some_and(|c| c.is_ascii_alphabetic())
}

/// `ESCAPES`: the code point a backslash and `c` stand for.
fn escaped(c: u32) -> Option<u32> {
    let code = match char::from_u32(c)? {
        'a' => 7,
        'b' => 8,
        'f' => 12,
        'n' => 10,
        'r' => 13,
        't' => 9,
        'v' => 11,
        '\\' => BACKSLASH,
        _ => return None,
    };
    Some(code)
}

/// The `IN` rows of `CATEGORIES`.
fn category(c: u32) -> Option<Category> {
    let category = match char::from_u32(c)? {
        'd' => Category::CATEGORY_DIGIT,
        'D' => Category::CATEGORY_NOT_DIGIT,
        's' => Category::CATEGORY_SPACE,
        'S' => Category::CATEGORY_NOT_SPACE,
        'w' => Category::CATEGORY_WORD,
        'W' => Category::CATEGORY_NOT_WORD,
        _ => return None,
    };
    Some(category)
}

/// The `AT` rows of `CATEGORIES`.
fn anchor(c: u32) -> Option<At> {
    let at = match char::from_u32(c)? {
        'A' => At::AT_BEGINNING_STRING,
        'b' => At::AT_BOUNDARY,
        'B' => At::AT_NON_BOUNDARY,
        'Z' => At::AT_END_STRING,
        _ => return None,
    };
    Some(at)
}

/// Consumes the next token when it is a bare digit of `radix`
/// (`source.next in DIGITS`: an escaped digit is in no charset), and
/// returns its character.
fn take_digit(source: &mut Tokenizer, radix: u32) -> Built<Option<u32>> {
    match source.next {
        Some(Tok { esc: false, c }) if digit(c, radix).is_some() => {
            source.get()?;
            Ok(Some(c))
        }
        _ => Ok(None),
    }
}

/// The branches `_escape` and `_class_escape` share, for a backslash and
/// `c`: `\xhh`, and in a str pattern `\uhhhh`, `\Uhhhhhhhh` and `\N{...}`.
/// `None` for any other escape.
fn coded(source: &mut Tokenizer, c: u32) -> Built<Option<u32>> {
    let width = match char::from_u32(c) {
        Some('x') => 2,
        Some('u') if source.istext => 4,
        Some('U') if source.istext => 8,
        Some('N') if source.istext => return Err(named(source)?),
        _ => return Ok(None),
    };
    let digits = source.getwhile(width, |c| digit(c, 16).is_some())?;
    // `len(escape)`: the backslash, the letter and the digits read.
    let written = 2 + digits.len();
    if digits.len() != width {
        return Err(source.error(Why::IncompleteEscape, written));
    }
    let code = number(&digits, 16);
    if code > 0x10_ffff {
        // `chr(c)` raises ValueError, which the reader ends as "bad escape".
        return Err(source.error(Why::BadEscape, written));
    }
    Ok(Some(code))
}

/// `\N{name}` up to the lookup of the name: "missing {" and the errors of
/// `getuntil` are `re`'s; past them the escape is refused at its
/// backslash, where the reference places "undefined character name".
fn named(source: &mut Tokenizer) -> Built<BuildError> {
    if !source.matches('{')? {
        return Err(source.error(Why::MissingBrace, 0));
    }
    let name = source.getuntil('}', Why::MissingCharacterName)?;
    // `len(charname) + len(r'\N{}')`.
    Ok(source.refuse(Feature::NamedCharacter, name.len() + 4))
}

/// `_class_escape`: the member of a set a backslash and `c` stand for.
/// `\b` is U+0008 here, and `\d` and its kin are their category.
pub fn class_escape(source: &mut Tokenizer, c: u32) -> Built<Member> {
    if let Some(code) = escaped(c) {
        return Ok(Member::Literal(code));
    }
    if let Some(category) = category(c) {
        return Ok(Member::Category(category));
    }
    if let Some(code) = coded(source, c)? {
        return Ok(Member::Literal(code));
    }
    if digit(c, 8).is_some() {
        // Up to three octal digits.
        let mut digits = vec![c];
        digits.extend(source.getwhile(2, |c| digit(c, 8).is_some())?);
        let code = number(&digits, 8);
        if code > 0o377 {
            return Err(source.error(Why::OctalRange, 1 + digits.len()));
        }
        return Ok(Member::Literal(code));
    }
    // 8 and 9 raise ValueError, which ends as "bad escape" too.
    if digit(c, 10).is_some() || ascii_letter(c) {
        return Err(source.error(Why::BadEscape, 2));
    }
    Ok(Member::Literal(c))
}

/// `_escape`: the item a backslash and `c` stand for outside a set. A
/// reference to a group that passes the reference's checks, which it
/// returns as `GROUPREF`, is refused there (`Feature::Backreference`), at
/// its backslash.
pub fn item_escape(source: &mut Tokenizer, c: u32, state: &State) -> Built<Node> {
    if let Some(at) = anchor(c) {
        return Ok(Node::At(at));
    }
    if let Some(category) = category(c) {
        return Ok(Node::In(vec![Member::Category(category)]));
    }
    if let Some(code) = escaped(c) {
        return Ok(Node::Literal(code));
    }
    if let Some(code) = coded(source, c)? {
        return Ok(Node::Literal(code));
    }
    if c == u32::from('0') {
        // An octal escape of at most three digits, the first this 0.
        let digits = source.getwhile(2, |c| digit(c, 8).is_some())?;
        return Ok(Node::Literal(number(&digits, 8)));
    }
    if digit(c, 10).is_none() {
        if ascii_letter(c) {
            return Err(source.error(Why::BadEscape, 2));
        }
        return Ok(Node::Literal(c));
    }
    // An octal escape or a group reference: three octal digits are the
    // one, anything else of one or two digits the other.
    let mut digits = vec![c];
    if let Some(second) = take_digit(source, 10)? {
        digits.push(second);
        if digit(c, 8).is_some() && digit(second, 8).is_some() {
            if let Some(third) = take_digit(source, 8)? {
                digits.push(third);
                let code = number(&digits, 8);
                if code > 0o377 {
                    return Err(source.error(Why::OctalRange, 1 + digits.len()));
                }
                return Ok(Node::Literal(code));
            }
        }
    }
    let group = number(&digits, 10) as usize;
    // `len(escape)`: the backslash and the digits.
    let written = 1 + digits.len();
    if group >= state.groups() {
        return Err(source.error(Why::InvalidGroupReference, written - 1));
    }
    if !state.checkgroup(group) {
        return Err(source.error(Why::OpenGroup, written));
    }
    if let Err(why) = state.checklookbehindgroup(group) {
        return Err(source.error(why, 0));
    }
    Err(source.refuse(Feature::Backreference, written))
}

/// `_uniq`: each member once, where it first appears.
fn uniq(mut members: Vec<Member>) -> Vec<Member> {
    let mut seen = HashSet::with_capacity(members.len());
    members.retain(|member| seen.insert(*member));
    members
}

/// One of the characters of `chars`, bare.
fn bare(token: Tok, chars: &str) -> bool {
    chars.chars().any(|c| token.is(c))
}

/// A set, read after its `[` (`_parse` 549 to 636): the item it stands
/// for. A set of one literal is that literal, negated `NOT_LITERAL`. The
/// three places where the reference warns refuse instead, each at the
/// position its warning names.
pub fn set(source: &mut Tokenizer) -> Built<Node> {
    // `here`, the `[`: "unterminated character set" is raised at
    // `tell() - (tell() - here)`, which is `here`.
    let here = source.tell() - 1;
    let unterminated = BuildError::Invalid {
        at: here,
        why: Why::UnterminatedSet,
    };
    if source.next.is_some_and(|next| next.is('[')) {
        // "Possible nested set at position tell()".
        return Err(source.refuse(Feature::SetWarning, 0));
    }
    let negate = source.matches('^')?;
    let mut members = Vec::new();
    loop {
        let Some(this) = source.get()? else {
            return Err(unterminated);
        };
        if this.is(']') && !members.is_empty() {
            break;
        }
        let code1 = if this.esc {
            class_escape(source, this.c)?
        } else {
            if !members.is_empty() && bare(this, "-&~|") && source.next == Some(this) {
                // "Possible set difference (intersection, ...) at
                // position tell() - 1".
                return Err(source.refuse(Feature::SetWarning, 1));
            }
            Member::Literal(this.c)
        };
        if !source.matches('-')? {
            members.push(code1);
            continue;
        }
        // A range, or a `-` that ends the set.
        let Some(that) = source.get()? else {
            return Err(unterminated);
        };
        if that.is(']') {
            members.extend([code1, Member::Literal(u32::from('-'))]);
            break;
        }
        let code2 = if that.esc {
            class_escape(source, that.c)?
        } else {
            if that.is('-') {
                // "Possible set difference at position tell() - 2".
                return Err(source.refuse(Feature::SetWarning, 2));
            }
            Member::Literal(that.c)
        };
        match (code1, code2) {
            (Member::Literal(lo), Member::Literal(hi)) if lo <= hi => {
                members.push(Member::Range(lo, hi));
            }
            // Each token counts as written, an escape as two characters
            // whatever it read after them.
            _ => return Err(source.error(Why::BadRange, this.width() + 1 + that.width())),
        }
    }
    let mut members = uniq(members);
    if let [Member::Literal(c)] = members[..] {
        return Ok(if negate {
            Node::NotLiteral(c)
        } else {
            Node::Literal(c)
        });
    }
    if negate {
        members.insert(0, Member::Negate);
    }
    Ok(Node::In(members))
}

/// `WHITESPACE`: what VERBOSE skips, with `#`, which starts a comment.
fn verbose_skips(c: u32) -> bool {
    matches!(c, 0x09..=0x0d | 0x20 | 0x23)
}

/// `parse(pattern)` for a pattern of atoms: escapes, literals, sets, the
/// dot, `^` and `$`, the tree `re._parser.parse` returns for them. What
/// `_parse` would read next and this does not is refused at the token
/// that begins it: a repeat (`*`, `+`, `?`, `{`), a group (`(`), an
/// alternative (after its `|`); under `verbose`, whitespace or `#` outside
/// a set. A `)` is "unbalanced parenthesis", as in `parse`. `istext` is
/// false for a bytes pattern, given as its bytes.
pub fn parse_flat(pattern: &[u32], istext: bool, verbose: bool) -> Built<SubPattern> {
    let mut source = Tokenizer::new(pattern, istext)?;
    let state = State::new();
    let mut items = Vec::new();
    while let Some(this) = source.next {
        if this.is('|') || this.is(')') {
            break;
        }
        source.get()?;
        if verbose && !this.esc && verbose_skips(this.c) {
            return Err(source.refuse(Feature::Verbose, 1));
        }
        if this.esc {
            items.push(item_escape(&mut source, this.c, &state)?);
            continue;
        }
        items.push(match char::from_u32(this.c) {
            Some('(') => return Err(source.refuse(Feature::Group, 1)),
            Some('*' | '+' | '?' | '{') => return Err(source.refuse(Feature::Repeat, 1)),
            Some('[') => set(&mut source)?,
            Some('.') => Node::Any,
            Some('^') => Node::At(At::AT_BEGINNING),
            Some('$') => Node::At(At::AT_END),
            _ => Node::Literal(this.c),
        });
    }
    // `_parse_sub` takes the `|` before it reads the next alternative.
    if source.matches('|')? {
        return Err(source.refuse(Feature::Alternation, 1));
    }
    if source.next.is_some() {
        return Err(source.error(Why::UnbalancedParenthesis, 0));
    }
    Ok(SubPattern(items))
}

/// `re.escape`: a backslash before each of `()[]{}?*+-|^$\.&~#`, the space,
/// `\t`, `\n`, `\r`, `\v` and `\f`, and every other character as it is. A
/// bytes pattern is given and returned as its bytes: the reference escapes
/// one through Latin-1, which is the same.
pub fn escape(pattern: &[u32]) -> Vec<u32> {
    const SPECIAL: &[u8] = b"()[]{}?*+-|^$\\.&~# \t\n\r\x0b\x0c";
    let mut out = Vec::with_capacity(pattern.len());
    for &c in pattern {
        if u8::try_from(c).is_ok_and(|b| SPECIAL.contains(&b)) {
            out.push(BACKSLASH);
        }
        out.push(c);
    }
    out
}
