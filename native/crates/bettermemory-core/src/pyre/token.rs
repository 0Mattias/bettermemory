//! `re._parser`'s `Tokenizer` (CPython 3.13.13, `Lib/re/_parser.py` 229
//! to 301).
//!
//! A pattern is code points. A bytes pattern is its bytes as code points
//! with `istext` false, as the reference decodes one by Latin-1. A token
//! is a character, or a backslash and the character after it.
//!
//! The reference raises where these return an error, and it leaves the
//! tokenizer as it was, except that `seek` has already moved the index.
//! That state is kept too, so a position can be negative after a `seek`
//! that failed: positions are signed, as Python's are.
//!
//! `checkgroupname` (303 to 309) is left to the unit that reads group
//! names: it needs `str.isidentifier`.

use super::tree::{BuildError, Feature, Why};

const BACKSLASH: u32 = 0x5c;

/// What a step of the parser returns where the reference may raise.
pub type Built<T> = Result<T, BuildError>;

/// One token: the character `c`, or a backslash and `c`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tok {
    pub esc: bool,
    pub c: u32,
}

impl Tok {
    /// `len(this)`: 1, or 2 with the backslash.
    pub fn width(self) -> usize {
        1 + usize::from(self.esc)
    }

    /// `this == c` for the character `c` alone.
    pub fn is(self, c: char) -> bool {
        !self.esc && self.c == u32::from(c)
    }
}

/// A position as Python counts it, which has no bound, as an `isize`:
/// one past either end of an `isize` is that end. No slice is longer than
/// `isize::MAX`, so only a `seek` past every pattern or an offset past
/// every position gets there.
fn position(exact: i128) -> isize {
    isize::try_from(exact).unwrap_or(if exact < 0 { isize::MIN } else { isize::MAX })
}

/// `Tokenizer`: the pattern, the index after the next token, and that
/// token (`None` at the end).
#[derive(Clone, Debug)]
pub struct Tokenizer<'a> {
    pub istext: bool,
    string: &'a [u32],
    index: usize,
    pub next: Option<Tok>,
}

impl<'a> Tokenizer<'a> {
    /// `Tokenizer(string)`, which loads the first token.
    pub fn new(string: &'a [u32], istext: bool) -> Built<Tokenizer<'a>> {
        let next = None;
        let mut source = Tokenizer {
            istext,
            string,
            index: 0,
            next,
        };
        source.advance()?;
        Ok(source)
    }

    /// `__next`. A backslash that ends the pattern raises "bad escape
    /// (end of pattern)" at the last index and changes nothing.
    fn advance(&mut self) -> Built<()> {
        let Some(&c) = self.string.get(self.index) else {
            self.next = None;
            return Ok(());
        };
        let esc = c == BACKSLASH;
        let Some(&c) = self.string.get(self.index + usize::from(esc)) else {
            let (at, why) = (position(self.string.len() as i128 - 1), Why::BadEscapeEnd);
            return Err(BuildError::Invalid { at, why });
        };
        self.index += 1 + usize::from(esc);
        self.next = Some(Tok { esc, c });
        Ok(())
    }

    /// `match`: consumes the next token when it is the character `c`.
    pub fn matches(&mut self, c: char) -> Built<bool> {
        let hit = self.next.is_some_and(|next| next.is(c));
        if hit {
            self.advance()?;
        }
        Ok(hit)
    }

    /// `get`: the next token, consumed.
    pub fn get(&mut self) -> Built<Option<Tok>> {
        let this = self.next;
        self.advance()?;
        Ok(this)
    }

    /// `getwhile(n, charset)`: at most `n` characters of `charset`. A
    /// token with a backslash is in no charset.
    pub fn getwhile(&mut self, n: usize, charset: impl Fn(u32) -> bool) -> Built<Vec<u32>> {
        let mut result = Vec::new();
        while result.len() < n {
            match self.next {
                Some(Tok { esc: false, c }) if charset(c) => result.push(c),
                _ => break,
            }
            self.advance()?;
        }
        Ok(result)
    }

    /// `getuntil(terminator, name)`: the characters before `terminator`,
    /// which is consumed. `missing` names "missing " + `name`.
    pub fn getuntil(&mut self, terminator: char, missing: Why) -> Built<Vec<u32>> {
        let mut result = Vec::new();
        loop {
            let c = self.next;
            self.advance()?;
            let Some(c) = c else {
                if result.is_empty() {
                    return Err(self.error(missing, 0));
                }
                return Err(self.error(Why::UnterminatedName, result.len()));
            };
            if c.is(terminator) {
                if result.is_empty() {
                    return Err(self.error(missing, 1));
                }
                return Ok(result);
            }
            if c.esc {
                result.push(BACKSLASH);
            }
            result.push(c.c);
        }
    }

    /// `tell()` as the reference counts it, before an `isize` holds it.
    fn exact(&self) -> i128 {
        self.index as i128 - self.next.map_or(0, Tok::width) as i128
    }

    /// `tell` (and `pos`): where the next token begins.
    pub fn tell(&self) -> isize {
        position(self.exact())
    }

    /// `seek`: the token at `index` becomes the next. The parser seeks to
    /// what `tell` returned before any step failed, so never below zero.
    pub fn seek(&mut self, index: usize) -> Built<()> {
        self.index = index;
        self.advance()
    }

    /// `error(msg, offset)`: `re.error` at `tell() - offset`, subtracted
    /// before either is narrowed.
    pub fn error(&self, why: Why, offset: usize) -> BuildError {
        let at = position(self.exact() - offset as i128);
        BuildError::Invalid { at, why }
    }

    /// A refusal of `what` at `tell() - offset`, placed as `error` places
    /// an error.
    pub fn refuse(&self, what: Feature, offset: usize) -> BuildError {
        let at = position(self.exact() - offset as i128);
        BuildError::Unsupported { at, what }
    }
}
