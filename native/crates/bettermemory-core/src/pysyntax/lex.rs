//! CPython 3.13.13's tokenizer for one expression in eval mode, as
//! `ast.literal_eval(text)` meets it: the text after `lstrip(' \t')`, its
//! line ends translated as `_PyTokenizer_FromString` translates them, read
//! into NAME, NUMBER, STRING and OP tokens with each string literal decoded
//! and each number literal read. Every failure is one `Err`: the callers
//! catch every exception, so only which inputs fail matters.

use crate::pyvalue::PyStr;
use crate::unicode;

/// Why a text does not lex.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LexError {
    /// No longer answered: a name past ASCII is read with the XID tables
    /// (unit claims-6). Kept until unit 5d, since callers match on it.
    NeedsIdentTable,
    /// No longer answered: `\N{...}` is read with `unicode::names` (unit
    /// claims-6). Kept until unit 5d, since callers match on it.
    NeedsNameTable,
    /// What CPython raises, by a short kind.
    Syntax(&'static str),
}

/// A token's value.
#[derive(Clone, Debug, PartialEq)]
pub enum Tok {
    Name,
    /// An integer as canonical decimal text, for `PyValue::int_from_decimal`.
    Int(String),
    Float(f64),
    /// An imaginary literal by its imaginary part.
    Imag(f64),
    Str(PyStr),
    Bytes(Vec<u8>),
    /// An f-string literal; lexing stops after it (the literal reader
    /// refuses it, whatever follows).
    FString,
    Op,
}

/// One token: its value and its span in [`Lexed::source`].
#[derive(Clone, Debug, PartialEq)]
pub struct Token {
    pub tok: Tok,
    pub start: usize,
    pub end: usize,
}

impl Token {
    /// The token type `tokenize` names (`FSTRING` for the whole f-string).
    pub fn type_name(&self) -> &'static str {
        match self.tok {
            Tok::Name => "NAME",
            Tok::Int(_) | Tok::Float(_) | Tok::Imag(_) => "NUMBER",
            Tok::Str(_) | Tok::Bytes(_) => "STRING",
            Tok::FString => "FSTRING",
            Tok::Op => "OP",
        }
    }
}

/// The text as the tokenizer read it, and its tokens.
#[derive(Clone, Debug, PartialEq)]
pub struct Lexed {
    pub source: PyStr,
    pub tokens: Vec<Token>,
}

/// CPython's `MAXLEVEL`: the brackets open at once.
pub const MAX_LEVEL: usize = 200;
/// The default of `sys.get_int_max_str_digits()`.
pub const INT_MAX_STR_DIGITS: usize = 4300;

const NL: u32 = 0x0A;
const BS: u32 = 0x5C;

fn err<T>(kind: &'static str) -> Result<T, LexError> {
    Err(LexError::Syntax(kind))
}

/// `ast.literal_eval`'s reading of `text`: `lstrip(' \t')`, then [`lex`].
pub fn literal_tokens(text: &[u32]) -> Result<Lexed, LexError> {
    let skip = text.iter().take_while(|&&c| c == 0x20 || c == 0x09).count();
    lex(&text[skip..])
}

/// The tokens of `text` in eval mode.
pub fn lex(text: &[u32]) -> Result<Lexed, LexError> {
    let mut s = Vec::with_capacity(text.len());
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
    let tokens = Lexer {
        s: &s,
        i: 0,
        out: Vec::new(),
    }
    .run()?;
    Ok(Lexed { source: s, tokens })
}

pub(super) struct Lexer<'a> {
    pub(super) s: &'a [u32],
    pub(super) i: usize,
    pub(super) out: Vec<Token>,
}

fn is_ident_start(c: u32) -> bool {
    c >= 0x80 || c == 0x5F || (c as u8).is_ascii_alphabetic()
}

fn is_ident_char(c: u32) -> bool {
    is_ident_start(c) || is_digit(c)
}

fn is_digit(c: u32) -> bool {
    (0x30..=0x39).contains(&c)
}

fn hex_val(c: u32) -> Option<u32> {
    char::from_u32(c).and_then(|ch| ch.to_digit(16))
}

