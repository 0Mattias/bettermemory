//! Hostile input to `gitobjects::stream`: streams that inflate to far
//! more than is asked, a byte a read, chains at their bounds, rings of
//! names, every argument at its integer bounds and seeded garbage. No
//! panic, in debug and release, and no test past [`LIMIT`], so a loop
//! that does not end fails.
//!
//! Time is held against work measured in the same run, so the checks
//! hold on a loaded machine and in debug alike: four times the bytes in
//! under eight times the time, and a call costs less than clearing one
//! block of output (32 KiB), so a port that clears or allocates a block
//! on each call, or a block of 16 MiB for a short stream, fails. The
//! bounds in seconds are ceilings for a loaded machine, not the
//! reference's cost. For scale, CPython 3.13.13 with macOS's libz, on the
//! machine the fixtures were made on, takes 26 ms to inflate 64 MiB of
//! one byte, 91 ms for 200 KiB fed a byte a read and 8 ms for a chain of
//! 4,096 under a cap; the release build takes less for each.

mod common;

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use bettermemory_core::gitobjects::stream::{
    chain, inflate_at, listed_in, take, Failure, Link, MemHandle, Stream, BODY_CHUNK, HEAD_CHUNK,
};
use bettermemory_core::gitobjects::Wide;
use common::gitobjects::{index, wide, within, File, Rng};
use serde_json::{json, Value};

/// The most time a test here may take; each takes about a second in
/// debug.
const LIMIT: Duration = Duration::from_secs(120);

/// The fastest of three runs.
fn fastest(mut run: impl FnMut()) -> Duration {
    (0..3)
        .map(|_| {
            let start = Instant::now();
            run();
            start.elapsed()
        })
        .min()
        .unwrap()
}

/// What clearing the first block of output once costs here: 32 KiB, the
/// first block CPython's buffer and the port write into. The fastest of
/// three runs of 1,024 clears, over 1,024.
fn a_block_cleared() -> Duration {
    let mut block: Vec<u8> = Vec::new();
    fastest(|| {
        for _ in 0..1024 {
            block.clear();
            block.resize(32 << 10, 0);
            std::hint::black_box(&mut block);
        }
    }) / 1024
}

/// The Adler-32 of `n` bytes of `byte`.
fn adler_of_run(byte: u8, n: u64) -> [u8; 4] {
    let (n, x) = (u128::from(n), u128::from(byte));
    let a = (1 + n * x) % 65521;
    let b = (n + x * n * (n + 1) / 2) % 65521;
    (((b as u32) << 16) | a as u32).to_be_bytes()
}

/// A zlib stream of one fixed block: the literal `a`, then `matches`
/// matches of 258 at distance 1 (13 bits each): `1 + 258 * matches`
/// bytes of `a`.
fn run_lengths(matches: u64) -> (Vec<u8>, u64) {
    let mut out = vec![0x78, 0x9c];
    let (mut acc, mut held) = (0u64, 0u32);
    let mut bits = |out: &mut Vec<u8>, value: u64, count: u32| {
        acc |= value << held;
        held += count;
        while held >= 8 {
            out.push(acc as u8);
            acc >>= 8;
            held -= 8;
        }
    };
    bits(&mut out, 3, 3); // the last block, fixed codes
    bits(&mut out, 0x89, 8); // literal 0x61, its code high bit first
    for _ in 0..matches {
        bits(&mut out, 0xa3, 13); // length symbol 285, distance symbol 0
    }
    bits(&mut out, 0, 7); // end of block
    bits(&mut out, 0, 7); // to the byte
    let size = 1 + 258 * matches;
    out.extend(adler_of_run(0x61, size));
    (out, size)
}

fn adler(data: &[u8]) -> [u8; 4] {
    let (mut a, mut b) = (1u32, 0u32);
    for &byte in data {
        a = (a + u32::from(byte)) % 65521;
        b = (b + a) % 65521;
    }
    ((b << 16) | a).to_be_bytes()
}

