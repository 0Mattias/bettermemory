//! `search._query_biased_snippet` and its two memos (`_snippet_tokens`,
//! `_token_surfaces`), and `top_hit_leads_runner_up`, the score half of the
//! `expand_top` gate.
//!
//! [`SnippetMemo`] is the reference's `_SNIPPET_TOKENS` and
//! `_TOKEN_SURFACES` made a value the caller holds, with their bounds and
//! eviction order. The window arithmetic is the reference's, signed.

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use crate::lexicon::{
    alias_anchor_starts, expand_kebab, kebab_parts, token_spans, tokenize, ALIAS_ANCHORS,
};
use crate::models_text::{snippet_for, snippet_window, strip};
use crate::pyvalue::PyStr;

use super::token_cache::{lock, Lru};

/// `_SNIPPET_SCAN_CHARS`.
pub const SNIPPET_SCAN_CHARS: usize = 8000;
/// `_SNIPPET_LEAD_CHARS`.
pub const SNIPPET_LEAD_CHARS: i64 = 40;
/// `SNIPPET_TOKEN_ENTRIES`.
pub const SNIPPET_TOKEN_ENTRIES: usize = 5000;
/// `TOKEN_SURFACE_ENTRIES`.
pub const TOKEN_SURFACE_ENTRIES: usize = 20_000;

/// `EXPAND_TOP_SCORE_MARGIN`, `(k + 3) / (k + 1) - 1.0` at `k` 60: the
/// quotient of two exact integers, correctly rounded, as `int / int` is.
pub const EXPAND_TOP_SCORE_MARGIN: f64 = 63.0 / 61.0 - 1.0;

/// `top_hit_leads_runner_up(top_score, next_score)`.
pub fn top_hit_leads_runner_up(top_score: f64, next_score: f64) -> bool {
    if next_score <= 0.0 {
        return top_score > 0.0;
    }
    top_score >= next_score * (1.0 + EXPAND_TOP_SCORE_MARGIN)
}

/// A raw token's surfaces, `frozenset(_expand_kebab(tokenize(raw)))`.
pub type Surfaces = Arc<BTreeSet<String>>;

/// `_SnippetTokens`: where each `_TOKEN_RE` match of the scan starts, and
/// its surfaces, index-aligned.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SnippetTokens {
    pub starts: Vec<usize>,
    pub surfaces: Vec<Surfaces>,
}

/// `_SNIPPET_TOKENS` and `_TOKEN_SURFACES`.
#[derive(Debug)]
pub struct SnippetMemo {
    tokens: Mutex<Lru<PyStr, Arc<SnippetTokens>>>,
    surfaces: Mutex<Lru<String, Surfaces>>,
}

impl Default for SnippetMemo {
    fn default() -> Self {
        SnippetMemo::with_entries(SNIPPET_TOKEN_ENTRIES, TOKEN_SURFACE_ENTRIES)
    }
}

impl SnippetMemo {
    pub fn with_entries(token_entries: usize, surface_entries: usize) -> SnippetMemo {
        SnippetMemo {
            tokens: Mutex::new(Lru::new(token_entries)),
            surfaces: Mutex::new(Lru::new(surface_entries)),
        }
    }

    /// `clear_snippet_caches`.
    pub fn clear(&self) {
        lock(&self.tokens).clear();
        lock(&self.surfaces).clear();
    }

    /// The scans held, least recently used first.
    pub fn scan_keys(&self) -> Vec<PyStr> {
        lock(&self.tokens).keys()
    }

    /// The raw tokens held, least recently used first.
    pub fn surface_keys(&self) -> Vec<String> {
        lock(&self.surfaces).keys()
    }