const OPS3: [&str; 5] = ["**=", "...", "//=", "<<=", ">>="];
const OPS2: [&str; 20] = [
    "!=", "%=", "&=", "**", "*=", "+=", "-=", "->", "//", "/=", ":=", "<<", "<=", "<>", "==", ">=",
    ">>", "@=", "^=", "|=",
];
const OPS1: &str = "%&()*+,-./:;<=>@[]^{|}~!$?`";

impl Lexer<'_> {
    fn at(&self, i: usize) -> Option<u32> {
        self.s.get(i).copied()
    }

    fn starts(&self, at: usize, text: &str) -> bool {
        text.bytes()
            .enumerate()
            .all(|(k, b)| self.at(at + k) == Some(u32::from(b)))
    }

    fn push(&mut self, tok: Tok, start: usize) {
        self.out.push(Token {
            tok,
            start,
            end: self.i,
        });
    }

    fn run(mut self) -> Result<Vec<Token>, LexError> {
        let mut stack: Vec<u32> = Vec::new();
        let mut at_bol = true;
        let mut ended = false;
        let mut line_tokens = false;
        loop {
            if at_bol {
                at_bol = false;
                let mut col = 0usize;
                // `cont_line_col`: the column at the first continuation
                // past column 0 holds, whatever a form feed does after it.
                let mut cont_col = 0usize;
                loop {
                    col = match self.at(self.i) {
                        Some(0x20) => col + 1,
                        Some(0x09) => (col / 8 + 1) * 8,
                        Some(0x0C) => 0,
                        Some(BS) => {
                            if cont_col == 0 {
                                cont_col = col;
                            }
                            self.continuation()?;
                            continue;
                        }
                        _ => break,
                    };
                    self.i += 1;
                }
                if cont_col != 0 {
                    col = cont_col;
                }
                match self.at(self.i) {
                    Some(0x23) | Some(NL) => {}
                    None if col == 0 => break,
                    _ if col > 0 => return err("unexpected indent"),
                    _ if ended => return err("token after the end"),
                    _ => {}
                }
            }
            let Some(c) = self.at(self.i) else { break };
            let start = self.i;
            match c {
                0x20 | 0x09 | 0x0C => self.i += 1,
                0x23 => {
                    while self.at(self.i).is_some_and(|c| c != NL) {
                        self.i += 1;
                    }
                }
                NL => {
                    self.i += 1;
                    if stack.is_empty() {
                        at_bol = true;
                        ended |= line_tokens;
                        line_tokens = false;
                    }
                }
                BS => self.continuation()?,
                _ => {
                    line_tokens = true;
                    if c == 0x27 || c == 0x22 {
                        self.string(start, false, false, false)?;
                    } else if is_ident_start(c) {
                        if self.name_or_prefix(start)? {
                            return Ok(self.out);
                        }
                    } else if is_digit(c)
                        || (c == 0x2E && self.at(self.i + 1).is_some_and(is_digit))
                    {
                        self.number(start)?;
                    } else {
                        self.op(c, start, &mut stack)?;
                    }
                }
            }
        }
        if !stack.is_empty() {
            return err("bracket never closed");
        }
        Ok(self.out)
    }

    /// A backslash: it must end the line, and a line must follow.
    pub(super) fn continuation(&mut self) -> Result<(), LexError> {
        if self.at(self.i + 1) != Some(NL) {
            return err("character after line continuation");
        }
        if self.i + 2 >= self.s.len() {
            return err("unexpected EOF");
        }
        self.i += 2;
        Ok(())
    }

    pub(super) fn op(
        &mut self,
        c: u32,
        start: usize,
        stack: &mut Vec<u32>,
    ) -> Result<(), LexError> {
        if c < 0x20 || c == 0x7F {
            return err("non-printable character");
        }
        let len = if OPS3.iter().any(|o| self.starts(start, o)) {
            3
        } else if OPS2.iter().any(|o| self.starts(start, o)) {
            2
        } else if c < 0x80 && OPS1.as_bytes().contains(&(c as u8)) {
            1
        } else {
            return err("invalid character");
        };
        if len == 1 {
            match c {
                0x28 | 0x5B | 0x7B => {
                    if stack.len() >= MAX_LEVEL {
                        return err("too many nested parentheses");
                    }
                    stack.push(c);
                }
                0x29 | 0x5D | 0x7D => {
                    let open = stack.pop().ok_or(LexError::Syntax("unmatched bracket"))?;
                    let want = if open == 0x28 { 0x29 } else { open + 2 };
                    if want != c {
                        return err("bracket does not match");
                    }
                }
                _ => {}
            }
        }
        self.i += len;
        self.push(Tok::Op, start);
        Ok(())
    }

    /// A name, or a string's prefix; true when an f-string ended lexing.
    fn name_or_prefix(&mut self, start: usize) -> Result<bool, LexError> {
        let (mut b, mut r, mut u, mut f) = (false, false, false, false);
        loop {
            let c = self.at(self.i).map_or(0, |c| c | 0x20);
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
            self.i += 1;
            if matches!(self.at(self.i), Some(0x27) | Some(0x22)) {
                self.string(start, b, r, f)?;
                return Ok(f);
            }
        }
        while self.at(self.i).is_some_and(is_ident_char) {
            self.i += 1;
        }
        let run = &self.s[start..self.i];
        // `verify_identifier`: `_PyUnicode_ScanIdentifier` over the raw
        // run. The token keeps the raw text; the parser reads its NFKC.
        if run.iter().any(|&c| c >= 0x80) && !unicode::xid::is_identifier(run) {
            return err("invalid character");
        }
        self.push(Tok::Name, start);
        Ok(false)
    }

    /// A string literal from its opening quote at `self.i`.
    pub(super) fn string(
        &mut self,
        start: usize,
        b: bool,
        r: bool,
        f: bool,
    ) -> Result<(), LexError> {
        let quote = self.s[self.i];
        let size = if self.at(self.i + 1) == Some(quote) && self.at(self.i + 2) == Some(quote) {
            3
        } else {
            1
        };
        self.i += size;
        let body = self.i;
        let mut run = 0;
        while run < size {
            let Some(c) = self.at(self.i) else {
                return err("unterminated string");
            };
            self.i += 1;
            if c == quote {
                run += 1;
                continue;
            }
            run = 0;
            if size == 1 && c == NL {
                return err("unterminated string");
            }
            if c == BS && self.i < self.s.len() {
                self.i += 1;
            }
        }
        let text = &self.s[body..self.i - size];
        let tok = if f {
            Tok::FString
        } else if b {
            if text.iter().any(|&c| c >= 0x80) {
                return err("bytes past ASCII");
            }
            Tok::Bytes(if r {
                text.iter().map(|&c| c as u8).collect()
            } else {
                decode_bytes(text)?
            })
        } else if r {
            Tok::Str(text.to_vec())
        } else {
            Tok::Str(decode_str(text)?)
        };
        self.push(tok, start);
        Ok(())
    }

    fn digits_while(&mut self, ok: impl Fn(u32) -> bool) {
        while self.at(self.i).is_some_and(&ok) {
            self.i += 1;
        }
    }

    /// `tok_decimal_tail`: digits, one underscore between two.
    fn decimal_tail(&mut self) -> Result<(), LexError> {
        loop {
            self.digits_while(is_digit);
            if self.at(self.i) != Some(0x5F) {
                return Ok(());
            }
            if !self.at(self.i + 1).is_some_and(is_digit) {
                return err("invalid decimal literal");
            }
            self.i += 1;
        }
    }

    /// `verify_end_of_number`: a letter or digit may not follow, except
    /// the keywords that can (a warning in CPython, no error).
    fn end_of_number(&self) -> Result<(), LexError> {
        let Some(c) = self.at(self.i) else {
            return Ok(());
        };
        // `lookahead`: the keyword's letters, then no identifier character.
        let next = |t: &str| {
            self.starts(self.i + 1, t) && !self.at(self.i + 1 + t.len()).is_some_and(is_ident_char)
        };
        let keyword = match c {
            0x61 => next("nd"),
            0x65 => next("lse"),
            0x66 => next("or"),
            0x69 => matches!(self.at(self.i + 1), Some(0x66 | 0x6E | 0x73)),
            0x6F => next("r"),
            0x6E => next("ot"),
            _ => false,
        };
        if !keyword && c < 0x80 && is_ident_char(c) {
            return err("invalid number literal");
        }
        Ok(())
    }

    pub(super) fn number(&mut self, start: usize) -> Result<(), LexError> {
        let c = self.s[self.i];
        let radix = match (c, self.at(self.i + 1).map(|x| x | 0x20)) {
            (0x30, Some(0x78)) => 16,
            (0x30, Some(0x6F)) => 8,
            (0x30, Some(0x62)) => 2,
            _ => 0,
        };
        if radix > 0 {
            self.i += 2;
            let ok = move |c: u32| char::from_u32(c).is_some_and(|ch| ch.is_digit(radix));
            loop {
                if self.at(self.i) == Some(0x5F) {
                    self.i += 1;
                }
                if !self.at(self.i).is_some_and(ok) {
                    return err("invalid radix literal");
                }
                self.digits_while(ok);
                if self.at(self.i) != Some(0x5F) {
                    break;
                }
            }
            if radix < 16 && self.at(self.i).is_some_and(is_digit) {
                return err("invalid digit in literal");
            }
            self.end_of_number()?;
            let digits: Vec<u32> = self.s[start + 2..self.i]
                .iter()
                .filter_map(|&c| hex_val(c))
                .collect();
            self.push(Tok::Int(radix_to_decimal(&digits, radix)), start);
            return Ok(());
        }
        let mut float = false;
        if c == 0x2E {
            float = true;
            self.i += 1;
            self.decimal_tail()?;
        } else {
            self.decimal_tail()?;
            if self.at(self.i) == Some(0x2E) {
                float = true;
                self.i += 1;
                if self.at(self.i).is_some_and(is_digit) {
                    self.decimal_tail()?;
                }
            }
        }
        if matches!(self.at(self.i), Some(0x65 | 0x45)) {
            let e = self.i;
            self.i += 1;
            if matches!(self.at(self.i), Some(0x2B | 0x2D)) {
                self.i += 1;
                if !self.at(self.i).is_some_and(is_digit) {
                    return err("invalid decimal literal");
                }
            }
            if self.at(self.i).is_some_and(is_digit) {
                float = true;
                self.decimal_tail()?;
            } else {
                self.i = e;
                self.end_of_number()?;
            }
        }
        let text: String = self.s[start..self.i]
            .iter()
            .filter(|&&c| c != 0x5F)
            .map(|&c| c as u8 as char)
            .collect();
        if matches!(self.at(self.i), Some(0x6A | 0x4A)) {
            self.i += 1;
            self.end_of_number()?;
            self.push(Tok::Imag(parse_float(&text)), start);
            return Ok(());
        }
        if float {
            self.end_of_number()?;
            self.push(Tok::Float(parse_float(&text)), start);
            return Ok(());
        }
        if text.starts_with('0') && text.bytes().any(|b| b != b'0') {
            return err("leading zeros");
        }
        self.end_of_number()?;
        if text.len() > INT_MAX_STR_DIGITS && !text.starts_with('0') {
            return err("exceeds the integer digit limit");
        }
        let body = text.trim_start_matches('0');
        self.push(
            Tok::Int(if body.is_empty() {
                "0".into()
            } else {
                body.into()
            }),
            start,
        );
        Ok(())
    }
}

