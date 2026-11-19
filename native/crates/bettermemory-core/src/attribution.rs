//! `attribution.py`: phrase-match attribution of retrieved memories to
//! the text of an assistant reply.
//!
//! Texts are code points (`PyStr`), so a lone surrogate passes as it does
//! in the reference. The three regular expressions are hand-written
//! matchers over the crate's Unicode tables: `\s` is `str.isspace`
//! (`Py_UNICODE_ISSPACE`, which `re`, `str.split` and `str.strip` share)
//! and `\w` is `unicode::is_word`.

use std::collections::BTreeSet;

use crate::pyvalue::PyStr;
use crate::unicode::{self, Form};

/// `AttributionMatch`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AttributionMatch {
    pub memory_id: PyStr,
    pub claim_excerpt: PyStr,
}

/// `_STOPWORDS`, sorted for a binary search.
pub const STOPWORDS: [&str; 72] = [
    "a", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been", "being", "but", "by",
    "can", "could", "did", "do", "does", "for", "from", "had", "has", "have", "he", "her", "his",
    "i", "if", "in", "is", "it", "its", "just", "may", "me", "might", "my", "no", "not", "of",
    "on", "only", "or", "our", "shall", "she", "should", "so", "than", "that", "the", "their",
    "them", "then", "these", "they", "this", "those", "to", "too", "us", "very", "was", "we",
    "were", "will", "with", "would", "yes", "you", "your",
];

pub const MIN_TOKEN_COUNT: usize = 6;
pub const MIN_CHAR_COUNT: usize = 30;
pub const MAX_EXCERPT_CHARS: usize = 500;
pub const MIN_CONTAINMENT: f64 = 0.60;
pub const MIN_CONTAINMENT_TOKENS: usize = 4;
pub const STOPWORD_RATIO_MAX: f64 = 0.80;

/// The distinct content tokens of a text (`_content_token_set`); each
/// token is ASCII, held as bytes.
pub type TokenSet = BTreeSet<Vec<u8>>;

fn is_stopword(token: &[u32]) -> bool {
    if token.is_empty() || token.len() > 6 || token.iter().any(|&c| c >= 0x80) {
        return false;
    }
    let bytes: Vec<u8> = token.iter().map(|&c| c as u8).collect();
    STOPWORDS
        .binary_search_by(|w| w.as_bytes().cmp(&bytes[..]))
        .is_ok()
}

fn is_space(cp: u32) -> bool {
    unicode::is_space(cp)
}

/// `str.strip()`.
fn strip(text: &[u32]) -> &[u32] {
    let start = text
        .iter()
        .position(|&c| !is_space(c))
        .unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|&c| !is_space(c))
        .map_or(start, |i| i + 1);
    &text[start..end]
}

/// `str.split()`.
fn split_whitespace(text: &[u32]) -> Vec<&[u32]> {
    text.split(|&c| is_space(c))
        .filter(|t| !t.is_empty())
        .collect()
}

/// `_SENTENCE_SPLIT_RE.split(body)`: `(?:[.!?]\s+|\n+)`, leftmost first,
/// the first alternative tried first, each run greedy.
fn split_sentences(body: &[u32]) -> Vec<&[u32]> {
    let mut out = Vec::new();
    let (mut piece, mut i) = (0, 0);
    while i < body.len() {
        let c = body[i];
        let end =
            if matches!(c, 0x2E | 0x21 | 0x3F) && body.get(i + 1).is_some_and(|&n| is_space(n)) {
                let mut j = i + 1;
                while j < body.len() && is_space(body[j]) {
                    j += 1;
                }
                j
            } else if c == 0x0A {
                let mut j = i;
                while j < body.len() && body[j] == 0x0A {
                    j += 1;
                }
                j
            } else {
                i += 1;
                continue;
            };
        out.push(&body[piece..i]);
        piece = end;
        i = end;
    }
    out.push(&body[piece..]);
    out
}

/// `_PUNCT_STRIP_RE.sub("", token)`: `^[^\w]+|[^\w]+$` over a token
/// `str.split()` made (no newline in it, so `$` is the end).
fn punct_strip(token: &[u32]) -> &[u32] {
    let lead = token
        .iter()
        .position(|&c| unicode::is_word(c))
        .unwrap_or(token.len());
    let rest = &token[lead..];
    let keep = rest
        .iter()
        .rposition(|&c| unicode::is_word(c))
        .map_or(0, |i| i + 1);
    &rest[..keep]
}

