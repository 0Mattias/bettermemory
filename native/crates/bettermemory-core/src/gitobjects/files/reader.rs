//! The object questions of the reference's `ObjectReader`: unit 4 of the
//! port of `src/bettermemory/gitobjects.py` (see the README's "Git
//! objects read from a repository's files").
//!
//! `has`, `has_many`, `name_length`, `resolve`, `header`, `read`,
//! `commit`, `tree` and `fingerprint`, with `_raw`, `_places`, `_object`,
//! `_take_loose`, `_loose` and `_packed`, over the loaded state of
//! [`ObjectReader`]. Where the reference raises out of a question, the
//! answer is a [`Fault`]; what `_loose` and `_packed` catch (`OSError` and
//! `zlib.error`) is an absent object.

use std::collections::{BTreeSet, HashMap, HashSet};

use super::{FsHandle, ObjectReader, Stop};
use crate::fsdata::Fs;
use crate::gitfile_rule::hostpath::join;
use crate::gitobjects::stream::{
    chain, inflate_at, take, Failure, Handle, Stream, BODY_CHUNK, HEAD_CHUNK,
};
use crate::gitobjects::{
    apply_delta, commit_fingerprint, hex_of, is_hex, loose_header, parse_commit, parse_tree,
    Commit, Loose, PackIndex, Raised, TreeEntry, Wide, MIN_ABBREV,
};

/// `_LOOSE_HEAD`: inflated bytes that hold a loose header and its NUL.
pub const LOOSE_HEAD: u64 = 32;
/// `_MAX_OBJECT_BYTES`: the default cap of `read`.
pub const MAX_OBJECT_BYTES: u64 = 4 << 20;

/// What the reference raises out of an object question.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    /// What loading raises or declines (unit 3's [`Stop`]).
    Stop(Stop),
    /// `OverflowError`: a pack offset of 2**63 or more.
    Overflow,
    /// `IndexError`, `struct.error` or `ValueError` from a pack index or a
    /// loose header.
    Raised(Raised),
    /// `KeyError`: `_TYPE_NAMES[code]` of a code outside 1 to 4.
    Key(u8),
}

impl From<Stop> for Fault {
    fn from(stop: Stop) -> Fault {
        Fault::Stop(stop)
    }
}

/// An object as `_object` answers it: type, size and content.
pub type Found = (&'static str, Wide, Vec<u8>);

/// `_TYPE_NAMES[code]`.
fn type_name(code: u8) -> Result<&'static str, Fault> {
    match code {
        1 => Ok("commit"),
        2 => Ok("tree"),
        3 => Ok("blob"),
        4 => Ok("tag"),
        other => Err(Fault::Key(other)),
    }
}

/// `_HEX.fullmatch(text)` over a `str`: one or more ASCII hex digits.
fn is_hex_text(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(is_hex)
}

/// What `except (OSError, zlib.error)` makes of a failure: an absent
/// object; anything else goes through.
fn caught<T>(failure: Failure) -> Result<Option<T>, Fault> {
    match failure {
        Failure::Zlib(_) | Failure::Os(_) => Ok(None),
        Failure::Overflow => Err(Fault::Overflow),
        Failure::Raised(raised) => Err(Fault::Raised(raised)),
    }
}

macro_rules! guarded {
    ($e:expr) => {
        match $e {
            Ok(v) => v,
            Err(failure) => return caught(failure),
        }
    };
}

/// The body of `_loose` inside its `try`.
fn loose_in<H: Handle + ?Sized>(fh: &mut H, cap: Option<u64>) -> Result<Option<Found>, Fault> {
    let mut stream = Stream::new(
        fh,
        if cap.is_none() {
            HEAD_CHUNK
        } else {
            BODY_CHUNK
        },
    );
    let head = guarded!(stream.read(LOOSE_HEAD));
    let Some(Loose { kind, size, start }) = loose_header(&head).map_err(Fault::Raised)? else {
        return Ok(None);
    };
    let Some(cap) = cap else {
        return Ok(Some((kind, size, Vec::new())));
    };
    let Some(wanted) = size.to_u64().filter(|&s| s <= cap) else {
        return Ok(None);
    };
    let mut body = head[start..].to_vec();
    if body.len() as u64 > wanted {
        return Ok(None);
    }
    let rest = guarded!(stream.read_to_end(wanted - body.len() as u64));
    let Some(rest) = rest else {
        return Ok(None);
    };
    body.extend_from_slice(&rest);
    Ok(Some((kind, size, body)))
}

