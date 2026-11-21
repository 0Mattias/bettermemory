//! The byte parsers of `src/bettermemory/gitobjects.py`: unit 1 of its
//! port (see the README's "Git objects read from a repository's files").
//!
//! Everything here is a pure function of bytes: the loose object header,
//! the pack entry header and its varints, a delta's sizes and its
//! application, the commit and tree parsers, the committer time and the
//! parent lines git reads, the fingerprint of design 2.1, git's C-style
//! unquoting and the alternates file, and a version 2 pack index held in
//! memory. No file is opened and nothing is inflated here. Unit 2, the
//! zlib streams and the delta chain over a file handle the caller gives,
//! is [`stream`]; the file reads and the object reader are later units.
//!
//! The reference's integers are unbounded. A size or distance a header
//! states can run past 64 bits, so those answers are [`Wide`], an exact
//! unsigned integer of any width. Where the reference raises (`int()`'s
//! digit limit, an index out of range, a short `struct` read, a malformed
//! hex prefix), the port answers [`Raised`].

pub mod files;
pub mod stream;

use sha2::{Digest, Sha256};

/// `_TIME_MAX`: git's `timestamp_t`, where `strtoumax` saturates.
pub const TIME_MAX: u64 = u64::MAX;
/// `_IDX_NAMES_AT`: the first object name of a version 2 pack index.
pub const IDX_NAMES_AT: usize = 8 + 256 * 4;
/// `MIN_ABBREV`.
pub const MIN_ABBREV: usize = 4;
/// `int()`'s default limit on the digits of a decimal string (CPython
/// 3.11 and later, `sys.int_info.default_max_str_digits`).
pub const INT_MAX_STR_DIGITS: usize = 4300;

const IDX_MAGIC: &[u8] = b"\xfftOc";
const IDX_VERSION: &[u8] = b"\x00\x00\x00\x02";
const LARGE_OFFSET: u32 = 0x8000_0000;
const OFS_DELTA: u8 = 6;
const REF_DELTA: u8 = 7;

/// What the reference raises where the port answers this.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `ValueError`: `int()` past its digit limit, or a prefix `int(.., 16)`
    /// or `bytes.fromhex` refuses.
    ValueError,
    /// `IndexError`: `raw[0]` of an empty name.
    IndexError,
    /// `struct.error`: an offset read past the index's end.
    StructError,
}

/// An unsigned integer of any width, little-endian 32-bit limbs with no
/// high zero limb (zero is no limb).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Wide(Vec<u32>);

impl Wide {
    pub fn from_u64(value: u64) -> Wide {
        let mut out = Wide(vec![value as u32, (value >> 32) as u32]);
        out.trim();
        out
    }

    fn trim(&mut self) {
        while self.0.last() == Some(&0) {
            self.0.pop();
        }
    }

    /// `self |= group << shift`, for a group of at most 8 bits.
    fn or_shifted(&mut self, group: u8, shift: usize) {
        if group == 0 {
            return;
        }
        let (limb, bit) = (shift / 32, shift % 32);
        let wide = u64::from(group) << bit;
        if self.0.len() < limb + 2 {
            self.0.resize(limb + 2, 0);
        }
        self.0[limb] |= wide as u32;
        self.0[limb + 1] |= (wide >> 32) as u32;
        self.trim();
    }

    /// `self = self * mul + add`.
    fn mul_add(&mut self, mul: u32, add: u32) {
        let mut carry = u64::from(add);
        for limb in &mut self.0 {
            let v = u64::from(*limb) * u64::from(mul) + carry;
            *limb = v as u32;
            carry = v >> 32;
        }
        if carry != 0 {
            self.0.push(carry as u32);
        }
    }

    /// `((self + 1) << 7) | group`, the OFS_DELTA distance step.
    fn ofs_step(&mut self, group: u8) {
        self.mul_add(1, 1);
        self.mul_add(128, u32::from(group & 0x7F));
    }

    /// The value where it fits in 64 bits.
    pub fn to_u64(&self) -> Option<u64> {
        match self.0.len() {
            0 => Some(0),
            1 => Some(u64::from(self.0[0])),
            2 => Some(u64::from(self.0[0]) | (u64::from(self.0[1]) << 32)),
            _ => None,
        }
    }

    /// Whether it equals `n`.
    pub fn is(&self, n: usize) -> bool {
        self.to_u64() == Some(n as u64)
    }

