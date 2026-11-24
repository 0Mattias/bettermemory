//! The chain's segment map with the caller's key (`log.Segment`,
//! `log._ring_keys`, `log._leading_key`, `log._anchored`,
//! `log._segments_from`, `log.chain_trust`, `log.chain_segments`).
//!
//! The caller's key (`current_key`) is an argument, apart from the ring:
//! the reference tries it first when checking the origin anchor and when
//! naming the leading segment, and seeds the per-name key memo with it, so
//! a store whose ring file holds another key still reads its own history
//! under the key the caller opened it with.

use super::{
    check_origin_with_keys, fingerprint, rekey_fingerprints, row_verifies, Key, KeyRing, LogRow,
    LogSource, OriginCheck, VerifyError, REKEY,
};
use std::collections::BTreeMap;

use crate::pyvalue::{PyError, PyStr};
use crate::unicode;

/// `log.Segment`: one run of the chain under one key, from `first_row`
/// (the log's first row or a `rekey` row) to the row before the next
/// `rekey` row. `key_fingerprint` is any text a rekey payload can hold (a
/// lone surrogate among them), the empty text for a rekey row that names
/// no fingerprints.
#[derive(Clone, Debug)]
pub struct Segment {
    pub first_seq: i64,
    pub key_fingerprint: PyStr,
    pub key: Option<Key>,
    pub foreign: bool,
    pub first_row: Option<LogRow>,
}

impl Segment {
    /// `Segment.held`.
    pub fn held(&self) -> bool {
        self.key.is_some()
    }
}

/// `log.ChainTrust` whole: the segments in the reference's order and the
/// origin check they were read under.
#[derive(Clone, Debug)]
pub struct SegmentTrust {
    pub segments: Vec<Segment>,
    pub origin: OriginCheck,
}

/// `log._ring_keys`: the caller's key first, then every held key that is
/// not equal to it.
fn ring_keys(ring: &KeyRing, current_key: Option<&Key>) -> Vec<Key> {
    let mut keys: Vec<Key> = current_key.into_iter().cloned().collect();
    keys.extend(
        ring.held_keys()
            .into_iter()
            .filter(|key| Some(key) != current_key),
    );
    keys
}

fn lookup(ring: &KeyRing, name: &[u32]) -> Option<Key> {
    match unicode::to_string(name) {
        Some(text) if !text.is_empty() => ring.key_for_fingerprint(&text),
        _ => None,
    }
}

/// `log._leading_key`.
fn leading_key(
    ring: &KeyRing,
    named: &[u32],
    first_row: &LogRow,
    current_key: Option<&Key>,
) -> (PyStr, Option<Key>) {
    if let Some(current) = current_key {
        if unicode::cps(&fingerprint(current.bytes())) == named {
            return (named.to_vec(), Some(current.clone()));
        }
    }
    if let Some(key) = lookup(ring, named) {
        return (named.to_vec(), Some(key));
    }
    let mut candidates = ring.held_keys();
    if let Some(current) = current_key {
        if !candidates.contains(current) {
            candidates.insert(0, current.clone());
        }
    }
    for candidate in candidates {
        if row_verifies(candidate.bytes(), first_row) {
            return (
                unicode::cps(&fingerprint(candidate.bytes())),
                Some(candidate),
            );
        }
    }
    (named.to_vec(), None)
}

/// `log._anchored`: under an anchor that holds, a segment is foreign when
/// its key is not held and the next segment does not start at or before
/// `origin_seq`.
fn anchored(segments: &mut [Segment], origin_seq: i64) {
    for position in 0..segments.len() {
        let ends_before = segments
            .get(position + 1)
            .is_some_and(|next| next.first_seq <= origin_seq);
        let segment = &mut segments[position];
        segment.foreign = !segment.held() && !ends_before;
    }
}

