//! CPython 3.13.13's tokenizer in module mode, as `ast.parse(source)` in
//! `claims.check_claim` (`claims.py` 672) meets it: the source checked for
//! NUL and lone surrogates, its line ends translated and a final newline
//! added as `_PyTokenizer_translate_newlines` adds it for exec input, then
//! read by `tok_get_normal_mode` into logical lines with INDENT and DEDENT
//! (tabs to multiples of 8, the tab-size-1 consistency check, the
//! 100-level limit). Numbers, strings and operators are read by
//! [`super::lex`]'s reader. No function recurses.

use super::lex::{LexError, Lexer, Tok};
use crate::unicode;

/// A token's kind.
#[derive(Clone, Debug, PartialEq)]
pub enum Kind {
    /// NAME, NUMBER, STRING or OP, as [`super::lex`] reads it.
    Lex(Tok),
    /// An f-string without a replacement field, escape or brace: one
    /// token for `FSTRING_START`, `FSTRING_MIDDLE` and `FSTRING_END`.
    FString,
    Comment,
    Newline,
    /// A line end inside brackets or after a blank or comment-only line.
    Nl,
    Indent,
    Dedent,
    EndMarker,
}

/// One token and its span in [`Module::source`].
#[derive(Clone, Debug, PartialEq)]
pub struct ModToken {
    pub kind: Kind,
    pub start: usize,
    pub end: usize,
}

impl ModToken {
    /// The token type `tokenize` names (`FSTRING` for the whole f-string).
    pub fn type_name(&self) -> &'static str {
        match &self.kind {
            Kind::Lex(Tok::Name) => "NAME",
            Kind::Lex(Tok::Int(_) | Tok::Float(_) | Tok::Imag(_)) => "NUMBER",
            Kind::Lex(Tok::Str(_) | Tok::Bytes(_)) => "STRING",
            Kind::Lex(Tok::FString) | Kind::FString => "FSTRING",
            Kind::Lex(Tok::Op) => "OP",
            Kind::Comment => "COMMENT",
            Kind::Newline => "NEWLINE",
            Kind::Nl => "NL",
            Kind::Indent => "INDENT",
            Kind::Dedent => "DEDENT",
            Kind::EndMarker => "ENDMARKER",
        }
    }
}

/// The source as the tokenizer read it, and its tokens.
#[derive(Clone, Debug, PartialEq)]
pub struct Module {
    pub source: Vec<u32>,
    pub tokens: Vec<ModToken>,
}

impl Module {
    /// A NAME token's identifier as the parser keeps it: NFKC normalised.
    pub fn name(&self, t: &ModToken) -> Vec<u32> {
        let mut out = Vec::new();
        unicode::nfkc_into(&self.source[t.start..t.end], &mut out);
        out
    }
}

/// Why a source does not tokenize.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ModuleError {
    /// What CPython raises (SyntaxError, its subclasses, or ValueError),
    /// by a short kind: `check_claim` catches them all alike.
    Syntax(&'static str),
    /// An f-string holding a brace or a backslash: its fields are read by
    /// the f-string tokenizer of unit 5b.
    NeedsFString,
    /// A `\N{...}` escape in a str literal: the name table is not ported.
    NeedsNameTable,
}

impl From<LexError> for ModuleError {
    fn from(e: LexError) -> Self {
        match e {
            LexError::Syntax(k) => ModuleError::Syntax(k),
            LexError::NeedsNameTable => ModuleError::NeedsNameTable,
            // `lex` answers this for names alone, which this reader reads.
            LexError::NeedsIdentTable => ModuleError::Syntax("name"),
        }
    }
}

/// CPython's `MAXINDENT`: the indentation stack holds at most 99 levels.
pub const MAX_INDENT: usize = 100;
/// The column a tab advances to a multiple of (`tok->tabsize`).
const TABSIZE: usize = 8;

const NL: u32 = 0x0A;
const BS: u32 = 0x5C;

