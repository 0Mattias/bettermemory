//! The leaves of `claims.py`: the claim record and its wire string, the
//! path spellings, and the column-zero patterns the drift index reads a
//! patch by. Nothing here reads the file system, the clock or git.
//!
//! Text is a slice of code points, as in [`crate::unicode`], since a Python
//! `str` may hold a lone surrogate. Each `re` pattern of the reference is a
//! hand-written matcher here; `\w` and `\d` are [`unicode::is_word`] and
//! [`unicode::is_re_digit`], `str.strip()` is [`unicode::is_space`].

use crate::unicode::{self, Form};

/// `MAX_CLAIMS`.
pub const MAX_CLAIMS: usize = 64;
/// `MAX_CLAIM_LEN`.
pub const MAX_CLAIM_LEN: usize = 1024;
/// `COMMIT_MARK`: the record separator of a `git log` patch stream.
pub const COMMIT_MARK: u32 = 0x01;
/// `MAX_SPELLINGS`.
pub const MAX_SPELLINGS: usize = 64;
/// `INDETERMINATE_REASON_MARKER`.
pub const INDETERMINATE_REASON_MARKER: &str = "could not be read";
/// `sys.int_info.default_max_str_digits`: `int()` of a longer digit run
/// raises ValueError.
pub const MAX_INT_DIGITS: usize = 4300;

/// A claim's kind, as `parse_claim` names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Path,
    Symbol,
    Literal,
    Absent,
}

impl Kind {
    /// The reference's `kind` string.
    pub fn name(self) -> &'static str {
        match self {
            Kind::Path => "path",
            Kind::Symbol => "symbol",
            Kind::Literal => "literal",
            Kind::Absent => "absent",
        }
    }
}

/// `Claim`: one declared assertion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Claim {
    pub kind: Kind,
    pub rel_path: Vec<u32>,
    pub name: Vec<u32>,
    pub value: Vec<u32>,
}

impl Claim {
    /// `Claim.render`: the canonical wire string.
    pub fn render(&self) -> Vec<u32> {
        let mut out = Vec::new();
        match self.kind {
            Kind::Absent => {
                out.push(u32::from(b'!'));
                out.extend_from_slice(&self.rel_path);
            }
            Kind::Path => out.extend_from_slice(&self.rel_path),
            Kind::Symbol | Kind::Literal => {
                out.extend_from_slice(&self.rel_path);
                out.extend(cps("::"));
                out.extend_from_slice(&self.name);
                if self.kind == Kind::Literal {
                    out.push(u32::from(b'='));
                    out.extend_from_slice(&self.value);
                }
            }
        }
        out
    }
}

fn cps(s: &str) -> impl Iterator<Item = u32> + '_ {
    s.chars().map(u32::from)
}

fn starts(text: &[u32], prefix: &str) -> bool {
    let p: Vec<u32> = cps(prefix).collect();
    text.starts_with(&p)
}

fn ascii_ident_start(cp: u32) -> bool {
    cp == u32::from(b'_') || (cp < 0x80 && (cp as u8).is_ascii_alphabetic())
}

/// `control_character`: the first C0 control or DEL `path` holds.
pub fn control_character(path: &[u32]) -> Option<u32> {
    path.iter().copied().find(|&c| c < 0x20 || c == 0x7F)
}

/// `lone_surrogate`: the first surrogate code point `path` holds.
pub fn lone_surrogate(path: &[u32]) -> Option<u32> {
    path.iter()
        .copied()
        .find(|&c| (0xD800..=0xDFFF).contains(&c))
}

/// `_IDENT_RE.match(text)` (`^[A-Za-z_]\w*$`): `$` also matches before
/// one final line feed, so `"name\n"` matches.
pub fn is_identifier(text: &[u32]) -> bool {
    let body = match text.split_last() {
        Some((&0x0A, rest)) if word_tail(rest) => return true,
        _ => text,
    };
    word_tail(body)
}

fn word_tail(text: &[u32]) -> bool {
    match text.split_first() {
        Some((&first, rest)) => {
            ascii_ident_start(first) && rest.iter().all(|&c| unicode::is_word(c))
        }
        None => false,
    }
}