/// `log._segments_from`: the segment list from the first row and the
/// rekey rows in seq order. Err(RecursionError) for a rekey payload nested
/// past the JSON depth limit, which `_rekey_fingerprints` does not catch.
pub(crate) fn segments_from(
    first_row: Option<&LogRow>,
    rekeys: &[LogRow],
    ring: &KeyRing,
    current_fingerprint: &[u32],
    current_key: Option<&Key>,
    origin_seq: Option<i64>,
) -> Result<Vec<Segment>, PyError> {
    let Some(first_row) = first_row else {
        return Ok(Vec::new());
    };
    let mut starts: Vec<(&LogRow, PyStr)> = Vec::new();
    let mut leading_name = current_fingerprint.to_vec();
    for (position, row) in rekeys.iter().filter(|r| r.kind == REKEY).enumerate() {
        let names = rekey_fingerprints(row)?;
        if position == 0 {
            if let Some((old, _)) = &names {
                leading_name = old.clone();
            }
        }
        starts.push((row, names.map_or_else(Vec::new, |(_, new)| new)));
    }
    let mut segments: Vec<Segment> = Vec::new();
    let mut held_seen = false;
    if starts
        .first()
        .is_none_or(|(row, _)| row.seq != first_row.seq)
    {
        let (name, key) = leading_key(ring, &leading_name, first_row, current_key);
        held_seen = key.is_some();
        segments.push(Segment {
            first_seq: first_row.seq,
            key_fingerprint: name,
            key,
            foreign: false,
            first_row: Some(first_row.clone()),
        });
    }
    // The reference's per-name dict, seeded with the caller's key under
    // its own fingerprint before any ring lookup.
    let mut memo: BTreeMap<PyStr, Option<Key>> = BTreeMap::new();
    if let Some(current) = current_key {
        memo.insert(
            unicode::cps(&fingerprint(current.bytes())),
            Some(current.clone()),
        );
    }
    for (row, name) in starts {
        let key = memo
            .entry(name.clone())
            .or_insert_with(|| lookup(ring, &name))
            .clone();
        let foreign = key.is_none() && held_seen;
        held_seen = held_seen || key.is_some();
        segments.push(Segment {
            first_seq: row.seq,
            key_fingerprint: name,
            key,
            foreign,
            first_row: Some(row.clone()),
        });
    }
    if let Some(origin_seq) = origin_seq {
        anchored(&mut segments, origin_seq);
    }
    Ok(segments)
}

/// `log.chain_trust(conn, keyring=ring, current_fingerprint=...,
/// current_key=...)` over any [`LogSource`]: the origin anchor checked
/// against the row at its seq under the caller's key then the held keys,
/// and the segments read off the first row and the rekey rows. A source's
/// own failure is [`VerifyError::Source`]; a ring or rekey payload nested
/// past the JSON depth limit is RecursionError, as in the reference.
pub fn chain_trust_with<S: LogSource + ?Sized>(
    source: &S,
    ring: &KeyRing,
    current_fingerprint: &[u32],
    current_key: Option<&Key>,
) -> Result<SegmentTrust, VerifyError<S::Error>> {
    let anchor = ring.read_origin()?;
    let anchored_row = match &anchor {
        Some(a) => source.at_seq(a.seq).map_err(VerifyError::Source)?,
        None => None,
    };
    let origin = check_origin_with_keys(
        anchor.as_ref(),
        anchored_row.as_ref(),
        ring_keys(ring, current_key),
        row_verifies,
    );
    let first_row = source.first().map_err(VerifyError::Source)?;
    let rekeys = source.rekeys().map_err(VerifyError::Source)?;
    let segments = segments_from(
        first_row.as_ref(),
        &rekeys,
        ring,
        current_fingerprint,
        current_key,
        if origin.holds() { origin.seq } else { None },
    )?;
    Ok(SegmentTrust { segments, origin })
}

/// `log.chain_segments`: the segments of [`chain_trust_with`].
pub fn chain_segments<S: LogSource + ?Sized>(
    source: &S,
    ring: &KeyRing,
    current_fingerprint: &[u32],
    current_key: Option<&Key>,
) -> Result<Vec<Segment>, VerifyError<S::Error>> {
    Ok(chain_trust_with(source, ring, current_fingerprint, current_key)?.segments)
}
