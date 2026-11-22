//! `search._memory_tokens` and its cache: each candidate's token streams,
//! built once per distinct body and scopes and held in a bounded LRU, the
//! reference's `_TOKEN_CACHE` made a value the caller holds.
//!
//! The reference's `OrderedDict` with `move_to_end` on a hit and
//! `popitem(last=False)` past the bound is [`Lru`], which the snippet memos
//! share. Each look-up-and-touch and each insert-and-evict holds the lock;
//! the tokenizing runs outside it, as in the reference.

use std::borrow::Borrow;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::hash::Hash;
use std::sync::{Arc, Mutex, MutexGuard};

use crate::lexicon::strip_stopwords;
use crate::pyvalue::PyStr;

use super::{body_stream, scope_tokens};

/// `TOKEN_CACHE_ENTRIES`.
pub const TOKEN_CACHE_ENTRIES: usize = 5000;

/// An `OrderedDict` used as an LRU: a hit moves its key to the end, an
/// insert of a new key goes at the end, an insert of a held key keeps its
/// place and takes the new value (`d[k] = v`), and past `entries` the
/// front goes (`popitem(last=False)`).
#[derive(Debug)]
pub struct Lru<K, V> {
    entries: usize,
    map: HashMap<K, (u64, V)>,
    order: BTreeMap<u64, K>,
    next: u64,
}

impl<K: Hash + Eq + Clone, V: Clone> Lru<K, V> {
    pub fn new(entries: usize) -> Lru<K, V> {
        Lru {
            entries,
            map: HashMap::new(),
            order: BTreeMap::new(),
            next: 0,
        }
    }

    /// `d.get(k)`, and on a hit `d.move_to_end(k)`.
    pub fn get<Q: Hash + Eq + ?Sized>(&mut self, key: &Q) -> Option<V>
    where
        K: Borrow<Q>,
    {
        let (seq, value) = self.map.get_mut(key)?;
        let k = self.order.remove(seq)?;
        *seq = self.next;
        self.order.insert(self.next, k);
        self.next += 1;
        Some(value.clone())
    }

    /// `key in d`, and on a hit `d.move_to_end(k)`.
    pub fn touch<Q: Hash + Eq + ?Sized>(&mut self, key: &Q) -> bool
    where
        K: Borrow<Q>,
    {
        self.get(key).is_some()
    }

    /// `d[k] = v`, then evict past the bound.
    pub fn put(&mut self, key: K, value: V) {
        if let Some((_, held)) = self.map.get_mut(&key) {
            *held = value;
        } else {
            self.order.insert(self.next, key.clone());
            self.map.insert(key, (self.next, value));
            self.next += 1;
        }
        while self.map.len() > self.entries {
            let Some((_, oldest)) = self.order.pop_first() else {
                break;
            };
            self.map.remove(&oldest);
        }
    }

    /// `d.clear()`.
    pub fn clear(&mut self) {
        self.map.clear();
        self.order.clear();
    }

    /// The keys held, least recently used first.
    pub fn keys(&self) -> Vec<K> {
        self.order.values().cloned().collect()
    }
}

pub(crate) fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// `_MemoryTokens`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemoryTokens {
    pub body: Vec<String>,
    /// `_strip_stopwords(body)`.
    pub content: Vec<String>,
    pub scope_set: BTreeSet<String>,
    /// `term_counts(body)`: each token and its count, in the order of first
    /// occurrence (the dict's order).
    pub body_tf: Vec<(String, i64)>,
}

/// `search_bm25.term_counts`.
pub fn term_counts(tokens: &[String]) -> Vec<(String, i64)> {
    let mut at: HashMap<&str, usize> = HashMap::new();
    let mut out: Vec<(String, i64)> = Vec::new();
    for t in tokens {
        match at.get(t.as_str()) {
            Some(&i) => out[i].1 += 1,
            None => {
                at.insert(t, out.len());
                out.push((t.clone(), 1));
            }
        }
    }
    out
}

fn build(body: Vec<String>, scopes: &[String]) -> MemoryTokens {
    let scope_set = scopes.iter().flat_map(|s| scope_tokens(s)).collect();
    MemoryTokens {
        content: strip_stopwords(&body),
        body_tf: term_counts(&body),
        scope_set,
        body,
    }
}

type Key = (PyStr, Vec<String>);

/// `_TOKEN_CACHE` with `clear_token_cache`, `_memory_tokens` and
/// `seed_memory_tokens`. A memory is its `(body, scopes)`.
#[derive(Debug)]
pub struct TokenCache {
    lru: Mutex<Lru<Key, Arc<MemoryTokens>>>,
}

impl Default for TokenCache {
    fn default() -> Self {
        TokenCache::with_entries(TOKEN_CACHE_ENTRIES)
    }
}

impl TokenCache {
    pub fn with_entries(entries: usize) -> TokenCache {
        TokenCache {
            lru: Mutex::new(Lru::new(entries)),
        }
    }

    /// `clear_token_cache`.
    pub fn clear(&self) {
        lock(&self.lru).clear();
    }

    /// The keys held, least recently used first.
    pub fn keys(&self) -> Vec<(PyStr, Vec<String>)> {
        lock(&self.lru).keys()
    }

    /// `_memory_tokens(memory)`.
    pub fn memory_tokens(&self, body: &[u32], scopes: &[String]) -> Arc<MemoryTokens> {
        let key = (body.to_vec(), scopes.to_vec());
        if let Some(held) = lock(&self.lru).get(&key) {
            return held;
        }
        let tokens = Arc::new(build(body_stream(body), scopes));
        lock(&self.lru).put(key, tokens.clone());
        tokens
    }

    /// `seed_memory_tokens(memory, index_text)`: the body stream is
    /// `index_text.split(" ")` (none for an empty text); a held entry stands.
    pub fn seed_memory_tokens(&self, body: &[u32], scopes: &[String], index_text: &str) {
        let key = (body.to_vec(), scopes.to_vec());
        if lock(&self.lru).touch(&key) {
            return;
        }
        let stream: Vec<String> = if index_text.is_empty() {
            Vec::new()
        } else {
            index_text.split(' ').map(str::to_owned).collect()
        };
        let tokens = Arc::new(build(stream, scopes));
        lock(&self.lru).put(key, tokens);
    }
}