/// `claim_reason_is_indeterminate`.
pub fn claim_reason_is_indeterminate(reason: &[u32]) -> bool {
    let marker: Vec<u32> = cps(INDETERMINATE_REASON_MARKER).collect();
    contains_run(reason, &marker)
}

/// Whether `needle` occurs in `hay`, in time linear in both (Knuth, Morris
/// and Pratt), as `in` on two `str` is.
fn contains_run(hay: &[u32], needle: &[u32]) -> bool {
    if needle.is_empty() {
        return true;
    }
    let mut fail = vec![0usize; needle.len()];
    let mut k = 0;
    for i in 1..needle.len() {
        while k > 0 && needle[i] != needle[k] {
            k = fail[k - 1];
        }
        if needle[i] == needle[k] {
            k += 1;
        }
        fail[i] = k;
    }
    let mut k = 0;
    for &c in hay {
        while k > 0 && c != needle[k] {
            k = fail[k - 1];
        }
        if c == needle[k] {
            k += 1;
            if k == needle.len() {
                return true;
            }
        }
    }
    false
}

/// `path_key`: `path` composed (NFC).
pub fn path_key(path: &[u32]) -> Vec<u32> {
    unicode::normalize(Form::Nfc, path)
}

fn is_ascii(path: &[u32]) -> bool {
    path.iter().all(|&c| c < 0x80)
}

fn distinct(items: Vec<Vec<u32>>) -> Vec<Vec<u32>> {
    let mut seen: std::collections::HashSet<Vec<u32>> =
        std::collections::HashSet::with_capacity(items.len());
    let mut out: Vec<Vec<u32>> = Vec::with_capacity(items.len());
    for item in items {
        if seen.insert(item.clone()) {
            out.push(item);
        }
    }
    out
}

/// `_component_forms`: each `/` component as written, composed and
/// decomposed, the repeats dropped in that order.
fn component_forms(path: &[u32]) -> Vec<Vec<Vec<u32>>> {
    path.split(|&c| c == u32::from(b'/'))
        .map(|part| {
            distinct(vec![
                part.to_vec(),
                unicode::normalize(Form::Nfc, part),
                unicode::normalize(Form::Nfd, part),
            ])
        })
        .collect()
}

fn capped(forms: &[Vec<Vec<u32>>]) -> bool {
    let mut product: usize = 1;
    for form in forms {
        product = product.saturating_mul(form.len());
    }
    product > MAX_SPELLINGS
}

/// `spellings_capped`.
pub fn spellings_capped(path: &[u32]) -> bool {
    !is_ascii(path) && capped(&component_forms(path))
}

/// `stored_spellings`: `path`, then every other spelling by normalisation
/// of its components (in `itertools.product`'s order), or past
/// `MAX_SPELLINGS` the whole path in each form.
pub fn stored_spellings(path: &[u32]) -> Vec<Vec<u32>> {
    if is_ascii(path) {
        return vec![path.to_vec()];
    }
    let forms = component_forms(path);
    if capped(&forms) {
        return distinct(vec![
            path.to_vec(),
            path_key(path),
            unicode::normalize(Form::Nfd, path),
        ]);
    }
    let mut joined: Vec<Vec<u32>> = vec![Vec::new()];
    for (i, form) in forms.iter().enumerate() {
        let mut next = Vec::with_capacity(joined.len() * form.len());
        for head in &joined {
            for part in form {
                let mut t = head.clone();
                if i > 0 {
                    t.push(u32::from(b'/'));
                }
                t.extend_from_slice(part);
                next.push(t);
            }
        }
        joined = next;
    }
    distinct(joined)
}

/// `claim_paths`: the distinct paths, in declaration order.
pub fn claim_paths(claims: &[Claim]) -> Vec<Vec<u32>> {
    distinct(claims.iter().map(|c| c.rel_path.clone()).collect())
}

/// What `_binding_token` names a line's binding as.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Binding {
    Def,
    Assign,
}

fn ident_end(t: &[u32], i: usize) -> Option<usize> {
    if i < t.len() && ascii_ident_start(t[i]) {
        let mut j = i + 1;
        while j < t.len() && unicode::is_word(t[j]) {
            j += 1;
        }
        Some(j)
    } else {
        None
    }
}

fn blanks(t: &[u32], mut i: usize) -> usize {
    while i < t.len() && (t[i] == 0x20 || t[i] == 0x09) {
        i += 1;
    }
    i
}

