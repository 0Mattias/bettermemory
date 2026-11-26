//! Corpus-level contradiction candidates (`conflicts.py`), unit
//! conflicts-1 (reference lines 115-731): the queue row's type
//! (`ConflictCandidate` with `to_dict` and `from_dict`), its two hashes,
//! and the pairwise dedup scan with the contradiction guards that keep a
//! disagreement out of the dedup candidates (`_pick_keeper`,
//! `_find_dedup_with_skips`, `find_conflict_candidates`).
//!
//! Texts are code points (`&[u32]`); token sets are the tokenizer's
//! `String`s. Both patterns are the engine's [`Regex`] built once from the
//! reference's own text, called with the method the reference calls. The
//! clock `find_conflict_candidates` reads is its caller's argument.

use std::cmp::Ordering;
use std::collections::{BTreeSet, HashSet};
use std::sync::LazyLock;

use sha2::{Digest, Sha256};

use crate::config::py_float_from_str;
use crate::datetime::DateTime;
use crate::models_text::snippet_for;
use crate::pyre::regex::{Item, Regex, RegexError};
use crate::pyre::replace::ReplaceError;
use crate::pyvalue::{py_str_limited, PyError, PyStr, PyValue};
use crate::records::Memory;
use crate::search::num::round4;
use crate::search_dedup::{pairwise_content_jaccard, raw_content_token_set, TokenSet};
use crate::store::digest::strip_provenance;
use crate::supersession::{numeric_token_set, SupersessionError};
use crate::unicode;

/// `_VALID_STATUSES`.
pub const VALID_STATUSES: [&str; 3] = ["pending", "confirmed", "dismissed"];
/// `_DEFAULT_JACCARD_THRESHOLD`.
pub const DEFAULT_JACCARD_THRESHOLD: f64 = 0.75;
/// `_NEGATION_MARKERS`.
pub const NEGATION_MARKERS: [&str; 20] = [
    "no", "not", "never", "none", "neither", "nor", "without", "cannot", "dont", "doesnt", "didnt",
    "wont", "cant", "isnt", "arent", "wasnt", "werent", "shouldnt", "wouldnt", "couldnt",
];
/// `_NEGATION_TOKEN_RE.pattern`.
pub const NEGATION_TOKEN_PATTERN: &str = "[a-z0-9]+";
/// `_CLAUSE_SPLIT_RE.pattern` (a raw string: `\n` is two characters).
pub const CLAUSE_SPLIT_PATTERN: &str = r"[.;!?\n]+";
/// The summaries' length (`snippet_for(body, max_chars=100)`).
pub const SUMMARY_CHARS: i64 = 100;

type Built = LazyLock<Result<Regex, RegexError>>;

static NEGATION_TOKEN_RE: Built = LazyLock::new(|| Regex::new_str(NEGATION_TOKEN_PATTERN, 0));
static CLAUSE_SPLIT_RE: Built = LazyLock::new(|| Regex::new_str(CLAUSE_SPLIT_PATTERN, 0));