/// A zlib stream of stored blocks of at most 65,535 bytes.
fn stored(data: &[u8]) -> Vec<u8> {
    let mut out = vec![0x78, 0x01];
    let mut blocks = data.chunks(65535).peekable();
    if blocks.peek().is_none() {
        out.extend([1, 0, 0, 0xff, 0xff]);
    }
    while let Some(block) = blocks.next() {
        out.push(u8::from(blocks.peek().is_none()));
        out.extend((block.len() as u16).to_le_bytes());
        out.extend((!(block.len() as u16)).to_le_bytes());
        out.extend(block);
    }
    out.extend(adler(data));
    out
}

fn no_index() -> bettermemory_core::gitobjects::PackIndex {
    index(&json!({ "h": 20, "ix": [] }))
}

#[test]
fn a_stream_of_a_gibibyte_is_inflated_only_as_far_as_asked() {
    within(LIMIT, || {
        // 6.8 MB that inflate to 1 GiB.
        let (bomb, size) = run_lengths((1 << 30) / 258 + 1);
        assert!(size > 1 << 30 && bomb.len() < 7 << 20);
        let started = Instant::now();
        // A header's read: 20 bytes, one chunk of the file.
        let mut fh = File::new(bomb.clone(), vec![]);
        let head = Stream::new(&mut fh, HEAD_CHUNK).read(20).unwrap();
        assert_eq!((head, fh.log.as_str()), (vec![0x61; 20], "r1024"));
        // An object of 4 MiB claimed: the stream is longer, which one byte
        // past the claim shows; one chunk of 65,536 holds it all.
        let mut fh = File::new(bomb.clone(), vec![]);
        let claimed = Link {
            start: 0,
            size: Wide::from_u64(4 << 20),
            target: None,
        };
        assert_eq!(inflate_at(&mut fh, &claimed), Ok(None));
        assert_eq!(fh.log, "s0 r65536");
        // The same at the largest read the reference makes at once.
        let mut fh = MemHandle::new(&bomb);
        let piece = Stream::new(&mut fh, BODY_CHUNK).read(1 << 24).unwrap();
        assert_eq!(piece.len(), 1 << 24);
        assert!(
            started.elapsed() < Duration::from_secs(60),
            "{:?}",
            started.elapsed()
        );
    });
}

#[test]
fn time_grows_with_the_bytes_inflated() {
    within(LIMIT, || {
        let whole = |mib: u64| {
            let (stream, size) = run_lengths((mib << 20) / 258 + 1);
            fastest(|| {
                let mut fh = MemHandle::new(&stream);
                let got = Stream::new(&mut fh, BODY_CHUNK).read_to_end(size).unwrap();
                assert_eq!(got.map(|data| data.len() as u64), Some(size));
            })
        };
        let (one, four) = (whole(16), whole(64));
        // Four times the bytes in about four times the time: a quadratic
        // reader would take sixteen times as long.
        assert!(
            four < one * 8 + Duration::from_millis(500),
            "16 MiB {one:?}, 64 MiB {four:?}"
        );
        assert!(four < Duration::from_secs(60), "64 MiB took {four:?}");
    });
}

