//! Unicode operations the reference reads, over Python's own data.
//!
//! Every table comes from `tables.rs`, which `native/tools/gen_unicode_tables.py`
//! generates from the reference Python's `unicodedata`, `str` methods and `re`
//! (Unicode [`UNIDATA_VERSION`]). Text is handled as a slice of code points
//! (`u32`), because a Python `str` may hold a lone surrogate, which a Rust
//! `str` cannot; a surrogate has no mapping, no decomposition, combining class
//! 0 and no property, as in Python.

#[rustfmt::skip]
#[allow(clippy::all)]
mod tables;

pub use tables::UNIDATA_VERSION;

use std::sync::OnceLock;

const F_WORD: u8 = 1;
const F_DIGIT: u8 = 2;
const F_NFD: u8 = 4;
const F_NFKD: u8 = 8;
const F_LOWER: u8 = 16;
const F_ALPHA: u8 = 32;
/// The second of a composition pair (Hangul vowels and trailing
/// consonants included): text holding none can compose nothing.
const F_COMP2: u8 = 64;

/// Dense lookups for the Basic Multilingual Plane, built once from the
/// tables: each code point's combining class and a few flags, so the hot
/// paths (the tokenizer's) skip the binary searches.
struct Bmp {
    ccc: Vec<u8>,
    flags: Vec<u8>,
}

fn bmp() -> &'static Bmp {
    static BMP: OnceLock<Bmp> = OnceLock::new();
    BMP.get_or_init(|| {
        let mut ccc = vec![0u8; 0x10000];
        let mut flags = vec![0u8; 0x10000];
        for (cp, (c, f)) in ccc.iter_mut().zip(flags.iter_mut()).enumerate() {
            let cp = cp as u32;
            *c = run_value(tables::COMBINING_RUNS, cp);
            let mut bits = 0;
            if in_ranges(tables::RE_WORD, cp) {
                bits |= F_WORD;
            }
            if in_ranges(tables::RE_DIGIT, cp) {
                bits |= F_DIGIT;
            }
            if in_ranges(tables::ALPHA, cp) {
                bits |= F_ALPHA;
            }
            *f = bits;
        }
        for &(cp, _) in tables::NFD_INDEX.iter().filter(|e| e.0 < 0x10000) {
            flags[cp as usize] |= F_NFD;
        }
        for &(cp, _) in tables::NFKD_INDEX.iter().filter(|e| e.0 < 0x10000) {
            flags[cp as usize] |= F_NFKD;
        }
        for &(cp, _) in tables::LOWER_INDEX.iter().filter(|e| e.0 < 0x10000) {
            flags[cp as usize] |= F_LOWER;
        }
        for &(_, second, _) in tables::COMPOSE.iter().filter(|e| e.1 < 0x10000) {
            flags[second as usize] |= F_COMP2;
        }
        for cp in (V_BASE..V_BASE + V_COUNT).chain(T_BASE + 1..T_BASE + T_COUNT) {
            flags[cp as usize] |= F_COMP2;
        }
        Bmp { ccc, flags }
    })
}

fn bmp_flag(cp: u32, flag: u8) -> Option<bool> {
    (cp < 0x10000).then(|| bmp().flags[cp as usize] & flag != 0)
}

const S_BASE: u32 = 0xAC00;
const L_BASE: u32 = 0x1100;
const V_BASE: u32 = 0x1161;
const T_BASE: u32 = 0x11A7;
const L_COUNT: u32 = 19;
const V_COUNT: u32 = 21;
const T_COUNT: u32 = 28;
const N_COUNT: u32 = V_COUNT * T_COUNT;
const S_COUNT: u32 = L_COUNT * N_COUNT;

fn run_value<T: Copy>(runs: &[(u32, T)], cp: u32) -> T {
    let i = runs.partition_point(|&(start, _)| start <= cp);
    runs[i - 1].1
}