/// What the reference raises, or what the engine could not answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConflictError {
    /// `ConflictingPair(detector)`: `_pick_keeper` refusing a pair that
    /// carries a contradiction signal, before any tier.
    ConflictingPair(&'static str),
    /// TypeError: `updated` ordered naive against aware.
    NaiveAware,
    /// UnicodeEncodeError: a hashed text holding a lone surrogate.
    Encode,
    /// KeyError: a key `from_dict` reads as `raw[key]`.
    Key(&'static str),
    /// What `str()` or `float()` of a value raised in `from_dict`.
    Value(PyError),
    /// The engine did not build the pattern of this name.
    Pattern(&'static str, RegexError),
    /// A call of the pattern of this name answered no value.
    Call(&'static str, ReplaceError),
    /// `_numeric_token_set` answered no value.
    Numeric(SupersessionError),
}

impl ConflictError {
    /// The Python class raised (the engine's failures by their own name).
    pub fn name(&self) -> &'static str {
        match self {
            ConflictError::ConflictingPair(_) => "ConflictingPair",
            ConflictError::NaiveAware => "TypeError",
            ConflictError::Encode => "UnicodeEncodeError",
            ConflictError::Key(_) => "KeyError",
            ConflictError::Value(e) => e.name(),
            ConflictError::Pattern(..) => "Pattern",
            ConflictError::Call(..) => "Call",
            ConflictError::Numeric(_) => "Numeric",
        }
    }
}

fn built(re: &'static Built, name: &'static str) -> Result<&'static Regex, ConflictError> {
    re.as_ref()
        .map_err(|e| ConflictError::Pattern(name, e.clone()))
}

/// The first `digits` hex digits of SHA-256 over the strict UTF-8 of
/// `text`.
fn sha256_hex(text: &[u32], digits: usize) -> Result<String, ConflictError> {
    let utf8 = unicode::to_string(text).ok_or(ConflictError::Encode)?;
    let hex: String = Sha256::digest(utf8.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Ok(hex[..digits].to_string())
}

/// `_body_hash(body)`: 16 hex digits of SHA-256 over the raw body.
pub fn body_hash(body: &[u32]) -> Result<String, ConflictError> {
    sha256_hex(body, 16)
}

/// `_pair_id(a_id, b_id)`: `cf-` and 12 hex digits of SHA-256 over
/// `lo:hi`, the two ids in code point order.
pub fn pair_id(a_id: &[u32], b_id: &[u32]) -> Result<String, ConflictError> {
    let (lo, hi) = if a_id <= b_id {
        (a_id, b_id)
    } else {
        (b_id, a_id)
    };
    let mut text = lo.to_vec();
    text.push(u32::from(b':'));
    text.extend_from_slice(hi);
    Ok(format!("cf-{}", sha256_hex(&text, 12)?))
}

/// `ConflictCandidate`: one queued pair.
#[derive(Clone, Debug, PartialEq)]
pub struct ConflictCandidate {
    pub id: PyStr,
    pub a_id: PyStr,
    pub b_id: PyStr,
    pub summary_a: PyStr,
    pub summary_b: PyStr,
    pub similarity: f64,
    pub method: PyStr,
    pub detector: PyStr,
    pub created: PyStr,
    pub status: PyStr,
    pub verdict_ts: Option<PyStr>,
    pub note: Option<PyStr>,
    pub verdict_hash_a: Option<PyStr>,
    pub verdict_hash_b: Option<PyStr>,
}

fn entry(key: &str, value: PyValue) -> (PyValue, PyValue) {
    (PyValue::str(key), value)
}

/// The value of a dict's `str` key, the last one where a list of pairs
/// repeats it.
fn get<'a>(raw: &'a [(PyValue, PyValue)], key: &str) -> Option<&'a PyValue> {
    let key = unicode::cps(key);
    raw.iter().rev().find_map(|(k, v)| match k {
        PyValue::Str(s) if *s == key => Some(v),
        _ => None,
    })
}

/// `str(value)`.
fn str_of(value: &PyValue) -> Result<PyStr, ConflictError> {
    py_str_limited(value).map_err(ConflictError::Value)
}

/// `str(raw.get(key, default))`.
fn str_or(raw: &[(PyValue, PyValue)], key: &str, default: &str) -> Result<PyStr, ConflictError> {
    get(raw, key).map_or_else(|| Ok(unicode::cps(default)), str_of)
}

/// `str(raw[key])`.
fn str_at(raw: &[(PyValue, PyValue)], key: &'static str) -> Result<PyStr, ConflictError> {
    str_of(get(raw, key).ok_or(ConflictError::Key(key))?)
}

/// `str(raw[key]) if raw.get(key) is not None else None`.
fn str_opt(raw: &[(PyValue, PyValue)], key: &str) -> Result<Option<PyStr>, ConflictError> {
    match get(raw, key) {
        None | Some(PyValue::None) => Ok(None),
        Some(v) => str_of(v).map(Some),
    }
}

/// `float(value)` for a value a dict of the queue holds.
fn py_float(value: &PyValue) -> Result<f64, PyError> {
    match value {
        PyValue::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        PyValue::Int(t) => match t.parse::<f64>() {
            Ok(f) if f.is_finite() => Ok(f),
            _ => Err(PyError::OverflowError),
        },
        PyValue::Float(f) => Ok(*f),
        PyValue::Str(s) => float_from_text(s).ok_or(PyError::ValueError),
        _ => Err(PyError::TypeError),
    }
}