#[test]
fn a_byte_a_read_costs_what_it_feeds() {
    within(LIMIT, || {
        // Bytes no block compresses, in stored blocks, fed one byte a read:
        // each call inflates one byte's worth, with nothing cleared anew.
        let noise = |kib: usize| -> Vec<u8> {
            let mut rng = Rng(kib as u64);
            (0..kib << 10).map(|_| rng.next() as u8).collect()
        };
        // The time, and the calls of zlib: one a byte of the stream.
        let fed = |kib: usize| {
            let data = noise(kib);
            let stream = stored(&data);
            let took = fastest(|| {
                let mut fh = MemHandle::new(&stream);
                let got = Stream::new(&mut fh, 1)
                    .read_to_end(data.len() as u64)
                    .unwrap();
                assert_eq!(got.as_deref(), Some(&data[..]));
            });
            (took, stream.len() as u32)
        };
        let ((one, _), (four, calls)) = (fed(50), fed(200));
        assert!(
            four < one * 8 + Duration::from_millis(500),
            "50 KiB {one:?}, 200 KiB {four:?}"
        );
        assert!(four < Duration::from_secs(30), "200 KiB took {four:?}");
        // A call costs less than clearing the first block once: a port
        // that cleared or allocated a block of 32 KiB on each call would
        // pay that and more.
        let (call, cleared) = (four / calls, a_block_cleared());
        assert!(
            call < cleared,
            "a call {call:?}, a block of 32 KiB cleared {cleared:?}"
        );
        // The limit a byte at a time over one chunk of 64 KiB: the tail is
        // kept and moved down each call, as the reference copies it.
        let data = noise(64);
        let stream = stored(&data);
        let started = Instant::now();
        let mut fh = MemHandle::new(&stream);
        let mut one_by_one = Stream::new(&mut fh, BODY_CHUNK);
        for byte in &data[..20_000] {
            assert_eq!(one_by_one.read(1).unwrap(), [*byte]);
        }
        assert!(
            started.elapsed() < Duration::from_secs(30),
            "{:?}",
            started.elapsed()
        );
    });
}

#[test]
fn a_short_stream_asked_for_16_mib_takes_one_block() {
    within(LIMIT, || {
        // Ten bytes, the check cut off: two calls of zlib, the second
        // with nothing to feed. Each writes into the first block, as
        // CPython's buffer starts with one of 32 KiB and grows only when
        // a call fills it. A read here (a new inflate's state and its
        // first block) costs a few blocks cleared; a port that cleared a
        // block of 16 MiB, or grew its block through the rest while zlib
        // waits for input, would pay 512 of them on each read.
        let cut = &stored(b"0123456789")[..17];
        let reads = 64;
        let took = fastest(|| {
            for _ in 0..reads {
                let mut fh = MemHandle::new(cut);
                let got = Stream::new(&mut fh, BODY_CHUNK).read(1 << 24).unwrap();
                assert_eq!(got, b"0123456789");
            }
        });
        let (read, cleared) = (took / reads, a_block_cleared());
        assert!(
            read < cleared * 128,
            "a read {read:?}, a block of 32 KiB cleared {cleared:?}"
        );
    });
}

/// The fixture's ladder: a base and `rungs` deltas, each 23 bytes after
/// the entry it names.
fn ladder(rungs: usize) -> Vec<u8> {
    let mut data = b"PACK\x00\x00\x00\x02\x00\x00\x00\x00".to_vec();
    data.push(0x3b);
    data.extend(stored(b"0123456789a"));
    let delta = [11, 10, 0x90, 0x0a, 0, 0, 0, 0, 0, 0];
    let unit = [&[0x6a, 23][..], &stored(&delta)].concat();
    assert_eq!((data.len(), unit.len()), (35, 23));
    data.extend(unit.repeat(rungs));
    data
}

