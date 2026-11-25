//! The per-body features of `supersession.py` (lines 145-518): the
//! numeric tokens, the change cues and the value each fills, the proper
//! nouns, the value and anchor tokens, the shapes and kinship of two
//! values, a value's context, and the surface spelling of a token, with
//! the word-token cache they share.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::{Arc, LazyLock, Mutex, MutexGuard, Once, OnceLock};

use super::{
    SupersessionError, ASCII_DIGIT_PATTERN, CHANGE_CUES, CLAUSE_PATTERN, CONTEXT_WIDTH,
    DOTTED_PATTERN, EXTRA_CUE_TOKENS, INTEGER_PATTERN, NUMERIC_TOKEN_PATTERN, PROPER_NOUN_PATTERN,
    VALUE_PREPS, WORD_PATTERN,
};
use crate::caches;
use crate::lexicon::{is_stopword, kebab_parts, tokenize, tokenize_str};
use crate::pyre::atom::escape;
use crate::pyre::regex::{CallError, Item, Match, Regex, RegexError};
use crate::pyre::replace::ReplaceError;
use crate::pyvalue::PyStr;
use crate::unicode;

type Built = LazyLock<Result<Regex, RegexError>>;

static NUMERIC_TOKEN_RE: Built = LazyLock::new(|| Regex::new_str(NUMERIC_TOKEN_PATTERN, 0));
static ASCII_DIGIT_RE: Built = LazyLock::new(|| Regex::new_str(ASCII_DIGIT_PATTERN, 0));
static CUE_RE: Built = LazyLock::new(|| Regex::new(&cue_pattern(), 0));
static WORD_RE: Built = LazyLock::new(|| Regex::new_str(WORD_PATTERN, 0));
static CLAUSE_RE: Built = LazyLock::new(|| Regex::new_str(CLAUSE_PATTERN, 0));
static PROPER_NOUN_RE: Built = LazyLock::new(|| Regex::new_str(PROPER_NOUN_PATTERN, 0));
static INTEGER_RE: Built = LazyLock::new(|| Regex::new_str(INTEGER_PATTERN, 0));
static DOTTED_RE: Built = LazyLock::new(|| Regex::new_str(DOTTED_PATTERN, 0));

/// `_CUE_RE.pattern`: `\b(` and the cues joined by `|`, each through
/// `re.escape`, longest first (a stable sort, so cues of one length keep
/// `CHANGE_CUES`' order), then `)\b`.
pub fn cue_pattern() -> PyStr {
    let mut cues: Vec<&str> = CHANGE_CUES.iter().map(|(cue, _)| *cue).collect();
    cues.sort_by_key(|cue| std::cmp::Reverse(cue.len()));
    let mut out = unicode::cps(r"\b(");
    for (i, cue) in cues.iter().enumerate() {
        if i > 0 {
            out.push(u32::from(b'|'));
        }
        out.extend(escape(&unicode::cps(cue)));
    }
    out.extend(unicode::cps(r")\b"));
    out
}

fn built(re: &'static Built, name: &'static str) -> Result<&'static Regex, SupersessionError> {
    re.as_ref()
        .map_err(|e| SupersessionError::Pattern(name, e.clone()))
}

fn finditer(
    re: &'static Built,
    name: &'static str,
    text: &[u32],
) -> Result<Vec<Match>, SupersessionError> {
    built(re, name)?
        .finditer(text, 0, isize::MAX)
        .map_err(|e| SupersessionError::Call(name, ReplaceError::Call(e)))
}

/// `findall` of a pattern without groups: each whole match.
fn findall(
    re: &'static Built,
    name: &'static str,
    text: &[u32],
) -> Result<Vec<PyStr>, SupersessionError> {
    let found = built(re, name)?
        .findall(text, 0, isize::MAX)
        .map_err(|e| SupersessionError::Call(name, ReplaceError::Call(e)))?;
    Ok(found
        .into_iter()
        .map(|item| match item {
            Item::One(piece) => piece,
            Item::Tuple(pieces) => pieces.concat(),
        })
        .collect())
}

/// `split(text, maxsplit)` of a pattern without groups.
fn split(
    re: &'static Built,
    name: &'static str,
    text: &[u32],
    maxsplit: isize,
) -> Result<Vec<PyStr>, SupersessionError> {
    let pieces = built(re, name)?
        .split(text, maxsplit)
        .map_err(|e| SupersessionError::Call(name, e))?;
    Ok(pieces.into_iter().flatten().collect())
}

