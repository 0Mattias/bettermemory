//! `verify.py`'s claim anchors (lines at v9): `commit_drift_anchor_paths`
//! 325-376 without the body parse memo, `_anchor_paths` 379-414 and
//! `_attested_anchor` 417-433, with `_RELATIVE_CITATION_RE`
//! (`verify_candidates.py` 238-243) built once on the crate's regular
//! expression engine.
//!
//! `interpret_path` in the `literal` and `reference` modes reads no host:
//! it is `literal_path` and `reference_path`, called here directly. The
//! home and the user lookup are those `_extract_candidates` and
//! `_normalize_for_compare` read.

use std::collections::BTreeSet;
use std::sync::OnceLock;

use crate::attestation_paths::{literal_path, reference_path, Kind, Paths, Raised};
use crate::pyre::regex::Regex;
use crate::pyvalue::PyStr;
use crate::verify_candidates::scan::{
    extract_candidates, normalize_for_compare, CompareError, ScanError, UserLookup,
};
use crate::verify_candidates::validate::{bounded_scan_text, escapes_root};
use crate::verify_candidates::MAX_ANCHOR_CITATIONS;

/// `_RELATIVE_CITATION_RE`'s pattern text, as the reference compiles it
/// (no flags).
pub const RELATIVE_CITATION_PATTERN: &str = concat!(
    r"(?<![\w/~.\\-])",
    r"((?:[A-Za-z_.][\w.\-]{0,63}/){0,12}",
    r"[A-Za-z_.][\w.\-]{0,63}\.[A-Za-z][A-Za-z0-9_]{1,7})",
    r"(?![\w/]|\.\w)"
);

/// What stops an answer: what the scan or the compare form raises, what
/// `reference_path` raises, or the engine running out of memory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AnchorError {
    Compare(CompareError),
    Scan(ScanError),
    Raised(Raised),
    Memory,
}

/// `_RELATIVE_CITATION_RE.finditer(text)`: the span of group 1 of each
/// match, in order. The shape of `verify_anchoring`'s `CitationMatches`.
pub fn relative_citations(text: &[u32]) -> Result<Vec<(usize, usize)>, AnchorError> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| {
        Regex::new_str(RELATIVE_CITATION_PATTERN, 0)
            .expect("the engine builds _RELATIVE_CITATION_RE")
    });
    let found = re
        .finditer(text, 0, isize::MAX)
        .map_err(|_| AnchorError::Memory)?;
    Ok(found
        .iter()
        .filter_map(|m| m.group(1))
        .map(|(a, b)| (a.unsigned_abs(), b.unsigned_abs()))
        .collect())
}

/// `_attested_anchor(raw)`: `interpret_path(raw, mode="reference")`.
pub fn attested_anchor(raw: &[u32]) -> Result<Option<PyStr>, Raised> {
    reference_path(raw)
}

/// `commit_drift_anchor_paths(body, verified_paths)`, uncached: the
/// non-empty attestations, a `LiteralPaths` kept literal and any other
/// sequence read as references.
pub fn commit_drift_anchor_paths(
    body: &[u32],
    verified_paths: &Paths,
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<Vec<PyStr>, AnchorError> {
    let attested = Paths {
        kind: if verified_paths.kind == Kind::Literal {
            Kind::Literal
        } else {
            Kind::Reference
        },
        items: verified_paths
            .items
            .iter()
            .filter(|p| !p.is_empty())
            .cloned()
            .collect(),
    };
    anchor_paths(body, &attested, home, lookup)
}

/// `_anchor_paths(body, attested)`.
pub fn anchor_paths(
    body: &[u32],
    attested: &Paths,
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<Vec<PyStr>, AnchorError> {
    let mut anchors: Vec<PyStr> = Vec::new();
    let mut seen: BTreeSet<PyStr> = BTreeSet::new();
    let mut add = |candidate: &[u32], lookup: &mut UserLookup<'_>| -> Result<bool, AnchorError> {
        let key = normalize_for_compare(candidate, home, lookup).map_err(AnchorError::Compare)?;
        if key.is_empty() || !seen.insert(key) {
            return Ok(false);
        }
        anchors.push(candidate.to_vec());
        Ok(true)
    };
    for raw in &attested.items {
        let anchor = if attested.kind == Kind::Reference {
            attested_anchor(raw).map_err(AnchorError::Raised)?
        } else {
            literal_path(raw)
        };
        if let Some(anchor) = anchor {
            add(&anchor, lookup)?;
        }
    }
    if !body.is_empty() {
        for (path, _, _) in extract_candidates(body, home, lookup).map_err(AnchorError::Scan)? {
            add(&path, lookup)?;
        }
        let text = bounded_scan_text(body);
        let mut relative_added = 0;
        for (a, b) in relative_citations(text)? {
            if relative_added >= MAX_ANCHOR_CITATIONS {
                break;
            }
            let cited = &text[a..b];
            if escapes_root(cited) {
                continue;
            }
            if add(cited, lookup)? {
                relative_added += 1;
            }
        }
    }
    Ok(anchors)
}
