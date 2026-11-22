//! The trust appliers of `_response.ResponseBuilder`: `attach_provenance`,
//! `apply_trust_unavailable` and `apply_trust`, with
//! `TRUST_UNAVAILABLE_RECOMMENDATION`.
//!
//! A response row is a Python `dict`, here its pairs in insertion order.
//! An assignment replaces the value of the key's last pair in place (a
//! `dict` keeps one, at its first position) or appends a pair.

use crate::datetime::parse_event_ts;
use crate::pyvalue::PyValue;
use crate::store::provenance::{Provenance, ProvenanceError, TrustRow};
use crate::unicode;
use crate::verify_status::remote_verification_status;

/// `TRUST_UNAVAILABLE_RECOMMENDATION`.
pub const TRUST_UNAVAILABLE_RECOMMENDATION: &str = "This machine does not hold the store's key, \
so whether this host made this stamp is unknown. Spot-check a claim before relying on the \
memory; `bettermemory log verify` reports the same condition.";

/// A response row: a `dict`'s pairs.
pub type Row = Vec<(PyValue, PyValue)>;

/// The value of `row[key]` for a `str` key, as `dict.get` reads it.
pub fn get<'a>(row: &'a [(PyValue, PyValue)], key: &str) -> Option<&'a PyValue> {
    let key = unicode::cps(key);
    row.iter().rev().find_map(|(k, v)| match k {
        PyValue::Str(s) if *s == key => Some(v),
        _ => None,
    })
}

/// `row[key] = value`.
pub fn set(row: &mut Row, key: &str, value: PyValue) {
    let cps = unicode::cps(key);
    match row
        .iter_mut()
        .rev()
        .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == cps))
    {
        Some(slot) => slot.1 = value,
        None => row.push((PyValue::str(key), value)),
    }
}

/// A non-empty `str` at `row["last_verified_at"]`, the stamp both rules
/// read.
fn stamp_text(row: &Row) -> Option<Vec<u32>> {
    match get(row, "last_verified_at") {
        Some(PyValue::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// `ResponseBuilder.apply_trust_unavailable(row, recommendation=...)`.
pub fn apply_trust_unavailable(row: &mut Row, recommendation: &str) {
    set(row, "trust_unavailable", PyValue::Bool(true));
    if stamp_text(row).is_none() {
        return;
    }
    let cps = unicode::cps("verification");
    if let Some((_, PyValue::Dict(verification))) = row
        .iter_mut()
        .rev()
        .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == cps))
    {
        set(verification, "recommendation", PyValue::str(recommendation));
    }
    set(
        row,
        "staleness_verdict",
        PyValue::str("spot_check_required"),
    );
}

/// `ResponseBuilder.apply_trust(row, provenance=..., verified_locally_at=...)`,
/// the builder's `stale_after_days` an argument.
pub fn apply_trust(
    row: &mut Row,
    provenance: Option<&str>,
    verified_locally_at: Option<&str>,
    stale_after_days: i64,
) {
    if let Some(label) = provenance {
        set(row, "provenance", PyValue::str(label));
    }
    if provenance != Some("synced") || verified_locally_at.is_some() {
        return;
    }
    let Some(raw) = stamp_text(row) else {
        return;
    };
    let Some(stamp) = parse_event_ts(&raw) else {
        return;
    };
    let status = remote_verification_status(stamp, stale_after_days);
    set(row, "verification", status.to_dict());
    set(
        row,
        "staleness_verdict",
        PyValue::str("spot_check_required"),
    );
}

/// The store's `trust_rows`: None when it cannot verify (no key), else
/// each id's row.
pub trait TrustRows {
    type Error;
    fn trust_rows(&self, ids: &[String]) -> Result<Option<Vec<(String, TrustRow)>>, Self::Error>;
    /// Whether the store holds its key. Without it `trust_rows` answers
    /// None before it binds any id, so no id is encoded.
    fn verifies(&self) -> bool {
        true
    }
}

impl TrustRows for Provenance<'_> {
    type Error = ProvenanceError;
    fn trust_rows(&self, ids: &[String]) -> Result<Option<Vec<(String, TrustRow)>>, Self::Error> {
        let ids: Vec<&str> = ids.iter().map(String::as_str).collect();
        Provenance::trust_rows(self, &ids)
    }
    fn verifies(&self) -> bool {
        Provenance::verifies(self)
    }
}

/// What `attach_provenance` raises: the store's error, the `TypeError`
/// of looking an unhashable id up in the store's answer, or an id the
/// store cannot encode.
#[derive(Debug, PartialEq, Eq)]
pub enum AttachError<E> {
    Store(E),
    Unhashable,
    /// An id holding a lone surrogate, which the store's SQL binding
    /// cannot encode (`UnicodeEncodeError`).
    Encode,
}

/// `hash(value)` succeeds.
pub fn hashable(value: &PyValue) -> bool {
    let mut work = vec![value];
    while let Some(v) = work.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => return false,
            PyValue::Tuple(items) => work.extend(items),
            _ => {}
        }
    }
    true
}

/// `ResponseBuilder.attach_provenance(out, store=...)`. Rows before an
/// unhashable id are already changed when it raises, as in the reference.
pub fn attach_provenance<S: TrustRows>(
    out: &mut [Row],
    store: &S,
    stale_after_days: i64,
) -> Result<(), AttachError<S::Error>> {
    let mut ids = Vec::new();
    let mut unencodable = false;
    let mut any = false;
    for row in out.iter() {
        if let Some(PyValue::Str(s)) = get(row, "id") {
            any = true;
            match unicode::to_string(s) {
                Some(id) => ids.push(id),
                None => unencodable = true,
            }
        }
    }
    if !any {
        return Ok(());
    }
    // A keyless store answers None before its SQL binding would raise
    // `UnicodeEncodeError` on a lone surrogate.
    let answer = if unencodable {
        if store.verifies() {
            return Err(AttachError::Encode);
        }
        None
    } else {
        store.trust_rows(&ids).map_err(AttachError::Store)?
    };
    let Some(rows) = answer else {
        for row in out.iter_mut() {
            apply_trust_unavailable(row, TRUST_UNAVAILABLE_RECOMMENDATION);
        }
        return Ok(());
    };
    for row in out.iter_mut() {
        let id = get(row, "id").cloned().unwrap_or_else(|| PyValue::str(""));
        if !hashable(&id) {
            return Err(AttachError::Unhashable);
        }
        let PyValue::Str(id) = &id else { continue };
        let found = rows
            .iter()
            .rev()
            .find(|(k, _)| unicode::cps(k) == *id)
            .map(|(_, t)| t.clone());
        if let Some(trust) = found {
            apply_trust(
                row,
                Some(&trust.label),
                trust.verified_at.as_deref(),
                stale_after_days,
            );
        }
    }
    Ok(())
}
