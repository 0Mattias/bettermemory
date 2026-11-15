//! The search lexicon: `bettermemory.search_lexicon`'s tokenizer, the
//! text the FTS index stores, the MATCH expression and the fingerprint.
//!
//! The reference's regular expressions are written out by hand here, each
//! next to the pattern it reproduces, over the generated Unicode tables
//! (`re`'s `\w` and `\d`, `str.lower`, NFKC, the NFD fold). The input is a
//! code-point slice, since a Python `str` may hold a lone surrogate; no
//! token ever holds one (`\w` matches none), so tokens are Rust strings.

use std::collections::HashSet;
use std::sync::OnceLock;

use sha2::{Digest, Sha256};

use crate::unicode;

const STOPWORDS_EN: &[&str] = &[
    "a",
    "about",
    "am",
    "an",
    "and",
    "anything",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "do",
    "does",
    "everything",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "hers",
    "him",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "my",
    "no",
    "not",
    "of",
    "on",
    "or",
    "she",
    "so",
    "something",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "to",
    "too",
    "us",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
];
const STOPWORDS_SV: &[&str] = &[
    "alla", "allt", "ar", "att", "av", "aven", "bara", "blev", "blir", "da", "dar", "den", "det",
    "eller", "en", "ett", "hade", "han", "har", "hon", "hur", "innan", "inte", "jag", "kan",
    "kunde", "med", "mer", "mest", "mycket", "nar", "nu", "och", "ocksa", "om", "pa", "sa",
    "sedan", "sig", "sitt", "ska", "skulle", "som", "till", "utan", "vad", "vara", "varfor", "vem",
    "vill", "ville",
];
const STOPWORDS_DE: &[&str] = &[
    "aber", "alle", "als", "am", "auch", "auf", "aus", "bei", "das", "dass", "dem", "den", "der",
    "des", "durch", "ein", "eine", "einem", "einen", "einer", "er", "es", "haben", "hatte", "ich",
    "ihr", "ihre", "im", "kann", "muss", "nach", "nicht", "noch", "nur", "oder", "schon", "sehr",
    "sein", "seine", "sich", "sie", "sind", "um", "und", "von", "vor", "waren", "wenn", "werden",
    "wie", "wir", "wird", "wurde", "zu", "zum", "zur",
];
const STOPWORDS_FR: &[&str] = &[
    "alors", "au", "aussi", "aux", "avec", "avoir", "bien", "ce", "ces", "cette", "comme", "dans",
    "de", "deja", "des", "elle", "elles", "encore", "est", "et", "etre", "fait", "il", "ils", "je",
    "la", "le", "les", "leur", "mais", "ne", "nous", "ou", "par", "pas", "pour", "que", "qui",
    "quoi", "se", "sont", "sur", "tous", "tout", "toute", "tres", "tu", "un", "une", "vous",
];
const STOPWORDS_ES: &[&str] = &[
    "al", "como", "cuando", "del", "donde", "el", "ella", "ellos", "esta", "estar", "estas",
    "este", "esto", "estos", "ha", "han", "las", "le", "lo", "los", "mas", "mi", "mis", "muy",
    "nosotros", "pero", "por", "porque", "quien", "ser", "sus", "tambien", "unas", "unos", "ya",
    "yo",
];

/// The stopword lists by language (`_STOPWORDS_EN` and so on).
pub const STOPWORD_LISTS: [(&str, &[&str]); 5] = [
    ("_STOPWORDS_EN", STOPWORDS_EN),
    ("_STOPWORDS_SV", STOPWORDS_SV),
    ("_STOPWORDS_DE", STOPWORDS_DE),
    ("_STOPWORDS_FR", STOPWORDS_FR),
    ("_STOPWORDS_ES", STOPWORDS_ES),
];

/// `_STEM_EXCEPTIONS`.
pub const STEM_EXCEPTIONS: &[&str] = &["alias", "atlas", "bias", "canvas", "lens", "news"];