/// The body of `_packed` inside its `try`: the walk, then the base and
/// each delta inflated and applied from the base up.
fn packed_in<H: Handle + ?Sized>(
    fh: &mut H,
    pack: &PackIndex,
    offset: u64,
    cap: Option<u64>,
) -> Result<Option<Found>, Fault> {
    let Some((code, links)) = guarded!(chain(fh, pack, offset, cap)) else {
        return Ok(None);
    };
    let kind = type_name(code)?;
    if cap.is_none() {
        let top = &links[0];
        let size = top.target.clone().unwrap_or_else(|| top.size.clone());
        return Ok(Some((kind, size, Vec::new())));
    }
    let mut data = guarded!(inflate_at(fh, &links[links.len() - 1]));
    for link in links[..links.len() - 1].iter().rev() {
        let Some(base) = data else {
            return Ok(None);
        };
        let delta = guarded!(inflate_at(fh, link));
        data = delta.and_then(|delta| apply_delta(&base, &delta));
    }
    Ok(data.map(|data| (kind, Wide::from_u64(data.len() as u64), data)))
}

impl<F: Fs + ?Sized> ObjectReader<'_, F> {
    /// `has(sha)`.
    pub fn has(&mut self, sha: &str) -> Result<bool, Fault> {
        Ok(!self.has_many(&[sha])?.is_empty())
    }

    /// `has_many(shas)`: the names held, in lowercase.
    pub fn has_many(&mut self, shas: &[&str]) -> Result<BTreeSet<String>, Fault> {
        let mut want: Vec<(Vec<u8>, String)> = Vec::new();
        let mut at: HashMap<Vec<u8>, usize> = HashMap::new();
        for sha in shas {
            if let Some(raw) = self.raw(sha)? {
                let lower = sha.to_ascii_lowercase();
                match at.get(&raw) {
                    Some(&i) => want[i].1 = lower,
                    None => {
                        at.insert(raw.clone(), want.len());
                        want.push((raw, lower));
                    }
                }
            }
        }
        let mut found = BTreeSet::new();
        if !want.is_empty() {
            take(&self.packs, &mut want, &mut found).map_err(Fault::Raised)?;
        }
        if !want.is_empty() {
            self.take_loose(&mut want, &mut found);
        }
        if !want.is_empty() {
            let count = self.rescan()?;
            let new = &self.packs[self.packs.len() - count..];
            take(new, &mut want, &mut found).map_err(Fault::Raised)?;
        }
        Ok(found)
    }

    /// `name_length`.
    pub fn name_length(&mut self) -> Result<Option<usize>, Fault> {
        self.load()?;
        Ok(self.hash_len.map(|h| 2 * h))
    }

    /// `resolve(prefix)`: the one full name `prefix` abbreviates, in
    /// lowercase; None for none, for two or more, and for a prefix that
    /// is not hex of a length git accepts.
    pub fn resolve(&mut self, prefix: &str) -> Result<Option<String>, Fault> {
        self.load()?;
        let Some(length) = self.name_length()? else {
            return Ok(None);
        };
        let chars = prefix.chars().count();
        if !(MIN_ABBREV..=length).contains(&chars) || !is_hex_text(prefix) {
            return Ok(None);
        }
        let want = prefix.to_ascii_lowercase();
        if chars == length {
            return Ok(self.has(&want)?.then_some(want));
        }
        let mut found: HashSet<Vec<u8>> = HashSet::new();
        for pack in &self.packs {
            found.extend(pack.starting(&want, 2).map_err(Fault::Raised)?);
            if found.len() > 1 {
                return Ok(None);
            }
        }
        let (fan, rest) = want.as_bytes().split_at(2);
        let width = length - 2;
        for objdir in &self.dirs {
            let Ok(names) = self.fs.listdir(&join(self.flavour, objdir, fan)) else {
                continue;
            };
            for name in names {
                if name.len() == width && name.starts_with(rest) && name.iter().all(|&c| is_hex(c))
                {
                    let mut text = fan.to_vec();
                    text.extend_from_slice(&name);
                    found.insert(unhex(&text));
                    if found.len() > 1 {
                        return Ok(None);
                    }
                }
            }
        }
        let count = self.rescan()?;
        for pack in &self.packs[self.packs.len() - count..] {
            found.extend(pack.starting(&want, 2).map_err(Fault::Raised)?);
            if found.len() > 1 {
                return Ok(None);
            }
        }
        Ok(found.into_iter().next().map(|raw| hex_of(&raw)))
    }

    /// `header(sha)`: the type and the size from the headers alone.
    pub fn header(&mut self, sha: &str) -> Result<Option<(&'static str, Wide)>, Fault> {
        Ok(self.object(sha, None)?.map(|(kind, size, _)| (kind, size)))
    }

    /// `read(sha, max_size=max_size)`: the type and the content.
    pub fn read(
        &mut self,
        sha: &str,
        max_size: u64,
    ) -> Result<Option<(&'static str, Vec<u8>)>, Fault> {
        Ok(self
            .object(sha, Some(max_size))?
            .map(|(kind, _, data)| (kind, data)))
    }

    /// `commit(sha, max_size=max_size)`.
    pub fn commit(&mut self, sha: &str, max_size: u64) -> Result<Option<Commit>, Fault> {
        match (self.read(sha, max_size)?, self.hash_len) {
            (Some(("commit", body)), Some(hash_len)) => Ok(parse_commit(&body, hash_len)),
            _ => Ok(None),
        }
    }

    /// `tree(sha, max_size=max_size)`.
    pub fn tree(&mut self, sha: &str, max_size: u64) -> Result<Option<Vec<TreeEntry>>, Fault> {
        match (self.read(sha, max_size)?, self.hash_len) {
            (Some(("tree", body)), Some(hash_len)) => Ok(parse_tree(&body, hash_len)),
            _ => Ok(None),
        }
    }

    /// `fingerprint(sha, max_size=max_size)`.
    pub fn fingerprint(&mut self, sha: &str, max_size: u64) -> Result<Option<String>, Fault> {
        match self.read(sha, max_size)? {
            Some(("commit", body)) => Ok(commit_fingerprint(&body)),
            _ => Ok(None),
        }
    }

    /// `_raw(sha)`: the name's bytes where it is hex of the repository's
    /// length, counted in characters.
    fn raw(&mut self, sha: &str) -> Result<Option<Vec<u8>>, Fault> {
        self.load()?;
        let Some(hash_len) = self.hash_len else {
            return Ok(None);
        };
        if sha.chars().count() != 2 * hash_len || !is_hex_text(sha) {
            return Ok(None);
        }
        Ok(Some(unhex(sha.as_bytes())))
    }

    /// `_take_loose(want, found)`: a fanout directory two or more live
    /// names share is listed once (an error passes it over); a name alone
    /// is checked with `isfile`.
    fn take_loose(&mut self, want: &mut Vec<(Vec<u8>, String)>, found: &mut BTreeSet<String>) {
        let mut groups: Vec<(&[u8], Vec<usize>)> = Vec::new();
        for (i, (_, sha)) in want.iter().enumerate() {
            let prefix = &sha.as_bytes()[..2];
            match groups.iter_mut().find(|(p, _)| *p == prefix) {
                Some((_, members)) => members.push(i),
                None => groups.push((prefix, vec![i])),
            }
        }
        let mut taken = vec![false; want.len()];
        for objdir in &self.dirs {
            for (prefix, members) in &groups {
                let live: Vec<usize> = members.iter().copied().filter(|&i| !taken[i]).collect();
                if live.is_empty() {
                    continue;
                }
                let fan = join(self.flavour, objdir, prefix);
                let mut names: Option<HashSet<Vec<u8>>> = None;
                if live.len() > 1 {
                    match self.fs.listdir(&fan) {
                        Ok(listed) => names = Some(listed.into_iter().collect()),
                        Err(_) => continue,
                    }
                }
                for i in live {
                    let rest = &want[i].1.as_bytes()[2..];
                    let present = match &names {
                        Some(names) => names.contains(rest),
                        None => self.isfile(&join(self.flavour, &fan, rest)),
                    };
                    if present {
                        taken[i] = true;
                        found.insert(want[i].1.clone());
                    }
                }
            }
        }
        let mut flags = taken.into_iter();
        want.retain(|_| !flags.next().unwrap_or(false));
    }

    /// `_object(sha, cap=cap)` over `_places`: the loaded indexes that
    /// list it, its loose path in each object directory, then the indexes
    /// a rescan finds.
    fn object(&mut self, sha: &str, cap: Option<u64>) -> Result<Option<Found>, Fault> {
        let Some(raw) = self.raw(sha)? else {
            return Ok(None);
        };
        let packs = std::mem::take(&mut self.packs);
        let found = self.in_packs(&packs, &raw, cap);
        self.packs = packs;
        if let Some(found) = found? {
            return Ok(Some(found));
        }
        let hexsha = hex_of(&raw);
        for objdir in self.dirs.clone() {
            let fan = join(self.flavour, &objdir, &hexsha.as_bytes()[..2]);
            let path = join(self.flavour, &fan, &hexsha.as_bytes()[2..]);
            if let Some(found) = self.loose(&path, cap)? {
                return Ok(Some(found));
            }
        }
        let count = self.rescan()?;
        let packs = std::mem::take(&mut self.packs);
        let found = self.in_packs(&packs[packs.len() - count..], &raw, cap);
        self.packs = packs;
        found
    }

    fn in_packs(
        &mut self,
        packs: &[PackIndex],
        raw: &[u8],
        cap: Option<u64>,
    ) -> Result<Option<Found>, Fault> {
        // `_listed_in` is a generator: each pack is asked only once every
        // pack before it has failed to answer.
        for pack in packs {
            let Some(position) = pack.find(raw).map_err(Fault::Raised)? else {
                continue;
            };
            let Some(offset) = pack.offset(position).map_err(Fault::Raised)? else {
                continue;
            };
            if let Some(found) = self.packed(pack, offset, cap)? {
                return Ok(Some(found));
            }
        }
        Ok(None)
    }

    /// `_loose(path, cap=cap)`.
    fn loose(&mut self, path: &[u8], cap: Option<u64>) -> Result<Option<Found>, Fault> {
        let Some(file) = self.open(path)? else {
            return Ok(None);
        };
        let mut fh = FsHandle { fs: self.fs, file };
        let answer = loose_in(&mut fh, cap);
        let _ = self.fs.close(fh.file);
        answer
    }

    /// `_packed(pack, offset, cap=cap)`.
    fn packed(
        &mut self,
        pack: &PackIndex,
        offset: u64,
        cap: Option<u64>,
    ) -> Result<Option<Found>, Fault> {
        let Some(file) = self.open(&pack.pack)? else {
            return Ok(None);
        };
        let mut fh = FsHandle { fs: self.fs, file };
        let answer = packed_in(&mut fh, pack, offset, cap);
        let _ = self.fs.close(fh.file);
        answer
    }
}

/// `bytes.fromhex` of ASCII hex text of even length.
fn unhex(text: &[u8]) -> Vec<u8> {
    let digit = |c: u8| match c {
        b'0'..=b'9' => c - b'0',
        b'a'..=b'f' => c - b'a' + 10,
        _ => c - b'A' + 10,
    };
    text.chunks_exact(2)
        .map(|p| digit(p[0]) << 4 | digit(p[1]))
        .collect()
}
