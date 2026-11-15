//! The rescue lane's word tables (`expansion.py`), stemmed into ranker
//! token space once, as `search_bm25._EXPANSION_TABLES` is, and the
//! synthesized terms of a query (`expansion_terms`).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::OnceLock;

use crate::lexicon::stem_token;

/// `QUERY_FILLER_WORDS`.
pub const QUERY_FILLER_WORDS: &[&str] = &[
    "actually",
    "anyway",
    "anyways",
    "apparently",
    "basically",
    "certainly",
    "definitely",
    "ever",
    "exactly",
    "forever",
    "forget",
    "forgot",
    "forgotten",
    "guess",
    "guessing",
    "honestly",
    "just",
    "kinda",
    "knew",
    "know",
    "knows",
    "literally",
    "maybe",
    "perhaps",
    "probably",
    "properly",
    "really",
    "remember",
    "remembered",
    "remembers",
    "seriously",
    "somehow",
    "sorta",
    "stuff",
    "supposed",
    "supposedly",
    "sure",
    "thing",
    "things",
    "think",
    "thinking",
    "thought",
    "vaguely",
    "want",
    "wanted",
    "whatever",
    "whole",
    "wonder",
    "wondered",
    "wondering",
];

/// `IRREGULAR_PAST`.
pub const IRREGULAR_PAST: &[(&str, &[&str])] = &[
    ("brought", &["bring"]),
    ("broken", &["break"]),
    ("built", &["build"]),
    ("caught", &["catch"]),
    ("chosen", &["choose"]),
    ("held", &["hold"]),
    ("kept", &["keep"]),
    ("made", &["make"]),
    ("paid", &["pay"]),
    ("ran", &["run"]),
    ("sent", &["send"]),
    ("taken", &["take"]),
    ("written", &["write"]),
];

/// `CLIPPINGS`.
pub const CLIPPINGS: &[(&str, &[&str])] = &[
    ("auth", &["authentication"]),
    ("config", &["configuration"]),
    ("configs", &["configuration"]),
    ("cred", &["credential"]),
    ("creds", &["credential"]),
    ("db", &["database"]),
    ("dbs", &["database"]),
    ("dep", &["dependency"]),
    ("deps", &["dependency"]),
    ("dev", &["development"]),
    ("docs", &["documentation"]),
    ("env", &["environment"]),
    ("envs", &["environment"]),
    ("info", &["information"]),
    ("k8s", &["kubernetes"]),
    ("lib", &["library"]),
    ("libs", &["library"]),
    ("perf", &["performance"]),
    ("pr", &["pull", "request"]),
    ("prs", &["pull", "request"]),
    ("prod", &["production"]),
    ("repo", &["repository"]),
    ("repos", &["repository"]),
    ("spec", &["specification"]),
    ("specs", &["specification"]),
];

/// `SYNONYM_GROUPS`.
pub const SYNONYM_GROUPS: &[&[&str]] = &[
    &["toggle", "toggles", "flag", "flags"],
    &["rip", "remove", "removal"],
    &["hash", "hashes", "digest", "sha256", "checksum"],
    &[
        "cred",
        "creds",
        "credential",
        "credentials",
        "secret",
        "secrets",
    ],
    &[
        "library",
        "libraries",
        "dependency",
        "dependencies",
        "package",
        "packages",
    ],
    &["bar", "criteria", "approval", "policy"],
    &["undo", "rollback", "revert"],
    &["dump", "dumps", "backup", "backups", "restore"],
    &["staged", "phased", "stage", "phase", "phases"],
    &["old", "legacy"],
    &[
        "cutover",
        "switchover",
        "switching",
        "decommission",
        "shutdown",
    ],
];

/// `_MIN_EXPANSION_LEN`, in code points.
const MIN_EXPANSION_LEN: usize = 3;

/// `ExpansionTables`, stemmed.
pub struct Tables {
    pub filler_stems: HashSet<String>,
    pub irregular: HashMap<String, Vec<String>>,
    pub clippings: HashMap<String, Vec<String>>,
    pub synonyms: HashMap<String, BTreeSet<String>>,
}