/// `_ACRONYM_PLURALS`.
pub const ACRONYM_PLURALS: &[(&str, &str)] = &[
    ("apis", "api"),
    ("clis", "cli"),
    ("cpus", "cpu"),
    ("gpus", "gpu"),
    ("guis", "gui"),
    ("skus", "sku"),
    ("uris", "uri"),
];

/// `_FINGERPRINT_PROBES`.
pub const FINGERPRINT_PROBES: [&str; 7] = [
    "cookies cookie policies policy caches cache branches branch",
    "class status analysis k8s aws alias aliases news apis api",
    "does these skulle eine cette estas todos notes here",
    "東京オフィスは移転する予定 猫",
    "ｻｰﾊﾞｰ ＧＰＵ２０２６",
    "Zürich café naïve",
    "docker-containers what's C++ .NET 3.12.1",
];

/// `_UNSEGMENTED_RE`'s class: scripts written without spaces between
/// words.
pub const UNSEGMENTED: &[(u32, u32)] = &[
    (0x0E01, 0x0E5B),
    (0x1100, 0x11FF),
    (0x3041, 0x30FF),
    (0x31F0, 0x31FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA960, 0xA97F),
    (0xAC00, 0xD7FF),
    (0xF900, 0xFAFF),
    (0xFF66, 0xFF9D),
    (0x20000, 0x2EBEF),
];

/// The reference's spelling-table text (`search_lexicon._spelling_tables()`),
/// a copy of `lexicon_spelling_tables.json`. The tokenizer never reads it:
/// the lists above that it does read are written out by hand, and this text
/// is only hashed into the fingerprint, since it spells Python objects
/// (pattern sources and flags, `repr`-sorted pairs) that have no
/// counterpart here. `golden_lexicon` ties the two together: this text to
/// the golden file byte for byte, and each hand-written list to its entry
/// in it.
pub const REFERENCE_SPELLING_TABLES: &str = include_str!("reference_spelling_tables.json");

/// FNV-1a: the stopword set's keys are fixed and short, so a keyed hash
/// buys nothing and costs a third of the stemmer's time.
#[derive(Default)]
struct Fnv(u64);

impl std::hash::Hasher for Fnv {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        let mut h = if self.0 == 0 {
            0xcbf2_9ce4_8422_2325
        } else {
            self.0
        };
        for &b in bytes {
            h = (h ^ u64::from(b)).wrapping_mul(0x0100_0000_01b3);
        }
        self.0 = h;
    }
}

type FnvSet = HashSet<&'static str, std::hash::BuildHasherDefault<Fnv>>;

fn stopwords() -> &'static FnvSet {
    static SET: OnceLock<FnvSet> = OnceLock::new();
    SET.get_or_init(|| {
        STOPWORD_LISTS
            .iter()
            .flat_map(|(_, l)| l.iter().copied())
            .collect()
    })
}

/// Is `token` in `_STOPWORDS`?
pub fn is_stopword(token: &str) -> bool {
    stopwords().contains(token)
}

fn is_unsegmented(cp: u32) -> bool {
    if cp < UNSEGMENTED[0].0 {
        return false;
    }
    let i = UNSEGMENTED.partition_point(|&(_, hi)| hi < cp);
    UNSEGMENTED.get(i).is_some_and(|&(lo, _)| lo <= cp)
}

/// A code unit the token scanner reads: a byte of ASCII text or a code
/// point.
trait Sym: Copy + Into<u32> + From<u8> + PartialEq {}
impl Sym for u8 {}
impl Sym for u32 {}

fn word_at<T: Sym>(text: &[T], i: usize) -> bool {
    text.get(i).is_some_and(|&c| unicode::is_word(c.into()))
}

fn at<T: Sym>(text: &[T], i: usize, lit: &str) -> bool {
    text.len() >= i + lit.len() && lit.bytes().zip(&text[i..]).all(|(b, &c)| T::from(b) == c)
}

