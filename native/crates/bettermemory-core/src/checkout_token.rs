//! The checkout token's text, as `checkout_token.py` writes and reads it:
//! the leaves of the module (its first unit). A token is 26 Crockford
//! base-32 characters; its file is the header line and the token line.
//!
//! Randomness is an argument: each function that draws from `secrets`
//! takes the bytes `os.urandom` would have returned to
//! `secrets.randbits` ([`random_bytes_needed`] says how many). Text is a
//! [`PyStr`], since a Python `str` may hold a lone surrogate. No pattern
//! goes through a regex crate: `[CROCKFORD]{26}` and `-?[0-9]+` under
//! `fullmatch` are written out by hand (an exact count of ASCII members,
//! and an optional minus then one or more ASCII digits).
//!
//! The reads, `write`, `remint` and the claim are later units over the
//! file-system trait.

pub mod blake2b;

use crate::config::{self, pure_path};
use crate::fsdata::Flavour;
use crate::gitfile_rule::hostpath;
use crate::pyvalue::{PyStr, PyValue};

/// The token's file name inside the git directory's `info/` folder.
pub const TOKEN_NAME: &str = "bettermemory";
/// The first line of every token file.
pub const TOKEN_HEADER: &str = "bettermemory checkout token v1";
/// Characters in a token.
pub const TOKEN_LENGTH: usize = 26;
/// Crockford's base-32 alphabet, upper case.
pub const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
/// Characters of a re-mint's token drawn at random.
pub const NONCE_LENGTH: usize = 13;
/// BLAKE2b's personalization for the link.
pub const LINK_PERSON: &[u8] = b"bm-token-remint";
/// What `_malformed_detail` says of an empty file.
pub const EMPTY: &str = "empty";
/// What `_malformed_detail` says of a file a writer has not finished.
pub const CUT_SHORT: &str = "cut short";

/// What the reference raises here, by its Python class.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `secrets.randbits` of a negative count: ValueError.
    NegativeBits,
    /// `str.encode` of a character the codec cannot write:
    /// UnicodeEncodeError.
    Encode,
    /// Not the reference's: the caller handed fewer or more random bytes
    /// than `os.urandom` would have been asked for.
    RandomBytes { needed: usize, given: usize },
}

impl Raised {
    /// The Python class, or `"RandomBytes"` for the caller's error.
    pub fn class(&self) -> &'static str {
        match self {
            Raised::NegativeBits => "ValueError",
            Raised::Encode => "UnicodeEncodeError",
            Raised::RandomBytes { .. } => "RandomBytes",
        }
    }
}

/// How many bytes `secrets.randbits(5 * length)` asks `os.urandom` for:
/// `(k + 7) // 8`, saturated at `usize::MAX`. None for a negative length,
/// where it raises.
pub fn random_bytes_needed(length: i64) -> Option<usize> {
    if length < 0 {
        return None;
    }
    let k = (length as u128) * 5;
    Some(usize::try_from(k.div_ceil(8)).unwrap_or(usize::MAX))
}

/// `random_crockford(length)`: `secrets.randbits(5 * length)` is
/// `int.from_bytes(urandom, "big") >> (8 * len - 5 * length)`, written
/// most significant character first, five bits each.
pub fn random_crockford(length: i64, urandom: &[u8]) -> Result<String, Raised> {
    let needed = random_bytes_needed(length).ok_or(Raised::NegativeBits)?;
    if urandom.len() != needed {
        return Err(Raised::RandomBytes {
            needed,
            given: urandom.len(),
        });
    }
    // `needed` bytes were given and hold at least `5 * length` bits, so
    // `length` fits.
    let length = length as usize;
    let bit = |i: usize| (urandom[i / 8] >> (7 - i % 8)) & 1;
    let mut out = String::with_capacity(length);
    for c in 0..length {
        let mut v = 0usize;
        for j in 0..5 {
            v = (v << 1) | usize::from(bit(5 * c + j));
        }
        out.push(char::from(CROCKFORD[v]));
    }
    Ok(out)
}

/// `mint()`: 26 random characters from 17 random bytes.
pub fn mint(urandom: &[u8]) -> Result<String, Raised> {
    random_crockford(TOKEN_LENGTH as i64, urandom)
}