    /// `format(value, "x")`.
    pub fn hex(&self) -> String {
        let Some((top, rest)) = self.0.split_last() else {
            return "0".into();
        };
        let mut out = format!("{top:x}");
        for limb in rest.iter().rev() {
            out.push_str(&format!("{limb:08x}"));
        }
        out
    }
}

fn is_hex(c: u8) -> bool {
    c.is_ascii_hexdigit()
}

fn hex_of(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}

/// `bytes.find(needle, start)`, with Python's clamping of `start`.
fn find(hay: &[u8], needle: &[u8], start: usize) -> Option<usize> {
    if start > hay.len() {
        return None;
    }
    hay[start..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|p| p + start)
}

fn starts_at(hay: &[u8], prefix: &[u8], at: usize) -> bool {
    at <= hay.len() && hay[at..].starts_with(prefix)
}

/// `bytes.partition(b"\n\n")`: the head and the message.
fn partition_blank(body: &[u8]) -> (&[u8], &[u8]) {
    match find(body, b"\n\n", 0) {
        Some(at) => (&body[..at], &body[at + 2..]),
        None => (body, b""),
    }
}

/// A commit's header fields and message (`Commit`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Commit {
    pub tree: String,
    pub parents: Vec<String>,
    pub author: Vec<u8>,
    pub committer: Vec<u8>,
    pub message: Vec<u8>,
}

/// One entry of a tree object (`TreeEntry`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TreeEntry {
    pub mode: u32,
    pub name: Vec<u8>,
    pub sha: String,
}

/// `committed_at(body, hexsz)`: the committer time git reads, saturated
/// at 2**64 - 1 and wrapped modulo 2**64 under a `-`; 0 where git reads
/// none.
pub fn committed_at(body: &[u8], hexsz: usize) -> u64 {
    let Some((_, mut at)) = parent_lines(body, hexsz) else {
        return 0;
    };
    if !starts_at(body, b"author", at) {
        return 0;
    }
    at = match find(body, b"\n", at) {
        Some(nl) => nl + 1,
        None => return 0,
    };
    if !starts_at(body, b"committer", at) {
        return 0;
    }
    let Some(eol) = find(body, b"\n", at) else {
        return 0;
    };
    let Some(mark) = body[at..eol].iter().rposition(|&c| c == b'>') else {
        return 0;
    };
    at += mark + 1;
    while at < eol && matches!(body[at], b' ' | b'\t' | b'\r') {
        at += 1;
    }
    if at == eol || !(body[at].is_ascii_digit() || body[at] == b'-') {
        return 0;
    }
    let minus = body[at] == b'-';
    let from = at + usize::from(minus);
    let digits = body[from..eol]
        .iter()
        .take_while(|c| c.is_ascii_digit())
        .count();
    let run = &body[from..from + digits];
    let run = &run[run.iter().take_while(|&&c| c == b'0').count()..];
    if run.len() > 20 {
        return TIME_MAX;
    }
    let mut value: u128 = 0;
    for &c in run {
        value = value * 10 + u128::from(c - b'0');
    }
    if value > u128::from(TIME_MAX) {
        return TIME_MAX;
    }
    let value = value as u64;
    if minus {
        value.wrapping_neg()
    } else {
        value
    }
}

/// `parent_lines(body, hexsz)`: the parents of the fixed-length `parent`
/// lines after the `tree` line, and where the header goes on; None where
/// git refuses the object.
pub fn parent_lines(body: &[u8], hexsz: usize) -> Option<(Vec<String>, usize)> {
    if hexsz >= body.len() {
        return None;
    }
    let mut at = hexsz + 6;
    if !(body.starts_with(b"tree ") && body.len() > at && body[at - 1] == b'\n') {
        return None;
    }
    if !hex_at(body, 5, hexsz) {
        return None;
    }
    let step = hexsz + 8;
    let mut parents = Vec::new();
    while at + step - 1 < body.len() && starts_at(body, b"parent ", at) {
        if body.len() <= at + step || !hex_at(body, at + 7, hexsz) {
            return None;
        }
        if body[at + step - 1] != b'\n' {
            return None;
        }
        let name = &body[at + 7..at + step - 1];
        parents.push(String::from_utf8_lossy(name).to_ascii_lowercase());
        at += step;
    }
    Some((parents, at))
}

