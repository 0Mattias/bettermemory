//! The drift index of `claims.py`: `_Headers`, `build_binding_index` and
//! `claim_level_drift`, pure over the text of one `git log -p -U0` stream.
//!
//! The four literal helpers the index and the scoring call
//! (`string_fragment`, `_rhs_repr`, `anchors_from_value` and
//! `ast.literal_eval` of a claim value) are the [`LiteralLines`] trait, so
//! this module needs no literal reader. Every reference call site catches
//! `Exception`, so the trait has no error: a failure is None or nothing.

use std::collections::{HashMap, HashSet};

use crate::claims::{
    self, binding_token, header_path, is_mark_hash, patch_header_path, strip, Binding, Claim,
    IntTooLong, Kind, PathSet, COMMIT_MARK,
};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;

/// `_MIN_ANCHOR_CHARS`.
pub const MIN_ANCHOR_CHARS: usize = 12;

/// `_PATH_HEADERS`.
const PATH_HEADERS: [&str; 4] = [
    "deleted file mode ",
    "new file mode ",
    "old mode ",
    "new mode ",
];
/// `_NAMED_HEADERS`.
const NAMED_HEADERS: [&str; 3] = ["rename from ", "rename to ", "copy to "];

/// The literal helpers of `claims.py` the index and the scoring call.
pub trait LiteralLines {
    /// `string_fragment(content)`.
    fn string_fragment(&self, content: &[u32]) -> Option<PyStr>;
    /// `_rhs_repr(content)`.
    fn rhs_repr(&self, content: &[u32]) -> Option<PyStr>;
    /// `anchors_from_value(value)`, in its order.
    fn anchors_from_value(&self, value: &[u32]) -> Vec<PyStr>;
    /// `ast.literal_eval(value)` when that is a `str`; None when it is
    /// another value or raises.
    fn claimed_str(&self, value: &[u32]) -> Option<PyStr>;
}

fn starts(text: &[u32], prefix: &str) -> bool {
    prefix
        .chars()
        .enumerate()
        .all(|(i, c)| text.get(i) == Some(&u32::from(c)))
}

/// A set of commit hashes (or texts), as the reference's `set[str]`.
pub type TextSet = HashSet<PyStr>;
/// `dict[str, set[str]]`.
pub type ShaMap = HashMap<PyStr, TextSet>;

/// `_Headers`: the extended header of the open `diff --git` section.
#[derive(Default)]
struct Headers {
    only: ShaMap,
    path: PyStr,
    open: bool,
    touched: PathSet,
}

impl Headers {
    fn start(&mut self, line: &[u32], sha: &[u32]) {
        self.close(sha);
        self.open = starts(line, "diff --git ");
        self.path = if self.open {
            header_path(&line[11..])
        } else {
            Vec::new()
        };
    }

    fn close(&mut self, sha: &[u32]) {
        for path in self.touched.drain() {
            self.only.entry(path).or_default().insert(sha.to_vec());
        }
        self.open = false;
    }

    fn body(&mut self) {
        self.open = false;
        self.touched.clear();
    }

    fn read(&mut self, line: &[u32], files: &mut PathSet, deleted: &mut PathSet) -> bool {
        if !self.open {
            return false;
        }
        let (path, gone) = if PATH_HEADERS.iter().any(|p| starts(line, p)) {
            (self.path.clone(), starts(line, "deleted "))
        } else {
            let Some(prefix) = NAMED_HEADERS.iter().find(|p| starts(line, p)) else {
                return false;
            };
            (line[prefix.len()..].to_vec(), *prefix == "rename from ")
        };
        if !path.is_empty() {
            if gone {
                deleted.insert(path.clone());
            }
            files.insert(path.clone());
            self.touched.insert(path);
        }
        true
    }
}

/// A binding's key: path, kind (`"def"` or `"assign"`, as the reference's
/// tuple holds it) and name.
pub type BindingKey = (PyStr, &'static str, PyStr);

/// One binding's entry: what the reference's inner dict holds.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Entry {
    pub commits: TextSet,
    pub adds: u64,
    pub removes: u64,
    pub edit_lines: u64,
    pub rhs_added: TextSet,
    pub rhs_removed: TextSet,
}

/// The dict `build_binding_index` returns, field by key.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Index {
    pub bindings: HashMap<BindingKey, Entry>,
    pub changed_text: HashMap<PyStr, ShaMap>,
    pub changed_fragments: HashMap<PyStr, ShaMap>,
    pub deleted: PathSet,
    pub files: PathSet,
    pub header_only: ShaMap,
    pub commits: usize,
    pub unindexed_commits: TextSet,
    pub hunks: usize,
    pub parse_mismatches: usize,
}