/// `float(text)`: each non-ASCII space read as a space, then Python's
/// ASCII whitespace stripped (`Py_ISSPACE`), then the number.
fn float_from_text(s: &[u32]) -> Option<f64> {
    let mapped: PyStr = s
        .iter()
        .map(|&c| {
            if c > 0x7F && unicode::is_space(c) {
                0x20
            } else {
                c
            }
        })
        .collect();
    let space = |c: &u32| matches!(c, 0x09..=0x0D | 0x20);
    let start = mapped.iter().position(|c| !space(c))?;
    let end = mapped.iter().rposition(|c| !space(c))? + 1;
    py_float_from_str(&unicode::to_string(&mapped[start..end])?)
}

impl ConflictCandidate {
    /// `to_dict()`: the similarity rounded to four places, each optional
    /// key only when set, in the reference's order.
    pub fn to_dict(&self) -> PyValue {
        let s = |t: &PyStr| PyValue::Str(t.clone());
        let mut out = vec![
            entry("id", s(&self.id)),
            entry("a_id", s(&self.a_id)),
            entry("b_id", s(&self.b_id)),
            entry("summary_a", s(&self.summary_a)),
            entry("summary_b", s(&self.summary_b)),
            entry("similarity", PyValue::Float(round4(self.similarity))),
            entry("method", s(&self.method)),
            entry("detector", s(&self.detector)),
            entry("created", s(&self.created)),
            entry("status", s(&self.status)),
        ];
        for (key, value) in [
            ("verdict_ts", &self.verdict_ts),
            ("note", &self.note),
            ("verdict_hash_a", &self.verdict_hash_a),
            ("verdict_hash_b", &self.verdict_hash_b),
        ] {
            if let Some(v) = value {
                out.push(entry(key, s(v)));
            }
        }
        PyValue::Dict(out)
    }

    /// `ConflictCandidate.from_dict(raw)` over a dict's items, in the
    /// reference's order of reads: `status` first, then each field as the
    /// constructor's arguments list them.
    pub fn from_dict(raw: &[(PyValue, PyValue)]) -> Result<ConflictCandidate, ConflictError> {
        let mut status = str_or(raw, "status", "pending")?;
        if !VALID_STATUSES.iter().any(|v| unicode::cps(v) == status) {
            status = unicode::cps("pending");
        }
        Ok(ConflictCandidate {
            id: str_at(raw, "id")?,
            a_id: str_at(raw, "a_id")?,
            b_id: str_at(raw, "b_id")?,
            summary_a: str_or(raw, "summary_a", "")?,
            summary_b: str_or(raw, "summary_b", "")?,
            similarity: get(raw, "similarity")
                .map_or(Ok(0.0), py_float)
                .map_err(ConflictError::Value)?,
            method: str_or(raw, "method", "jaccard")?,
            detector: str_or(raw, "detector", "polarity")?,
            created: str_or(raw, "created", "")?,
            status,
            verdict_ts: str_opt(raw, "verdict_ts")?,
            note: str_opt(raw, "note")?,
            verdict_hash_a: str_opt(raw, "verdict_hash_a")?,
            verdict_hash_b: str_opt(raw, "verdict_hash_b")?,
        })
    }
}

/// `DedupCandidate`: `keeper_id` is kept, `duplicate_id` proposed for
/// tombstoning.
#[derive(Clone, Debug, PartialEq)]
pub struct DedupCandidate {
    pub keeper_id: PyStr,
    pub keeper_summary: PyStr,
    pub duplicate_id: PyStr,
    pub duplicate_summary: PyStr,
    pub similarity: f64,
    pub method: &'static str,
}

impl DedupCandidate {
    /// `to_dict()`.
    pub fn to_dict(&self) -> PyValue {
        PyValue::Dict(vec![
            entry("keeper_id", PyValue::Str(self.keeper_id.clone())),
            entry("keeper_summary", PyValue::Str(self.keeper_summary.clone())),
            entry("duplicate_id", PyValue::Str(self.duplicate_id.clone())),
            entry(
                "duplicate_summary",
                PyValue::Str(self.duplicate_summary.clone()),
            ),
            entry("similarity", PyValue::Float(round4(self.similarity))),
            entry("method", PyValue::str(self.method)),
        ])
    }
}

