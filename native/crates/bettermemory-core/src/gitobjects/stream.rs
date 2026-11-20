//! The zlib streams and the delta chain of `src/bettermemory/gitobjects.py`:
//! unit 2 of its port (see the README's "Git objects read from a
//! repository's files").
//!
//! [`Stream`] is `_Stream`: a zlib stream inflated on demand from a file
//! handle the caller gives ([`Handle`]: `seek` and `read`), fed one chunk
//! of compressed bytes at a time. [`chain`] is `_chain`, the walk down a
//! delta chain by entry headers and delta heads alone; [`inflate_at`],
//! [`listed_in`] and [`take`] are `_inflate_at`, `_listed_in` and `_take`.
//!
//! The inflate is the `zlib-rs` crate, driven as CPython's
//! `zlib.decompressobj().decompress(data, max_length)` drives zlib: one
//! `inflate(Z_SYNC_FLUSH)` per block of output, so a call stops where
//! zlib stops (at the next symbol that needs output once the limit is
//! reached, past an end of block, a block header and the stream's check
//! where no output is needed) and consumes the input zlib consumes. What
//! a stream asks of its handle, and when it raises, is therefore the
//! reference's for valid, damaged, cut and trailed streams alike.
//!
//! Where the reference raises, the answer is a [`Failure`]: the callers
//! of a later unit catch `OSError` and `zlib.error` and let the rest
//! through, as the reference does.

use std::collections::{BTreeSet, HashSet};

use zlib_rs::{Inflate, InflateError, InflateFlush, Status};

use super::{delta_sizes, entry_header, Base, PackIndex, Raised, Wide};
use crate::fsdata::PyErr;

/// `_ENTRY_PEEK`: bytes read at a pack entry's offset for its header.
pub const ENTRY_PEEK: usize = 64;
/// `_HEAD_CHUNK`: compressed bytes fed per read where a header is wanted.
pub const HEAD_CHUNK: usize = 1024;
/// `_BODY_CHUNK`: the most fed per read where a whole object is wanted.
pub const BODY_CHUNK: usize = 1 << 16;
/// `_CHUNK_MARGIN`: fed beyond a packed entry's inflated size.
pub const CHUNK_MARGIN: u64 = 64;
/// `_PIECE`: the most output asked of zlib in one call.
pub const PIECE: usize = 1 << 24;
/// `_DELTA_HEAD`: inflated bytes that hold a delta's two size varints.
pub const DELTA_HEAD: u64 = 20;
/// `_WORK_FACTOR`: the total a read may inflate, as a multiple of its cap.
pub const WORK_FACTOR: u128 = 16;
/// `_MAX_CHAIN`: entries one read may follow down a delta chain.
pub const MAX_CHAIN: usize = 4096;

/// The sizes CPython's output buffer grows by, block after block
/// (`pycore_blocks_output_buffer.h`); their sum passes [`PIECE`].
const BLOCKS: [usize; 7] = [
    32 << 10,
    64 << 10,
    256 << 10,
    1 << 20,
    4 << 20,
    8 << 20,
    16 << 20,
];

/// The open file a stream or a walk reads (`FileIO`, unbuffered).
pub trait Handle {
    /// `fh.seek(offset)` from the file's start, for an offset below
    /// 2**63 (the callers here refuse a larger one first, as `FileIO`
    /// does). The error is the `OSError` `lseek` raises.
    fn seek(&mut self, offset: u64) -> Result<(), PyErr>;
    /// `fh.read(n) or b""`: one read of at most `n` bytes from the
    /// position, which moves past them. Empty at the end of the file and
    /// where a non-blocking read has nothing (`FileIO` answers None).
    fn read(&mut self, n: usize) -> Result<Vec<u8>, PyErr>;
}

/// A file's bytes held in memory, read as `FileIO` reads a regular file:
/// a seek goes anywhere, a read past the end is empty.
#[derive(Clone, Debug)]
pub struct MemHandle<'a> {
    data: &'a [u8],
    at: u64,
}

impl<'a> MemHandle<'a> {
    pub fn new(data: &'a [u8]) -> MemHandle<'a> {
        MemHandle { data, at: 0 }
    }
}

impl Handle for MemHandle<'_> {
    fn seek(&mut self, offset: u64) -> Result<(), PyErr> {
        self.at = offset;
        Ok(())
    }

    fn read(&mut self, n: usize) -> Result<Vec<u8>, PyErr> {
        let Some(rest) = usize::try_from(self.at)
            .ok()
            .and_then(|at| self.data.get(at..))
        else {
            return Ok(Vec::new());
        };
        let out = rest[..n.min(rest.len())].to_vec();
        self.at += out.len() as u64;
        Ok(out)
    }
}