/// `_CONTRACTION_RE.sub("", text)`:
/// `(?<=\w)['’](?:s|t|d|m|ll|re|ve)\b`, in place. Nothing is ever written
/// past what was read, and the lookbehind reads a position that is either
/// unwritten or written with its own value.
fn strip_contractions<T: Sym>(text: &mut Vec<T>) {
    const SUFFIXES: [&str; 7] = ["s", "t", "d", "m", "ll", "re", "ve"];
    let mut written = 0;
    let mut i = 0;
    'scan: while i < text.len() {
        let c = text[i];
        let cp: u32 = c.into();
        if (cp == 0x27 || cp == 0x2019) && i > 0 && word_at(text, i - 1) {
            for suffix in SUFFIXES {
                let end = i + 1 + suffix.len();
                // `\b` after a word character: the next one is not one.
                if at(text, i + 1, suffix) && !word_at(text, end) {
                    i = end;
                    continue 'scan;
                }
            }
        }
        text[written] = c;
        written += 1;
        i += 1;
    }
    text.truncate(written);
}

/// One `_SYMBOL_ALIASES` substitution: `(?<!\w)<literal>` and, with
/// `guard`, `(?!\w)` after it, from `text` into `out`.
fn alias<T: Sym>(text: &[T], literal: &str, guard: bool, replacement: &str, out: &mut Vec<T>) {
    out.clear();
    let n = literal.len();
    let mut i = 0;
    while i < text.len() {
        if (i == 0 || !word_at(text, i - 1))
            && at(text, i, literal)
            && !(guard && word_at(text, i + n))
        {
            out.extend(replacement.bytes().map(T::from));
            i += n;
            continue;
        }
        out.push(text[i]);
        i += 1;
    }
}

/// `_fold_ascii_safe`, in place: contractions, symbol aliases, `_` to `-`.
fn fold_ascii_safe<T: Sym>(text: &mut Vec<T>, spare: &mut Vec<T>) {
    strip_contractions(text);
    let (plus, hash, dot) = (T::from(b'+'), T::from(b'#'), T::from(b'.'));
    if text.iter().any(|&c| c == plus || c == hash || c == dot) {
        for (literal, guard, replacement) in [
            ("c++", false, "cpp "),
            ("c#", true, "csharp"),
            ("f#", true, "fsharp"),
            (".net", true, "dotnet"),
        ] {
            alias(text, literal, guard, replacement, spare);
            std::mem::swap(text, spare);
        }
    }
    let (under, dash) = (T::from(b'_'), T::from(b'-'));
    for c in text.iter_mut() {
        if *c == under {
            *c = dash;
        }
    }
}

/// `_TOKEN_RE.findall(text)`: `\d+(?:\.\d+)+|\w(?:[\w\-]*\w)?`, each
/// match handed to `found` as a range.
fn find_tokens<T: Sym>(text: &[T], mut found: impl FnMut(&[T])) {
    let digit = |i: usize| text.get(i).is_some_and(|&c| unicode::is_re_digit(c.into()));
    let (dot, dash) = (T::from(b'.'), T::from(b'-'));
    let mut i = 0;
    let n = text.len();
    while i < n {
        if digit(i) {
            let mut j = i;
            while digit(j) {
                j += 1;
            }
            let mut groups = 0;
            while text.get(j) == Some(&dot) && digit(j + 1) {
                j += 1;
                while digit(j) {
                    j += 1;
                }
                groups += 1;
            }
            if groups > 0 {
                found(&text[i..j]);
                i = j;
                continue;
            }
        }
        if word_at(text, i) {
            let mut last = i;
            let mut j = i + 1;
            while j < n && (unicode::is_word(text[j].into()) || text[j] == dash) {
                if text[j] != dash {
                    last = j;
                }
                j += 1;
            }
            found(&text[i..=last]);
            i = last + 1;
            continue;
        }
        i += 1;
    }
}

