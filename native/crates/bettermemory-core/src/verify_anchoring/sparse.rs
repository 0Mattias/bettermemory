//! The leaves of `verify_anchoring.py`'s sparse-checkout reading
//! (reference lines 598-606 and 654-788): the `core.sparseCheckout`
//! switch read from a config text (`_config_switch`), the index's
//! constants, git's varint (`_index_varint`), the index reader
//! (`_parse_skip_index`) and its answer (`_SkipIndex`, `_holds`,
//! `_any_below`). The memo of `_skip_index` and `_absent_by_sparsity`,
//! which read the disk through discovery, are unit verify_anchoring-1b.

use crate::gitconfig::{git_bool, git_entries};
use crate::unicode;

/// `_CE_EXTENDED`: the first flags word's bit that says a second follows.
pub const CE_EXTENDED: u16 = 0x4000;
/// `_CE_SKIP_WORKTREE`: the second word's skip-worktree bit.
pub const CE_SKIP_WORKTREE: u16 = 0x4000;
/// `_CE_EXTENDED_FLAGS`: every bit of the second word git reads.
pub const CE_EXTENDED_FLAGS: u16 = 0x4000 | 0x2000;

/// `_INDEX_HASH_LEN.get(name)`: the length of an object name in the
/// object format `name`.
pub fn index_hash_len(name: &[u8]) -> Option<usize> {
    match name {
        b"sha1" => Some(20),
        b"sha256" => Some(32),
        _ => None,
    }
}

/// `UnicodeEncodeError`: a text `_config_switch` cannot encode (a lone
/// surrogate outside U+DC80..U+DCFF, or a name not ASCII once lowered).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct UnicodeEncodeError;

/// `text.encode("utf-8", "surrogateescape")` of code points.
fn encode_surrogateescape(text: &[u32]) -> Result<Vec<u8>, UnicodeEncodeError> {
    let mut out = Vec::with_capacity(text.len());
    for &cp in text {
        if (0xDC80..=0xDCFF).contains(&cp) {
            out.push((cp - 0xDC00) as u8);
        } else {
            let c = char::from_u32(cp).ok_or(UnicodeEncodeError)?;
            let mut buf = [0u8; 4];
            out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
        }
    }
    Ok(out)
}

/// `_config_switch(text, section, key)`: the boolean the config text
/// sets for `section.key` (no subsection), the last assignment winning;
/// None where unset, where git dies on the text, or where git dies on
/// the value. The text is encoded first and the name only once the text
/// parses, as the reference does, so a name that is not ASCII raises
/// only over a text git reads.
pub fn config_switch(
    text: &[u32],
    section: &[u32],
    key: &[u32],
) -> Result<Option<bool>, UnicodeEncodeError> {
    let data = encode_surrogateescape(text)?;
    let Ok(entries) = git_entries(&data) else {
        return Ok(None);
    };
    let mut spelled = section.to_vec();
    spelled.push(u32::from('.'));
    spelled.extend_from_slice(key);
    let lowered = unicode::lower(&spelled);
    let mut variable = Vec::with_capacity(lowered.len());
    for cp in lowered {
        let byte = u8::try_from(cp)
            .ok()
            .filter(u8::is_ascii)
            .ok_or(UnicodeEncodeError)?;
        variable.push(byte);
    }
    let last = entries.iter().rev().find(|e| e.name() == variable);
    Ok(last.and_then(|e| git_bool(e.value.as_deref())))
}

/// `_IndexDeclined`: the index is not one [`parse_skip_index`] reads
/// exactly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IndexDeclined;

/// `_index_varint(data, pos)`: git's `decode_varint`, the value at `pos`
/// and the position after it. The reference also takes a negative `pos`
/// (Python indexing from the end); no caller passes one, and a `usize`
/// has none.
pub fn index_varint(data: &[u8], pos: usize) -> Result<(u64, usize), IndexDeclined> {
    let mut pos = pos;
    let mut byte = *data.get(pos).ok_or(IndexDeclined)?;
    pos += 1;
    let mut value = u64::from(byte & 0x7F);
    while byte & 0x80 != 0 {
        value += 1;
        if value >> 57 != 0 || pos >= data.len() {
            return Err(IndexDeclined);
        }
        byte = data[pos];
        pos += 1;
        value = (value << 7) + u64::from(byte & 0x7F);
    }
    Ok((value, pos))
}

/// `_SkipIndex`: the names of an index's stage-0 entries split by the
/// skip-worktree bit, each list in byte order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SkipIndex {
    pub skipped: Vec<Vec<u8>>,
    pub kept: Vec<Vec<u8>>,
}

/// `_holds(names, name)`.
fn holds(names: &[Vec<u8>], name: &[u8]) -> bool {
    let at = names.partition_point(|n| n.as_slice() < name);
    at < names.len() && names[at] == name
}

