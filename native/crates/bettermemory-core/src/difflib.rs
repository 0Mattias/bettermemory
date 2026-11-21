//! `difflib.unified_diff` as `history._body_diff` calls it, and
//! `str.splitlines`.
//!
//! The reference is CPython 3.13.13's `Lib/difflib.py`: `SequenceMatcher`
//! with no junk function and `autojunk` on (`__chain_b`,
//! `find_longest_match`, `get_matching_blocks`, `get_opcodes`,
//! `get_grouped_opcodes`), `_format_range_unified` and `unified_diff` with
//! `lineterm=""` and no dates. The matcher is generic over the element; the
//! diff's lines are [`PyStr`]. With no junk function the junk set is empty,
//! so `find_longest_match`'s second pair of extension loops never moves and
//! is not written; popular elements are not junk and the first pair extends
//! over them, as the reference does.

use std::collections::HashMap;
use std::hash::Hash;

use crate::pyvalue::PyStr;

/// An opcode's tag, as `get_opcodes` names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tag {
    Replace,
    Delete,
    Insert,
    Equal,
}

/// `(tag, i1, i2, j1, j2)`.
pub type Opcode = (Tag, usize, usize, usize, usize);

/// `SequenceMatcher(None, a, b)` with `autojunk=True`.
pub struct SequenceMatcher<'s, T> {
    a: &'s [T],
    b: &'s [T],
    b2j: HashMap<&'s T, Vec<usize>>,
}

impl<'s, T: Eq + Hash> SequenceMatcher<'s, T> {
    /// `set_seqs` and `__chain_b`: every element of `b` to its indices,
    /// less the popular ones when `b` holds 200 or more elements (those
    /// occurring more than `len(b) // 100 + 1` times).
    pub fn new(a: &'s [T], b: &'s [T]) -> Self {
        let mut b2j: HashMap<&'s T, Vec<usize>> = HashMap::new();
        for (i, elt) in b.iter().enumerate() {
            b2j.entry(elt).or_default().push(i);
        }
        let n = b.len();
        if n >= 200 {
            let ntest = n / 100 + 1;
            b2j.retain(|_, idxs| idxs.len() <= ntest);
        }
        SequenceMatcher { a, b, b2j }
    }

    /// `find_longest_match(alo, ahi, blo, bhi)`: the earliest `i`, then the
    /// earliest `j`, of the longest block over the non-popular elements,
    /// then extended over equal elements on both sides.
    pub fn find_longest_match(
        &self,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let (a, b) = (self.a, self.b);
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, elt) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            if let Some(js) = self.b2j.get(elt) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let prev = if j == 0 {
                        0
                    } else {
                        j2len.get(&(j - 1)).copied().unwrap_or(0)
                    };
                    let k = prev + 1;
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
            besti -= 1;
            bestj -= 1;
            bestsize += 1;
        }
        while besti + bestsize < ahi
            && bestj + bestsize < bhi
            && a[besti + bestsize] == b[bestj + bestsize]
        {
            bestsize += 1;
        }
        (besti, bestj, bestsize)
    }

    /// `get_matching_blocks`: the reference's explicit queue, the blocks
    /// sorted, adjacent ones merged, and the sentinel `(len(a), len(b), 0)`.
    pub fn get_matching_blocks(&self) -> Vec<(usize, usize, usize)> {
        let (la, lb) = (self.a.len(), self.b.len());
        let mut queue = vec![(0, la, 0, lb)];
        let mut blocks = Vec::new();
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let (i, j, k) = self.find_longest_match(alo, ahi, blo, bhi);
            if k > 0 {
                blocks.push((i, j, k));
                if alo < i && blo < j {
                    queue.push((alo, i, blo, j));
                }
                if i + k < ahi && j + k < bhi {
                    queue.push((i + k, ahi, j + k, bhi));
                }
            }
        }
        blocks.sort_unstable();
        let (mut i1, mut j1, mut k1) = (0, 0, 0);
        let mut out = Vec::new();
        for (i2, j2, k2) in blocks {
            if i1 + k1 == i2 && j1 + k1 == j2 {
                k1 += k2;
            } else {
                if k1 > 0 {
                    out.push((i1, j1, k1));
                }
                (i1, j1, k1) = (i2, j2, k2);
            }
        }
        if k1 > 0 {
            out.push((i1, j1, k1));
        }
        out.push((la, lb, 0));
        out
    }

    /// `get_opcodes`.
    pub fn get_opcodes(&self) -> Vec<Opcode> {
        let (mut i, mut j) = (0, 0);
        let mut out = Vec::new();
        for (ai, bj, size) in self.get_matching_blocks() {
            let tag = if i < ai && j < bj {
                Some(Tag::Replace)
            } else if i < ai {
                Some(Tag::Delete)
            } else if j < bj {
                Some(Tag::Insert)
            } else {
                None
            };
            if let Some(tag) = tag {
                out.push((tag, i, ai, j, bj));
            }
            (i, j) = (ai + size, bj + size);
            if size > 0 {
                out.push((Tag::Equal, ai, i, bj, j));
            }
        }
        out
    }

    /// `get_grouped_opcodes(n)`.
    pub fn get_grouped_opcodes(&self, n: usize) -> Vec<Vec<Opcode>> {
        let mut codes = self.get_opcodes();
        if codes.is_empty() {
            codes.push((Tag::Equal, 0, 1, 0, 1));
        }
        if let Some(c) = codes.first_mut() {
            if c.0 == Tag::Equal {
                *c = (
                    c.0,
                    c.1.max(c.2.saturating_sub(n)),
                    c.2,
                    c.3.max(c.4.saturating_sub(n)),
                    c.4,
                );
            }
        }
        if let Some(c) = codes.last_mut() {
            if c.0 == Tag::Equal {
                *c = (
                    c.0,
                    c.1,
                    c.2.min(c.1.saturating_add(n)),
                    c.3,
                    c.4.min(c.3.saturating_add(n)),
                );
            }
        }
        // Python's integers do not overflow: past usize::MAX the sum is
        // larger than any range, which saturating keeps.
        let nn = n.saturating_add(n);
        let mut groups = Vec::new();
        let mut group = Vec::new();
        for (tag, mut i1, i2, mut j1, j2) in codes {
            if tag == Tag::Equal && i2 - i1 > nn {
                group.push((
                    tag,
                    i1,
                    i2.min(i1.saturating_add(n)),
                    j1,
                    j2.min(j1.saturating_add(n)),
                ));
                groups.push(std::mem::take(&mut group));
                i1 = i1.max(i2.saturating_sub(n));
                j1 = j1.max(j2.saturating_sub(n));
            }
            group.push((tag, i1, i2, j1, j2));
        }
        let lone_equal = group.len() == 1 && group[0].0 == Tag::Equal;
        if !group.is_empty() && !lone_equal {
            groups.push(group);
        }
        groups
    }
}