/// `match`, `fullmatch` or `search` found something.
fn hit(
    re: &'static Built,
    name: &'static str,
    text: &[u32],
    how: fn(&Regex, &[u32]) -> Result<Option<Match>, CallError>,
) -> Result<bool, SupersessionError> {
    how(built(re, name)?, text)
        .map(|m| m.is_some())
        .map_err(|e| SupersessionError::Call(name, ReplaceError::Call(e)))
}

fn search(re: &Regex, text: &[u32]) -> Result<Option<Match>, CallError> {
    re.search(text, 0, isize::MAX)
}

fn fullmatch(re: &Regex, text: &[u32]) -> Result<Option<Match>, CallError> {
    re.fullmatch(text, 0, isize::MAX)
}

fn match_(re: &Regex, text: &[u32]) -> Result<Option<Match>, CallError> {
    re.r#match(text, 0, isize::MAX)
}

/// The text of a pattern's ASCII-only match.
fn ascii(text: &[u32]) -> String {
    text.iter()
        .map(|&c| char::from_u32(c).unwrap_or(char::REPLACEMENT_CHARACTER))
        .collect()
}

/// `text.strip(chars)` for a set of ASCII characters.
fn strip<'a>(text: &'a [u32], chars: &str) -> &'a [u32] {
    let drop = |c: &u32| u8::try_from(*c).is_ok_and(|b| chars.as_bytes().contains(&b));
    let start = text.iter().position(|c| !drop(c)).unwrap_or(text.len());
    let end = text.iter().rposition(|c| !drop(c)).map_or(start, |i| i + 1);
    &text[start..end.max(start)]
}

/// `str.split()`: the runs between whitespace as `str.isspace` reads it.
fn split_whitespace(text: &[u32]) -> Vec<&[u32]> {
    text.split(|&c| unicode::is_space(c))
        .filter(|w| !w.is_empty())
        .collect()
}

/// `_numeric_token_set(body)`: the digit-bearing tokens of `body.lower()`
/// (`_NUMERIC_TOKEN_RE.findall`), each stripped of `.-_` at both ends,
/// at most 16 long and holding an ASCII digit; nothing when `body` holds
/// no ASCII digit.
pub fn numeric_token_set(body: &[u32]) -> Result<BTreeSet<String>, SupersessionError> {
    let mut out = BTreeSet::new();
    if !hit(&ASCII_DIGIT_RE, "_ASCII_DIGIT_RE", body, search)? {
        return Ok(out);
    }
    for tok in findall(
        &NUMERIC_TOKEN_RE,
        "_NUMERIC_TOKEN_RE",
        &unicode::lower(body),
    )? {
        let tok = strip(&tok, ".-_");
        if !tok.is_empty()
            && tok.len() <= 16
            && hit(&ASCII_DIGIT_RE, "_ASCII_DIGIT_RE", tok, search)?
        {
            out.insert(ascii(tok));
        }
    }
    Ok(out)
}

/// `change_cues(body)`: group 1 of each `_CUE_RE` match over
/// `body.lower()`, in order.
pub fn change_cues(body: &[u32]) -> Result<Vec<PyStr>, SupersessionError> {
    let low = unicode::lower(body);
    Ok(finditer(&CUE_RE, "_CUE_RE", &low)?
        .iter()
        .filter_map(|m| m.group(1))
        .map(|(a, b)| low[a as usize..b as usize].to_vec())
        .collect())
}

/// `_WORD_TOKEN_ENTRIES`.
pub const WORD_TOKEN_ENTRIES: usize = 65_536;

/// `_WORD_TOKENS`: a word's content tokens by the word as given.
type WordTokens = HashMap<PyStr, Arc<[String]>>;

static WORD_TOKENS: LazyLock<Mutex<WordTokens>> = LazyLock::new(|| Mutex::new(HashMap::new()));
static REGISTERED: Once = Once::new();

/// The cache, registered with [`caches`] on its first use (the
/// reference's `@_caches.register` at import).
fn word_cache() -> MutexGuard<'static, WordTokens> {
    REGISTERED.call_once(|| {
        caches::register(
            "supersession.clear_word_token_cache",
            clear_word_token_cache,
        );
    });
    WORD_TOKENS
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// `clear_word_token_cache()`.
pub fn clear_word_token_cache() {
    WORD_TOKENS
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .clear();
}

/// How many words the cache holds (for the tests).
pub fn word_token_cache_len() -> usize {
    word_cache().len()
}