/// Rust's correctly rounded reading; an overflow is infinity, as in CPython.
fn parse_float(text: &str) -> f64 {
    text.parse().unwrap_or(f64::NAN)
}

/// Digits of a power-of-two radix to decimal text, in limbs of 10**18.
fn radix_to_decimal(digits: &[u32], radix: u32) -> String {
    let mut pows: Vec<Vec<u64>> = Vec::new();
    let limbs = to_limbs(digits, radix, &mut pows);
    let mut out = limbs.last().map_or("0".to_string(), |l| l.to_string());
    for limb in limbs.iter().rev().skip(1) {
        out.push_str(&format!("{limb:018}"));
    }
    out
}

/// Limbs of 10**18, least significant first, no high zero limb.
const LIMB: u64 = 1_000_000_000_000_000_000;

/// The value of `digits` in limbs of 10**18: halves split at a power of
/// two digits and joined as `high * radix**m + low`, so the cost is that of
/// Karatsuba multiplication rather than quadratic.
fn to_limbs(digits: &[u32], radix: u32, pows: &mut Vec<Vec<u64>>) -> Vec<u64> {
    if digits.len() <= 512 {
        return small_to_limbs(digits, radix);
    }
    let k = (digits.len() - 1).ilog2() as usize;
    let m = 1usize << k;
    let (high, low) = digits.split_at(digits.len() - m);
    let high = to_limbs(high, radix, pows);
    let low = to_limbs(low, radix, pows);
    while pows.len() <= k {
        let next = match pows.last() {
            None => small_to_limbs(&[1, 0], radix),
            Some(p) => mul(p, p),
        };
        pows.push(next);
    }
    // pows[j] is radix**(2**j).
    let mut v = mul(&high, &pows[k]);
    add_at(&mut v, &low, 0);
    trim(&mut v);
    v
}

