//! The literal reader over [`super::lex`]'s tokens: `ast.literal_eval` of
//! CPython 3.13.13 (the eval-mode parse of the literal forms and
//! `Lib/ast.py`'s `_convert`, `_convert_num`, `_convert_signed_num`, the
//! `set()` call and the complex `BinOp` rule) into a [`Literal`], and
//! `claims._canonical_repr` (`claims.py` 204-231) of that value.
//!
//! A text that parses to something other than a literal (ValueError in
//! the reference) and one that does not parse (SyntaxError) are one
//! [`LiteralError::Invalid`]: every caller catches every exception. Depth
//! is bounded by the tokenizer's bracket limit ([`super::lex::MAX_LEVEL`]):
//! a sign or a binary `+`/`-` adds one level at most, since a second one
//! in a row is never a literal and is refused where it is read.

use std::collections::{HashMap, HashSet};

use super::lex::{literal_tokens, LexError, Tok, Token, INT_MAX_STR_DIGITS};
use crate::float_repr;
use crate::pyvalue::{repr_str, PyStr};

/// Why a text is not a literal, or why its value has no canonical text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LiteralError {
    /// [`LexError::NeedsIdentTable`], passed through.
    NeedsIdentTable,
    /// [`LexError::NeedsNameTable`], passed through.
    NeedsNameTable,
    /// What the reference raises, by a short kind.
    Invalid(&'static str),
}

impl From<LexError> for LiteralError {
    fn from(e: LexError) -> Self {
        match e {
            LexError::NeedsIdentTable => Self::NeedsIdentTable,
            LexError::NeedsNameTable => Self::NeedsNameTable,
            LexError::Syntax(k) => Self::Invalid(k),
        }
    }
}

type R<T> = Result<T, LiteralError>;

fn bad<T>(kind: &'static str) -> R<T> {
    Err(LiteralError::Invalid(kind))
}

/// A value `ast.literal_eval` returns. An int is canonical decimal text
/// (`-` for a negative one); a set keeps the first of equal elements and a
/// dict the first of equal keys with the last value, in insertion order.
#[derive(Clone, Debug, PartialEq)]
pub enum Literal {
    None,
    Bool(bool),
    Int(String),
    Float(f64),
    Complex(f64, f64),
    Str(PyStr),
    Bytes(Vec<u8>),
    Ellipsis,
    Tuple(Vec<Literal>),
    List(Vec<Literal>),
    Set(Vec<Literal>),
    Dict(Vec<(Literal, Literal)>),
}

impl Literal {
    /// A tuple, list, set or dict.
    pub fn is_container(&self) -> bool {
        matches!(
            self,
            Self::Tuple(_) | Self::List(_) | Self::Set(_) | Self::Dict(_)
        )
    }

    /// The text of a `str`.
    pub fn as_str(&self) -> Option<&PyStr> {
        match self {
            Self::Str(s) => Some(s),
            _ => None,
        }
    }
}

/// The parsed tree, as far as the literal forms need it.
enum Node {
    Const(Literal),
    Unary(bool, Box<Node>),
    Bin(bool, Box<Node>, Box<Node>),
    Tuple(Vec<Node>),
    List(Vec<Node>),
    Set(Vec<Node>),
    Dict(Vec<(Node, Node)>),
    /// The name `set`, a literal only when called with nothing.
    SetName,
    EmptySet,
}

struct Parser<'a> {
    src: &'a [u32],
    toks: &'a [Token],
    i: usize,
}

fn same(text: &[u32], s: &str) -> bool {
    text.iter().copied().eq(s.bytes().map(u32::from))
}