fn in_ranges(ranges: &[(u32, u32)], cp: u32) -> bool {
    let i = ranges.partition_point(|&(start, _)| start <= cp);
    i > 0 && cp <= ranges[i - 1].1
}

fn mapped(index: &[(u32, u32)], data: &'static [u32], cp: u32) -> Option<&'static [u32]> {
    let i = index.binary_search_by_key(&cp, |&(c, _)| c).ok()?;
    let packed = index[i].1;
    let start = (packed >> 8) as usize;
    Some(&data[start..start + (packed & 0xFF) as usize])
}

/// `unicodedata.category`.
pub fn category(cp: u32) -> &'static str {
    tables::CATEGORY_NAMES[run_value(tables::CATEGORY_RUNS, cp) as usize]
}

/// `unicodedata.combining`: the canonical combining class.
pub fn combining(cp: u32) -> u8 {
    if cp < 0x300 {
        return 0;
    }
    if cp < 0x10000 {
        return bmp().ccc[cp as usize];
    }
    run_value(tables::COMBINING_RUNS, cp)
}

/// `str.isalpha` of one code point.
pub fn is_alpha(cp: u32) -> bool {
    if cp < 0x80 {
        return (cp as u8).is_ascii_alphabetic();
    }
    bmp_flag(cp, F_ALPHA).unwrap_or_else(|| in_ranges(tables::ALPHA, cp))
}

/// `str.isalnum` of one code point.
pub fn is_alnum(cp: u32) -> bool {
    in_ranges(tables::ALNUM, cp)
}

/// `str.isdigit` of one code point.
pub fn is_digit(cp: u32) -> bool {
    in_ranges(tables::DIGIT, cp)
}

/// `str.isdecimal` of one code point.
pub fn is_decimal(cp: u32) -> bool {
    in_ranges(tables::DECIMAL, cp)
}

/// `str.isnumeric` of one code point.
pub fn is_numeric(cp: u32) -> bool {
    in_ranges(tables::NUMERIC, cp)
}

/// `str.isspace` of one code point.
pub fn is_space(cp: u32) -> bool {
    in_ranges(tables::SPACE, cp)
}

/// `re`'s `\w` on a `str` pattern (Unicode): what the tokenizer's
/// patterns, lookarounds and `\b` read.
pub fn is_word(cp: u32) -> bool {
    if cp < 0x80 {
        let b = cp as u8;
        return b.is_ascii_alphanumeric() || b == b'_';
    }
    bmp_flag(cp, F_WORD).unwrap_or_else(|| in_ranges(tables::RE_WORD, cp))
}

/// `re`'s `\d` on a `str` pattern (Unicode decimal digits).
pub fn is_re_digit(cp: u32) -> bool {
    if cp < 0x80 {
        return (cp as u8).is_ascii_digit();
    }
    bmp_flag(cp, F_DIGIT).unwrap_or_else(|| in_ranges(tables::RE_DIGIT, cp))
}

/// Case-ignorable, as `str.lower`'s final-sigma rule reads it.
pub fn is_case_ignorable(cp: u32) -> bool {
    in_ranges(tables::CASE_IGNORABLE, cp)
}

/// Cased, as `str.lower`'s final-sigma rule reads it (only ever asked of a
/// code point that is not case-ignorable).
pub fn is_cased(cp: u32) -> bool {
    in_ranges(tables::CASED, cp)
}

fn is_hangul_syllable(cp: u32) -> bool {
    (S_BASE..S_BASE + S_COUNT).contains(&cp)
}

