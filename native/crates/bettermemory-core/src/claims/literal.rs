//! Unit 2 of `claims.py`: the functions that read a value with
//! `ast.literal_eval` (`parse_claim`, `parse_claims`, `load_claims`,
//! `claim_is_addressable`, `governed_claim_paths`, `_rhs_repr`,
//! `string_fragment`, `anchors_from_value`), over
//! [`crate::pysyntax::literal`].
//!
//! The reference catches every exception `literal_eval` raises, so a text
//! that is not a literal is one answer here ([`LiteralError::Invalid`]).
//! A text whose reading needs a table the crate does not hold yet
//! ([`LiteralError::NeedsIdentTable`], [`LiteralError::NeedsNameTable`])
//! has no answer: each function returns that gap as an error of its own,
//! never as the reference's refusal or its `None`.

use std::collections::HashSet;

use super::{
    claim_paths, control_character, is_identifier, lone_surrogate, path_key, strip, Claim, Kind,
    MAX_CLAIMS, MAX_CLAIM_LEN,
};
use crate::pysyntax::literal::{canonical_repr, literal_eval, Literal, LiteralError};
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::unicode;

/// `_MIN_ANCHOR_CHARS`: the fewest code points an anchor line holds.
pub const MIN_ANCHOR_CHARS: usize = 12;

/// Why `parse_claim` or `parse_claims` gives no claim.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ClaimError {
    /// The reference's `ValueError`, by its message.
    Refused(PyStr),
    /// The value needs a table the crate does not hold: the reference
    /// answers, the port cannot say how.
    Gap(LiteralError),
}

/// A message under construction.
struct Msg(PyStr);

impl Msg {
    fn new() -> Self {
        Msg(PyStr::new())
    }
    fn s(mut self, text: &str) -> Self {
        self.0.extend(text.chars().map(u32::from));
        self
    }
    fn r(mut self, text: &[u32]) -> Self {
        self.0.extend(repr_str(text));
        self
    }
    fn refuse<T>(self) -> Result<T, ClaimError> {
        Err(ClaimError::Refused(self.0))
    }
}

const BANG: u32 = 0x21;
const EQ: u32 = 0x3D;
const COLON: u32 = 0x3A;
const BACKSLASH: u32 = 0x5C;
const NL: u32 = 0x0A;

/// `str.lstrip()` with no argument.
fn lstrip(t: &[u32]) -> &[u32] {
    let s = t
        .iter()
        .position(|&c| !unicode::is_space(c))
        .unwrap_or(t.len());
    &t[s..]
}

/// `str.partition(sep)`: before, whether found, after.
fn partition<'a>(t: &'a [u32], sep: &[u32]) -> (&'a [u32], bool, &'a [u32]) {
    match t.windows(sep.len()).position(|w| w == sep) {
        Some(i) => (&t[..i], true, &t[i + sep.len()..]),
        None => (t, false, &[]),
    }
}

/// `ast.literal_eval(text)`: the value, `None` for any exception.
fn read(text: &[u32]) -> Result<Option<Literal>, LiteralError> {
    match literal_eval(text) {
        Ok(v) => Ok(Some(v)),
        Err(LiteralError::Invalid(_)) => Ok(None),
        Err(gap) => Err(gap),
    }
}

/// `_canonical_repr(ast.literal_eval(text))`, `None` for any exception.
fn canon(text: &[u32]) -> Result<Option<PyStr>, LiteralError> {
    let Some(v) = read(text)? else {
        return Ok(None);
    };
    match canonical_repr(&v) {
        Ok(s) => Ok(Some(s)),
        Err(LiteralError::Invalid(_)) => Ok(None),
        Err(gap) => Err(gap),
    }
}

fn claim(kind: Kind, path: &[u32], name: &[u32], value: PyStr) -> Claim {
    Claim {
        kind,
        rel_path: path.to_vec(),
        name: name.to_vec(),
        value,
    }
}