/// `PolaritySkippedPair`: a pair over the threshold a guard kept out.
#[derive(Clone, Debug, PartialEq)]
pub struct PolaritySkippedPair {
    pub memory_id_a: PyStr,
    pub summary_a: PyStr,
    pub memory_id_b: PyStr,
    pub summary_b: PyStr,
    pub similarity: f64,
    pub method: &'static str,
    pub detector: &'static str,
}

impl PolaritySkippedPair {
    /// `to_dict()`.
    pub fn to_dict(&self) -> PyValue {
        PyValue::Dict(vec![
            entry("memory_id_a", PyValue::Str(self.memory_id_a.clone())),
            entry("summary_a", PyValue::Str(self.summary_a.clone())),
            entry("memory_id_b", PyValue::Str(self.memory_id_b.clone())),
            entry("summary_b", PyValue::Str(self.summary_b.clone())),
            entry("similarity", PyValue::Float(round4(self.similarity))),
            entry("method", PyValue::str(self.method)),
            entry("detector", PyValue::str(self.detector)),
        ])
    }
}

/// `_has_negation(body)`: `body.lower()` less `’` and `'`, any
/// `_NEGATION_TOKEN_RE.findall` token in `_NEGATION_MARKERS`.
pub fn has_negation(body: &[u32]) -> Result<bool, ConflictError> {
    let normalized: PyStr = unicode::lower(body)
        .into_iter()
        .filter(|&c| c != 0x2019 && c != u32::from(b'\''))
        .collect();
    let found = built(&NEGATION_TOKEN_RE, "_NEGATION_TOKEN_RE")?
        .findall(&normalized[..], 0, isize::MAX)
        .map_err(|e| ConflictError::Call("_NEGATION_TOKEN_RE", ReplaceError::Call(e)))?;
    Ok(found.into_iter().any(|item| {
        let token = match item {
            Item::One(piece) => piece,
            Item::Tuple(pieces) => pieces.concat(),
        };
        NEGATION_MARKERS.iter().any(|m| unicode::cps(m) == token)
    }))
}

/// `_numeric_divergence(a, b)`: each side holds a token the other lacks.
pub fn numeric_divergence(nums_a: &BTreeSet<String>, nums_b: &BTreeSet<String>) -> bool {
    nums_a.difference(nums_b).next().is_some() && nums_b.difference(nums_a).next().is_some()
}

/// `_clause_polarity(body)`: `(asserted, negated)` content tokens, each
/// clause of `_CLAUSE_SPLIT_RE.split(body)` wholly one or the other, the
/// tokens on both sides dropped from both.
pub fn clause_polarity(body: &[u32]) -> Result<(TokenSet, TokenSet), ConflictError> {
    let pieces = built(&CLAUSE_SPLIT_RE, "_CLAUSE_SPLIT_RE")?
        .split(body, 0)
        .map_err(|e| ConflictError::Call("_CLAUSE_SPLIT_RE", e))?;
    let mut asserted = TokenSet::new();
    let mut negated = TokenSet::new();
    for clause in pieces.into_iter().flatten() {
        let tokens = raw_content_token_set(&clause);
        if tokens.is_empty() {
            continue;
        }
        if has_negation(&clause)? {
            negated.extend(tokens);
        } else {
            asserted.extend(tokens);
        }
    }
    let only_asserted = asserted.difference(&negated).cloned().collect();
    let only_negated = negated.difference(&asserted).cloned().collect();
    Ok((only_asserted, only_negated))
}

/// `_BodySignals`: every guard input for one body.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BodySignals {
    pub has_negation: bool,
    pub asserted: TokenSet,
    pub negated: TokenSet,
    pub numbers: BTreeSet<String>,
}

/// `_body_signals(body)`: the guard inputs of the provenance-stripped
/// body.
pub fn body_signals(body: &[u32]) -> Result<BodySignals, ConflictError> {
    let stripped = strip_provenance(body);
    let (asserted, negated) = clause_polarity(&stripped)?;
    Ok(BodySignals {
        has_negation: has_negation(&stripped)?,
        asserted,
        negated,
        numbers: numeric_token_set(&stripped).map_err(ConflictError::Numeric)?,
    })
}