fn push_decomposition(out: &mut Vec<u32>, cp: u32, compat: bool) {
    if cp < 0xC0 && !compat || cp < 0xA0 {
        out.push(cp);
        return;
    }
    if is_hangul_syllable(cp) {
        let s = cp - S_BASE;
        out.push(L_BASE + s / N_COUNT);
        out.push(V_BASE + (s % N_COUNT) / T_COUNT);
        let t = s % T_COUNT;
        if t != 0 {
            out.push(T_BASE + t);
        }
        return;
    }
    if bmp_flag(cp, if compat { F_NFKD } else { F_NFD }) == Some(false) {
        out.push(cp);
        return;
    }
    let found = if compat {
        mapped(tables::NFKD_INDEX, tables::NFKD_DATA, cp)
    } else {
        mapped(tables::NFD_INDEX, tables::NFD_DATA, cp)
    };
    match found {
        Some(seq) => out.extend_from_slice(seq),
        None => out.push(cp),
    }
}

/// Canonical ordering: each run of non-starters stably sorted by class.
fn reorder(text: &mut [u32]) {
    let mut i = 0;
    while i < text.len() {
        if combining(text[i]) == 0 {
            i += 1;
            continue;
        }
        let start = i;
        while i < text.len() && combining(text[i]) != 0 {
            i += 1;
        }
        if i - start > 1 {
            text[start..i].sort_by_key(|&cp| combining(cp));
        }
    }
}

fn decompose(text: &[u32], compat: bool) -> Vec<u32> {
    let mut out = Vec::with_capacity(text.len() + 8);
    for &cp in text {
        push_decomposition(&mut out, cp, compat);
    }
    reorder(&mut out);
    out
}

fn compose_pair(first: u32, second: u32) -> Option<u32> {
    if (L_BASE..L_BASE + L_COUNT).contains(&first) && (V_BASE..V_BASE + V_COUNT).contains(&second) {
        return Some(S_BASE + ((first - L_BASE) * V_COUNT + (second - V_BASE)) * T_COUNT);
    }
    if is_hangul_syllable(first)
        && (first - S_BASE).is_multiple_of(T_COUNT)
        && (T_BASE + 1..T_BASE + T_COUNT).contains(&second)
    {
        return Some(first + (second - T_BASE));
    }
    let pairs = tables::COMPOSE;
    pairs
        .binary_search_by(|&(a, b, _)| (a, b).cmp(&(first, second)))
        .ok()
        .map(|i| pairs[i].2)
}

/// The canonical composition of fully decomposed, reordered text.
fn compose(mut text: Vec<u32>) -> Vec<u32> {
    compose_in_place(&mut text);
    text
}

/// The canonical composition, in place: the output never outgrows the
/// input, so it is written over the part already read.
fn compose_in_place(text: &mut Vec<u32>) {
    let mut written = 0;
    let mut starter: Option<usize> = None;
    let mut last_class: u8 = 0;
    for read in 0..text.len() {
        let cp = text[read];
        let class = combining(cp);
        if let Some(s) = starter {
            let adjacent = written == s + 1;
            if adjacent || (last_class != 0 && last_class < class) {
                if let Some(composite) = compose_pair(text[s], cp) {
                    text[s] = composite;
                    continue;
                }
            }
        }
        if class == 0 {
            starter = Some(written);
        }
        last_class = class;
        text[written] = cp;
        written += 1;
    }
    text.truncate(written);
}

/// Can NFKC leave `text` as it is without looking further? True when every
/// code point is in the Basic Multilingual Plane, decomposes to itself, is
/// a starter, and is no composition's second half.
fn nfkc_is_identity(text: &[u32]) -> bool {
    let bmp = bmp();
    let stable = nfkc_stable();
    text.iter().all(|&cp| {
        cp < 0xA0
            || cp < 0x10000
                && bmp.ccc[cp as usize] == 0
                && bmp.flags[cp as usize] & F_COMP2 == 0
                && (bmp.flags[cp as usize] & F_NFKD == 0 || stable[cp as usize])
    })
}