fn err<T>(kind: &'static str) -> Result<T, ModuleError> {
    Err(ModuleError::Syntax(kind))
}

/// The tokens of `text` read as a module, ending with ENDMARKER.
pub fn module_tokens(text: &[u32]) -> Result<Module, ModuleError> {
    let mut s = Vec::with_capacity(text.len() + 1);
    let mut it = text.iter().peekable();
    while let Some(&c) = it.next() {
        if c == 0 {
            return err("null byte");
        }
        if (0xD800..=0xDFFF).contains(&c) {
            return err("surrogate");
        }
        if c == 0x0D {
            it.next_if_eq(&&NL);
            s.push(NL);
        } else {
            s.push(c);
        }
    }
    if s.last().is_some_and(|&c| c != NL) {
        s.push(NL);
    }
    let tokens = read(&s)?;
    Ok(Module { source: s, tokens })
}

fn is_name_start(c: u32) -> bool {
    c >= 0x80 || c == 0x5F || (c as u8).is_ascii_alphabetic()
}

fn is_name_char(c: u32) -> bool {
    is_name_start(c) || (0x30..=0x39).contains(&c)
}

fn token(kind: Kind, start: usize, end: usize) -> ModToken {
    ModToken { kind, start, end }
}

/// `tok_get_normal_mode` over the translated source.
fn read(s: &[u32]) -> Result<Vec<ModToken>, ModuleError> {
    let mut lx = Lexer {
        s,
        i: 0,
        out: Vec::new(),
    };
    let mut out: Vec<ModToken> = Vec::new();
    let mut stack: Vec<u32> = Vec::new();
    // `indstack` and `altindstack`: columns at tab size 8 and at 1.
    let mut ind: Vec<(usize, usize)> = vec![(0, 0)];
    let mut at_bol = true;
    let mut blank = false;
    loop {
        if at_bol {
            at_bol = false;
            let (mut col, mut alt, mut cont) = (0usize, 0usize, 0usize);
            loop {
                match s.get(lx.i) {
                    Some(0x20) => (col, alt) = (col + 1, alt + 1),
                    Some(0x09) => (col, alt) = ((col / TABSIZE + 1) * TABSIZE, alt + 1),
                    Some(0x0C) => (col, alt) = (0, 0),
                    Some(&BS) => {
                        // The first continuation's column holds.
                        if cont == 0 {
                            cont = col;
                        }
                        lx.continuation()?;
                        continue;
                    }
                    _ => break,
                }
                lx.i += 1;
            }
            blank = matches!(s.get(lx.i), Some(0x23) | Some(&NL));
            if !blank && stack.is_empty() {
                if cont != 0 {
                    (col, alt) = (cont, cont);
                }
                let at = lx.i;
                let (top, top_alt) = ind[ind.len() - 1];
                if col == top {
                    if alt != top_alt {
                        return err("inconsistent use of tabs and spaces");
                    }
                } else if col > top {
                    if ind.len() >= MAX_INDENT {
                        return err("too many levels of indentation");
                    }
                    if alt <= top_alt {
                        return err("inconsistent use of tabs and spaces");
                    }
                    ind.push((col, alt));
                    out.push(token(Kind::Indent, line_start(s, at), at));
                } else {
                    while ind.len() > 1 && col < ind[ind.len() - 1].0 {
                        ind.pop();
                        out.push(token(Kind::Dedent, at, at));
                    }
                    let (top, top_alt) = ind[ind.len() - 1];
                    if col != top {
                        return err("unindent does not match any outer indentation level");
                    }
                    if alt != top_alt {
                        return err("inconsistent use of tabs and spaces");
                    }
                }
            }
        }
        let Some(&c) = s.get(lx.i) else {
            if !stack.is_empty() {
                return err("bracket never closed");
            }
            out.push(token(Kind::EndMarker, lx.i, lx.i));
            return Ok(out);
        };
        let start = lx.i;
        match c {
            0x20 | 0x09 | 0x0C => lx.i += 1,
            0x23 => {
                while s.get(lx.i).is_some_and(|&c| c != NL) {
                    lx.i += 1;
                }
                out.push(token(Kind::Comment, start, lx.i));
            }
            NL => {
                lx.i += 1;
                at_bol = true;
                let kind = if blank || !stack.is_empty() {
                    Kind::Nl
                } else {
                    Kind::Newline
                };
                out.push(token(kind, start, lx.i));
            }
            BS => lx.continuation()?,
            _ => {
                if c == 0x27 || c == 0x22 {
                    lx.string(start, false, false, false)?;
                } else if is_name_start(c) {
                    if let Some(t) = name_or_prefix(&mut lx, start)? {
                        out.push(t);
                        continue;
                    }
                } else if (0x30..=0x39).contains(&c)
                    || (c == 0x2E && s.get(lx.i + 1).is_some_and(|d| (0x30..=0x39).contains(d)))
                {
                    lx.number(start)?;
                } else {
                    lx.op(c, start, &mut stack)?;
                }
                let t = lx.out.pop().expect("the reader pushed a token");
                out.push(token(Kind::Lex(t.tok), t.start, t.end));
            }
        }
    }
}