/// `_polarity_flip(a, b)`: exactly one side negates, or each side
/// asserts a term the other negates.
pub fn polarity_flip(a: &BodySignals, b: &BodySignals) -> bool {
    if a.has_negation != b.has_negation {
        return true;
    }
    a.negated.intersection(&b.asserted).next().is_some()
        && b.negated.intersection(&a.asserted).next().is_some()
}

/// `_conflict_signal(a, b)`: `polarity` first, then `numeric`.
pub fn conflict_signal(a: &BodySignals, b: &BodySignals) -> Option<&'static str> {
    if polarity_flip(a, b) {
        return Some("polarity");
    }
    if numeric_divergence(&a.numbers, &b.numbers) {
        return Some("numeric");
    }
    None
}

/// `a.updated` against `b.updated` once `!=` holds: instants when both
/// are aware, wall clocks when both are naive, else TypeError.
fn updated_order(a: &DateTime, b: &DateTime) -> Result<Ordering, ConflictError> {
    match (a.instant_us(), b.instant_us()) {
        (Some(x), Some(y)) => Ok(x.cmp(&y)),
        (None, None) => Ok(a.wall_micros().cmp(&b.wall_micros())),
        _ => Err(ConflictError::NaiveAware),
    }
}

/// `_pick_keeper(a, b, signals_a=, signals_b=)`: `(keeper, duplicate)`.
/// A contradiction signal raises `ConflictingPair` before any tier; then
/// attestation, `updated`, the count of `verified_paths`, the id. A
/// signal not given is computed from the body, `a`'s first.
pub fn pick_keeper<'m>(
    a: &'m Memory,
    b: &'m Memory,
    signals_a: Option<&BodySignals>,
    signals_b: Option<&BodySignals>,
) -> Result<(&'m Memory, &'m Memory), ConflictError> {
    let own_a;
    let sig_a = match signals_a {
        Some(s) => s,
        None => {
            own_a = body_signals(&a.fields.body)?;
            &own_a
        }
    };
    let own_b;
    let sig_b = match signals_b {
        Some(s) => s,
        None => {
            own_b = body_signals(&b.fields.body)?;
            &own_b
        }
    };
    if let Some(detector) = conflict_signal(sig_a, sig_b) {
        return Err(ConflictError::ConflictingPair(detector));
    }
    let (fa, fb) = (&a.fields, &b.fields);
    let a_attested = !fa.verified_paths.is_empty() || fa.last_verified_at.is_some();
    let b_attested = !fb.verified_paths.is_empty() || fb.last_verified_at.is_some();
    let order = if a_attested != b_attested {
        a_attested.cmp(&b_attested)
    } else {
        match updated_order(&fa.updated, &fb.updated) {
            Ok(Ordering::Equal) => fa
                .verified_paths
                .len()
                .cmp(&fb.verified_paths.len())
                .then_with(|| fa.id.cmp(&fb.id)),
            other => other?,
        }
    };
    Ok(if order == Ordering::Greater {
        (a, b)
    } else {
        (b, a)
    })
}

/// `_polarity_skip(a, b, similarity, method, detector)`.
fn polarity_skip(
    a: &Memory,
    b: &Memory,
    similarity: f64,
    detector: &'static str,
) -> PolaritySkippedPair {
    PolaritySkippedPair {
        memory_id_a: a.fields.id.clone(),
        summary_a: snippet_for(&a.fields.body, SUMMARY_CHARS),
        memory_id_b: b.fields.id.clone(),
        summary_b: snippet_for(&b.fields.body, SUMMARY_CHARS),
        similarity,
        method: "jaccard",
        detector,
    }
}

/// The scan's two lists.
pub type Scan = (Vec<DedupCandidate>, Vec<PolaritySkippedPair>);