impl Parser<'_> {
    fn text(&self, t: &Token) -> &[u32] {
        &self.src[t.start..t.end]
    }

    fn is_op(&self, s: &str) -> bool {
        self.toks
            .get(self.i)
            .is_some_and(|t| t.tok == Tok::Op && same(self.text(t), s))
    }

    fn eat(&mut self, s: &str) -> bool {
        let yes = self.is_op(s);
        self.i += usize::from(yes);
        yes
    }

    fn expect(&mut self, s: &str) -> R<()> {
        if self.eat(s) {
            Ok(())
        } else {
            bad("unexpected token")
        }
    }

    fn sign(&self) -> Option<bool> {
        if self.is_op("+") {
            Some(false)
        } else if self.is_op("-") {
            Some(true)
        } else {
            None
        }
    }

    /// `expressions` at the top: one expression or a bare tuple.
    fn top(&mut self) -> R<Node> {
        let first = self.sum()?;
        let node = if self.is_op(",") {
            let mut items = vec![first];
            while self.eat(",") && self.i < self.toks.len() {
                items.push(self.sum()?);
            }
            Node::Tuple(items)
        } else {
            first
        };
        if self.i != self.toks.len() {
            return bad("unexpected token");
        }
        Ok(node)
    }

    /// `sum` to one binary `+` or `-`: a second is never a literal.
    fn sum(&mut self) -> R<Node> {
        let left = self.factor()?;
        let Some(neg) = self.sign() else {
            return Ok(left);
        };
        self.i += 1;
        let right = self.factor()?;
        if self.sign().is_some() {
            return bad("not a literal");
        }
        Ok(Node::Bin(neg, Box::new(left), Box::new(right)))
    }

    /// `factor` to one sign: a sign on a sign is never a literal.
    fn factor(&mut self) -> R<Node> {
        let Some(neg) = self.sign() else {
            return self.atom();
        };
        self.i += 1;
        if self.sign().is_some() {
            return bad("not a literal");
        }
        Ok(Node::Unary(neg, Box::new(self.atom()?)))
    }

    /// Items up to `close`, and whether a comma was read.
    fn items(&mut self, close: &str) -> R<(Vec<Node>, bool)> {
        let (mut items, mut comma) = (Vec::new(), false);
        while !self.is_op(close) {
            items.push(self.sum()?);
            if !self.eat(",") {
                break;
            }
            comma = true;
        }
        self.expect(close)?;
        Ok((items, comma))
    }

    fn braces(&mut self) -> R<Node> {
        if self.eat("}") {
            return Ok(Node::Dict(Vec::new()));
        }
        let first = self.sum()?;
        if !self.eat(":") {
            let mut items = vec![first];
            while self.eat(",") && !self.is_op("}") {
                items.push(self.sum()?);
            }
            self.expect("}")?;
            return Ok(Node::Set(items));
        }
        let mut pairs = vec![(first, self.sum()?)];
        while self.eat(",") && !self.is_op("}") {
            let key = self.sum()?;
            self.expect(":")?;
            pairs.push((key, self.sum()?));
        }
        self.expect("}")?;
        Ok(Node::Dict(pairs))
    }

    fn atom(&mut self) -> R<Node> {
        let Some(t) = self.toks.get(self.i) else {
            return bad("unexpected end");
        };
        self.i += 1;
        let mut node = match &t.tok {
            Tok::Int(d) => Node::Const(Literal::Int(d.clone())),
            Tok::Float(x) => Node::Const(Literal::Float(*x)),
            Tok::Imag(x) => Node::Const(Literal::Complex(0.0, *x)),
            Tok::Str(s) => {
                let mut out = s.clone();
                while let Some(Token { tok, .. }) = self.toks.get(self.i) {
                    match tok {
                        Tok::Str(s) => out.extend_from_slice(s),
                        Tok::Bytes(_) => return bad("str and bytes joined"),
                        _ => break,
                    }
                    self.i += 1;
                }
                Node::Const(Literal::Str(out))
            }
            Tok::Bytes(b) => {
                let mut out = b.clone();
                while let Some(Token { tok, .. }) = self.toks.get(self.i) {
                    match tok {
                        Tok::Bytes(b) => out.extend_from_slice(b),
                        Tok::Str(_) => return bad("str and bytes joined"),
                        _ => break,
                    }
                    self.i += 1;
                }
                Node::Const(Literal::Bytes(out))
            }
            Tok::FString => return bad("f-string"),
            Tok::Name => {
                let name = self.text(t);
                if same(name, "True") {
                    Node::Const(Literal::Bool(true))
                } else if same(name, "False") {
                    Node::Const(Literal::Bool(false))
                } else if same(name, "None") {
                    Node::Const(Literal::None)
                } else if same(name, "set") {
                    Node::SetName
                } else {
                    return bad("not a literal");
                }
            }
            Tok::Op => {
                let op = self.text(t);
                if same(op, "(") {
                    let (mut items, comma) = self.items(")")?;
                    if items.len() == 1 && !comma {
                        items.pop().unwrap_or(Node::SetName)
                    } else {
                        Node::Tuple(items)
                    }
                } else if same(op, "[") {
                    Node::List(self.items("]")?.0)
                } else if same(op, "{") {
                    self.braces()?
                } else if same(op, "...") {
                    Node::Const(Literal::Ellipsis)
                } else {
                    return bad("unexpected token");
                }
            }
        };
        // Trailers: only `set()` is a literal call.
        if self.is_op("(") {
            let closed = self
                .toks
                .get(self.i + 1)
                .is_some_and(|t| t.tok == Tok::Op && same(self.text(t), ")"));
            if !(matches!(node, Node::SetName) && closed) {
                return bad("not a literal");
            }
            self.i += 2;
            node = Node::EmptySet;
        }
        if self.is_op("(") || self.is_op("[") || self.is_op(".") {
            return bad("not a literal");
        }
        Ok(node)
    }
}