fn ascii(text: &str) -> impl Iterator<Item = u32> + '_ {
    text.bytes().map(u32::from)
}

/// `_format_range_unified(start, stop)`.
pub fn format_range_unified(start: usize, stop: usize) -> String {
    // In i128, as Python's integers: a stop before the start gives a
    // negative length, and `usize::MAX + 1` does not wrap.
    let beginning = start as i128 + 1;
    let length = stop as i128 - start as i128;
    if length == 1 {
        return beginning.to_string();
    }
    if length == 0 {
        return format!("{},{}", beginning - 1, length);
    }
    format!("{beginning},{length}")
}

/// `difflib.unified_diff(a, b, fromfile, tofile, lineterm="")`, its lines
/// in order: nothing at all when no group differs.
pub fn unified_diff(a: &[PyStr], b: &[PyStr], fromfile: &[u32], tofile: &[u32]) -> Vec<PyStr> {
    let mut out: Vec<PyStr> = Vec::new();
    let groups = SequenceMatcher::new(a, b).get_grouped_opcodes(3);
    for (g, group) in groups.iter().enumerate() {
        if g == 0 {
            out.push(ascii("--- ").chain(fromfile.iter().copied()).collect());
            out.push(ascii("+++ ").chain(tofile.iter().copied()).collect());
        }
        let (first, last) = (group[0], group[group.len() - 1]);
        let head = format!(
            "@@ -{} +{} @@",
            format_range_unified(first.1, last.2),
            format_range_unified(first.3, last.4)
        );
        out.push(ascii(&head).collect());
        for &(tag, i1, i2, j1, j2) in group {
            let mut lines = |mark: u8, from: &[PyStr]| {
                for line in from {
                    out.push(
                        std::iter::once(u32::from(mark))
                            .chain(line.iter().copied())
                            .collect(),
                    );
                }
            };
            match tag {
                Tag::Equal => lines(b' ', &a[i1..i2]),
                Tag::Replace => {
                    lines(b'-', &a[i1..i2]);
                    lines(b'+', &b[j1..j2]);
                }
                Tag::Delete => lines(b'-', &a[i1..i2]),
                Tag::Insert => lines(b'+', &b[j1..j2]),
            }
        }
    }
    out
}

/// `"\n".join(unified_diff(a, b, fromfile, tofile, lineterm=""))`.
pub fn unified_diff_lines(a: &[PyStr], b: &[PyStr], fromfile: &[u32], tofile: &[u32]) -> PyStr {
    let lines = unified_diff(a, b, fromfile, tofile);
    let mut out = PyStr::new();
    for (n, line) in lines.iter().enumerate() {
        if n > 0 {
            out.push(0x0a);
        }
        out.extend_from_slice(line);
    }
    out
}

/// Whether `str.splitlines` ends a line at this code point.
fn is_line_boundary(cp: u32) -> bool {
    matches!(
        cp,
        0x0a | 0x0b | 0x0c | 0x0d | 0x1c | 0x1d | 0x1e | 0x85 | 0x2028 | 0x2029
    )
}

/// `str.splitlines()`: the lines without their ends, `\r\n` one end, no
/// empty last line after a final end.
pub fn splitlines(text: &[u32]) -> Vec<PyStr> {
    let mut out = Vec::new();
    let (mut start, mut i) = (0, 0);
    while i < text.len() {
        if is_line_boundary(text[i]) {
            out.push(text[start..i].to_vec());
            if text[i] == 0x0d && text.get(i + 1) == Some(&0x0a) {
                i += 1;
            }
            start = i + 1;
        }
        i += 1;
    }
    if start < text.len() {
        out.push(text[start..].to_vec());
    }
    out
}

/// `history._body_diff(before, after, old=old, new=new)`: `None` reads as
/// the empty text.
pub fn body_diff(before: Option<&[u32]>, after: &[u32], old: &[u32], new: &[u32]) -> PyStr {
    let a = splitlines(before.unwrap_or(&[]));
    let b = splitlines(after);
    unified_diff_lines(&a, &b, old, new)
}