/// The quadratic conversion, for a few hundred digits.
fn small_to_limbs(digits: &[u32], radix: u32) -> Vec<u64> {
    let base = u128::from(LIMB);
    let per = (60 / radix.trailing_zeros()) as usize;
    let mut limbs: Vec<u64> = Vec::new();
    for chunk in digits.chunks(per) {
        let mut add = chunk
            .iter()
            .fold(0u128, |v, &d| v * u128::from(radix) + u128::from(d));
        let mul = u128::from(radix).pow(chunk.len() as u32);
        for limb in limbs.iter_mut() {
            let v = u128::from(*limb) * mul + add;
            *limb = (v % base) as u64;
            add = v / base;
        }
        while add > 0 {
            limbs.push((add % base) as u64);
            add /= base;
        }
    }
    limbs
}

fn trim(v: &mut Vec<u64>) {
    while v.last() == Some(&0) {
        v.pop();
    }
}

/// `acc += x * LIMB**at`, growing `acc` as needed.
fn add_at(acc: &mut Vec<u64>, x: &[u64], at: usize) {
    if acc.len() < at + x.len() {
        acc.resize(at + x.len(), 0);
    }
    let mut carry = 0u64;
    let mut i = 0;
    while i < x.len() || carry > 0 {
        if at + i == acc.len() {
            acc.push(0);
        }
        let v = acc[at + i] + x.get(i).copied().unwrap_or(0) + carry;
        (acc[at + i], carry) = if v >= LIMB { (v - LIMB, 1) } else { (v, 0) };
        i += 1;
    }
}