/// `_find_dedup_jaccard(memories, threshold=)`: every pair `i < j` of
/// non-empty token sets at or over the threshold, a candidate where
/// `_pick_keeper` crowns one and a skipped pair where it raises
/// `ConflictingPair`.
pub fn find_dedup_jaccard(memories: &[Memory], threshold: f64) -> Result<Scan, ConflictError> {
    let mut held = Vec::with_capacity(memories.len());
    for m in memories {
        let tokens = raw_content_token_set(&strip_provenance(&m.fields.body));
        held.push((m, tokens, body_signals(&m.fields.body)?));
    }
    let mut out = Vec::new();
    let mut skipped = Vec::new();
    for (i, (m_i, t_i, sig_i)) in held.iter().enumerate() {
        if t_i.is_empty() {
            continue;
        }
        for (m_j, t_j, sig_j) in &held[i + 1..] {
            if t_j.is_empty() {
                continue;
            }
            let sim = pairwise_content_jaccard(t_i, t_j);
            if sim < threshold {
                continue;
            }
            match pick_keeper(m_i, m_j, Some(sig_i), Some(sig_j)) {
                Err(ConflictError::ConflictingPair(detector)) => {
                    skipped.push(polarity_skip(m_i, m_j, sim, detector));
                }
                Err(e) => return Err(e),
                Ok((keeper, duplicate)) => out.push(DedupCandidate {
                    keeper_id: keeper.fields.id.clone(),
                    keeper_summary: snippet_for(&keeper.fields.body, SUMMARY_CHARS),
                    duplicate_id: duplicate.fields.id.clone(),
                    duplicate_summary: snippet_for(&duplicate.fields.body, SUMMARY_CHARS),
                    similarity: sim,
                    method: "jaccard",
                }),
            }
        }
    }
    Ok((out, skipped))
}

/// `list.sort(key=similarity, reverse=True)`: descending, stable.
fn by_similarity<T>(items: &mut [T], sim: impl Fn(&T) -> f64) {
    items.sort_by(|x, y| sim(y).partial_cmp(&sim(x)).unwrap_or(Ordering::Equal));
}

/// `_find_dedup_with_skips(memories, threshold=)`: both lists by
/// similarity descending (stable), and the method. Under two memories
/// nothing is read; `None` is the default threshold.
pub fn find_dedup_with_skips(
    memories: &[Memory],
    threshold: Option<f64>,
) -> Result<(Vec<DedupCandidate>, Vec<PolaritySkippedPair>, &'static str), ConflictError> {
    if memories.len() < 2 {
        return Ok((Vec::new(), Vec::new(), "jaccard"));
    }
    let eff = threshold.unwrap_or(DEFAULT_JACCARD_THRESHOLD);
    let (mut candidates, mut skipped) = find_dedup_jaccard(memories, eff)?;
    by_similarity(&mut candidates, |c| c.similarity);
    by_similarity(&mut skipped, |p| p.similarity);
    Ok((candidates, skipped, "jaccard"))
}

/// `skip_to_candidate(pair, created=)`: a skipped pair as a pending row.
pub fn skip_to_candidate(
    pair: &PolaritySkippedPair,
    created: &[u32],
) -> Result<ConflictCandidate, ConflictError> {
    Ok(ConflictCandidate {
        id: unicode::cps(&pair_id(&pair.memory_id_a, &pair.memory_id_b)?),
        a_id: pair.memory_id_a.clone(),
        b_id: pair.memory_id_b.clone(),
        summary_a: pair.summary_a.clone(),
        summary_b: pair.summary_b.clone(),
        similarity: pair.similarity,
        method: unicode::cps(pair.method),
        detector: unicode::cps(pair.detector),
        created: created.to_vec(),
        status: unicode::cps("pending"),
        verdict_ts: None,
        note: None,
        verdict_hash_a: None,
        verdict_hash_b: None,
    })
}

/// `find_conflict_candidates(memories, threshold=)` with `utcnow()` as
/// `now`: the scan's skipped pairs as rows created at `now.isoformat()`,
/// the first per pair id.
pub fn find_conflict_candidates(
    memories: &[Memory],
    threshold: Option<f64>,
    now: &DateTime,
) -> Result<Vec<ConflictCandidate>, ConflictError> {
    let (_, skipped, _) = find_dedup_with_skips(memories, threshold)?;
    let created = unicode::cps(&now.isoformat());
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for pair in &skipped {
        let cand = skip_to_candidate(pair, &created)?;
        if seen.insert(cand.id.clone()) {
            out.push(cand);
        }
    }
    Ok(out)
}