/// `_hex_at(body, at, hexsz)`: `hexsz` hex digits of either case at `at`.
pub fn hex_at(body: &[u8], at: usize, hexsz: usize) -> bool {
    at.checked_add(hexsz)
        .is_some_and(|end| end <= body.len() && body[at..end].iter().all(|&c| is_hex(c)))
}

/// `commit_fingerprint(body)`: sha256 over the `author ` and `committer `
/// lines of the head, a blank line and the message, as 32 hex digits;
/// None unless there are exactly two such lines.
pub fn commit_fingerprint(body: &[u8]) -> Option<String> {
    let (head, message) = partition_blank(body);
    let kept: Vec<&[u8]> = head
        .split(|&c| c == b'\n')
        .filter(|line| line.starts_with(b"author ") || line.starts_with(b"committer "))
        .collect();
    if kept.len() != 2 {
        return None;
    }
    let mut digest = Sha256::new();
    digest.update(kept[0]);
    digest.update(b"\n");
    digest.update(kept[1]);
    digest.update(b"\n\n");
    digest.update(message);
    let mut out = hex_of(&digest.finalize());
    out.truncate(32);
    Some(out)
}

/// `_alternate_entries(text)`: the entries of an alternates file as git's
/// `parse_alt_odb_entry` reads them.
pub fn alternate_entries(text: &[u8]) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < text.len() {
        let quoted = if text[i] == b'"' {
            unquote_c(text, i)
        } else {
            None
        };
        let (entry, end) = match quoted {
            Some(found) => found,
            None => {
                let end = find(text, b"\n", i).unwrap_or(text.len());
                let entry = if text[i] == b'#' {
                    Vec::new()
                } else {
                    text[i..end].to_vec()
                };
                (entry, end)
            }
        };
        if !entry.is_empty() {
            out.push(entry);
        }
        i = end + 1;
    }
    out
}

/// `_unquote_c(text, start)`: the C-quoted string opening at `start` and
/// the index past its closing quote; None when malformed or unclosed.
pub fn unquote_c(text: &[u8], start: usize) -> Option<(Vec<u8>, usize)> {
    let mut out = Vec::new();
    let mut i = start.saturating_add(1);
    while i < text.len() {
        let c = text[i];
        i += 1;
        if c == b'"' {
            return Some((out, i));
        }
        if c != b'\\' {
            out.push(c);
            continue;
        }
        let c = *text.get(i)?;
        i += 1;
        let escaped = match c {
            b'a' => 7,
            b'b' => 8,
            b'f' => 12,
            b'n' => 10,
            b'r' => 13,
            b't' => 9,
            b'v' => 11,
            b'\\' | b'"' => c,
            b'0'..=b'3'
                if text.len() >= i + 2
                    && text[i..i + 2].iter().all(|d| (b'0'..=b'7').contains(d)) =>
            {
                let value = (c - b'0') * 64 + (text[i] - b'0') * 8 + (text[i + 1] - b'0');
                i += 2;
                value
            }
            _ => return None,
        };
        out.push(escaped);
    }
    None
}

/// A loose object's header (`_loose_header`'s answer).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Loose {
    pub kind: &'static str,
    pub size: Wide,
    pub start: usize,
}

/// `_loose_header(head)`: the type, the canonical decimal size and where
/// the content starts. `Raised::ValueError` where the size has more digits
/// than `int()` reads.
pub fn loose_header(head: &[u8]) -> Result<Option<Loose>, Raised> {
    let Some(nul) = head.iter().position(|&c| c == 0) else {
        return Ok(None);
    };
    let line = &head[..nul];
    let (kind, digits) = match line.iter().position(|&c| c == b' ') {
        Some(sp) => (&line[..sp], &line[sp + 1..]),
        None => return Ok(None),
    };
    let kind = match kind {
        b"commit" => "commit",
        b"tree" => "tree",
        b"blob" => "blob",
        b"tag" => "tag",
        _ => return Ok(None),
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return Ok(None);
    }
    if digits.len() > 1 && digits[0] == b'0' {
        return Ok(None);
    }
    if digits.len() > INT_MAX_STR_DIGITS {
        return Err(Raised::ValueError);
    }
    let mut size = Wide::default();
    // Nine digits at a time, as CPython's `int` reads them: 10**9 fits a
    // limb, so the cost is a ninth of one multiply per digit.
    for chunk in digits.chunks(9) {
        let (mut mul, mut add) = (1u32, 0u32);
        for &d in chunk {
            mul *= 10;
            add = add * 10 + u32::from(d - b'0');
        }
        size.mul_add(mul, add);
    }
    Ok(Some(Loose {
        kind,
        size,
        start: nul + 1,
    }))
}