/// `acc -= x`, where `acc >= x`.
fn sub_from(acc: &mut [u64], x: &[u64]) {
    let mut borrow = 0u64;
    let mut i = 0;
    while i < x.len() || borrow > 0 {
        let s = x.get(i).copied().unwrap_or(0) + borrow;
        (acc[i], borrow) = if acc[i] >= s {
            (acc[i] - s, 0)
        } else {
            (acc[i] + LIMB - s, 1)
        };
        i += 1;
    }
}

fn mul(a: &[u64], b: &[u64]) -> Vec<u64> {
    let (a, b) = if a.len() >= b.len() { (a, b) } else { (b, a) };
    if b.is_empty() {
        return Vec::new();
    }
    if b.len() < 32 {
        return schoolbook(a, b);
    }
    if a.len() >= 2 * b.len() {
        // Unbalanced: pieces of `a` as long as `b`.
        let mut out = Vec::new();
        for (n, piece) in a.chunks(b.len()).enumerate() {
            add_at(&mut out, &mul(piece, b), n * b.len());
        }
        trim(&mut out);
        return out;
    }
    let h = a.len() / 2;
    let (a0, a1) = a.split_at(h);
    let (b0, b1) = b.split_at(h.min(b.len()));
    let z0 = mul(a0, b0);
    let z2 = mul(a1, b1);
    let mut sa = a0.to_vec();
    add_at(&mut sa, a1, 0);
    let mut sb = b0.to_vec();
    add_at(&mut sb, b1, 0);
    let mut z1 = mul(&sa, &sb);
    sub_from(&mut z1, &z0);
    sub_from(&mut z1, &z2);
    let mut out = z0;
    add_at(&mut out, &z1, h);
    add_at(&mut out, &z2, 2 * h);
    trim(&mut out);
    out
}