#[test]
fn a_chain_at_its_bound_and_a_ring_of_names_end() {
    within(LIMIT, || {
        let data = ladder(100_000);
        let ix = no_index();
        let top = 35 + 23 * 99_999;
        let took = fastest(|| {
            let mut fh = MemHandle::new(&data);
            // 100,000 deltas deep: the walk stops at 4,096 entries.
            assert_eq!(chain(&mut fh, &ix, top, None), Ok(None));
            assert_eq!(chain(&mut fh, &ix, top, Some(4 << 20)), Ok(None));
            // 4,096 entries, each delta's head inflated under the cap.
            let at = 35 + 23 * 4094;
            let walked = chain(&mut fh, &ix, at, Some(4 << 20)).unwrap().unwrap();
            assert_eq!(walked.1.len(), 4096);
            // A cap of zero stops at the first entry.
            assert_eq!(chain(&mut fh, &ix, at, Some(0)), Ok(None));
        });
        assert!(took < Duration::from_secs(30), "the walks took {took:?}");

        // 6,000 entries, each naming the next by name and the last the
        // first: a ring. The walk ends at 4,096; from a ring of 100 it ends
        // where it meets an offset again.
        for ring in [6000usize, 100] {
            let name = |k: usize| -> Vec<u8> {
                let mut out = vec![0x10 + (k / 256) as u8, (k % 256) as u8];
                out.extend([0xee; 18]);
                out
            };
            let delta = stored(&[1, 1]);
            let mut pack = vec![0u8; 12];
            let mut listed: Vec<Value> = Vec::new();
            for k in 0..ring {
                listed.push(json!([common::hex(&name(k)), pack.len()]));
                pack.push(0x72);
                pack.extend(name((k + 1) % ring));
                pack.extend(&delta);
            }
            let ix = index(&json!({ "h": 20, "ix": listed }));
            let mut fh = File::new(pack, vec![]);
            let started = Instant::now();
            assert_eq!(chain(&mut fh, &ix, 12, None), Ok(None));
            let walked = fh.log.matches("r64").count();
            assert_eq!(walked, ring.min(4096));
            assert!(
                started.elapsed() < Duration::from_secs(30),
                "{:?}",
                started.elapsed()
            );
        }
    });
}

#[test]
fn every_argument_at_its_bounds() {
    within(LIMIT, || {
        let good = stored(b"0123456789");
        // A chunk of the largest count and of none; the largest read.
        let mut fh = MemHandle::new(&good);
        let all = Stream::new(&mut fh, usize::MAX).read(u64::MAX).unwrap();
        assert_eq!(all, b"0123456789");
        let mut fh = MemHandle::new(&good);
        assert_eq!(Stream::new(&mut fh, 0).read(5).unwrap(), b"");
        let mut fh = MemHandle::new(&good);
        assert_eq!(Stream::new(&mut fh, 0).read_to_end(0), Ok(None));
        let mut fh = MemHandle::new(&good);
        assert_eq!(Stream::new(&mut fh, 7).read_to_end(u64::MAX), Ok(None));
        let mut fh = MemHandle::new(&good);
        assert_eq!(Stream::new(&mut fh, 7).read(0).unwrap(), b"");

        // Offsets, caps and links at the ends of 64 bits, and sizes past it.
        let ix = no_index();
        let pack = [&[0u8; 12][..], &[0x3a], &good].concat();
        let caps = [None, Some(0), Some(1), Some(u64::MAX - 1), Some(u64::MAX)];
        for cap in caps {
            for at in [
                0,
                11,
                12,
                13,
                pack.len() as u64,
                u64::MAX / 2,
                u64::MAX / 2 + 1,
                u64::MAX,
            ] {
                let mut fh = MemHandle::new(&pack);
                let got = chain(&mut fh, &ix, at, cap);
                assert_eq!(
                    got == Err(Failure::Overflow),
                    at > u64::MAX / 2,
                    "{at} {cap:?}"
                );
            }
        }
        let widest = "f".repeat(112);
        for start in [0, 13, u64::MAX / 2, u64::MAX / 2 + 1, u64::MAX] {
            for size in ["0", "a", "ffffffffffffffff", "10000000000000000", &widest] {
                let link = Link {
                    start,
                    size: wide(size),
                    target: Some(wide(size)),
                };
                let mut fh = MemHandle::new(&pack);
                let got = inflate_at(&mut fh, &link);
                assert_eq!(got == Err(Failure::Overflow), start > u64::MAX / 2);
                assert_eq!(
                    got == Ok(Some(b"0123456789".to_vec())),
                    start == 13 && size == "a"
                );
            }
        }

        // A header of 64 continuation bytes, and a distance of 63 of them:
        // sizes and distances of any width, none an entry here.
        for first in [0xbf, 0xef, 0xff] {
            let mut wide_header = vec![0u8; 12];
            wide_header.push(first);
            wide_header.extend([0xff; 200]);
            let mut fh = MemHandle::new(&wide_header);
            assert_eq!(chain(&mut fh, &ix, 12, Some(u64::MAX)), Ok(None));
        }
        let mut far = vec![0u8; 12];
        far.push(0x62);
        far.extend([0xff; 62]);
        far.push(0x7f);
        far.extend(&good);
        let mut fh = MemHandle::new(&far);
        assert_eq!(chain(&mut fh, &ix, 12, None), Ok(None));
        // A base of a size past 64 bits is a link with no cap and none under
        // one; inflating it reads the stream and finds it short.
        let mut huge = vec![0u8; 12];
        huge.push(0xbf);
        huge.extend([0xff; 61]);
        huge.push(0x7f);
        huge.extend(&good);
        let mut fh = MemHandle::new(&huge);
        let (code, links) = chain(&mut fh, &ix, 12, None).unwrap().unwrap();
        assert_eq!((code, links.len(), links[0].size.to_u64()), (3, 1, None));
        assert_eq!(inflate_at(&mut fh, &links[0]), Ok(None));
        assert_eq!(chain(&mut fh, &ix, 12, Some(u64::MAX)), Ok(None));
    });
}