fn crockford_member(c: u32) -> bool {
    c < 128 && CROCKFORD.contains(&(c as u8))
}

/// `_TOKEN_RE.fullmatch(text) is not None`.
pub fn token_text(text: &[u32]) -> bool {
    text.len() == TOKEN_LENGTH && text.iter().all(|&c| crockford_member(c))
}

/// `valid(token)`: a `str` that is exactly 26 Crockford characters.
pub fn valid(token: &PyValue) -> bool {
    matches!(token, PyValue::Str(s) if token_text(s))
}

/// `_DECIMAL.fullmatch(text)`: an optional `-`, then ASCII digits.
fn decimal(text: &[u32]) -> bool {
    let digits = text.strip_prefix(&[u32::from(b'-')]).unwrap_or(text);
    !digits.is_empty() && digits.iter().all(|&c| (0x30..=0x39).contains(&c))
}

/// What a re-mint replaces: the token and the git directory's device and
/// inode number as text. Built directly it may hold any text, as the
/// reference's `NamedTuple` may; [`lineage`] admits only well-formed ones.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Lineage {
    pub token: PyStr,
    pub device: PyStr,
    pub inode: PyStr,
}

impl Lineage {
    /// `Lineage.read(value)`: a list or tuple of three, read by
    /// [`lineage`] as the token and a key of the other two.
    pub fn read(value: &PyValue) -> Option<Lineage> {
        match value {
            PyValue::List(items) | PyValue::Tuple(items) if items.len() == 3 => {
                lineage_of(&items[0], &items[1..])
            }
            _ => None,
        }
    }
}

/// `lineage(token, key)`: a valid token and a list or tuple of at least
/// two whose first two are decimal text.
pub fn lineage(token: &PyValue, key: &PyValue) -> Option<Lineage> {
    if !valid(token) {
        return None;
    }
    match key {
        PyValue::List(items) | PyValue::Tuple(items) => lineage_of(token, items),
        _ => None,
    }
}

fn lineage_of(token: &PyValue, key: &[PyValue]) -> Option<Lineage> {
    let PyValue::Str(t) = token else { return None };
    if !token_text(t) || key.len() < 2 {
        return None;
    }
    let text = |v: &PyValue| match v {
        PyValue::Str(s) if decimal(s) => Some(s.clone()),
        _ => None,
    };
    Some(Lineage {
        token: t.clone(),
        device: text(&key[0])?,
        inode: text(&key[1])?,
    })
}

/// `str.encode()` (UTF-8, strict): a lone surrogate raises.
fn utf8(text: &[u32]) -> Result<Vec<u8>, Raised> {
    // An ASCII run is copied a byte per code point; anything else is
    // encoded one character at a time, a surrogate raising.
    let mut out = Vec::with_capacity(text.len());
    let mut rest = text;
    while !rest.is_empty() {
        let ascii = rest.iter().position(|&c| c >= 0x80).unwrap_or(rest.len());
        out.extend(rest[..ascii].iter().map(|&c| c as u8));
        rest = &rest[ascii..];
        if let Some((&c, tail)) = rest.split_first() {
            let ch = char::from_u32(c).ok_or(Raised::Encode)?;
            let mut buf = [0u8; 4];
            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
            rest = tail;
        }
    }
    Ok(out)
}

/// `_link(replaced, nonce)`: BLAKE2b (16 bytes, personalized) of the four
/// fields, each as its UTF-8 length in decimal, `:`, its bytes and `,`;
/// the digest's low 65 bits as 13 Crockford characters.
pub fn link(replaced: &Lineage, nonce: &[u32]) -> Result<String, Raised> {
    let mut text = Vec::new();
    for field in [&replaced.token, &replaced.device, &replaced.inode] {
        let bytes = utf8(field)?;
        text.extend_from_slice(format!("{}:", bytes.len()).as_bytes());
        text.extend_from_slice(&bytes);
        text.push(b',');
    }
    let bytes = utf8(nonce)?;
    text.extend_from_slice(format!("{}:", bytes.len()).as_bytes());
    text.extend_from_slice(&bytes);
    text.push(b',');
    let digest = blake2b::blake2b(&text, 16, LINK_PERSON).unwrap_or_default();
    let mut value = 0u128;
    for b in digest {
        value = (value << 8) | u128::from(b);
    }
    let width = TOKEN_LENGTH - NONCE_LENGTH;
    Ok((0..width)
        .rev()
        .map(|i| char::from(CROCKFORD[((value >> (5 * i)) & 31) as usize]))
        .collect())
}