/// `ast.literal_eval(text)`.
pub fn literal_eval(text: &[u32]) -> R<Literal> {
    let lexed = literal_tokens(text)?;
    let mut p = Parser {
        src: &lexed.source,
        toks: &lexed.tokens,
        i: 0,
    };
    convert(p.top()?)
}

/// `_convert_num`: an int, float or complex constant (never a bool).
fn convert_num(node: Node) -> R<Literal> {
    match node {
        Node::Const(v @ (Literal::Int(_) | Literal::Float(_) | Literal::Complex(..))) => Ok(v),
        _ => bad("malformed node"),
    }
}

/// `_convert_signed_num`.
fn convert_signed(node: Node) -> R<Literal> {
    let Node::Unary(neg, operand) = node else {
        return convert_num(node);
    };
    let v = convert_num(*operand)?;
    Ok(match v {
        _ if !neg => v,
        Literal::Int(d) if d == "0" => Literal::Int(d),
        Literal::Int(d) => match d.strip_prefix('-') {
            Some(abs) => Literal::Int(abs.to_owned()),
            None => Literal::Int(format!("-{d}")),
        },
        Literal::Float(x) => Literal::Float(-x),
        Literal::Complex(re, im) => Literal::Complex(-re, -im),
        other => other,
    })
}

/// `float(int)`: correctly rounded, OverflowError past the float range.
fn int_to_float(d: &str) -> R<f64> {
    match d.parse::<f64>() {
        Ok(x) if x.is_finite() => Ok(x),
        _ => bad("int too large to convert to float"),
    }
}