/// `_any_below(names, prefix)`: the first name not below `prefix` in
/// byte order starts with it.
fn any_below(names: &[Vec<u8>], prefix: &[u8]) -> bool {
    let at = names.partition_point(|n| n.as_slice() < prefix);
    at < names.len() && names[at].starts_with(prefix)
}

impl SkipIndex {
    /// `_SkipIndex.skips(parts)`: an entry of the joined name carries the
    /// bit, or a directory entry above it does, or entries below it do and
    /// none below it lacks it.
    pub fn skips<P: AsRef<[u8]>>(&self, parts: &[P]) -> bool {
        let mut name = Vec::new();
        let mut above = Vec::new();
        for (i, part) in parts.iter().enumerate() {
            if i > 0 {
                name.push(b'/');
            }
            name.extend_from_slice(part.as_ref());
            if i + 1 < parts.len() {
                above.push(name.len());
            }
        }
        if holds(&self.skipped, &name) {
            return true;
        }
        let mut probe = Vec::with_capacity(name.len() + 1);
        for &cut in &above {
            probe.clear();
            probe.extend_from_slice(&name[..cut]);
            probe.push(b'/');
            if holds(&self.skipped, &probe) {
                return true;
            }
        }
        name.push(b'/');
        any_below(&self.skipped, &name) && !any_below(&self.kept, &name)
    }
}

fn word(data: &[u8], at: usize, width: usize) -> u64 {
    data[at..at + width]
        .iter()
        .fold(0, |n, &b| (n << 8) | u64::from(b))
}

/// `data.find(b"\0", start, stop)` with `stop <= data.len()`.
fn find_nul(data: &[u8], start: usize, stop: usize) -> Option<usize> {
    if start >= stop {
        return None;
    }
    data[start..stop]
        .iter()
        .position(|&b| b == 0)
        .map(|i| start + i)
}

/// `_parse_skip_index(data, hash_len)`: the [`SkipIndex`] of the index
/// file `data` whose object names are `hash_len` bytes long, read as
/// git's `do_read_index` lays it out; [`IndexDeclined`] for anything git
/// would refuse or lay out otherwise, and for a split index.
pub fn parse_skip_index(data: &[u8], hash_len: usize) -> Result<SkipIndex, IndexDeclined> {
    let end = data
        .len()
        .checked_sub(hash_len)
        .filter(|&e| e >= 12)
        .ok_or(IndexDeclined)?;
    if &data[..4] != b"DIRC" {
        return Err(IndexDeclined);
    }
    let version = word(data, 4, 4);
    if !(2..=4).contains(&version) {
        return Err(IndexDeclined);
    }
    let mut skipped = Vec::new();
    let mut kept = Vec::new();
    let mut pos = 12usize;
    let mut previous: Vec<u8> = Vec::new();
    for _ in 0..word(data, 8, 4) {
        let start = pos;
        let flags_at = start + 40 + hash_len;
        if flags_at + 2 > end {
            return Err(IndexDeclined);
        }
        let flags = word(data, flags_at, 2) as u16;
        let mut name_at = flags_at + 2;
        let mut extended = 0u16;
        if flags & CE_EXTENDED != 0 {
            if name_at + 2 > end {
                return Err(IndexDeclined);
            }
            extended = word(data, name_at, 2) as u16;
            if extended & !CE_EXTENDED_FLAGS != 0 {
                return Err(IndexDeclined);
            }
            name_at += 2;
        }
        let mut length = usize::from(flags & 0x0FFF);
        let name = if version == 4 {
            let (strip, at) = index_varint(data, name_at)?;
            name_at = at;
            let stop = find_nul(data, name_at, end);
            let keep = usize::try_from(strip)
                .ok()
                .and_then(|s| previous.len().checked_sub(s));
            let (Some(keep), Some(stop)) = (keep, stop) else {
                return Err(IndexDeclined);
            };
            let mut name = previous[..keep].to_vec();
            name.extend_from_slice(&data[name_at..stop]);
            if length != 0x0FFF && name.len() != length {
                return Err(IndexDeclined);
            }
            pos = stop + 1;
            name
        } else {
            if length == 0x0FFF {
                let stop = find_nul(data, name_at, end).ok_or(IndexDeclined)?;
                length = stop - name_at;
            }
            pos = start + ((name_at - start + length + 8) & !7);
            if pos > end {
                return Err(IndexDeclined);
            }
            data[name_at..name_at + length].to_vec()
        };
        if pos > end {
            return Err(IndexDeclined);
        }
        if (flags >> 12) & 0x3 == 0 {
            if extended & CE_SKIP_WORKTREE != 0 {
                skipped.push(name.clone());
            } else {
                kept.push(name.clone());
            }
        }
        previous = name;
    }
    while pos + 8 <= end {
        if &data[pos..pos + 4] == b"link" {
            return Err(IndexDeclined);
        }
        pos = pos.saturating_add(8 + word(data, pos + 4, 4) as usize);
    }
    skipped.sort();
    kept.sort();
    Ok(SkipIndex { skipped, kept })
}