/// Code points as text; a token holds no surrogate, since `\w` matches
/// none, and one that did would be dropped.
fn push_cps(text: &[u32], out: &mut String) {
    out.extend(text.iter().filter_map(|&c| char::from_u32(c)));
}

/// `_segment_unspaced`: unspaced-script runs as overlapping bigrams (a
/// lone character as itself), the chunks around them whole and stripped of
/// `-`. Each piece goes to `emit`.
fn segment_unspaced(token: &[u32], scratch: &mut String, mut emit: impl FnMut(&str)) {
    let chunk = |part: &[u32], scratch: &mut String, emit: &mut dyn FnMut(&str)| {
        let start = part.iter().position(|&c| c != 0x2D);
        let end = part.iter().rposition(|&c| c != 0x2D);
        if let (Some(s), Some(e)) = (start, end) {
            scratch.clear();
            push_cps(&part[s..=e], scratch);
            emit(scratch);
        }
    };
    let mut pos = 0;
    let mut i = 0;
    while i < token.len() {
        if token[i] < UNSEGMENTED[0].0 || !is_unsegmented(token[i]) {
            i += 1;
            continue;
        }
        let start = i;
        while i < token.len() && is_unsegmented(token[i]) {
            i += 1;
        }
        if start > pos {
            chunk(&token[pos..start], scratch, &mut emit);
        }
        let run = &token[start..i];
        if run.len() == 1 {
            scratch.clear();
            push_cps(run, scratch);
            emit(scratch);
        } else {
            for pair in run.windows(2) {
                scratch.clear();
                push_cps(pair, scratch);
                emit(scratch);
            }
        }
        pos = i;
    }
    if pos < token.len() {
        chunk(&token[pos..], scratch, &mut emit);
    }
}

fn without_last(s: &str) -> &str {
    let mut it = s.char_indices();
    it.next_back().map_or(s, |(i, _)| &s[..i])
}

/// `_stem_segment` appended to `out`, worked in `scratch`.
fn stem_segment_into(seg: &str, out: &mut String, scratch: &mut String) {
    if seg.chars().count() < 4 || is_stopword(seg) || STEM_EXCEPTIONS.contains(&seg) {
        out.push_str(seg);
        return;
    }
    if let Some(&(_, singular)) = ACRONYM_PLURALS.iter().find(|(p, _)| *p == seg) {
        out.push_str(singular);
        return;
    }
    let stem = scratch;
    stem.clear();
    stem.push_str(seg);
    if stem.ends_with("sses") {
        stem.truncate(stem.len() - 2);
    } else if stem.ends_with("ies") {
        if stem.chars().count() > 4 {
            stem.truncate(stem.len() - 3);
            stem.push('y');
        } else {
            stem.truncate(stem.len() - 1);
        }
    } else if stem.ends_with("ss") || stem.ends_with("us") || stem.ends_with("is") {
    } else if stem.ends_with('s')
        && stem
            .chars()
            .rev()
            .nth(1)
            .is_some_and(|c| unicode::is_alpha(c as u32))
    {
        stem.truncate(stem.len() - 1);
    }
    if stem.chars().count() >= 4
        && stem.ends_with('e')
        && !stem.ends_with("ee")
        && !is_stopword(without_last(stem))
    {
        stem.truncate(stem.len() - 1);
    }
    if stem.chars().count() >= 4 && stem.ends_with('y') {
        stem.pop();
        stem.push('i');
        if is_stopword(stem) {
            stem.pop();
            stem.push('y');
        }
    }
    out.push_str(if is_stopword(stem) { seg } else { stem });
}

/// `_stem_segment`: the light plural stemmer.
pub fn stem_segment(seg: &str) -> String {
    let mut out = String::new();
    stem_segment_into(seg, &mut out, &mut String::new());
    out
}

/// `_stem_token` appended to `out`: the stemmer per `-`-separated segment.
fn stem_token_into(token: &str, out: &mut String, scratch: &mut String) {
    for (i, seg) in token.split('-').enumerate() {
        if i > 0 {
            out.push('-');
        }
        stem_segment_into(seg, out, scratch);
    }
}