/// `parse_claim`: one wire string into a [`Claim`].
pub fn parse_claim(raw: &[u32]) -> Result<Claim, ClaimError> {
    let full = strip(raw);
    let mut text = full;
    if text.is_empty() {
        return Msg::new().s("a claim cannot be empty").refuse();
    }
    if text.len() > MAX_CLAIM_LEN {
        return Msg::new()
            .s(&format!(
                "claim is {} chars \u{2014} cap is {MAX_CLAIM_LEN}. Claims ",
                text.len()
            ))
            .s("are short path/symbol addresses, not prose.")
            .refuse();
    }
    let absent = text.first() == Some(&BANG);
    if absent {
        text = lstrip(&text[1..]);
        if text.is_empty() {
            return Msg::new()
                .s("an absence claim needs a path after '!' (`!src/gone.py` ")
                .s("asserts nothing exists at that path)")
                .refuse();
        }
    }
    let (path_part, sep, rest) = partition(text, &[COLON, COLON]);
    let path_part = strip(path_part);
    if path_part.is_empty() {
        return Msg::new()
            .s("claim ")
            .r(text)
            .s(" has no path before '::'")
            .refuse();
    }
    if path_part.contains(&BACKSLASH) {
        return Msg::new()
            .s("claim path ")
            .r(path_part)
            .s(" must use forward slashes")
            .refuse();
    }
    if absent {
        if sep {
            return Msg::new()
                .s("claim ")
                .r(full)
                .s(": absence claims are path-only \u{2014} ")
                .s("`!path` asserts nothing exists at that path; symbol and ")
                .s("literal absence are not claimable kinds")
                .refuse();
        }
        return Ok(claim(Kind::Absent, path_part, path_part, PyStr::new()));
    }
    if !sep {
        return Ok(claim(Kind::Path, path_part, path_part, PyStr::new()));
    }
    let rest = strip(rest);
    if rest.is_empty() {
        return Msg::new()
            .s("claim ")
            .r(text)
            .s(" ends at '::' \u{2014} name a symbol ")
            .s("(`path::name`) or a constant (`path::NAME=value`)")
            .refuse();
    }
    let (name, eq, value) = partition(rest, &[EQ]);
    let name = strip(name);
    if !is_identifier(name) {
        return Msg::new()
            .s("claim symbol ")
            .r(name)
            .s(" is not a Python identifier")
            .refuse();
    }
    if !eq {
        return Ok(claim(Kind::Symbol, path_part, name, PyStr::new()));
    }
    let value = strip(value);
    if value.is_empty() {
        return Msg::new()
            .s("claim ")
            .r(text)
            .s(" ends at '=' \u{2014} write the literal value, ")
            .s("quoting strings (`path::NAME='foo'`)")
            .refuse();
    }
    match canon(value) {
        Ok(Some(normalized)) => Ok(claim(Kind::Literal, path_part, name, normalized)),
        Ok(None) => Msg::new()
            .s("claim value ")
            .r(value)
            .s(" is not a Python literal \u{2014} quote ")
            .s("strings (`path::NAME='foo'`), and claim the symbol form ")
            .s("(`path::NAME`) for values that aren't literals")
            .refuse(),
        Err(gap) => Err(ClaimError::Gap(gap)),
    }
}

/// `parse_claims`: a wire list, with the count cap, the path rules and
/// duplicates (by [`Claim::render`]) collapsed in declaration order.
pub fn parse_claims(raw: &[Vec<u32>]) -> Result<Vec<Claim>, ClaimError> {
    if raw.len() > MAX_CLAIMS {
        return Msg::new()
            .s(&format!(
                "claims capped at {MAX_CLAIMS} entries (got {}); ",
                raw.len()
            ))
            .s("a memory claims a handful of facts, not a manifest")
            .refuse();
    }
    let mut out = Vec::new();
    let mut seen: HashSet<Vec<u32>> = HashSet::new();
    for entry in raw {
        let c = parse_claim(entry)?;
        if let Some(cp) = control_character(&c.rel_path) {
            return Msg::new()
                .s("claim path ")
                .r(&c.rel_path)
                .s(&format!(" holds a control character (U+{cp:04X}): "))
                .s("git quotes such a name in every ")
                .s("patch it prints, so claim drift could not read the claim. ")
                .s("Claim a path whose name holds none.")
                .refuse();
        }
        if let Some(cp) = lone_surrogate(&c.rel_path) {
            return Msg::new()
                .s("claim path ")
                .r(&c.rel_path)
                .s(&format!(" holds a lone surrogate (U+{cp:04X}), "))
                .s("which spells no character: no ")
                .s("file name holds one, so claim drift could not read the ")
                .s("claim. Claim the path as its name is spelled.")
                .refuse();
        }
        if seen.insert(c.render()) {
            out.push(c);
        }
    }
    Ok(out)
}

