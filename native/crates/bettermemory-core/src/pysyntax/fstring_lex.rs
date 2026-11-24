//! CPython 3.13.13's f-string modes (PEP 701), as `tok_get_fstring_mode`
//! and the f-string branches of `tok_get_normal_mode` read them for
//! [`super::module_lex`]: FSTRING_START at a prefix holding `f`, then
//! FSTRING_MIDDLE for each literal part, the ordinary tokens of each
//! replacement field, and FSTRING_END. Each open f-string is one [`Mode`]
//! on a vector; nothing recurses. A literal part of a non-raw f-string is
//! decoded as the parser decodes it (`decode_unicode_with_escapes`), and
//! a part that does not decode refuses the source.

use super::lex::{decode_str, LexError, Lexer};
use super::module_lex::{Kind, ModToken, ModuleError};

/// `MAXFSTRINGLEVEL`: the f-string modes stacked over the regular one.
/// The 150th nested f-string is refused.
pub const MAX_FSTRING_LEVEL: usize = 150;
/// `MAX_EXPR_NESTING`: fields nested through format specifications.
pub const MAX_EXPR_NESTING: i32 = 3;

const NL: u32 = 0x0A;
const BS: u32 = 0x5C;
const LB: u32 = 0x7B;
const RB: u32 = 0x7D;

/// One open f-string: `tokenizer_mode` of the C tokenizer.
#[derive(Clone, Debug)]
pub(super) struct Mode {
    quote: u32,
    size: usize,
    raw: bool,
    /// `curly_bracket_depth`: the brackets of every kind open in fields.
    depth: i32,
    /// `curly_bracket_expr_start_depth`: -1 outside a field.
    expr_start: i32,
    in_spec: bool,
    /// `kind == TOK_FSTRING_MODE`: a literal part is read next.
    pub(super) literal: bool,
}

/// What one step in a literal mode answered.
pub(super) enum Step {
    Token(ModToken),
    /// FSTRING_END: the mode is popped.
    End(ModToken),
    /// A field opens at the next `{`: the regular mode reads it.
    Field,
}

fn err<T>(kind: &'static str) -> Result<T, ModuleError> {
    Err(ModuleError::Syntax(kind))
}

/// FSTRING_START from the prefix at `start` to the opening quote or three
/// at `lx.i`, pushing the f-string's mode.
pub(super) fn open(
    lx: &mut Lexer<'_>,
    start: usize,
    raw: bool,
    modes: &mut Vec<Mode>,
) -> Result<ModToken, ModuleError> {
    let quote = lx.s[lx.i];
    let three = lx.s.get(lx.i + 1) == Some(&quote) && lx.s.get(lx.i + 2) == Some(&quote);
    let size = if three { 3 } else { 1 };
    lx.i += size;
    if modes.len() + 1 >= MAX_FSTRING_LEVEL {
        return err("too many nested f-strings");
    }
    modes.push(Mode {
        quote,
        size,
        raw,
        depth: 0,
        expr_start: -1,
        in_spec: false,
        literal: true,
    });
    Ok(tok(Kind::FStringStart, start, lx.i))
}

fn tok(kind: Kind, start: usize, end: usize) -> ModToken {
    ModToken { kind, start, end }
}

impl Mode {
    /// A field opens: `curly_bracket_expr_start_depth++` and the regular
    /// mode reads its `{`.
    fn field(&mut self) -> Result<(), ModuleError> {
        self.expr_start += 1;
        if self.expr_start >= MAX_EXPR_NESTING {
            return err("f-string: expressions nested too deeply");
        }
        self.literal = false;
        self.in_spec = false;
        Ok(())
    }

    /// In the regular mode, whether `c` at the field's own depth is the
    /// `:` that opens a format specification (then the mode turns).
    pub(super) fn spec_colon(&mut self, c: u32) -> bool {
        if c != 0x3A || self.expr_start < 0 || self.depth - 1 != self.expr_start {
            return false;
        }
        self.literal = true;
        self.in_spec = true;
        true
    }