/// `_varint(data, i)`: a little-endian base-128 value and the index after.
pub fn varint(data: &[u8], mut i: usize) -> Option<(Wide, usize)> {
    let mut value = Wide::default();
    let mut shift = 0;
    while i < data.len() {
        let c = data[i];
        i += 1;
        value.or_shifted(c & 0x7F, shift);
        shift += 7;
        if c & 0x80 == 0 {
            return Some((value, i));
        }
    }
    None
}

/// A pack entry's base (`_entry_header`'s third member).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Base {
    /// A whole object.
    None,
    /// An OFS_DELTA's distance back.
    Distance(Wide),
    /// A REF_DELTA's base name.
    Name(Vec<u8>),
}

/// `_entry_header(buf, hash_len)`: (type code, size, base, header length).
pub fn entry_header(buf: &[u8], hash_len: usize) -> Option<(u8, Wide, Base, usize)> {
    let mut c = *buf.first()?;
    let code = (c >> 4) & 7;
    let mut size = Wide::from_u64(u64::from(c & 15));
    let (mut shift, mut i) = (4, 1);
    while c & 0x80 != 0 {
        c = *buf.get(i)?;
        i += 1;
        size.or_shifted(c & 0x7F, shift);
        shift += 7;
    }
    if (1..=4).contains(&code) {
        return Some((code, size, Base::None, i));
    }
    if code == OFS_DELTA {
        c = *buf.get(i)?;
        i += 1;
        let mut distance = Wide::from_u64(u64::from(c & 0x7F));
        while c & 0x80 != 0 {
            c = *buf.get(i)?;
            i += 1;
            distance.ofs_step(c);
        }
        return Some((code, size, Base::Distance(distance), i));
    }
    if code == REF_DELTA && i.checked_add(hash_len).is_some_and(|end| end <= buf.len()) {
        let name = buf[i..i + hash_len].to_vec();
        return Some((code, size, Base::Name(name), i + hash_len));
    }
    None
}

/// `_delta_sizes(delta)`: (source size, target size, first instruction).
pub fn delta_sizes(delta: &[u8]) -> Option<(Wide, Wide, usize)> {
    let (source, at) = varint(delta, 0)?;
    let (target, at) = varint(delta, at)?;
    Some((source, target, at))
}

/// `_apply_delta(base, delta)`: the instructions applied as git's
/// `patch_delta` checks them; None for any failure.
pub fn apply_delta(base: &[u8], delta: &[u8]) -> Option<Vec<u8>> {
    let (source, target, mut i) = delta_sizes(delta)?;
    if !source.is(base.len()) {
        return None;
    }
    let target = target.to_u64();
    let n = delta.len();
    let mut out = Vec::new();
    while i < n {
        let c = delta[i];
        i += 1;
        if c & 0x80 != 0 {
            let (mut offset, mut size) = (0u64, 0u64);
            for bit in 0..4 {
                if c & (1 << bit) != 0 {
                    offset |= u64::from(*delta.get(i)?) << (8 * bit);
                    i += 1;
                }
            }
            for bit in 0..3 {
                if c & (0x10 << bit) != 0 {
                    size |= u64::from(*delta.get(i)?) << (8 * bit);
                    i += 1;
                }
            }
            if size == 0 {
                size = 0x10000;
            }
            if offset + size > base.len() as u64 {
                return None;
            }
            out.extend_from_slice(&base[offset as usize..(offset + size) as usize]);
        } else if c != 0 {
            let c = usize::from(c);
            if i + c > n {
                return None;
            }
            out.extend_from_slice(&delta[i..i + c]);
            i += c;
        } else {
            return None;
        }
        if target.is_some_and(|t| out.len() as u64 > t) {
            return None;
        }
    }
    (target == Some(out.len() as u64)).then_some(out)
}

/// `_hex_name(value, hash_len)`: the name in lowercase where it is
/// `2 * hash_len` hex digits.
pub fn hex_name(value: &[u8], hash_len: usize) -> Option<String> {
    let whole = hash_len.checked_mul(2) == Some(value.len());
    if !whole || value.is_empty() || !value.iter().all(|&c| is_hex(c)) {
        return None;
    }
    Some(String::from_utf8_lossy(value).to_ascii_lowercase())
}

