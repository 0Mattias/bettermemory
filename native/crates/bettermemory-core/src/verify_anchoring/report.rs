//! `PathDriftReport` of `verify_anchoring.py` (reference lines 68-296):
//! the result of a path-drift scan, its three properties and `to_dict`.

/// The result of a path-drift scan. Every bucket keeps the reference's
/// order; `anchor` is set only on a report read through a claim anchor.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PathDriftReport {
    pub checked: Vec<String>,
    pub missing: Vec<String>,
    pub verified: Vec<String>,
    pub expected_absent: Vec<String>,
    pub dropped_as_route: Vec<String>,
    pub claim_anchored_missing: Vec<String>,
    pub unconfirmed: Vec<String>,
    pub unconfirmed_reasons: Vec<String>,
    pub present_but_attested_absent: Vec<String>,
    pub anchor: Option<String>,
}

/// One value of `to_dict`: a list of paths, the anchor's rule, or the
/// unconfirmed claims as (path, reason) pairs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DictField {
    Paths(Vec<String>),
    Text(String),
    Unconfirmed(Vec<(String, String)>),
}

/// `zip(..., strict=True)` over `unconfirmed` and `unconfirmed_reasons`
/// of unequal lengths: the reference raises `ValueError`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ZipLengthError;

impl PathDriftReport {
    /// `has_drift`: a non-empty `missing`.
    pub fn has_drift(&self) -> bool {
        !self.missing.is_empty()
    }

    /// `has_claim_anchored_drift`: a non-empty `claim_anchored_missing`.
    pub fn has_claim_anchored_drift(&self) -> bool {
        !self.claim_anchored_missing.is_empty()
    }

    /// `should_report`: any of missing, verified, expected_absent or
    /// present_but_attested_absent.
    pub fn should_report(&self) -> bool {
        !(self.missing.is_empty()
            && self.verified.is_empty()
            && self.expected_absent.is_empty()
            && self.present_but_attested_absent.is_empty())
    }

    /// `to_dict`, its keys in the reference's insertion order. The three
    /// anchored keys follow only when `anchor` is set.
    pub fn to_dict(&self) -> Result<Vec<(&'static str, DictField)>, ZipLengthError> {
        let p = |v: &Vec<String>| DictField::Paths(v.clone());
        let mut out = vec![
            ("checked", p(&self.checked)),
            ("missing", p(&self.missing)),
            ("verified", p(&self.verified)),
            ("expected_absent", p(&self.expected_absent)),
            ("dropped_as_route", p(&self.dropped_as_route)),
            ("claim_anchored_missing", p(&self.claim_anchored_missing)),
        ];
        if let Some(anchor) = &self.anchor {
            out.push(("anchor", DictField::Text(anchor.clone())));
            if self.unconfirmed.len() != self.unconfirmed_reasons.len() {
                return Err(ZipLengthError);
            }
            let pairs = self
                .unconfirmed
                .iter()
                .zip(&self.unconfirmed_reasons)
                .map(|(a, b)| (a.clone(), b.clone()))
                .collect();
            out.push(("unconfirmed", DictField::Unconfirmed(pairs)));
            out.push((
                "present_but_attested_absent",
                p(&self.present_but_attested_absent),
            ));
        }
        Ok(out)
    }
}