/// `successor(replaced)`: 13 random characters (nine random bytes), then
/// the link of `replaced` and them.
pub fn successor(replaced: &Lineage, urandom: &[u8]) -> Result<String, Raised> {
    let nonce = random_crockford(NONCE_LENGTH as i64, urandom)?;
    let cps: PyStr = nonce.chars().map(u32::from).collect();
    Ok(nonce.clone() + &link(replaced, &cps)?)
}

/// `succeeds(token, replaced)`: a valid token other than the one
/// replaced whose last 13 characters are the link of its first 13.
pub fn succeeds(token: &PyValue, replaced: Option<&Lineage>) -> Result<bool, Raised> {
    let Some(replaced) = replaced else {
        return Ok(false);
    };
    let PyValue::Str(t) = token else {
        return Ok(false);
    };
    if !token_text(t) || *t == replaced.token {
        return Ok(false);
    }
    let tail: String = t[NONCE_LENGTH..]
        .iter()
        .map(|&c| char::from(c as u8))
        .collect();
    Ok(tail == link(replaced, &t[..NONCE_LENGTH])?)
}

/// `str(token_path(git_dir))`: `Path(git_dir) / "info" / TOKEN_NAME`
/// under `flavour`, `os.path.join` then `PurePath`'s spelling.
pub fn token_path(git_dir: &str, flavour: Flavour) -> String {
    let joined = hostpath::join(flavour, git_dir.as_bytes(), b"info");
    let joined = hostpath::join(flavour, &joined, TOKEN_NAME.as_bytes());
    let text = String::from_utf8_lossy(&joined);
    let flavour = match flavour {
        Flavour::Posix => config::Flavour::Posix,
        Flavour::Windows => config::Flavour::Windows,
    };
    pure_path(&text, flavour)
}

/// `render(token)`: the header line and the token line, ASCII encoded.
pub fn render(token: &[u32]) -> Result<Vec<u8>, Raised> {
    let mut out = Vec::with_capacity(TOKEN_HEADER.len() + token.len() + 2);
    out.extend_from_slice(TOKEN_HEADER.as_bytes());
    out.push(b'\n');
    if token.iter().any(|&c| c >= 128) {
        return Err(Raised::Encode);
    }
    out.extend(token.iter().map(|&c| c as u8));
    out.push(b'\n');
    Ok(out)
}

fn header_cps() -> PyStr {
    TOKEN_HEADER.chars().map(u32::from).collect()
}

/// `parse(text)`: split at `\n`, one empty last piece dropped, exactly two
/// lines, each with one trailing `\r` removed, the header and a token.
pub fn parse(text: &[u32]) -> Option<PyStr> {
    let mut lines: Vec<&[u32]> = text.split(|&c| c == 0x0a).collect();
    if lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    if lines.len() != 2 {
        return None;
    }
    let strip = |l: &[u32]| -> PyStr { l.strip_suffix(&[0x0d]).unwrap_or(l).to_vec() };
    let (header, value) = (strip(lines[0]), strip(lines[1]));
    if header != header_cps() || !token_text(&value) {
        return None;
    }
    Some(value)
}

/// `_cut_short(text)`: empty, a prefix of the header line and its line
/// end, or that line and fewer than 26 Crockford characters.
pub fn cut_short(text: &[u32]) -> bool {
    let mut head = header_cps();
    head.push(0x0a);
    if text.len() <= head.len() {
        return head.starts_with(text);
    }
    let rest = &text[head.len()..];
    text.starts_with(&head)
        && rest.len() < TOKEN_LENGTH
        && rest.iter().all(|&c| crockford_member(c))
}

/// `_malformed_detail(text)`.
pub fn malformed_detail(text: &[u32]) -> String {
    if text.is_empty() {
        return EMPTY.to_owned();
    }
    if cut_short(text) {
        return CUT_SHORT.to_owned();
    }
    format!("not a {TOKEN_HEADER} file")
}
