//! A store's key ring read from a keys directory (`log.KeyRing`'s files),
//! into the in-memory [`KeyRing`] that `verify_chain` reads.
//!
//! Only the fixed names are read: `<store_id>.key`, `<store_id>.*.key`
//! (the retired keys `retired_paths` globs), `<store_id>.head` and
//! `<store_id>.origin`, the store id first held to `STORE_ID_PATTERN` (32
//! lowercase hex) as `KeyRing.__post_init__` holds it. No name read from a
//! row or a file is ever joined to the directory: a lookup by fingerprint
//! is answered from what was read here, behind `log::is_fingerprint`.
//!
//! As the reference reads them: a file is read whole and followed through
//! a symbolic link; a file that cannot be read (absent, a dangling link, a
//! directory, no permission) is not in the ring, as `held_keys` and
//! `key_for_fingerprint` skip it and `read_head` / `read_origin` read it as
//! none. One difference, on purpose: a file over [`MAX_FILE_BYTES`] is
//! read as unreadable, where the reference would read it whole (a key is
//! 32 bytes; a head or origin file under 200). Another: a name that is
//! not a regular file (a FIFO, a socket, a device) is read as unreadable
//! without being opened, where the reference's read of a FIFO waits for a
//! writer.
//!
//! Retired keys are kept in `KeyRing::retired` by the part of the file
//! name between the id and `.key`; `held_keys` gives them in that part's
//! order, where the reference sorts whole file names. The two orders differ
//! only between a part and a longer part that starts with it and a dot
//! (`ab` and `ab.c`), which no name the reference writes (16 hex) makes.

use std::fs;
use std::io::{self, Read};

use std::path::Path;
use zeroize::Zeroizing;

use crate::log::{Key, KeyRing};

/// The most bytes a key ring file is read for.
pub const MAX_FILE_BYTES: u64 = 1 << 20;

/// `STORE_ID_PATTERN`: 32 lowercase hex digits.
pub fn is_store_id(text: &str) -> bool {
    text.len() == 32
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Why no ring was read.
#[derive(Debug)]
pub enum KeyRingError {
    /// `KeyRing.__post_init__`'s ValueError.
    NotAStoreId,
}

/// A file's bytes, read whole into a buffer sized from its length and
/// wiped when dropped: no partial copy of a key is left in freed memory
/// by a growing buffer. A file that grows while it is read is read again
/// into a larger buffer (the smaller one wiped). None when it cannot be
/// read or is over [`MAX_FILE_BYTES`].
fn read_secret(path: &Path) -> Option<Zeroizing<Vec<u8>>> {
    // Only a regular file (through links) is opened: opening a FIFO waits
    // for a writer, forever if none comes (the reference's `read_bytes`
    // waits). A directory, a FIFO, a socket or a device reads as
    // unreadable.
    if !fs::metadata(path).ok()?.is_file() {
        return None;
    }
    let mut file = fs::File::open(path).ok()?;
    let mut capacity = usize::try_from(file.metadata().ok()?.len().min(MAX_FILE_BYTES))
        .ok()?
        .saturating_add(1);
    loop {
        let mut buffer = Zeroizing::new(vec![0u8; capacity]);
        let mut filled = 0;
        loop {
            match file.read(&mut buffer[filled..]) {
                Ok(0) => {
                    buffer.truncate(filled);
                    return Some(buffer);
                }
                Ok(n) => {
                    filled += n;
                    if filled == capacity {
                        break;
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => return None,
            }
        }
        // Full: the file is longer than its length said.
        if capacity as u64 > MAX_FILE_BYTES {
            return None;
        }
        capacity = usize::try_from(MAX_FILE_BYTES).ok()?.saturating_add(1);
        file = fs::File::open(path).ok()?;
    }
}

fn read_file(path: &Path) -> Option<Vec<u8>> {
    read_secret(path).map(|mut b| std::mem::take(&mut *b))
}

fn read_key(path: &Path) -> Option<Key> {
    read_secret(path).map(|mut b| Key::new(std::mem::take(&mut *b)))
}

/// The retired key files: names `<store_id>.<part>.key` in the directory,
/// as `glob("<store_id>.*.key")` lists them (the part may be empty or hold
/// dots). A directory that cannot be listed has none.
fn retired_parts(keys_dir: &Path, store_id: &str) -> Vec<String> {
    let Ok(entries) = fs::read_dir(keys_dir) else {
        return Vec::new();
    };
    let lead = format!("{store_id}.");
    let mut parts = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        if name.len() >= lead.len() + 4 && name.starts_with(&lead) && name.ends_with(".key") {
            parts.push(name[lead.len()..name.len() - 4].to_owned());
        }
    }
    parts.sort();
    parts
}

/// Read the ring for `store_id` under `keys_dir`.
pub fn load_key_ring(keys_dir: &Path, store_id: &str) -> Result<KeyRing, KeyRingError> {
    if !is_store_id(store_id) {
        return Err(KeyRingError::NotAStoreId);
    }
    let mut ring = KeyRing {
        current: read_key(&keys_dir.join(format!("{store_id}.key"))),
        head_file: read_file(&keys_dir.join(format!("{store_id}.head"))),
        origin_file: read_file(&keys_dir.join(format!("{store_id}.origin"))),
        ..KeyRing::default()
    };
    for part in retired_parts(keys_dir, store_id) {
        if let Some(key) = read_key(&keys_dir.join(format!("{store_id}.{part}.key"))) {
            ring.retired.insert(part, key);
        }
    }
    Ok(ring)
}

/// Whether the ring holds a key file that is there but could not be read
/// (for a caller that reports on the key, as `load_current` raises).
pub fn current_key_unreadable(keys_dir: &Path, store_id: &str) -> bool {
    let path = keys_dir.join(format!("{store_id}.key"));
    match fs::symlink_metadata(&path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => false,
        _ => read_file(&path).is_none(),
    }
}