/// `_parse_commit(body, hash_len)`.
pub fn parse_commit(body: &[u8], hash_len: usize) -> Option<Commit> {
    let (head, message) = partition_blank(body);
    let lines: Vec<&[u8]> = head.split(|&c| c == b'\n').collect();
    if !lines[0].starts_with(b"tree ") {
        return None;
    }
    let tree = hex_name(&lines[0][5..], hash_len)?;
    let mut parents = Vec::new();
    let mut i = 1;
    while i < lines.len() && lines[i].starts_with(b"parent ") {
        parents.push(hex_name(&lines[i][7..], hash_len)?);
        i += 1;
    }
    let (mut author, mut committer): (Option<&[u8]>, Option<&[u8]>) = (None, None);
    for line in &lines[i..] {
        if author.is_none() && line.starts_with(b"author ") {
            author = Some(&line[7..]);
        } else if committer.is_none() && line.starts_with(b"committer ") {
            committer = Some(&line[10..]);
        }
    }
    Some(Commit {
        tree,
        parents,
        author: author.unwrap_or_default().to_vec(),
        committer: committer.unwrap_or_default().to_vec(),
        message: message.to_vec(),
    })
}

/// `_parse_tree(body, hash_len)`.
pub fn parse_tree(body: &[u8], hash_len: usize) -> Option<Vec<TreeEntry>> {
    let mut entries = Vec::new();
    let (mut i, n) = (0, body.len());
    while i < n {
        let space = find(body, b" ", i)?;
        let nul = find(body, b"\0", space + 1)?;
        let end = (nul + 1).checked_add(hash_len).filter(|&end| end <= n);
        let end = end.filter(|_| nul != space + 1)?;
        let mode = &body[i..space];
        if mode.is_empty() || mode.len() > 6 || !mode.iter().all(|c| (b'0'..=b'7').contains(c)) {
            return None;
        }
        let mode = mode.iter().fold(0u32, |v, &c| v * 8 + u32::from(c - b'0'));
        entries.push(TreeEntry {
            mode,
            name: body[space + 1..nul].to_vec(),
            sha: hex_of(&body[nul + 1..end]),
        });
        i = end;
    }
    Some(entries)
}

/// One version 2 pack index held in memory, beside its pack (`_PackIndex`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackIndex {
    data: Vec<u8>,
    fanout: Vec<u32>,
    pub pack: Vec<u8>,
    pub hash_len: usize,
    offsets_at: usize,
    large_at: usize,
    large_count: usize,
}

/// `_parse_index(data, pack, hash_len)`: an index of a size git accepts
/// whose fanout never decreases; None otherwise.
pub fn parse_index(data: Vec<u8>, pack: Vec<u8>, hash_len: usize) -> Option<PackIndex> {
    if data.len() < IDX_NAMES_AT || &data[..4] != IDX_MAGIC || &data[4..8] != IDX_VERSION {
        return None;
    }
    let fanout: Vec<u32> = data[8..IDX_NAMES_AT]
        .chunks_exact(4)
        .map(|c| u32::from_be_bytes([c[0], c[1], c[2], c[3]]))
        .collect();
    if fanout.windows(2).any(|w| w[0] > w[1]) {
        return None;
    }
    let count = u128::from(fanout[255]);
    let width = hash_len as u128;
    let offsets_at = IDX_NAMES_AT as u128 + count * (width + 4);
    let large_at = offsets_at + 4 * count;
    let min_size = large_at + 2 * width;
    let max_size = min_size + 8 * count.saturating_sub(1);
    let len = data.len() as u128;
    if !(min_size <= len && len <= max_size && (len - min_size).is_multiple_of(8)) {
        return None;
    }
    Some(PackIndex {
        large_count: ((len - min_size) / 8) as usize,
        offsets_at: offsets_at as usize,
        large_at: large_at as usize,
        data,
        fanout,
        pack,
        hash_len,
    })
}

impl PackIndex {
    fn bounds(&self, first: usize) -> (usize, usize) {
        let lo = if first > 0 { self.fanout[first - 1] } else { 0 };
        (lo as usize, self.fanout[first] as usize)
    }

    fn name(&self, position: usize) -> &[u8] {
        let start = IDX_NAMES_AT + position * self.hash_len;
        &self.data[start..start + self.hash_len]
    }