/// The value of a Unicode decimal digit: its place in its run of ten.
fn digit_value(cp: u32) -> u64 {
    let mut k = 0u64;
    let mut c = cp;
    while c > 0 && unicode::is_re_digit(c - 1) {
        k += 1;
        c -= 1;
    }
    k % 10
}

fn digit_end(t: &[u32], mut i: usize) -> usize {
    while i < t.len() && unicode::is_re_digit(t[i]) {
        i += 1;
    }
    i
}

/// `int()` of a digit run, saturated at `u64::MAX` (no line count reaches
/// it); past the digit limit, ValueError.
fn count_of(run: &[u32]) -> Result<u64, IntTooLong> {
    if run.len() > claims::MAX_INT_DIGITS {
        return Err(IntTooLong);
    }
    Ok(run.iter().fold(0u64, |n, &c| {
        n.saturating_mul(10).saturating_add(digit_value(c))
    }))
}

/// `int(match.group(2) or 1)` and `int(match.group(4) or 1)` of
/// `_HUNK_RE.match(line)`; None where it does not match. The match itself
/// is `claims::hunk_lines`; past it the header's shape is fixed, so the
/// two counts are read by walking it.
fn hunk_counts(line: &[u32]) -> Result<Option<(u64, u64)>, IntTooLong> {
    if claims::hunk_lines(line)?.is_none() {
        return Ok(None);
    }
    let mut counts = [1u64; 2];
    let mut i = 4;
    for (n, step) in [(0usize, 2usize), (1, 0)] {
        i = digit_end(line, i);
        if line.get(i) == Some(&u32::from(b',')) && digit_end(line, i + 1) > i + 1 {
            let e = digit_end(line, i + 1);
            counts[n] = count_of(&line[i + 1..e])?;
            i = e;
        }
        i += step;
    }
    Ok(Some((counts[0], counts[1])))
}

fn add_sha(map: &mut HashMap<PyStr, ShaMap>, path: &[u32], key: &[u32], sha: &[u32]) {
    map.entry(path.to_vec())
        .or_default()
        .entry(key.to_vec())
        .or_default()
        .insert(sha.to_vec());
}

/// `build_binding_index(diff_text)`. `Err` is `int()`'s ValueError on a
/// hunk count past the digit limit, which the reference raises.
pub fn build_binding_index(diff_text: &[u32], lit: &dyn LiteralLines) -> Result<Index, IntTooLong> {
    let mut ix = Index::default();
    let mut commits = TextSet::new();
    let mut file_commits = TextSet::new();
    let mut sha: PyStr = Vec::new();
    let mut path: PyStr = Vec::new();
    let mut headers = Headers::default();
    let lines: Vec<&[u32]> = diff_text.split(|&c| c == 0x0A).collect();
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        i += 1;
        if line.first() == Some(&COMMIT_MARK) {
            headers.start(line, &sha);
            path.clear();
            let mark = strip(&line[1..]);
            if !is_mark_hash(mark) {
                ix.parse_mismatches += 1;
                continue;
            }
            sha = mark.to_vec();
            commits.insert(sha.clone());
            continue;
        }
        if starts(line, "diff --git ") {
            headers.start(line, &sha);
            file_commits.insert(sha.clone());
            path.clear();
            continue;
        }
        if headers.read(line, &mut ix.files, &mut ix.deleted) {
            continue;
        }
        if starts(line, "--- ") || starts(line, "+++ ") || starts(line, "@@") {
            headers.body();
        }
        if let Some(p) = patch_header_path(line, &path, &mut ix.files, &mut ix.deleted) {
            path = p;
            continue;
        }
        if !starts(line, "@@") || path.is_empty() {
            continue;
        }
        let Some((removed, added)) = hunk_counts(line)? else {
            continue;
        };
        ix.hunks += 1;
        let want = u128::from(removed) + u128::from(added);
        let (mut minus, mut plus) = (0u64, 0u64);
        while u128::from(minus) + u128::from(plus) < want && i < lines.len() {
            let body = lines[i];
            i += 1;
            let added_side = match body.first() {
                Some(0x5C) => continue,
                Some(0x2D) => {
                    minus += 1;
                    false
                }
                Some(0x2B) => {
                    plus += 1;
                    true
                }
                _ => {
                    i -= 1;
                    break;
                }
            };
            let content = &body[1..];
            let stripped = strip(content);
            if stripped.len() >= MIN_ANCHOR_CHARS {
                add_sha(&mut ix.changed_text, &path, stripped, &sha);
            }
            if let Some(fragment) = lit.string_fragment(content) {
                if strip(&fragment).len() >= MIN_ANCHOR_CHARS {
                    add_sha(&mut ix.changed_fragments, &path, &fragment, &sha);
                }
            }
            let Some((kind, name)) = binding_token(content) else {
                continue;
            };
            let entry = ix
                .bindings
                .entry((path.clone(), binding_name(kind), name))
                .or_default();
            entry.commits.insert(sha.clone());
            entry.edit_lines += 1;
            if added_side {
                entry.adds += 1;
            } else {
                entry.removes += 1;
            }
            if kind == Binding::Assign {
                if let Some(rhs) = lit.rhs_repr(content) {
                    if added_side {
                        entry.rhs_added.insert(rhs);
                    } else {
                        entry.rhs_removed.insert(rhs);
                    }
                }
            }
        }
        if minus != removed || plus != added {
            ix.parse_mismatches += 1;
        }
    }
    headers.close(&sha);
    ix.header_only = headers.only;
    ix.commits = commits.len();
    ix.unindexed_commits = commits.difference(&file_commits).cloned().collect();
    Ok(ix)
}