fn stemmed_map(table: &[(&str, &[&str])]) -> HashMap<String, Vec<String>> {
    let mut out = HashMap::new();
    for (k, vs) in table {
        let mut values: Vec<String> = Vec::new();
        for v in vs.iter().map(|v| stem_token(v)) {
            if !values.contains(&v) {
                values.push(v);
            }
        }
        // A later key that stems the same replaces the earlier, as the
        // dict comprehension does.
        out.insert(stem_token(k), values);
    }
    out
}

/// `build_tables(_stem_token)`, once.
pub fn tables() -> &'static Tables {
    static TABLES: OnceLock<Tables> = OnceLock::new();
    TABLES.get_or_init(|| {
        let mut synonyms: HashMap<String, BTreeSet<String>> = HashMap::new();
        for group in SYNONYM_GROUPS {
            let stems: BTreeSet<String> = group.iter().map(|w| stem_token(w)).collect();
            for s in &stems {
                let entry = synonyms.entry(s.clone()).or_default();
                entry.extend(stems.iter().filter(|o| *o != s).cloned());
            }
        }
        Tables {
            filler_stems: QUERY_FILLER_WORDS.iter().map(|w| stem_token(w)).collect(),
            irregular: stemmed_map(IRREGULAR_PAST),
            clippings: stemmed_map(CLIPPINGS),
            synonyms,
        }
    })
}

fn cp_len(s: &str) -> usize {
    s.chars().count()
}

/// `s[:-n]` in code points.
fn drop_last(s: &str, n: usize) -> &str {
    let mut end = s.len();
    for _ in 0..n {
        end = s[..end].char_indices().next_back().map_or(0, |(i, _)| i);
    }
    &s[..end]
}

/// `morph_variants(tok, stem)`.
pub fn morph_variants(tok: &str) -> BTreeSet<String> {
    let mut out: BTreeSet<String> = BTreeSet::new();
    let n = cp_len(tok);
    let bases_of = |base: &str| {
        let mut bases = vec![base.to_owned(), format!("{base}e")];
        let mut it = base.chars().rev();
        if cp_len(base) >= 3 && it.next() == it.next() {
            bases.push(drop_last(base, 1).to_owned());
        }
        bases
    };
    if tok.ends_with("ing") && n > 5 {
        for b in bases_of(drop_last(tok, 3)) {
            let v = if b.ends_with('e') {
                format!("{b}d")
            } else {
                format!("{b}ed")
            };
            out.insert(b);
            out.insert(v);
        }
    } else if tok.ends_with("ed") && n > 4 {
        for b in bases_of(drop_last(tok, 2)) {
            let stem_b = if b.ends_with('e') {
                drop_last(&b, 1)
            } else {
                &b
            };
            let v = format!("{stem_b}ing");
            out.insert(b);
            out.insert(v);
        }
    }
    out.into_iter()
        .filter(|v| cp_len(v) >= MIN_EXPANSION_LEN)
        .map(|v| stem_token(&v))
        .filter(|v| v != tok)
        .collect()
}

/// `expansion_terms(query_tokens, tables, stem)`: sorted by code point.
pub fn expansion_terms(query_tokens: &[String]) -> Vec<String> {
    let t = tables();
    let mut exp: BTreeSet<String> = BTreeSet::new();
    for tok in query_tokens {
        exp.extend(morph_variants(tok));
        for table in [&t.irregular, &t.clippings] {
            if let Some(vs) = table.get(tok) {
                exp.extend(vs.iter().cloned());
            }
        }
        if let Some(vs) = t.synonyms.get(tok) {
            exp.extend(vs.iter().cloned());
        }
    }
    exp.into_iter()
        .filter(|e| !query_tokens.contains(e) && !t.filler_stems.contains(e))
        .filter(|e| cp_len(e) >= MIN_EXPANSION_LEN)
        .collect()
}