/// `_word_tokens(word)`: `tokenize(word)` less the stopwords, from the
/// cache or computed and kept, the cache emptied whole first when it
/// holds `WORD_TOKEN_ENTRIES` words.
pub fn word_tokens(word: &[u32]) -> Arc<[String]> {
    let mut cache = word_cache();
    if let Some(found) = cache.get(word) {
        return Arc::clone(found);
    }
    let found: Arc<[String]> = tokenize(word)
        .into_iter()
        .filter(|t| !is_stopword(t))
        .collect();
    if cache.len() >= WORD_TOKEN_ENTRIES {
        cache.clear();
    }
    cache.insert(word.to_vec(), Arc::clone(&found));
    found
}

/// `_first_content_token(word)`.
pub fn first_content_token(word: &[u32]) -> Option<String> {
    word_tokens(word).first().cloned()
}

/// `anchored_values(body)`: per `_CUE_RE` match over `body.lower()`, the
/// first content token among the words (`_WORD_RE`) of the rest of its
/// clause that fill the cue's slot: the first three for a cue that takes
/// its value directly, else the three after the first of
/// [`VALUE_PREPS`] among the first three (none: no slot).
pub fn anchored_values(body: &[u32]) -> Result<BTreeSet<String>, SupersessionError> {
    let mut out = BTreeSet::new();
    let low = unicode::lower(body);
    for m in finditer(&CUE_RE, "_CUE_RE", &low)? {
        let (Some((a, b)), Some((_, end))) = (m.group(1), m.span(0)) else {
            continue;
        };
        let cue = &low[a as usize..b as usize];
        let direct = CHANGE_CUES
            .iter()
            .find(|(c, _)| unicode::cps(c) == cue)
            .map(|(_, direct)| *direct)
            .ok_or_else(|| SupersessionError::Cue(cue.to_vec()))?;
        let rest = &low[end as usize..];
        let clause = split(&CLAUSE_RE, "_CLAUSE_RE", rest, 1)?
            .into_iter()
            .next()
            .unwrap_or_default();
        let words = findall(&WORD_RE, "_WORD_RE", &clause)?;
        let head = &words[..words.len().min(3)];
        let candidates = if direct {
            head
        } else {
            let prep = head.iter().position(|w| {
                let w = ascii(w);
                VALUE_PREPS.contains(&w.as_str())
            });
            let Some(idx) = prep else {
                continue;
            };
            &words[idx + 1..words.len().min(idx + 4)]
        };
        for w in candidates {
            if let Some(tok) = first_content_token(w).filter(|t| !t.is_empty()) {
                out.insert(tok);
                break;
            }
        }
    }
    Ok(out)
}

/// `_proper_nouns(body)`: per clause (`_CLAUSE_RE.split`), each word of
/// `clause.split()` but the first, stripped of `(),'"` and backticks,
/// whose whole `_PROPER_NOUN_RE.match`es: its content tokens.
pub fn proper_nouns(body: &[u32]) -> Result<BTreeSet<String>, SupersessionError> {
    let mut out = BTreeSet::new();
    for clause in split(&CLAUSE_RE, "_CLAUSE_RE", body, 0)? {
        for word in split_whitespace(&clause).into_iter().skip(1) {
            let bare = strip(word, "(),'\"`");
            if hit(&PROPER_NOUN_RE, "_PROPER_NOUN_RE", bare, match_)? {
                out.extend(word_tokens(bare).iter().cloned());
            }
        }
    }
    Ok(out)
}

/// `value_tokens(body, raw_tokens)`: the numeric tokens, the compounds
/// (a `-` and more than two characters) and the proper nouns, each among
/// `raw_tokens`.
pub fn value_tokens(
    body: &[u32],
    raw_tokens: &BTreeSet<String>,
) -> Result<BTreeSet<String>, SupersessionError> {
    let mut values: BTreeSet<String> = numeric_token_set(body)?
        .intersection(raw_tokens)
        .cloned()
        .collect();
    values.extend(
        raw_tokens
            .iter()
            .filter(|t| t.contains('-') && t.chars().count() > 2)
            .cloned(),
    );
    values.extend(proper_nouns(body)?.intersection(raw_tokens).cloned());
    Ok(values)
}

/// `anchor_tokens(body, raw_tokens)`: the value tokens that hold a digit
/// (`str.isdigit`) or no `-`, and the compounds of three or more parts.
pub fn anchor_tokens(
    body: &[u32],
    raw_tokens: &BTreeSet<String>,
) -> Result<BTreeSet<String>, SupersessionError> {
    Ok(value_tokens(body, raw_tokens)?
        .into_iter()
        .filter(|tok| {
            tok.chars().any(|c| unicode::is_digit(c as u32))
                || !tok.contains('-')
                || kebab_parts(tok).len() >= 3
        })
        .collect())
}