/// Where the line holding `at` starts.
fn line_start(s: &[u32], at: usize) -> usize {
    s[..at].iter().rposition(|&c| c == NL).map_or(0, |p| p + 1)
}

/// A name, a string after its prefix, or an f-string. A name or an
/// f-string is returned; a string is left on the reader's output.
fn name_or_prefix(lx: &mut Lexer<'_>, start: usize) -> Result<Option<ModToken>, ModuleError> {
    let (mut b, mut r, mut u, mut f) = (false, false, false, false);
    loop {
        let c = lx.s.get(lx.i).map_or(0, |c| c | 0x20);
        if !(b || u || f) && c == 0x62 {
            b = true;
        } else if !(b || u || r || f) && c == 0x75 {
            u = true;
        } else if !(r || u) && c == 0x72 {
            r = true;
        } else if !(f || b || u) && c == 0x66 {
            f = true;
        } else {
            break;
        }
        lx.i += 1;
        if matches!(lx.s.get(lx.i), Some(0x27) | Some(0x22)) {
            if f {
                fstring(lx)?;
                return Ok(Some(token(Kind::FString, start, lx.i)));
            }
            lx.string(start, b, r, false)?;
            return Ok(None);
        }
    }
    while lx.s.get(lx.i).is_some_and(|&c| is_name_char(c)) {
        lx.i += 1;
    }
    let run = &lx.s[start..lx.i];
    // `verify_identifier`: `_PyUnicode_ScanIdentifier` over the raw run.
    if run.iter().any(|&c| c >= 0x80) && !unicode::xid::is_identifier(run) {
        return err("invalid character");
    }
    Ok(Some(token(Kind::Lex(Tok::Name), start, lx.i)))
}

/// An f-string from its opening quote: its end when no brace or
/// backslash comes first, else `NeedsFString`.
fn fstring(lx: &mut Lexer<'_>) -> Result<(), ModuleError> {
    let quote = lx.s[lx.i];
    let three = lx.s.get(lx.i + 1) == Some(&quote) && lx.s.get(lx.i + 2) == Some(&quote);
    let size = if three { 3 } else { 1 };
    lx.i += size;
    let mut run = 0;
    while run < size {
        let Some(&c) = lx.s.get(lx.i) else {
            return err("unterminated f-string");
        };
        lx.i += 1;
        if c == quote {
            run += 1;
            continue;
        }
        run = 0;
        match c {
            0x7B | 0x7D | BS => return Err(ModuleError::NeedsFString),
            NL if size == 1 => return err("unterminated f-string"),
            _ => {}
        }
    }
    Ok(())
}
