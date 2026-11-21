//! `path_drift_steps.py`: `_check_body_candidates`, the body-candidate
//! pass of `verify_anchoring.detect_path_drift`.
//!
//! The pass sorts each candidate into the buckets it is handed. What it
//! reads from the machine and from unbuilt units reaches it as closures:
//! `exists` is `verify_candidates._path_exists`, `routelike` is
//! `verify_candidates._is_multi_segment_routelike` and `keys` the claim
//! key function. They are called in the reference's order.

use std::collections::HashSet;

/// The buckets of one read and its seen set, appended to in place.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Buckets {
    pub checked: Vec<String>,
    pub missing: Vec<String>,
    pub claim_anchored: Vec<String>,
    pub verified: Vec<String>,
    pub expected_absent: Vec<String>,
    pub dropped_as_route: Vec<String>,
    pub seen: HashSet<String>,
}

/// `path.split(" ", 1)[0]`: the text before the first U+0020.
fn prefix_of(path: &str) -> &str {
    path.split(' ').next().unwrap_or(path)
}

/// `_check_body_candidates`: each `(path, drop_if_missing, bare_spaced)`
/// existence-checked and keyed, then, when missing and unattested,
/// arbitrated by its prefix (spaced), dropped (ambiguous) or recorded as a
/// route, in that order; otherwise recorded once per key in `checked`
/// and in `missing`, `claim_anchored`, `expected_absent` or `verified`.
pub fn check_body_candidates(
    candidates: &[(String, bool, bool)],
    verified_set: &HashSet<String>,
    absent_set: &HashSet<String>,
    b: &mut Buckets,
    exists: &mut dyn FnMut(&str) -> bool,
    routelike: &mut dyn FnMut(&str) -> bool,
    keys: &mut dyn FnMut(&str) -> String,
) {
    for (path, drop_if_missing, bare_spaced) in candidates {
        let found = exists(path);
        let norm = keys(path);
        let attested = verified_set.contains(&norm) || absent_set.contains(&norm);
        if !found && !attested {
            if *bare_spaced {
                let prefix = prefix_of(path);
                let prefix_key = keys(prefix);
                if exists(prefix) && !b.seen.contains(&prefix_key) {
                    b.seen.insert(prefix_key.clone());
                    b.checked.push(prefix.to_owned());
                    if verified_set.contains(&prefix_key) {
                        b.verified.push(prefix.to_owned());
                    }
                }
                continue;
            }
            if *drop_if_missing {
                continue;
            }
            if routelike(path) {
                b.dropped_as_route.push(path.clone());
                continue;
            }
        }
        if b.seen.contains(&norm) {
            continue;
        }
        b.seen.insert(norm.clone());
        b.checked.push(path.clone());
        if !found {
            if absent_set.contains(&norm) {
                b.expected_absent.push(path.clone());
            } else {
                b.missing.push(path.clone());
                if verified_set.contains(&norm) {
                    b.claim_anchored.push(path.clone());
                }
            }
            continue;
        }
        if verified_set.contains(&norm) {
            b.verified.push(path.clone());
        }
    }
}