/// The Basic Multilingual Plane code points that decompose under NFKC and
/// compose back to themselves alone (U+0130, the Hangul syllables): a
/// starter of these, next to no composition's second half, is left as it
/// is, the quick check `unicodedata.normalize` makes.
fn nfkc_stable() -> &'static [bool] {
    static STABLE: OnceLock<Vec<bool>> = OnceLock::new();
    STABLE.get_or_init(|| {
        let mut stable = vec![false; 0x10000];
        let hangul = (S_BASE..S_BASE + S_COUNT).map(|cp| (cp, 0));
        for (cp, _) in tables::NFKD_INDEX.iter().copied().chain(hangul) {
            if cp < 0x10000 {
                stable[cp as usize] = compose(decompose(&[cp], true)) == [cp];
            }
        }
        stable
    })
}

/// `unicodedata.normalize("NFKC", text)` written into `out` (cleared
/// first).
pub fn nfkc_into(text: &[u32], out: &mut Vec<u32>) {
    out.clear();
    if nfkc_is_identity(text) {
        out.extend_from_slice(text);
        return;
    }
    for &cp in text {
        push_decomposition(out, cp, true);
    }
    reorder(out);
    compose_in_place(out);
}

/// `_fold_diacritics(text.lower())` in one pass, written into `out`
/// (cleared first): each code point lowered (U+03A3 by its final-sigma
/// context in `text`), each lowered code point decomposed, every
/// non-starter dropped.
pub fn lower_fold_into(text: &[u32], out: &mut Vec<u32>) {
    out.clear();
    out.reserve(text.len());
    let fold = |cp: u32, out: &mut Vec<u32>| {
        if cp < 0xC0 {
            out.push(cp);
            return;
        }
        let start = out.len();
        push_decomposition(out, cp, false);
        let mut keep = start;
        for i in start..out.len() {
            if combining(out[i]) == 0 {
                out[keep] = out[i];
                keep += 1;
            }
        }
        out.truncate(keep);
    };
    for (i, &cp) in text.iter().enumerate() {
        if cp < 0x80 {
            out.push((cp as u8).to_ascii_lowercase() as u32);
            continue;
        }
        if cp == 0x3A3 {
            fold(if final_sigma(text, i) { 0x3C2 } else { 0x3C3 }, out);
            continue;
        }
        if bmp_flag(cp, F_LOWER) != Some(false) {
            if let Some(seq) = mapped(tables::LOWER_INDEX, tables::LOWER_DATA, cp) {
                for &c in seq {
                    fold(c, out);
                }
                continue;
            }
        }
        fold(cp, out);
    }
}

/// The normalization forms `unicodedata.normalize` takes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Form {
    Nfc,
    Nfd,
    Nfkc,
    Nfkd,
}

/// `unicodedata.normalize(form, text)`.
pub fn normalize(form: Form, text: &[u32]) -> Vec<u32> {
    if text.iter().all(|&cp| cp < 0xA0) {
        return text.to_vec();
    }
    match form {
        Form::Nfd => decompose(text, false),
        Form::Nfkd => decompose(text, true),
        Form::Nfc => compose(decompose(text, false)),
        Form::Nfkc => compose(decompose(text, true)),
    }
}

/// The diacritic fold the tokenizer applies: the canonical decomposition
/// with every code point of non-zero combining class dropped. Folding code
/// point by code point is exact: canonical reordering only permutes the
/// non-starters, all of which are dropped.
pub fn nfd_fold(text: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(text.len());
    let mut scratch = Vec::with_capacity(4);
    for &cp in text {
        if cp < 0xC0 {
            out.push(cp);
            continue;
        }
        scratch.clear();
        push_decomposition(&mut scratch, cp, false);
        out.extend(scratch.iter().copied().filter(|&c| combining(c) == 0));
    }
    out
}

fn map_case(text: &[u32], index: &[(u32, u32)], data: &'static [u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(text.len());
    for &cp in text {
        match mapped(index, data, cp) {
            Some(seq) => out.extend_from_slice(seq),
            None => out.push(cp),
        }
    }
    out
}