/// `_stem_token`: the stemmer per `-`-separated segment.
pub fn stem_token(token: &str) -> String {
    let mut out = String::with_capacity(token.len());
    stem_token_into(token, &mut out, &mut String::new());
    out
}

/// `text.lower()` then `_fold_diacritics`: what the tokenizer reads.
pub fn fold_lowered(text: &[u32]) -> Vec<u32> {
    unicode::nfd_fold(&unicode::lower(text))
}

/// One finished token: stemmed or not, into a `String` of its own.
fn emit(token: &str, stem: bool, scratch: &mut String, out: &mut Vec<String>) {
    let mut s = String::with_capacity(token.len() + 1);
    if stem {
        stem_token_into(token, &mut s, scratch);
    } else {
        s.push_str(token);
    }
    out.push(s);
}

/// The tokenizer over lowered ASCII bytes, in place.
fn tokenize_ascii(mut text: Vec<u8>, stem: bool) -> Vec<String> {
    let mut spare = Vec::new();
    fold_ascii_safe(&mut text, &mut spare);
    let mut out = Vec::new();
    let mut scratch = String::new();
    find_tokens(&text, |raw| {
        // ASCII bytes: always UTF-8.
        if let Ok(raw) = std::str::from_utf8(raw) {
            emit(raw, stem, &mut scratch, &mut out);
        }
    });
    out
}

fn tokenize_impl(text: &[u32], stem: bool) -> Vec<String> {
    if text.iter().all(|&c| c < 0x80) {
        let bytes = text
            .iter()
            .map(|&c| (c as u8).to_ascii_lowercase())
            .collect();
        return tokenize_ascii(bytes, stem);
    }
    let mut nfkc = Vec::with_capacity(text.len());
    unicode::nfkc_into(text, &mut nfkc);
    let mut folded = Vec::with_capacity(nfkc.len());
    unicode::lower_fold_into(&nfkc, &mut folded);
    fold_ascii_safe(&mut folded, &mut nfkc);
    let mut out = Vec::new();
    let mut scratch = String::new();
    let mut piece = String::new();
    find_tokens(&folded, |raw| {
        if raw.iter().any(|&c| is_unsegmented(c)) {
            segment_unspaced(raw, &mut piece, |seg| {
                emit(seg, stem, &mut scratch, &mut out)
            });
        } else {
            piece.clear();
            push_cps(raw, &mut piece);
            emit(&piece, stem, &mut scratch, &mut out);
        }
    });
    out
}

/// `tokenize`.
pub fn tokenize(text: &[u32]) -> Vec<String> {
    tokenize_impl(text, true)
}

/// `tokenize` of Rust text; ASCII text never leaves bytes.
pub fn tokenize_str(text: &str) -> Vec<String> {
    if text.is_ascii() {
        return tokenize_ascii(text.as_bytes().to_ascii_lowercase(), true);
    }
    tokenize(&unicode::cps(text))
}

/// `_tokenize_unstemmed`.
pub fn tokenize_unstemmed(text: &[u32]) -> Vec<String> {
    tokenize_impl(text, false)
}

/// `_strip_stopwords`.
pub fn strip_stopwords(tokens: &[String]) -> Vec<String> {
    tokens.iter().filter(|t| !is_stopword(t)).cloned().collect()
}

fn split_parts(token: &str) -> Option<Vec<&str>> {
    if token.contains('-') || token.contains('_') {
        Some(token.split(['-', '_']).filter(|p| !p.is_empty()).collect())
    } else if token.contains('.') {
        Some(token.split('.').filter(|p| !p.is_empty()).collect())
    } else {
        None
    }
}