/// `_DEF_RE.match(content)`: the name it captures.
fn def_name(t: &[u32]) -> Option<&[u32]> {
    let mut i = 0;
    if starts(t, "async") {
        let j = blanks(t, 5);
        if j > 5 {
            i = j;
        }
    }
    let rest = &t[i..];
    let k = if starts(rest, "def") {
        i + 3
    } else if starts(rest, "class") {
        i + 5
    } else {
        return None;
    };
    let s = blanks(t, k);
    if s == k {
        return None;
    }
    let e = ident_end(t, s)?;
    let after = blanks(t, e);
    match t.get(after) {
        Some(&c) if c == u32::from(b'(') || c == u32::from(b'[') || c == u32::from(b':') => {
            Some(&t[s..e])
        }
        _ => None,
    }
}

const EQ: u32 = b'=' as u32;

/// Whether `t[i..]` is an augmented operator (or none), then `=`, then not
/// `=`: `(?:\+|-|...|\|)?=(?!=)`.
fn op_assign_at(t: &[u32], i: usize) -> bool {
    let tail_ok = |k: usize| t.get(k) == Some(&EQ) && t.get(k + 1) != Some(&EQ);
    if tail_ok(i) {
        return true;
    }
    const OPS: [&str; 12] = [
        "+", "-", "*", "/", "//", "%", "**", ">>", "<<", "&", "^", "|",
    ];
    OPS.iter()
        .any(|op| starts(&t[i.min(t.len())..], op) && tail_ok(i + op.len()))
}

/// `_ASSIGN_RE.match(content)`: the name it captures.
fn assign_name(t: &[u32]) -> Option<&[u32]> {
    let e = ident_end(t, 0)?;
    let p = blanks(t, e);
    if t.get(p) == Some(&u32::from(b':')) {
        // The annotation `[^=\n]+` ends before the first `=` or line feed;
        // backtracking can end it at any earlier point, so a match exists
        // when that first stop is a `=` at least two past the colon, not
        // followed by another `=`.
        let r = (p + 1..t.len())
            .find(|&k| t[k] == EQ || t[k] == 0x0A)
            .unwrap_or(t.len());
        if r >= p + 2 && t.get(r) == Some(&EQ) && t.get(r + 1) != Some(&EQ) {
            return Some(&t[..e]);
        }
    }
    op_assign_at(t, p).then_some(&t[..e])
}

/// `_binding_token`: the column-zero binding a changed line names.
pub fn binding_token(content: &[u32]) -> Option<(Binding, Vec<u32>)> {
    if let Some(name) = def_name(content) {
        return Some((Binding::Def, name.to_vec()));
    }
    assign_name(content).map(|name| (Binding::Assign, name.to_vec()))
}

/// `int()` refused a digit run longer than [`MAX_INT_DIGITS`]: ValueError.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IntTooLong;

fn digit_run(t: &[u32], i: usize) -> usize {
    let mut j = i;
    while j < t.len() && unicode::is_re_digit(t[j]) {
        j += 1;
    }
    j
}

/// `int()` of a run of `\d` code points, as decimal ASCII digits with
/// the leading zeros dropped.
fn int_digits(run: &[u32]) -> Result<Vec<u8>, IntTooLong> {
    if run.len() > MAX_INT_DIGITS {
        return Err(IntTooLong);
    }
    let mut out: Vec<u8> = run.iter().map(|&c| decimal_value(c)).collect();
    let zeros = out.iter().take_while(|&&d| d == 0).count();
    out.drain(..zeros.min(out.len().saturating_sub(1)));
    Ok(out)
}

/// The value of a decimal digit: Unicode lays each decimal digit set out
/// as ten contiguous code points from zero.
fn decimal_value(cp: u32) -> u8 {
    let mut k = 0u32;
    let mut c = cp;
    while c > 0 && unicode::is_re_digit(c - 1) {
        k += 1;
        c -= 1;
    }
    (k % 10) as u8
}

fn add_decimal(a: &[u8], b: &[u8]) -> String {
    let mut out = Vec::with_capacity(a.len().max(b.len()) + 1);
    let (mut i, mut j, mut carry) = (a.len(), b.len(), 0u8);
    while i > 0 || j > 0 || carry > 0 {
        let mut s = carry;
        if i > 0 {
            i -= 1;
            s += a[i];
        }
        if j > 0 {
            j -= 1;
            s += b[j];
        }
        out.push(b'0' + s % 10);
        carry = s / 10;
    }
    out.reverse();
    String::from_utf8(out).unwrap_or_default()
}