/// What the reference raises out of a stream or a walk.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// `zlib.error`, with zlib's return code: -3 (`Z_DATA_ERROR`) for a
    /// stream that is not valid, 2 (`Z_NEED_DICT`) for one whose header
    /// asks for a preset dictionary. The text after the code is the zlib
    /// build's and no caller reads it.
    Zlib(i32),
    /// `OSError`, or the subclass named, from the handle.
    Os(PyErr),
    /// `OverflowError`: `fh.seek` of an offset of 2**63 or more.
    Overflow,
    /// `IndexError` or `struct.error` from the pack index.
    Raised(Raised),
}

fn seek<H: Handle + ?Sized>(fh: &mut H, offset: u64) -> Result<(), Failure> {
    if offset > i64::MAX as u64 {
        return Err(Failure::Overflow);
    }
    fh.seek(offset).map_err(Failure::Os)
}

/// `zlib.decompressobj().decompress(data, limit)` for a limit from 1 to
/// [`PIECE`], its output appended to `out`: how much of `data` it
/// consumed and whether the stream ended, or the code of the
/// `zlib.error` it raises. `block` is the room one `inflate` writes
/// into, kept between calls so that a call costs what it reads and
/// writes, not a block cleared each time.
fn decompress(
    z: &mut Inflate,
    data: &[u8],
    limit: usize,
    out: &mut Vec<u8>,
    block: &mut Vec<u8>,
) -> Result<(usize, bool), i32> {
    let (start, mut used) = (out.len(), 0);
    for size in BLOCKS {
        let room = size.min(limit - (out.len() - start));
        if block.len() < room {
            block.resize(room, 0);
        }
        let (read, written) = (z.total_in(), z.total_out());
        let status = z.decompress(&data[used..], &mut block[..room], InflateFlush::SyncFlush);
        used += (z.total_in() - read) as usize;
        let wrote = (z.total_out() - written) as usize;
        out.extend_from_slice(&block[..wrote]);
        match status {
            Err(InflateError::NeedDict { .. }) => return Err(2),
            Err(InflateError::StreamError) => return Err(-2),
            Err(InflateError::DataError) => return Err(-3),
            Err(InflateError::MemError) => return Err(-4),
            Ok(Status::StreamEnd) => return Ok((used, true)),
            // The block has room left (zlib wants input), or the limit
            // is reached.
            Ok(_) if wrote < room || out.len() - start == limit => break,
            Ok(_) => {}
        }
    }
    Ok((used, false))
}

/// `_Stream`: a zlib stream inflated on demand from a file, fed `chunk`
/// compressed bytes at a time.
pub struct Stream<'a, H: Handle + ?Sized> {
    fh: &'a mut H,
    chunk: usize,
    z: Inflate,
    eof: bool,
    pending: Vec<u8>,
    block: Vec<u8>,
}

impl<'a, H: Handle + ?Sized> Stream<'a, H> {
    /// `_Stream(fh, chunk)`: the stream that starts at the handle's
    /// position.
    pub fn new(fh: &'a mut H, chunk: usize) -> Stream<'a, H> {
        Stream {
            fh,
            chunk,
            z: Inflate::new(true, 15),
            eof: false,
            pending: Vec::new(),
            block: Vec::new(),
        }
    }

    /// `read(n)`: up to `n` inflated bytes, fewer only where the stream
    /// ends or the file does (or a read of the handle comes back empty).
    pub fn read(&mut self, n: u64) -> Result<Vec<u8>, Failure> {
        let mut out = Vec::new();
        while (out.len() as u64) < n && !self.eof {
            let held = !self.pending.is_empty();
            let mut fed = if held {
                std::mem::take(&mut self.pending)
            } else {
                self.fh.read(self.chunk).map_err(Failure::Os)?
            };
            let limit = (n - out.len() as u64).min(PIECE as u64) as usize;
            let (before, given) = (out.len(), fed.len());
            let done = decompress(&mut self.z, &fed, limit, &mut out, &mut self.block);
            let (used, end) = match done {
                Ok(done) => done,
                Err(code) => {
                    // The reference assigns `_pending` after the call, so
                    // a raise leaves it as it was: what it held, or
                    // nothing where the bytes came from the file.
                    if held {
                        self.pending = fed;
                    }
                    return Err(Failure::Zlib(code));
                }
            };
            self.eof = end;
            // `unconsumed_tail`: what the call left of its input. At the
            // stream's end CPython moves the rest to `unused_data` and
            // rewrites the tail only where it was not empty before.
            if (used < given && !end) || held {
                fed.drain(..used);
                self.pending = fed;
            }
            if out.len() == before && self.pending.len() == given {
                break;
            }
        }
        Ok(out)
    }

    /// `read_to_end(n)`: exactly `n` inflated bytes that end the stream;
    /// None for a stream that is shorter, longer or cut off before its
    /// end.
    pub fn read_to_end(&mut self, n: u64) -> Result<Option<Vec<u8>>, Failure> {
        let data = self.read(n)?;
        if data.len() as u64 != n || !self.read(1)?.is_empty() || !self.eof {
            return Ok(None);
        }
        Ok(Some(data))
    }
}

/// `_Link`: one entry of a delta chain: where its zlib stream starts, its
/// inflated size as its header states it, and for a delta the target size
/// its instructions state, where read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Link {
    pub start: u64,
    pub size: Wide,
    pub target: Option<Wide>,
}