/// `load_claims`: stored entries, skipping a non-`str` entry and one that
/// does not parse. Only a gap stops it.
pub fn load_claims(raw: &[PyValue]) -> Result<Vec<Claim>, LiteralError> {
    let mut out = Vec::new();
    for entry in raw {
        let PyValue::Str(text) = entry else {
            continue;
        };
        match parse_claim(text) {
            Ok(c) => out.push(c),
            Err(ClaimError::Refused(_)) => {}
            Err(ClaimError::Gap(gap)) => return Err(gap),
        }
    }
    Ok(out)
}

/// `claim_is_addressable`: every kind but a literal is; a literal whose
/// value does not read back is not, nor is a container.
pub fn claim_is_addressable(c: &Claim) -> Result<bool, LiteralError> {
    if c.kind != Kind::Literal {
        return Ok(true);
    }
    Ok(read(&c.value)?.is_some_and(|v| !v.is_container()))
}

/// `governed_claim_paths`: [`claim_paths`] less every path whose
/// [`path_key`] an unaddressable claim holds.
pub fn governed_claim_paths(claims: &[Claim]) -> Result<Vec<Vec<u32>>, LiteralError> {
    let mut blocked = HashSet::new();
    for c in claims {
        if !claim_is_addressable(c)? {
            blocked.insert(path_key(&c.rel_path));
        }
    }
    Ok(claim_paths(claims)
        .into_iter()
        .filter(|p| !blocked.contains(&path_key(p)))
        .collect())
}

/// `_rhs_repr`: the canonical text of what follows the first `=`.
pub fn rhs_repr(content: &[u32]) -> Result<Option<PyStr>, LiteralError> {
    let (_, sep, rhs) = partition(content, &[EQ]);
    if !sep {
        return Ok(None);
    }
    canon(strip(rhs))
}

/// `string_fragment`: the text a line that is one string literal holds.
pub fn string_fragment(content: &[u32]) -> Result<Option<PyStr>, LiteralError> {
    let mut s = strip(content);
    while s.last() == Some(&0x2C) {
        s = &s[..s.len() - 1];
    }
    if s.last() == Some(&0x29) {
        s = &s[..s.len() - 1];
    }
    if s.first() == Some(&0x28) {
        s = &s[1..];
    }
    let s = strip(s);
    if !matches!(s.first(), Some(&(0x22 | 0x27))) {
        return Ok(None);
    }
    Ok(match read(s)? {
        Some(Literal::Str(text)) => Some(text),
        _ => None,
    })
}

/// `anchors_from_value`: each line of a multi-line `str` value, stripped,
/// of at least [`MIN_ANCHOR_CHARS`] code points, first spelling first.
pub fn anchors_from_value(value: &[u32]) -> Result<Vec<PyStr>, LiteralError> {
    let Some(Literal::Str(text)) = read(value)? else {
        return Ok(Vec::new());
    };
    if !text.contains(&NL) {
        return Ok(Vec::new());
    }
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for line in text.split(|&c| c == NL) {
        let stripped = strip(line);
        if stripped.len() >= MIN_ANCHOR_CHARS && seen.insert(stripped) {
            out.push(stripped.to_vec());
        }
    }
    Ok(out)
}