#[test]
fn seeded_garbage_never_panics() {
    within(LIMIT, || {
        let mut rng = Rng(0x6761_7262);
        let ix = no_index();
        let good = stored(b"0123456789abcdefghij");
        for round in 0..20_000 {
            let len = rng.below(300);
            let mut data: Vec<u8> = (0..len).map(|_| rng.next() as u8).collect();
            match round % 4 {
                // A plausible header, then garbage.
                1 if len >= 2 => data[..2].copy_from_slice(&[0x78, 0x9c]),
                // A good stream with a few bytes overwritten.
                2 => {
                    data = good.clone();
                    for _ in 0..1 + rng.below(4) {
                        let at = rng.below(data.len());
                        data[at] = rng.next() as u8;
                    }
                }
                // One byte over and over.
                3 => data.fill(rng.next() as u8),
                _ => {}
            }
            let chunk = [1, 2, 7, 64, 1024][rng.below(5)];
            let mut fh = MemHandle::new(&data);
            let mut stream = Stream::new(&mut fh, chunk);
            let _ = stream.read(32);
            let _ = stream.read_to_end(rng.below(64) as u64);
            let _ = stream.read(u64::MAX);
            let mut fh = MemHandle::new(&data);
            let cap = [None, Some(0), Some(40), Some(u64::MAX)][rng.below(4)];
            if let Ok(Some((_, links))) = chain(&mut fh, &ix, rng.below(len + 2) as u64, cap) {
                for link in &links {
                    let _ = inflate_at(&mut fh, link);
                }
            }
        }
    });
}

#[test]
fn a_hundred_thousand_names_are_looked_up_in_time() {
    within(LIMIT, || {
        let name = |k: u32| -> Vec<u8> {
            let mut out = (k.wrapping_mul(2_654_435_761)).to_be_bytes().to_vec();
            out.extend(k.to_be_bytes());
            out.extend([0x5a; 12]);
            out
        };
        let mut names: Vec<Vec<u8>> = (0..100_000).map(name).collect();
        names.sort();
        let listed: Vec<Value> = names
            .iter()
            .enumerate()
            .map(|(i, n)| json!([common::hex(n), 12 + i]))
            .collect();
        let packs = [index(&json!({ "h": 20, "ix": listed })), no_index()];
        let took = fastest(|| {
            // Half the wanted names are listed, half are not.
            let mut want: Vec<(Vec<u8>, String)> = (50_000..150_000)
                .map(|k| (name(k), k.to_string()))
                .collect();
            let mut found = BTreeSet::new();
            assert_eq!(take(&packs, &mut want, &mut found), Ok(()));
            assert_eq!((want.len(), found.len()), (50_000, 50_000));
            assert_eq!(want[0].1, "100000");
            for k in (0..150_000).step_by(7) {
                let found = listed_in(&packs, &name(k)).unwrap();
                assert_eq!(found.len(), usize::from(k < 100_000));
            }
        });
        assert!(took < Duration::from_secs(30), "the lookups took {took:?}");
    });
}