/// The four groups of `_HUNK_RE.match(line)`
/// (`^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`), as spans.
type Span = (usize, usize);

fn hunk_groups(t: &[u32]) -> Option<[Option<Span>; 4]> {
    if !starts(t, "@@ -") {
        return None;
    }
    let mut g = [None; 4];
    let mut i = 4;
    for (n, sep) in [(0usize, " +"), (2, " @@")] {
        let e = digit_run(t, i);
        if e == i {
            return None;
        }
        g[n] = Some((i, e));
        i = e;
        if t.get(i) == Some(&u32::from(b',')) {
            let e2 = digit_run(t, i + 1);
            if e2 > i + 1 {
                g[n + 1] = Some((i + 1, e2));
                i = e2;
            }
        }
        if !starts(&t[i..], sep) {
            return None;
        }
        i += sep.chars().count();
    }
    Some(g)
}

/// `hunk_lines`: removed plus added lines of a `-U0` hunk header, as the
/// decimal text of the Python int; `Ok(None)` for a line that is no hunk
/// header.
pub fn hunk_lines(line: &[u32]) -> Result<Option<String>, IntTooLong> {
    let Some(g) = hunk_groups(line) else {
        return Ok(None);
    };
    let count = |span: Option<Span>| match span {
        Some((s, e)) => int_digits(&line[s..e]),
        None => Ok(vec![1]),
    };
    let removed = count(g[1])?;
    let added = count(g[3])?;
    Ok(Some(add_decimal(&removed, &added)))
}

/// `_MARK_HASH_RE.fullmatch(mark)`: 40 or 64 lowercase hex digits.
pub fn is_mark_hash(mark: &[u32]) -> bool {
    (mark.len() == 40 || mark.len() == 64)
        && mark
            .iter()
            .all(|&c| (0x30..=0x39).contains(&c) || (0x61..=0x66).contains(&c))
}

/// `_header_path(rest)`: P where `rest` is `a/P b/P`, else empty.
pub fn header_path(rest: &[u32]) -> Vec<u32> {
    let len = rest.len();
    if len < 7 || (len - 5) % 2 == 1 {
        return Vec::new();
    }
    let n = (len - 5) / 2;
    if !starts(rest, "a/") || !starts(&rest[2 + n..], " b/") {
        return Vec::new();
    }
    if rest[2..2 + n] == rest[5 + n..] {
        rest[2..2 + n].to_vec()
    } else {
        Vec::new()
    }
}

/// `str.strip()` with no argument.
pub fn strip(t: &[u32]) -> &[u32] {
    let s = t
        .iter()
        .position(|&c| !unicode::is_space(c))
        .unwrap_or(t.len());
    let e = t
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .map_or(s, |e| e + 1);
    &t[s..e.max(s)]
}

/// A set of paths, as the index's `set[str]` holds them: a hash set, so
/// adding one is constant time (order is not part of the answer).
pub type PathSet = std::collections::HashSet<Vec<u32>>;

fn add(set: &mut PathSet, path: &[u32]) {
    if !set.contains(path) {
        set.insert(path.to_vec());
    }
}

/// `_patch_header_path`: a `+++ ` or `--- ` line read into the path sets,
/// the path the section reads after it; None for another line.
pub fn patch_header_path(
    line: &[u32],
    path: &[u32],
    files: &mut PathSet,
    deleted: &mut PathSet,
) -> Option<Vec<u32>> {
    if starts(line, "+++ ") {
        let target = strip(&line[4..]);
        if target.iter().copied().eq(cps("/dev/null")) {
            if !path.is_empty() {
                add(deleted, path);
            }
            return Some(path.to_vec());
        }
        let p = if starts(target, "b/") {
            &target[2..]
        } else {
            target
        };
        add(files, p);
        return Some(p.to_vec());
    }
    if starts(line, "--- ") {
        if starts(line, "--- a/") {
            let p = strip(&line[6..]);
            add(files, p);
            return Some(p.to_vec());
        }
        return Some(path.to_vec());
    }
    None
}