/// `_convert`.
fn convert(node: Node) -> R<Literal> {
    Ok(match node {
        Node::Const(v) => v,
        Node::Tuple(items) => Literal::Tuple(items.into_iter().map(convert).collect::<R<_>>()?),
        Node::List(items) => Literal::List(items.into_iter().map(convert).collect::<R<_>>()?),
        Node::EmptySet => Literal::Set(Vec::new()),
        Node::Set(items) => {
            let mut seen = HashSet::new();
            let mut out = Vec::new();
            for item in items {
                let v = convert(item)?;
                if seen.insert(key(&v)?) {
                    out.push(v);
                }
            }
            Literal::Set(out)
        }
        Node::Dict(pairs) => {
            let mut at: HashMap<Key, usize> = HashMap::new();
            let mut out: Vec<(Literal, Literal)> = Vec::new();
            for (k, v) in pairs {
                let (k, v) = (convert(k)?, convert(v)?);
                match at.get(&key(&k)?) {
                    Some(&i) => out[i].1 = v,
                    None => {
                        at.insert(key(&k)?, out.len());
                        out.push((k, v));
                    }
                }
            }
            Literal::Dict(out)
        }
        Node::Bin(neg, left, right) => {
            let left = convert_signed(*left)?;
            let right = convert_num(*right)?;
            let x = match left {
                Literal::Int(d) => int_to_float(&d)?,
                Literal::Float(x) => x,
                _ => return bad("malformed node"),
            };
            let Literal::Complex(re, im) = right else {
                return bad("malformed node");
            };
            // 3.13's mixed arithmetic: the real operand is complex(x, 0.0).
            if neg {
                Literal::Complex(x - re, 0.0 - im)
            } else {
                Literal::Complex(x + re, 0.0 + im)
            }
        }
        other => convert_signed(other)?,
    })
}

/// A hashable value by Python equality: equal values, one key.
#[derive(Hash, PartialEq, Eq)]
enum Key {
    None,
    Ellipsis,
    /// An integral number by its exact decimal text.
    Int(String),
    /// A float that is not integral (or infinite), by its bits.
    Float(u64),
    Complex(u64, u64),
    Str(PyStr),
    Bytes(Vec<u8>),
    Tuple(Vec<Key>),
}

fn float_key(x: f64) -> Key {
    if x.is_finite() && x.fract() == 0.0 {
        Key::Int(exact_decimal(x))
    } else {
        Key::Float(x.to_bits())
    }
}

/// `hash(v)`'s precondition: TypeError for a list, set or dict.
fn key(v: &Literal) -> R<Key> {
    Ok(match v {
        Literal::None => Key::None,
        Literal::Ellipsis => Key::Ellipsis,
        Literal::Bool(b) => Key::Int(if *b { "1" } else { "0" }.to_owned()),
        Literal::Int(d) => Key::Int(d.clone()),
        Literal::Float(x) => float_key(*x),
        Literal::Complex(re, im) if *im == 0.0 => float_key(*re),
        Literal::Complex(re, im) => Key::Complex((re + 0.0).to_bits(), (im + 0.0).to_bits()),
        Literal::Str(s) => Key::Str(s.clone()),
        Literal::Bytes(b) => Key::Bytes(b.clone()),
        Literal::Tuple(items) => Key::Tuple(items.iter().map(key).collect::<R<_>>()?),
        _ => return bad("unhashable type"),
    })
}

/// The exact decimal text of an integral finite float.
fn exact_decimal(x: f64) -> String {
    if x.abs() < 1e18 {
        return format!("{}", x as i64);
    }
    let bits = x.to_bits();
    let mut shift = ((bits >> 52) & 0x7FF) as i64 - 1075;
    let mant = (bits & ((1 << 52) - 1)) | (1 << 52);
    // Limbs of 10**9, least significant first; the shift is positive here.
    let mut limbs = vec![mant % 1_000_000_000, mant / 1_000_000_000];
    while shift > 0 {
        let step = shift.min(29);
        shift -= step;
        let mut carry = 0u64;
        for limb in &mut limbs {
            let v = (*limb << step) + carry;
            *limb = v % 1_000_000_000;
            carry = v / 1_000_000_000;
        }
        while carry > 0 {
            limbs.push(carry % 1_000_000_000);
            carry /= 1_000_000_000;
        }
    }
    while limbs.len() > 1 && limbs.last() == Some(&0) {
        limbs.pop();
    }
    let mut out = String::from(if x < 0.0 { "-" } else { "" });
    let mut it = limbs.iter().rev();
    if let Some(top) = it.next() {
        out += &top.to_string();
    }
    for limb in it {
        out += &format!("{limb:09}");
    }
    out
}