fn final_sigma(text: &[u32], i: usize) -> bool {
    let mut j = i;
    let mut before = None;
    while j > 0 {
        j -= 1;
        if !is_case_ignorable(text[j]) {
            before = Some(text[j]);
            break;
        }
    }
    if !before.is_some_and(is_cased) {
        return false;
    }
    for &cp in &text[i + 1..] {
        if !is_case_ignorable(cp) {
            return !is_cased(cp);
        }
    }
    true
}

/// `str.lower`: full lowercase mappings, and U+03A3 as final sigma in
/// CPython's context (`handle_capital_sigma`).
pub fn lower(text: &[u32]) -> Vec<u32> {
    if text.iter().all(|&cp| cp < 0x80) {
        return text
            .iter()
            .map(|&cp| (cp as u8).to_ascii_lowercase() as u32)
            .collect();
    }
    let mut out = Vec::with_capacity(text.len());
    for (i, &cp) in text.iter().enumerate() {
        if cp == 0x3A3 {
            out.push(if final_sigma(text, i) { 0x3C2 } else { 0x3C3 });
            continue;
        }
        if bmp_flag(cp, F_LOWER) == Some(false) {
            out.push(cp);
            continue;
        }
        match mapped(tables::LOWER_INDEX, tables::LOWER_DATA, cp) {
            Some(seq) => out.extend_from_slice(seq),
            None => out.push(cp),
        }
    }
    out
}

/// `str.upper`: full uppercase mappings.
pub fn upper(text: &[u32]) -> Vec<u32> {
    map_case(text, tables::UPPER_INDEX, tables::UPPER_DATA)
}

/// `str.casefold`: full case folding.
pub fn casefold(text: &[u32]) -> Vec<u32> {
    map_case(text, tables::CASEFOLD_INDEX, tables::CASEFOLD_DATA)
}

fn first_mapped(index: &[(u32, u32)], data: &'static [u32], cp: u32) -> u32 {
    if index.last().is_none_or(|&(last, _)| cp > last) {
        return cp;
    }
    mapped(index, data, cp)
        .and_then(|seq| seq.first().copied())
        .unwrap_or(cp)
}

/// The first code point of `str.lower` of `cp` alone, `cp` itself where it
/// has no mapping: `_PyUnicode_ToLowercase`, by which `re` lowers one
/// character (`pyre::fold`). No final-sigma context, one code point out.
pub(crate) fn lower_first(cp: u32) -> u32 {
    if bmp_flag(cp, F_LOWER) == Some(false) {
        return cp;
    }
    first_mapped(tables::LOWER_INDEX, tables::LOWER_DATA, cp)
}

/// The first code point of `str.upper` of `cp` alone, `cp` itself where it
/// has no mapping: `_PyUnicode_ToUppercase`.
pub(crate) fn upper_first(cp: u32) -> u32 {
    first_mapped(tables::UPPER_INDEX, tables::UPPER_DATA, cp)
}

/// `re._casefix._EXTRA_CASES`, ascending by key: for a lowered code point,
/// the other lowered code points `re` takes for the same letter.
pub(crate) fn sre_extra_cases() -> &'static [(u32, &'static [u32])] {
    tables::SRE_EXTRA_CASES
}

/// The code points of `text`.
pub fn cps(text: &str) -> Vec<u32> {
    text.chars().map(|c| c as u32).collect()
}

/// `text` as a Rust string, or None when it holds a surrogate.
pub fn to_string(text: &[u32]) -> Option<String> {
    text.iter().map(|&cp| char::from_u32(cp)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hangul_round_trip() {
        let syllable = [0xAC01];
        let d = normalize(Form::Nfd, &syllable);
        assert_eq!(d, vec![0x1100, 0x1161, 0x11A8]);
        assert_eq!(normalize(Form::Nfc, &d), syllable.to_vec());
    }

    #[test]
    fn final_sigma_rule() {
        assert_eq!(lower(&cps("ΣΟΦΟΣ")), cps("σοφος"));
        assert_eq!(lower(&cps("Σ")), cps("σ"));
        assert_eq!(lower(&cps("AΣ.")), cps("aς."));
    }
}