/// `_chain(fh, pack, offset, cap=cap)`: the type code of the base the
/// chain from `offset` ends at, and its entries from `offset` down to
/// that base. None for a damaged header, a base that is not there, a
/// cycle, a chain past [`MAX_CHAIN`], and under a cap for a size or a
/// target above it or a total above [`WORK_FACTOR`] times it.
pub fn chain<H: Handle + ?Sized>(
    fh: &mut H,
    pack: &PackIndex,
    mut offset: u64,
    cap: Option<u64>,
) -> Result<Option<(u8, Vec<Link>)>, Failure> {
    let mut links: Vec<Link> = Vec::new();
    let mut seen = HashSet::new();
    let mut spent: u128 = 0;
    loop {
        if seen.len() >= MAX_CHAIN || !seen.insert(offset) {
            return Ok(None);
        }
        seek(fh, offset)?;
        let peek = fh.read(ENTRY_PEEK).map_err(Failure::Os)?;
        let Some((code, size, base, length)) = entry_header(&peek, pack.hash_len) else {
            return Ok(None);
        };
        // The seek held `offset` below 2**63 and a header is 64 bytes at
        // most, so the sum fits.
        let start = offset + length as u64;
        let mut target = None;
        if base != Base::None && (cap.is_some() || links.is_empty()) {
            seek(fh, start)?;
            let head = Stream::new(fh, HEAD_CHUNK).read(DELTA_HEAD)?;
            let Some((_, stated, _)) = delta_sizes(&head) else {
                return Ok(None);
            };
            target = Some(stated);
        }
        if let Some(cap) = cap {
            // A size past the cap is refused, so only sizes within it
            // (64 bits each, 8,192 at most) are ever summed.
            let within = |v: &Wide| v.to_u64().filter(|&v| v <= cap);
            let (Some(size), Some(target)) =
                (within(&size), target.as_ref().map_or(Some(0), within))
            else {
                return Ok(None);
            };
            spent += u128::from(size) + u128::from(target);
            if spent > WORK_FACTOR * u128::from(cap) {
                return Ok(None);
            }
        }
        links.push(Link {
            start,
            size,
            target,
        });
        offset = match base {
            Base::None => return Ok(Some((code, links))),
            Base::Distance(back) => match back.to_u64() {
                Some(back) if 0 < back && back < offset => offset - back,
                _ => return Ok(None),
            },
            Base::Name(name) => {
                let Some(position) = pack.find(&name).map_err(Failure::Raised)? else {
                    return Ok(None);
                };
                match pack.offset(position).map_err(Failure::Raised)? {
                    Some(located) => located,
                    None => return Ok(None),
                }
            }
        };
    }
}

/// `_inflate_at(fh, link)`: the whole inflated stream of one chain entry,
/// of exactly its size. A size past 64 bits is read as 2**64 - 1: either
/// way the stream ends first and the answer is None.
pub fn inflate_at<H: Handle + ?Sized>(fh: &mut H, link: &Link) -> Result<Option<Vec<u8>>, Failure> {
    seek(fh, link.start)?;
    let size = link.size.to_u64().unwrap_or(u64::MAX);
    let chunk = size.saturating_add(CHUNK_MARGIN).min(BODY_CHUNK as u64) as usize;
    Stream::new(fh, chunk).read_to_end(size)
}

/// `_listed_in(packs, raw)`: each pack that lists the object named `raw`
/// at an offset its index holds, with that offset, in the packs' order.
/// The reference's generator raises at its first pack or never (an empty
/// name is `IndexError` there), so the list is whole or the error.
pub fn listed_in<'a>(
    packs: &'a [PackIndex],
    raw: &[u8],
) -> Result<Vec<(&'a PackIndex, u64)>, Raised> {
    let mut out = Vec::new();
    for pack in packs {
        if let Some(position) = pack.find(raw)? {
            if let Some(located) = pack.offset(position)? {
                out.push((pack, located));
            }
        }
    }
    Ok(out)
}

/// `_take(packs, want, found)`: moves the names in `want` that `packs`
/// list into `found`. `want` is the reference's dict in its order: each
/// raw name once, with its lowercase hex. A raise (an empty name) comes
/// before the pack it met has moved anything.
pub fn take(
    packs: &[PackIndex],
    want: &mut Vec<(Vec<u8>, String)>,
    found: &mut BTreeSet<String>,
) -> Result<(), Raised> {
    for pack in packs {
        let mut hits = Vec::with_capacity(want.len());
        for (raw, _) in want.iter() {
            hits.push(pack.find(raw)?.is_some());
        }
        let mut hits = hits.into_iter();
        for (raw, sha) in std::mem::take(want) {
            if hits.next() == Some(true) {
                found.insert(sha);
            } else {
                want.push((raw, sha));
            }
        }
        if want.is_empty() {
            return Ok(());
        }
    }
    Ok(())
}