/// `_candidate_sentences(body)`.
pub fn candidate_sentences(body: &[u32]) -> Vec<PyStr> {
    let mut out = Vec::new();
    if body.is_empty() {
        return out;
    }
    for raw in split_sentences(body) {
        let sentence = strip(raw);
        if sentence.len() < MIN_CHAR_COUNT {
            continue;
        }
        let tokens = split_whitespace(sentence);
        if tokens.len() < MIN_TOKEN_COUNT {
            continue;
        }
        let stopwords = tokens
            .iter()
            .filter(|t| is_stopword(&unicode::lower(punct_strip(t))))
            .count();
        if stopwords as f64 / tokens.len() as f64 >= STOPWORD_RATIO_MAX {
            continue;
        }
        let end = sentence
            .iter()
            .rposition(|&c| !matches!(c, 0x2E | 0x21 | 0x3F))
            .map_or(0, |i| i + 1);
        let cleaned = strip(&sentence[..end]);
        if cleaned.len() < MIN_CHAR_COUNT {
            continue;
        }
        out.push(cleaned[..cleaned.len().min(MAX_EXCERPT_CHARS)].to_vec());
    }
    out
}

/// `_normalize(text)`: NFC, `lower()`, each `\s+` run made one space,
/// `strip()`.
pub fn normalize(text: &[u32]) -> PyStr {
    let lowered = unicode::lower(&unicode::normalize(Form::Nfc, text));
    let mut out = Vec::with_capacity(lowered.len());
    let mut in_run = false;
    for c in lowered {
        if is_space(c) {
            if !in_run {
                out.push(0x20);
            }
            in_run = true;
        } else {
            out.push(c);
            in_run = false;
        }
    }
    strip(&out).to_vec()
}

/// `_content_token_set(text)`: `[a-z0-9_]+` runs of the normalized text,
/// stopwords dropped.
pub fn content_token_set(text: &[u32]) -> TokenSet {
    let normalized = normalize(text);
    let in_token = |c: u32| matches!(c, 0x61..=0x7A | 0x30..=0x39 | 0x5F);
    normalized
        .split(|&c| !in_token(c))
        .filter(|t| !t.is_empty() && !is_stopword(t))
        .map(|t| t.iter().map(|&c| c as u8).collect())
        .collect()
}

/// `needle in haystack` over code points (Knuth-Morris-Pratt: linear).
fn contains(haystack: &[u32], needle: &[u32]) -> bool {
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
    for &c in haystack {
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

/// `_sentence_matches(sentence, haystack, reply_tokens)`.
pub fn sentence_matches(sentence: &[u32], haystack: &[u32], reply_tokens: &TokenSet) -> bool {
    let needle = normalize(sentence);
    if needle.is_empty() {
        return false;
    }
    if contains(haystack, &needle) {
        return true;
    }
    let sentence_tokens = content_token_set(sentence);
    let matched = sentence_tokens.intersection(reply_tokens).count();
    if matched < MIN_CONTAINMENT_TOKENS {
        return false;
    }
    matched as f64 / sentence_tokens.len() as f64 >= MIN_CONTAINMENT
}

/// `attribute_uses(memories, assistant_text)`: `memories` in the dict's
/// order, each `(memory_id, body)`.
pub fn attribute_uses(
    memories: &[(PyStr, PyStr)],
    assistant_text: &[u32],
) -> Vec<AttributionMatch> {
    let mut out = Vec::new();
    if assistant_text.is_empty() || memories.is_empty() {
        return out;
    }
    let haystack = normalize(assistant_text);
    if haystack.is_empty() {
        return out;
    }
    let reply_tokens = content_token_set(assistant_text);
    for (memory_id, body) in memories {
        if body.is_empty() {
            continue;
        }
        for sentence in candidate_sentences(body) {
            if sentence_matches(&sentence, &haystack, &reply_tokens) {
                out.push(AttributionMatch {
                    memory_id: memory_id.clone(),
                    claim_excerpt: sentence,
                });
                break;
            }
        }
    }
    out
}