/// The dict `claim_level_drift` returns, field by key.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Drift {
    pub cite_commits: usize,
    pub cite_edit_lines: u64,
    pub anchor_commits: usize,
    pub value_gone: bool,
    pub strict: bool,
    pub weak: bool,
    pub binding_shas: TextSet,
    pub anchor_shas: TextSet,
}

/// `needle in hay` for code points: Knuth, Morris and Pratt, linear in
/// the two lengths together.
fn contains(hay: &[u32], needle: &[u32]) -> bool {
    if needle.is_empty() {
        return true;
    }
    let mut fail = vec![0usize; needle.len()];
    let mut k = 0;
    for j in 1..needle.len() {
        while k > 0 && needle[j] != needle[k] {
            k = fail[k - 1];
        }
        if needle[j] == needle[k] {
            k += 1;
        }
        fail[j] = k;
    }
    k = 0;
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

/// `claim_level_drift(cite, index)`.
pub fn claim_level_drift(cite: &Claim, index: &Index, lit: &dyn LiteralLines) -> Drift {
    let path_gone = index.deleted.contains(&cite.rel_path);
    let empty = Entry::default();
    let lookup = |kind: Binding| {
        index
            .bindings
            .get(&(cite.rel_path.clone(), binding_name(kind), cite.name.clone()))
            .unwrap_or(&empty)
    };
    let mut anchor_shas = TextSet::new();
    let mut value_gone = false;
    let (entry, strict, weak) = match cite.kind {
        Kind::Path => (&empty, path_gone, path_gone),
        Kind::Absent => {
            let mut base = cite.rel_path.as_slice();
            while base.last() == Some(&u32::from(b'/')) {
                base = &base[..base.len() - 1];
            }
            let touched: Vec<&PyStr> = index
                .files
                .iter()
                .filter(|p| {
                    **p == cite.rel_path
                        || (p.starts_with(base) && p.get(base.len()) == Some(&u32::from(b'/')))
                })
                .collect();
            let strict = touched.iter().any(|p| !index.deleted.contains(*p));
            (&empty, strict, !touched.is_empty())
        }
        Kind::Symbol => {
            let e = lookup(Binding::Def);
            let strict = path_gone || e.removes > e.adds;
            (e, strict, path_gone || !e.commits.is_empty())
        }
        Kind::Literal => {
            let e = lookup(Binding::Assign);
            value_gone = e.rhs_removed.contains(&cite.value) && !e.rhs_added.contains(&cite.value);
            let per_file = index.changed_text.get(&cite.rel_path);
            for anchor in lit.anchors_from_value(&cite.value) {
                if let Some(shas) = per_file.and_then(|m| m.get(&anchor)) {
                    anchor_shas.extend(shas.iter().cloned());
                }
            }
            if let Some(claimed) = lit.claimed_str(&cite.value) {
                if let Some(frags) = index.changed_fragments.get(&cite.rel_path) {
                    let present: std::collections::HashSet<u32> = claimed.iter().copied().collect();
                    for (fragment, shas) in frags {
                        if fragment.len() <= claimed.len()
                            && fragment.iter().all(|c| present.contains(c))
                            && contains(&claimed, fragment)
                        {
                            anchor_shas.extend(shas.iter().cloned());
                        }
                    }
                }
            }
            let hit = !anchor_shas.is_empty();
            let strict = path_gone || value_gone || hit;
            (e, strict, path_gone || !e.commits.is_empty() || hit)
        }
    };
    Drift {
        cite_commits: entry.commits.len() + anchor_shas.len(),
        cite_edit_lines: entry.edit_lines,
        anchor_commits: anchor_shas.len(),
        value_gone,
        strict,
        weak,
        binding_shas: entry.commits.clone(),
        anchor_shas,
    }
}

// --- the render the fixtures compare ---------------------------------------

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn int(n: u64) -> PyValue {
    PyValue::int(i128::from(n))
}

/// A set as a sorted list of its texts.
pub fn sorted_set(set: &TextSet) -> PyValue {
    let mut v: Vec<&PyStr> = set.iter().collect();
    v.sort();
    PyValue::List(v.into_iter().map(|x| PyValue::Str(x.clone())).collect())
}

/// A `dict[str, set[str]]` as `[[key, sorted set], ...]` sorted by key.
fn sha_map(map: &ShaMap) -> PyValue {
    let mut v: Vec<(&PyStr, &TextSet)> = map.iter().collect();
    v.sort_by(|a, b| a.0.cmp(b.0));
    PyValue::List(
        v.into_iter()
            .map(|(k, set)| PyValue::List(vec![PyValue::Str(k.clone()), sorted_set(set)]))
            .collect(),
    )
}

fn nested(map: &HashMap<PyStr, ShaMap>) -> PyValue {
    let mut v: Vec<(&PyStr, &ShaMap)> = map.iter().collect();
    v.sort_by(|a, b| a.0.cmp(b.0));
    PyValue::List(
        v.into_iter()
            .map(|(k, m)| PyValue::List(vec![PyValue::Str(k.clone()), sha_map(m)]))
            .collect(),
    )
}

/// The reference's name of a binding kind.
pub fn binding_name(b: Binding) -> &'static str {
    match b {
        Binding::Def => "def",
        Binding::Assign => "assign",
    }
}