/// `_expand_kebab`: each token, then its kebab or dotted parts, or a
/// two-character unspaced-script token's characters.
pub fn expand_kebab(tokens: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(tokens.len() * 2);
    for t in tokens {
        out.push(t.clone());
        if let Some(parts) = split_parts(t) {
            out.extend(parts.into_iter().map(str::to_owned));
        } else if !t.is_ascii() {
            let mut chars = t.chars();
            if let (Some(a), Some(b), None) = (chars.next(), chars.next(), chars.next()) {
                if is_unsegmented(a as u32) && is_unsegmented(b as u32) {
                    out.push(a.to_string());
                    out.push(b.to_string());
                }
            }
        }
    }
    out
}

/// `_kebab_parts`: a joined token's components, or none.
pub fn kebab_parts(token: &str) -> Vec<String> {
    match split_parts(token) {
        Some(parts) if parts.len() >= 2 => parts.into_iter().map(str::to_owned).collect(),
        _ => Vec::new(),
    }
}

/// `fts_index_text`.
pub fn fts_index_text(text: &[u32]) -> String {
    expand_kebab(&tokenize(text)).join(" ")
}

fn fts_phrase(term: &str) -> String {
    format!("\"{}\"", term.replace('"', "\"\""))
}

/// `fts_match_query`.
pub fn fts_match_query(query: &[u32]) -> String {
    let mut seen = HashSet::new();
    let mut groups = Vec::new();
    for tok in tokenize(query) {
        if !seen.insert(tok.clone()) {
            continue;
        }
        let parts = kebab_parts(&tok);
        let phrase = fts_phrase(&tok);
        if parts.is_empty() {
            groups.push(phrase);
        } else {
            let all: Vec<String> = parts.iter().map(|p| fts_phrase(p)).collect();
            groups.push(format!("({phrase} OR ({}))", all.join(" AND ")));
        }
    }
    groups.join(" OR ")
}

/// The probes' index text, joined by line feeds.
pub fn probe_index_text() -> String {
    FINGERPRINT_PROBES
        .iter()
        .map(|p| fts_index_text(&unicode::cps(p)))
        .collect::<Vec<_>>()
        .join("\n")
}

fn fingerprint_input() -> Vec<u8> {
    let mut input = probe_index_text().into_bytes();
    input.extend_from_slice(b"\n\0\n");
    input.extend_from_slice(REFERENCE_SPELLING_TABLES.as_bytes());
    input
}

/// The reference's `tokenizer_fingerprint()`: sha256 over the probes'
/// index text, `b"\n\0\n"` and the spelling tables. It names the Python
/// build's spelling and is what a store the Python build wrote records.
pub fn python_tokenizer_fingerprint() -> String {
    hex(&Sha256::digest(fingerprint_input()))
}

/// The native build's fingerprint: the same bytes as
/// [`python_tokenizer_fingerprint`] hashes, then `b"\n\0\nunidata "` and the
/// Unicode version of the compiled tables (`15.1.0`). The Python digest
/// leaves the interpreter's Unicode data out (its docstring says so); the
/// native build carries its own tables, so their version is part of the
/// spelling it names.
pub fn native_tokenizer_fingerprint() -> String {
    let mut input = fingerprint_input();
    input.extend_from_slice(b"\n\0\nunidata ");
    input.extend_from_slice(unicode::UNIDATA_VERSION.as_bytes());
    hex(&Sha256::digest(input))
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stems() {
        for (word, stem) in [
            ("cookies", "cooki"),
            ("policies", "polici"),
            ("branches", "branch"),
            ("aliases", "alias"),
            ("notes", "note"),
            ("ties", "tie"),
            ("apis", "api"),
        ] {
            assert_eq!(stem_segment(word), stem, "{word}");
        }
    }

    #[test]
    fn tokens() {
        assert_eq!(
            tokenize_str("C++20 what's 3.12.1 pre- x"),
            ["cpp", "20", "what", "3.12.1", "pre", "x"]
        );
        assert_eq!(tokenize_str("東京オフ"), ["東京", "京オ", "オフ"]);
    }
}