fn schoolbook(a: &[u64], b: &[u64]) -> Vec<u64> {
    let base = u128::from(LIMB);
    let mut out = vec![0u64; a.len() + b.len()];
    for (i, &x) in a.iter().enumerate() {
        let mut carry = 0u128;
        for (j, &y) in b.iter().enumerate() {
            let v = u128::from(out[i + j]) + u128::from(x) * u128::from(y) + carry;
            out[i + j] = (v % base) as u64;
            carry = v / base;
        }
        let mut k = i + b.len();
        while carry > 0 {
            let v = u128::from(out[k]) + carry;
            out[k] = (v % base) as u64;
            carry = v / base;
            k += 1;
        }
    }
    trim(&mut out);
    out
}

/// Up to `max` digits of `radix` from `body[*i..]`.
fn take_digits(body: &[u32], i: &mut usize, radix: u32, max: usize) -> (u32, usize) {
    let (mut v, mut n) = (0u32, 0usize);
    while n < max {
        let Some(d) = body
            .get(*i)
            .and_then(|&c| char::from_u32(c)?.to_digit(radix))
        else {
            break;
        };
        v = v * radix + d;
        n += 1;
        *i += 1;
    }
    (v, n)
}

/// The escape shared by str and bytes after a backslash, or None.
fn common_escape(c: u32) -> Option<u32> {
    Some(match c {
        NL => return Some(u32::MAX),
        BS | 0x27 | 0x22 => c,
        0x61 => 7,
        0x62 => 8,
        0x66 => 12,
        0x6E => 10,
        0x72 => 13,
        0x74 => 9,
        0x76 => 11,
        _ => return None,
    })
}

/// A non-raw str literal's body, as `_PyUnicode_DecodeUnicodeEscape` reads it.
pub(super) fn decode_str(body: &[u32]) -> Result<PyStr, LexError> {
    let mut out = Vec::with_capacity(body.len());
    let mut i = 0;
    while i < body.len() {
        let c = body[i];
        i += 1;
        if c != BS {
            out.push(c);
            continue;
        }
        let e = body[i];
        if let Some(v) = common_escape(e) {
            i += 1;
            if v != u32::MAX {
                out.push(v);
            }
            continue;
        }
        let width = match e {
            0x30..=0x37 => {
                out.push(take_digits(body, &mut i, 8, 3).0);
                continue;
            }
            0x78 => 2,
            0x75 => 4,
            0x55 => 8,
            0x4E => {
                // `\N{name}`: the name runs to the next `}` and is read by
                // `getcode`; an empty, unclosed or unknown name raises.
                let name = body.get(i + 2..).unwrap_or_default();
                let found = (body.get(i + 1) == Some(&0x7B))
                    .then(|| name.iter().position(|&c| c == 0x7D))
                    .flatten()
                    .filter(|&k| k > 0)
                    .and_then(|k| Some((k, unicode::names::lookup(&name[..k])?)));
                let Some((k, cp)) = found else {
                    return err("malformed \\N escape");
                };
                out.push(cp);
                i += 3 + k;
                continue;
            }
            _ => {
                out.push(BS);
                continue;
            }
        };
        i += 1;
        let (v, n) = take_digits(body, &mut i, 16, width);
        if n < width || v > 0x10FFFF {
            return err("bad escape");
        }
        out.push(v);
    }
    Ok(out)
}

/// A non-raw bytes literal's body, as `_PyBytes_DecodeEscape` reads it.
fn decode_bytes(body: &[u32]) -> Result<Vec<u8>, LexError> {
    let mut out = Vec::with_capacity(body.len());
    let mut i = 0;
    while i < body.len() {
        let c = body[i];
        i += 1;
        if c != BS {
            out.push(c as u8);
            continue;
        }
        let e = body[i];
        if let Some(v) = common_escape(e) {
            i += 1;
            if v != u32::MAX {
                out.push(v as u8);
            }
        } else if (0x30..=0x37).contains(&e) {
            out.push(take_digits(body, &mut i, 8, 3).0 as u8);
        } else if e == 0x78 {
            i += 1;
            let (v, n) = take_digits(body, &mut i, 16, 2);
            if n < 2 {
                return err("invalid \\x escape");
            }
            out.push(v as u8);
        } else {
            out.push(b'\\');
        }
    }
    Ok(out)
}