fn entry_value(e: &Entry) -> PyValue {
    PyValue::Dict(vec![
        (s("commits"), sorted_set(&e.commits)),
        (s("adds"), int(e.adds)),
        (s("removes"), int(e.removes)),
        (s("edit_lines"), int(e.edit_lines)),
        (s("rhs_added"), sorted_set(&e.rhs_added)),
        (s("rhs_removed"), sorted_set(&e.rhs_removed)),
    ])
}

impl Index {
    /// The index in the reference's key order, each set sorted, each dict
    /// a list of `[key, value]` pairs sorted by key (a binding's key is
    /// its path, kind and name).
    pub fn render(&self) -> PyValue {
        let mut keys: Vec<&BindingKey> = self.bindings.keys().collect();
        keys.sort();
        let bindings = keys
            .into_iter()
            .map(|k| {
                PyValue::List(vec![
                    PyValue::Str(k.0.clone()),
                    s(k.1),
                    PyValue::Str(k.2.clone()),
                    entry_value(&self.bindings[k]),
                ])
            })
            .collect();
        PyValue::Dict(vec![
            (s("bindings"), PyValue::List(bindings)),
            (s("changed_text"), nested(&self.changed_text)),
            (s("changed_fragments"), nested(&self.changed_fragments)),
            (s("deleted"), sorted_set(&self.deleted)),
            (s("files"), sorted_set(&self.files)),
            (s("header_only"), sha_map(&self.header_only)),
            (s("commits"), int(self.commits as u64)),
            (s("unindexed_commits"), sorted_set(&self.unindexed_commits)),
            (s("hunks"), int(self.hunks as u64)),
            (s("parse_mismatches"), int(self.parse_mismatches as u64)),
        ])
    }
}

impl Drift {
    /// The answer in the reference's key order, each set sorted.
    pub fn render(&self) -> PyValue {
        PyValue::Dict(vec![
            (s("cite_commits"), int(self.cite_commits as u64)),
            (s("cite_edit_lines"), int(self.cite_edit_lines)),
            (s("anchor_commits"), int(self.anchor_commits as u64)),
            (s("value_gone"), PyValue::Bool(self.value_gone)),
            (s("strict"), PyValue::Bool(self.strict)),
            (s("weak"), PyValue::Bool(self.weak)),
            (s("binding_shas"), sorted_set(&self.binding_shas)),
            (s("anchor_shas"), sorted_set(&self.anchor_shas)),
        ])
    }
}