    /// `_token_surfaces(raw)`.
    pub fn token_surfaces(&self, raw: &str) -> Surfaces {
        if let Some(held) = lock(&self.surfaces).get(raw) {
            return held;
        }
        let cps: Vec<u32> = raw.chars().map(u32::from).collect();
        let surfaces: Surfaces = Arc::new(expand_kebab(&tokenize(&cps)).into_iter().collect());
        lock(&self.surfaces).put(raw.to_owned(), surfaces.clone());
        surfaces
    }

    /// `_snippet_tokens(scan)`.
    pub fn snippet_tokens(&self, scan: &[u32]) -> Arc<SnippetTokens> {
        if let Some(held) = lock(&self.tokens).get(scan) {
            return held;
        }
        let mut starts = Vec::new();
        let mut surfaces = Vec::new();
        for (s, e) in token_spans(scan) {
            // A token is `\w` and `-` or `.`: no surrogate.
            let raw: String = scan[s..e]
                .iter()
                .filter_map(|&c| char::from_u32(c))
                .collect();
            starts.push(s);
            surfaces.push(self.token_surfaces(&raw));
        }
        let tokens = Arc::new(SnippetTokens { starts, surfaces });
        lock(&self.tokens).put(scan.to_vec(), tokens.clone());
        tokens
    }

    /// `_query_biased_snippet(body, matched, max_chars)`.
    pub fn query_biased_snippet(&self, body: &[u32], matched: &[String], max_chars: i64) -> PyStr {
        let text = strip(body);
        if text.len() as i128 <= i128::from(max_chars) || matched.is_empty() {
            return snippet_for(text, max_chars);
        }
        let primary_terms: BTreeSet<&str> = matched.iter().map(String::as_str).collect();
        let part_terms: BTreeSet<String> = matched
            .iter()
            .flat_map(|t| kebab_parts(t))
            .filter(|p| !primary_terms.contains(p.as_str()))
            .collect();

        let scan = &text[..text.len().min(SNIPPET_SCAN_CHARS)];
        let scanned = self.snippet_tokens(scan);
        let mut starts: Vec<usize> = scanned.starts.clone();
        let mut primary: Vec<usize> = Vec::new();
        let mut secondary: Vec<usize> = Vec::new();
        for (&start, surfaces) in scanned.starts.iter().zip(&scanned.surfaces) {
            if surfaces.iter().any(|s| primary_terms.contains(s.as_str())) {
                primary.push(start);
            } else if surfaces.iter().any(|s| part_terms.contains(s)) {
                secondary.push(start);
            }
        }
        for (literal, guard, alias) in ALIAS_ANCHORS {
            let bucket = if primary_terms.contains(alias) {
                &mut primary
            } else if part_terms.contains(alias) {
                &mut secondary
            } else {
                continue;
            };
            for at in alias_anchor_starts(scan, literal, guard) {
                bucket.push(at);
                starts.push(at);
            }
        }

        primary.sort_unstable();
        secondary.sort_unstable();
        let anchors: Vec<i64> = if primary.is_empty() {
            secondary
        } else {
            primary
        }
        .into_iter()
        .map(|a| a as i64)
        .collect();
        if anchors.is_empty() {
            return snippet_for(text, max_chars);
        }

        let budget = i128::from(max_chars) - 3;
        let reach = budget - i128::from(SNIPPET_LEAD_CHARS);
        starts.sort_unstable();
        starts.dedup();
        let (mut best_at, mut best_cover) = (anchors[0], -1i128);
        for (i, &anchor) in anchors.iter().enumerate() {
            let bound = i128::from(anchor) + reach;
            let cover = anchors.partition_point(|&a| i128::from(a) < bound) as i128 - i as i128;
            if cover > best_cover {
                best_cover = cover;
                best_at = anchor;
            }
        }
        if best_at <= SNIPPET_LEAD_CHARS {
            return snippet_for(text, max_chars);
        }
        let lead = (best_at - SNIPPET_LEAD_CHARS) as usize;
        let start = starts[starts.partition_point(|&s| s < lead)];
        snippet_window(text, start as i64, max_chars)
    }
}