    /// `find(raw)`: the position of `raw` in the sorted names.
    pub fn find(&self, raw: &[u8]) -> Result<Option<usize>, Raised> {
        let first = *raw.first().ok_or(Raised::IndexError)?;
        let (mut lo, mut hi) = self.bounds(usize::from(first));
        while lo < hi {
            let mid = (lo + hi) >> 1;
            let name = self.name(mid);
            if name == raw {
                return Ok(Some(mid));
            }
            if name < raw {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        Ok(None)
    }

    /// `starting(prefix, limit=limit)`: up to `limit` names whose hex
    /// starts with `prefix`, in order.
    pub fn starting(&self, prefix: &str, limit: i64) -> Result<Vec<Vec<u8>>, Raised> {
        let p = prefix.as_bytes();
        // `bytes.fromhex` refuses any text outside ASCII, and `int` of the
        // first two characters raises ValueError or yields a value inside
        // the fanout, so a prefix outside ASCII always ends in ValueError.
        if !prefix.is_ascii() {
            return Err(Raised::ValueError);
        }
        let space = |c: &u8| matches!(c, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r');
        // `int(prefix[:2], 16)`.
        let head = &p[..p.len().min(2)];
        let digits: Vec<u8> = head.iter().copied().filter(|c| !space(c)).collect();
        let stripped = head.iter().position(|c| !space(c)).map_or(&head[..0], |s| {
            &head[s..=head.iter().rposition(|c| !space(c)).unwrap_or(s)]
        });
        if digits.is_empty() || stripped.len() != digits.len() {
            return Err(Raised::ValueError);
        }
        let unsigned = stripped.strip_prefix(b"+").or(stripped.strip_prefix(b"-"));
        if unsigned.unwrap_or(stripped).is_empty()
            || !unsigned.unwrap_or(stripped).iter().all(|&c| is_hex(c))
        {
            return Err(Raised::ValueError);
        }
        // `bytes.fromhex(prefix, or prefix + "0")`.
        let mut padded = p.to_vec();
        if padded.len() % 2 == 1 {
            padded.push(b'0');
        }
        let (mut i, mut low) = (0, Vec::new());
        while i < padded.len() {
            if space(&padded[i]) {
                i += 1;
                continue;
            }
            let pair = padded.get(i..i + 2).ok_or(Raised::ValueError)?;
            if !pair.iter().all(|&c| is_hex(c)) {
                return Err(Raised::ValueError);
            }
            let text = std::str::from_utf8(pair).map_err(|_| Raised::ValueError)?;
            low.push(u8::from_str_radix(text, 16).map_err(|_| Raised::ValueError)?);
            i += 2;
        }
        if unsigned.is_some() || !p.iter().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f')) {
            // A sign fails `fromhex` above; any other text no hex starts with.
            return Ok(Vec::new());
        }
        let first = usize::from_str_radix(&prefix[..p.len().min(2)], 16).unwrap_or(0);
        let (mut lo, mut hi) = self.bounds(first);
        while lo < hi {
            let mid = (lo + hi) >> 1;
            if self.name(mid) < low.as_slice() {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let mut out = Vec::new();
        let end = self.fanout[first] as usize;
        let mut position = lo;
        while position < end && (out.len() as i64) < limit {
            let name = self.name(position);
            if !hex_of(name).starts_with(prefix) {
                break;
            }
            out.push(name.to_vec());
            position += 1;
        }
        Ok(out)
    }

    /// `offset(position)`: the pack offset of the object at `position`;
    /// None for a 64-bit index past the table.
    pub fn offset(&self, position: usize) -> Result<Option<u64>, Raised> {
        let at = position
            .checked_mul(4)
            .and_then(|p| p.checked_add(self.offsets_at))
            .filter(|&at| at.checked_add(4).is_some_and(|end| end <= self.data.len()))
            .ok_or(Raised::StructError)?;
        let d = &self.data[at..at + 4];
        let value = u32::from_be_bytes([d[0], d[1], d[2], d[3]]);
        if value & LARGE_OFFSET == 0 {
            return Ok(Some(u64::from(value)));
        }
        let index = (value & !LARGE_OFFSET) as usize;
        if index >= self.large_count {
            return Ok(None);
        }
        let at = self.large_at + 8 * index;
        let mut q = [0u8; 8];
        q.copy_from_slice(&self.data[at..at + 8]);
        Ok(Some(u64::from_be_bytes(q)))
    }
}