    /// The bracket count after the regular mode read the one-character
    /// operator `c`; a `}` before it is refused when no bracket is open.
    pub(super) fn before_close(&self, c: u32) -> Result<(), ModuleError> {
        if c == RB && self.depth == 0 {
            return err("f-string: single '}' is not allowed");
        }
        Ok(())
    }

    pub(super) fn after_op(&mut self, c: u32) -> Result<(), ModuleError> {
        match c {
            0x28 | 0x5B | LB => self.depth += 1,
            0x29 | 0x5D | RB => {
                self.depth -= 1;
                if self.depth < 0 {
                    return err("f-string: unmatched bracket");
                }
                if c == RB && self.depth == self.expr_start {
                    self.expr_start -= 1;
                    self.literal = true;
                    self.in_spec = false;
                }
            }
            _ => {}
        }
        Ok(())
    }
}

/// `tok_get_fstring_mode`: one step of the literal mode at `lx.i`. A part
/// holding `\N{...}` sets `named_escape`: whether it decodes needs the name
/// table, and the caller declines only when nothing else refuses.
pub(super) fn literal(
    lx: &mut Lexer<'_>,
    m: &mut Mode,
    named_escape: &mut bool,
) -> Result<Step, ModuleError> {
    let s = lx.s;
    let start = lx.i;
    if s.get(start) == Some(&LB) && s.get(start + 1) != Some(&LB) {
        m.field()?;
        return Ok(Step::Field);
    }
    if (0..m.size).all(|k| s.get(start + k) == Some(&m.quote)) {
        lx.i += m.size;
        return Ok(Step::End(tok(Kind::FStringEnd, start, lx.i)));
    }
    let in_spec = m.in_spec && m.expr_start >= 0;
    let mut quotes = 0;
    let mut named = false;
    // The part's end, and where the next token starts.
    let (end, next) = loop {
        let Some(&c) = s.get(lx.i) else {
            return err("unterminated f-string literal");
        };
        if c == NL && m.size == 1 {
            return err("unterminated f-string literal");
        }
        lx.i += 1;
        if c == m.quote {
            quotes += 1;
            if quotes == m.size {
                break (lx.i - m.size, lx.i - m.size);
            }
            continue;
        }
        quotes = 0;
        if c == LB {
            if s.get(lx.i) != Some(&LB) || in_spec {
                m.field()?;
                break (lx.i - 1, lx.i - 1);
            }
            break (lx.i, lx.i + 1);
        } else if c == RB {
            if named {
                break (lx.i, lx.i);
            }
            if s.get(lx.i) == Some(&RB) && !in_spec && m.depth == 0 {
                break (lx.i, lx.i + 1);
            }
            m.literal = false;
            m.in_spec = false;
            break (lx.i - 1, lx.i - 1);
        } else if c == BS {
            match s.get(lx.i) {
                Some(&LB) | Some(&RB) | None => {}
                Some(&0x4E) if !m.raw => {
                    lx.i += 1;
                    if s.get(lx.i) == Some(&LB) {
                        lx.i += 1;
                        named = true;
                    }
                }
                Some(_) => lx.i += 1,
            }
        }
    };
    lx.i = next;
    if !m.raw {
        decode(&s[start..end], named_escape)?;
    }
    Ok(Step::Token(tok(Kind::FStringMiddle, start, end)))
}

/// `decode_unicode_with_escapes` over a literal part: a backslash at the
/// part's end (before a brace) is a backslash.
fn decode(part: &[u32], named_escape: &mut bool) -> Result<(), ModuleError> {
    let tail = part.iter().rev().take_while(|&&c| c == BS).count();
    let body = if tail % 2 == 1 {
        &part[..part.len() - 1]
    } else {
        part
    };
    match decode_str(body) {
        Ok(_) => Ok(()),
        Err(LexError::NeedsNameTable) => {
            *named_escape = true;
            Ok(())
        }
        Err(_) => err("(unicode error)"),
    }
}