fn push(out: &mut PyStr, s: &str) {
    out.extend(s.chars().map(u32::from));
}

/// `float.__repr__` without the `.0` that `complex_repr`'s `'r'` format
/// does not add.
fn bare(x: f64) -> String {
    let s = float_repr::repr(x);
    match s.strip_suffix(".0") {
        Some(t) => t.to_owned(),
        None => s,
    }
}

/// CPython's `complex_repr`: the real part is left out only when it is +0.0.
fn complex_repr(re: f64, im: f64) -> String {
    let im_text = bare(im);
    if re == 0.0 && re.is_sign_positive() {
        return format!("{im_text}j");
    }
    let sign = if im_text.starts_with('-') { "" } else { "+" };
    format!("({}{sign}{im_text}j)", bare(re))
}

/// `bytes.__repr__`.
fn bytes_repr(b: &[u8]) -> String {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = format!("b{quote}");
    for &c in b {
        match c {
            _ if c == quote as u8 || c == b'\\' => {
                out.push('\\');
                out.push(c as char);
            }
            b'\t' => out += "\\t",
            b'\n' => out += "\\n",
            b'\r' => out += "\\r",
            0x20..=0x7E => out.push(c as char),
            _ => out += &format!("\\x{c:02x}"),
        }
    }
    out.push(quote);
    out
}

fn joined(out: &mut PyStr, parts: &[PyStr]) {
    for (i, part) in parts.iter().enumerate() {
        if i > 0 {
            push(out, ", ");
        }
        out.extend_from_slice(part);
    }
}

/// `claims._canonical_repr`: `repr`, with sets and dicts sorted by their
/// members' canonical text (code point order). ValueError for an int of
/// more than 4,300 digits, as `int.__repr__` raises.
pub fn canonical_repr(v: &Literal) -> R<PyStr> {
    let all = |items: &[Literal]| items.iter().map(canonical_repr).collect::<R<Vec<_>>>();
    let mut out = PyStr::new();
    match v {
        Literal::None => push(&mut out, "None"),
        Literal::Bool(b) => push(&mut out, if *b { "True" } else { "False" }),
        Literal::Ellipsis => push(&mut out, "Ellipsis"),
        Literal::Int(d) if d.trim_start_matches('-').len() > INT_MAX_STR_DIGITS => {
            return bad("Exceeds the limit (4300 digits) for integer string conversion")
        }
        Literal::Int(d) => push(&mut out, d),
        Literal::Float(x) => push(&mut out, &float_repr::repr(*x)),
        Literal::Complex(re, im) => push(&mut out, &complex_repr(*re, *im)),
        Literal::Str(s) => out = repr_str(s),
        Literal::Bytes(b) => push(&mut out, &bytes_repr(b)),
        Literal::Set(items) if items.is_empty() => push(&mut out, "set()"),
        Literal::Set(items) => {
            let mut parts = all(items)?;
            parts.sort();
            push(&mut out, "{");
            joined(&mut out, &parts);
            push(&mut out, "}");
        }
        Literal::Dict(pairs) => {
            let mut parts = pairs
                .iter()
                .map(|(k, v)| Ok((canonical_repr(k)?, canonical_repr(v)?)))
                .collect::<R<Vec<_>>>()?;
            parts.sort();
            let parts: Vec<PyStr> = parts
                .into_iter()
                .map(|(mut k, v)| {
                    push(&mut k, ": ");
                    k.extend(v);
                    k
                })
                .collect();
            push(&mut out, "{");
            joined(&mut out, &parts);
            push(&mut out, "}");
        }
        Literal::Tuple(items) => {
            push(&mut out, "(");
            joined(&mut out, &all(items)?);
            push(&mut out, if items.len() == 1 { ",)" } else { ")" });
        }
        Literal::List(items) => {
            push(&mut out, "[");
            joined(&mut out, &all(items)?);
            push(&mut out, "]");
        }
    }
    Ok(out)
}
