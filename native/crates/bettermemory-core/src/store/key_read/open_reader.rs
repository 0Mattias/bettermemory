//! The keys a store opened without writing keeps (`store_open.py`
//! `_open_connected` 82-116 and `_open_reader` 146-203, with `Store.has_key`,
//! `Store._verify_key` and `Store.can_verify`, `store.py` 1117-1145).
//!
//! Once the open read its key state (`ReaderRing::key_state_at`), a present
//! key binds the store with it, also on a read-only connection; any other
//! state binds it with no key, and keeps the ring's current key for
//! checking pointers (`_read_key`) where this machine has a history of the
//! store: the key matches meta, the ring holds an origin anchor, or the key
//! signs a segment of the log (`store_keys._signs_a_segment` over
//! `log.chain_segments` with the key in hand). The three are asked in that
//! order and the first that holds ends the question: the origin file is
//! read only when the key does not match meta, the log only when there is
//! no anchor.

use std::fmt;
use std::path::{Path, PathBuf};

use crate::log::segments::chain_segments;
use crate::log::{fingerprint, Key, KeyRing, LogRow, LogSource, VerifyError};
use crate::store::key_state::{KeyState, KeyStatus};
use crate::store::rotation::{signs_a_segment, SegmentStart};
use crate::unicode;

/// How `_open_connected` binds a store once its key state is read (82-116).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Binding {
    /// `KEY_PRESENT`: bound with its key, the store's own.
    Present,
    /// `_open_reader`: the open may not rekey, or the ring is withheld
    /// (bound to another file, or unclaimed).
    Reader,
    /// The owner's opener reclaims or rekeys the store (`store::open`).
    Owner,
}

/// `_open_connected`'s dispatch: `KEY_PRESENT` binds the key; a state
/// other than present opens a reader when the open may not rekey or the
/// ring is withheld whatever its key is (`KeyState.withheld`).
pub fn binding(state: &KeyState, allow_rekey: bool) -> Binding {
    if state.state == KeyStatus::Present {
        Binding::Present
    } else if !allow_rekey || state.withheld().is_some() {
        Binding::Reader
    } else {
        Binding::Owner
    }
}

/// What a store bound as present or as a reader holds of its keys:
/// `Store._key`, `Store._read_key` and `Store._bound_elsewhere`.
#[derive(Clone, Default)]
pub struct ReaderKeys {
    key: Option<Key>,
    read_key: Option<Key>,
    bound_elsewhere: Option<PathBuf>,
}

impl ReaderKeys {
    /// `Store.has_key`: whether the store holds the key it writes with.
    pub fn has_key(&self) -> bool {
        self.key.is_some()
    }
    /// The key a present store is bound with.
    pub fn key(&self) -> Option<&Key> {
        self.key.as_ref()
    }
    /// `Store._read_key`: the key a reader keeps for checking.
    pub fn read_key(&self) -> Option<&Key> {
        self.read_key.as_ref()
    }
    /// `Store._verify_key`: the store's own key, else the read key.
    pub fn verify_key(&self) -> Option<&Key> {
        self.key.as_ref().or(self.read_key.as_ref())
    }
    /// `Store.can_verify`: whether reads check pointers into the log.
    pub fn can_verify(&self) -> bool {
        self.verify_key().is_some()
    }
    /// `Store._bound_elsewhere`: what withholds the ring from this file
    /// (`KeyState.withheld`), a reader's only.
    pub fn bound_elsewhere(&self) -> Option<&Path> {
        self.bound_elsewhere.as_deref()
    }
}

// Only whether each key is held: no key's bytes or fingerprint.
impl fmt::Debug for ReaderKeys {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ReaderKeys")
            .field("has_key", &self.key.is_some())
            .field("read_key", &self.read_key.is_some())
            .field("bound_elsewhere", &self.bound_elsewhere)
            .finish()
    }
}

/// The keys of a store whose open binds it as present or as a reader
/// ([`binding`] is not [`Binding::Owner`]), from the key state the open
/// read, the key ring in memory and the store's log. `state.key` is the
/// ring's current key as the open loaded it, and `ring` the ring as read
/// after it, which names the log's segments; the origin anchor is read from
/// its file under `state.keyring`, whatever its size, and `ring`'s copy of
/// it is not asked. An origin file or a rekey payload nested past the JSON
/// depth limit is `VerifyError::Py(RecursionError)` and a failed read of
/// the log is `VerifyError::Source`: `_open_reader` catches neither.
pub fn reader_keys<S: LogSource + ?Sized>(
    source: &S,
    ring: &KeyRing,
    state: &KeyState,
) -> Result<ReaderKeys, VerifyError<S::Error>> {
    if state.state == KeyStatus::Present {
        return Ok(ReaderKeys {
            key: state.key.clone(),
            ..ReaderKeys::default()
        });
    }
    let read_key = match &state.key {
        Some(key) if keeps(source, ring, state, key)? => Some(key.clone()),
        _ => None,
    };
    Ok(ReaderKeys {
        key: None,
        read_key,
        bound_elsewhere: state.withheld(),
    })
}

/// `_open_reader`'s condition (189-201) for a key in hand, short-circuit
/// as the reference's `or`.
fn keeps<S: LogSource + ?Sized>(
    source: &S,
    ring: &KeyRing,
    state: &KeyState,
    key: &Key,
) -> Result<bool, VerifyError<S::Error>> {
    // `matches`: `fingerprint(key) == expected`, the text meta records.
    if unicode::cps(&fingerprint(key.bytes())) == state.expected {
        return Ok(true);
    }
    // `keyring.read_origin()`: the file read whole, as the reference reads
    // it, through the ring on disk (`KeyFiles`), not the ring in memory,
    // whose loader leaves out a file over 1 MiB (`store::keys`).
    if state.keyring.read_origin()?.is_some() {
        return Ok(true);
    }
    let segments = chain_segments(source, ring, &state.expected, Some(key))?;
    // A name that is not text (a lone surrogate) cannot equal a
    // fingerprint, which is hex.
    let names: Vec<(String, Option<&LogRow>)> = segments
        .iter()
        .filter_map(|s| unicode::to_string(&s.key_fingerprint).map(|n| (n, s.first_row.as_ref())))
        .collect();
    let starts: Vec<SegmentStart<'_>> = names
        .iter()
        .map(|(name, first_row)| SegmentStart {
            key_fingerprint: name,
            first_row: *first_row,
        })
        .collect();
    Ok(signs_a_segment(&starts, key.bytes()))
}
