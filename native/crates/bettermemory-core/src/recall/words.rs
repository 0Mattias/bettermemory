//! `recall.WordsSeen`, `_fold` and `_bounded` (recall.py 291-368).

use std::collections::VecDeque;
use std::sync::{Mutex, MutexGuard};

use super::{MIN_QUOTE_CHARS, WORDS_CHECKOUTS, WORDS_PER_CHECKOUT};
use crate::control_tokens::defang_control_tokens;
use crate::pyvalue::PyStr;
use crate::unicode;

type Key = (PyStr, PyStr);

/// `WordsSeen`: per (store path, checkout), the last `WORDS_PER_CHECKOUT`
/// prompts, the oldest key dropped past `WORDS_CHECKOUTS`. The store path
/// is the text `str(store_path)` gives, which the caller passes.
#[derive(Debug, Default)]
pub struct WordsSeen {
    /// Keys in the `OrderedDict`'s order, the least recent first.
    words: Mutex<Vec<(Key, VecDeque<PyStr>)>>,
}

impl WordsSeen {
    /// An empty table.
    pub fn new() -> WordsSeen {
        WordsSeen::default()
    }

    fn lock(&self) -> MutexGuard<'_, Vec<(Key, VecDeque<PyStr>)>> {
        self.words.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `remember(store_path, checkout, words)`.
    pub fn remember(&self, store_path: &[u32], checkout: &[u32], words: &[u32]) {
        let key = (store_path.to_vec(), checkout.to_vec());
        let mut held = self.lock();
        match held.iter().position(|(k, _)| *k == key) {
            None => {
                held.push((key, VecDeque::with_capacity(WORDS_PER_CHECKOUT)));
                while held.len() > WORDS_CHECKOUTS {
                    held.remove(0);
                }
            }
            Some(at) => {
                let entry = held.remove(at);
                held.push(entry);
            }
        }
        let deque = &mut held.last_mut().expect("the key was just placed last").1;
        if deque.len() == WORDS_PER_CHECKOUT {
            deque.pop_front();
        }
        deque.push_back(words.to_vec());
    }

    /// `seen(store_path, checkout)`.
    pub fn seen(&self, store_path: &[u32], checkout: &[u32]) -> bool {
        self.lock()
            .iter()
            .any(|((s, c), _)| s == store_path && c == checkout)
    }

    /// `holds(store_path, checkout, quote)`.
    pub fn holds(&self, store_path: &[u32], checkout: &[u32], quote: &[u32]) -> bool {
        let wanted = fold(quote);
        let named = fold(&defang_control_tokens(quote));
        if wanted.len() < MIN_QUOTE_CHARS {
            return false;
        }
        self.held(store_path, checkout).iter().any(|text| {
            bounded(&wanted, &fold(text)) == Some(true)
                || (named.len() >= MIN_QUOTE_CHARS
                    && bounded(&named, &fold(&defang_control_tokens(text))) == Some(true))
        })
    }

    /// `clear()`.
    pub fn clear(&self) {
        self.lock().clear();
    }

    /// The prompts held for a key, oldest first (for tests).
    pub fn held(&self, store_path: &[u32], checkout: &[u32]) -> Vec<PyStr> {
        self.lock()
            .iter()
            .find(|((s, c), _)| s == store_path && c == checkout)
            .map(|(_, d)| d.iter().cloned().collect())
            .unwrap_or_default()
    }
}

/// `" ".join(text.split())`, `str.split` reading `str.isspace`.
pub fn join_words(text: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    for word in text
        .split(|&c| unicode::is_space(c))
        .filter(|w| !w.is_empty())
    {
        if !out.is_empty() {
            out.push(0x20);
        }
        out.extend_from_slice(word);
    }
    out
}

/// `_fold(text)`: whitespace folded, then case folded.
pub fn fold(text: &[u32]) -> PyStr {
    unicode::casefold(&join_words(text))
}

/// Every index where `wanted` (not empty) starts in `text`, overlapping
/// ones included, in order: the answers `text.find(wanted, start + 1)`
/// walks through, found by one Knuth-Morris-Pratt pass.
pub(crate) fn occurrences(wanted: &[u32], text: &[u32]) -> Vec<usize> {
    let m = wanted.len();
    let mut fail = vec![0usize; m];
    let mut k = 0;
    for i in 1..m {
        while k > 0 && wanted[i] != wanted[k] {
            k = fail[k - 1];
        }
        if wanted[i] == wanted[k] {
            k += 1;
        }
        fail[i] = k;
    }
    let mut out = Vec::new();
    let mut k = 0;
    for (i, &c) in text.iter().enumerate() {
        while k > 0 && c != wanted[k] {
            k = fail[k - 1];
        }
        if c == wanted[k] {
            k += 1;
        }
        if k == m {
            out.push(i + 1 - m);
            k = fail[k - 1];
        }
    }
    out
}

/// `_bounded(wanted, text)`. `None` where the reference raises
/// `IndexError`: an empty `wanted` against a text whose first character
/// is alphanumeric (`wanted[-1]` is read). The reference calls it only
/// with a quote of at least `MIN_QUOTE_CHARS`.
pub fn bounded(wanted: &[u32], text: &[u32]) -> Option<bool> {
    if wanted.is_empty() {
        // find answers 0; `before` holds at 0, and `after` reads
        // `wanted[-1]` only when `text[0]` is alphanumeric.
        return match text.first() {
            None => Some(true),
            Some(&c) if unicode::is_alnum(c) => None,
            Some(_) => Some(true),
        };
    }
    let (first, last) = (wanted[0], wanted[wanted.len() - 1]);
    for start in occurrences(wanted, text) {
        let end = start + wanted.len();
        let before =
            start == 0 || !(unicode::is_alnum(text[start - 1]) && unicode::is_alnum(first));
        let after = end == text.len() || !(unicode::is_alnum(text[end]) && unicode::is_alnum(last));
        if before && after {
            return Some(true);
        }
    }
    Some(false)
}