/// `_numeric_shape(token)`: `int:<len>` for a run of `\d` (Unicode
/// decimal digits), `dotted:<dots>` for dotted runs, else None.
pub fn numeric_shape(token: &[u32]) -> Result<Option<String>, SupersessionError> {
    if hit(&INTEGER_RE, "_INTEGER_RE", token, fullmatch)? {
        return Ok(Some(format!("int:{}", token.len())));
    }
    if hit(&DOTTED_RE, "_DOTTED_RE", token, fullmatch)? {
        let dots = token.iter().filter(|&&c| c == u32::from(b'.')).count();
        return Ok(Some(format!("dotted:{dots}")));
    }
    Ok(None)
}

/// `values_are_kin(a, b)`: two different values of one numeric shape, or
/// two compounds sharing at least half the shorter one's parts (a
/// stopword part of `a` not counted).
pub fn values_are_kin(a: &str, b: &str) -> Result<bool, SupersessionError> {
    if a == b {
        return Ok(false);
    }
    let shape_a = numeric_shape(&unicode::cps(a))?;
    let shape_b = numeric_shape(&unicode::cps(b))?;
    if shape_a.is_some() || shape_b.is_some() {
        return Ok(shape_a.is_some() && shape_a == shape_b);
    }
    let (parts_a, parts_b) = (kebab_parts(a), kebab_parts(b));
    if parts_a.len() < 2 || parts_b.len() < 2 {
        return Ok(false);
    }
    let in_b: HashSet<&String> = parts_b.iter().collect();
    let shared: HashSet<&String> = parts_a
        .iter()
        .filter(|p| !is_stopword(p) && in_b.contains(p))
        .collect();
    let least = parts_a.len().min(parts_b.len());
    Ok(shared.len() as f64 >= (0.5 * least as f64).ceil())
}

/// `_content_stream(body)`: the tokens, stopwords left out.
pub fn content_stream(body: &[u32]) -> Vec<String> {
    tokenize(body)
        .into_iter()
        .filter(|t| !is_stopword(t))
        .collect()
}

/// `_CUE_TOKENS`: the tokens of every cue phrase and [`EXTRA_CUE_TOKENS`].
pub fn cue_tokens() -> &'static BTreeSet<String> {
    static TOKENS: OnceLock<BTreeSet<String>> = OnceLock::new();
    TOKENS.get_or_init(|| {
        CHANGE_CUES
            .iter()
            .flat_map(|(cue, _)| tokenize_str(cue))
            .chain(EXTRA_CUE_TOKENS.iter().map(|t| (*t).to_string()))
            .collect()
    })
}

/// `_context(stream, value)`: the tokens within `CONTEXT_WIDTH` of each
/// occurrence of `value`, less `value` and the cue tokens.
pub fn context(stream: &[String], value: &str) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for (i, tok) in stream.iter().enumerate() {
        if tok == value {
            out.extend(stream[i.saturating_sub(CONTEXT_WIDTH)..i].iter().cloned());
            let end = stream.len().min(i + 1 + CONTEXT_WIDTH);
            out.extend(stream[i + 1..end].iter().cloned());
        }
    }
    out.remove(value);
    let cues = cue_tokens();
    out.retain(|t| !cues.contains(t));
    out
}

/// `_surface_forms(body)`: each content token of a word (`_WORD_RE`) with
/// the first word that gave it, stripped of `.,;:()`, in first-seen order.
pub fn surface_forms(body: &[u32]) -> Result<Vec<(String, PyStr)>, SupersessionError> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for word in findall(&WORD_RE, "_WORD_RE", body)? {
        if let Some(tok) = first_content_token(&word) {
            if seen.insert(tok.clone()) {
                out.push((tok, strip(&word, ".,;:()").to_vec()));
            }
        }
    }
    Ok(out)
}

/// `_surface_form(body, token)`: the first word giving `token`, stripped.
pub fn surface_form(body: &[u32], token: &str) -> Result<Option<PyStr>, SupersessionError> {
    for word in findall(&WORD_RE, "_WORD_RE", body)? {
        if first_content_token(&word).as_deref() == Some(token) {
            return Ok(Some(strip(&word, ".,;:()").to_vec()));
        }
    }
    Ok(None)
}
