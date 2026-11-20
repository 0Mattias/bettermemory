//! Answers of `gitobjects::stream` a plausible wrong port misses. Each
//! expectation is the reference's, run on the same bytes
//! (`gitobjects._Stream`, `_chain`, `_inflate_at`, `_listed_in`, `_take`).

mod common;

use std::collections::BTreeSet;

use bettermemory_core::fsdata::PyErr;
use bettermemory_core::gitobjects::stream::{
    chain, inflate_at, listed_in, take, Failure, Handle, Link, MemHandle, Stream,
};
use bettermemory_core::gitobjects::{PackIndex, Raised, Wide};
use common::gitobjects::{index, ops, wide, File};
use common::unhex;
use serde_json::{json, Value};

/// `0123456789abcdefghij` in one fixed block: 28 bytes.
const GOOD: &str = "789c3330343236313533b7b04c4c4a4e494d4bcfc8cc020035070605";
const PLAIN: &[u8] = b"0123456789abcdefghij";

fn adler(data: &[u8]) -> [u8; 4] {
    let (mut a, mut b) = (1u32, 0u32);
    for &byte in data {
        a = (a + u32::from(byte)) % 65521;
        b = (b + a) % 65521;
    }
    ((b << 16) | a).to_be_bytes()
}

/// A zlib stream of one stored block.
fn stored(data: &[u8]) -> Vec<u8> {
    let mut out = vec![0x78, 0x01, 0x01];
    out.extend((data.len() as u16).to_le_bytes());
    out.extend((!(data.len() as u16)).to_le_bytes());
    out.extend(data);
    out.extend(adler(data));
    out
}

/// The answers of a stream's calls: `r` is `read`, `e` is `read_to_end`.
fn run(data: &[u8], chunk: usize, script: &[i64], calls: &[(char, u64)]) -> Value {
    let mut fh = File::new(data.to_vec(), script.to_vec());
    let calls: Vec<(bool, u64)> = calls.iter().map(|&(k, n)| (k == 'e', n)).collect();
    Value::Array(ops(&mut fh, chunk, &calls))
}

fn b(data: &[u8]) -> Value {
    json!(["b", common::hex(data)])
}

#[test]
fn the_end_is_seen_with_the_last_byte_where_the_chunk_holds_it() {
    let good = unhex(GOOD);
    // One read: zlib goes on past a full output to the check.
    assert_eq!(
        run(&good, 1024, &[], &[('e', 20)]),
        json!([[b(PLAIN), 1, 28]])
    );
    // The check in the next chunk: `read(1)` fetches it.
    assert_eq!(
        run(&good, 24, &[], &[('e', 20)]),
        json!([[b(PLAIN), 2, 28]])
    );
    // Bytes after the stream, and a second stream, are not looked at.
    let mut tailed = good.clone();
    tailed.extend(b"tail");
    let want = json!([[b(PLAIN), 1, 32], [b(b""), 1, 32]]);
    assert_eq!(run(&tailed, 1024, &[], &[('e', 20), ('r', 1)]), want);
    let twice = [good.clone(), good.clone()].concat();
    assert_eq!(
        run(&twice, 1024, &[], &[('e', 20)]),
        json!([[b(PLAIN), 1, 56]])
    );
}

#[test]
fn read_to_end_is_none_for_longer_shorter_and_cut() {
    let good = unhex(GOOD);
    assert_eq!(run(&good, 1024, &[], &[('e', 19)]), json!([[["n"], 1, 28]]));
    assert_eq!(run(&good, 1024, &[], &[('e', 21)]), json!([[["n"], 1, 28]]));
    // The last byte of the check missing: all 20 bytes come, the end
    // does not.
    let cut = &good[..27];
    assert_eq!(run(cut, 1024, &[], &[('e', 20)]), json!([[["n"], 2, 27]]));
    assert_eq!(
        run(cut, 1024, &[], &[('r', 20)]),
        json!([[b(PLAIN), 1, 27]])
    );
}

#[test]
fn a_wrong_check_raises_with_the_last_byte_not_before() {
    let mut wrong = unhex(GOOD);
    *wrong.last_mut().unwrap() ^= 1;
    // All 20 bytes are there, and the call that takes the last raises.
    let want = json!([[["z", -3], 1, 28], [["z", -3], 2, 28]]);
    assert_eq!(run(&wrong, 1024, &[], &[('r', 20), ('r', 1)]), want);
    let want = json!([[b(&PLAIN[..19]), 1, 28], [["z", -3], 1, 28]]);
    assert_eq!(run(&wrong, 1024, &[], &[('r', 19), ('r', 1)]), want);
}

#[test]
fn past_a_met_limit_a_bad_block_raises_and_a_far_match_waits() {
    // A stored block of 20 bytes, then a block of type 3.
    let bad = unhex("789c001400ebff303132333435363738396162636465666768696a0735070605");
    assert_eq!(
        run(&bad, 1024, &[], &[('r', 20)]),
        json!([[["z", -3], 1, 32]])
    );
    // The bad block in the next chunk: the 20 bytes come first.
    let want = json!([[b(PLAIN), 1, 27], [["z", -3], 2, 32], [["z", -3], 3, 32]]);
    assert_eq!(run(&bad, 27, &[], &[('r', 20), ('r', 1), ('r', 1)]), want);
    // Five literals, then a match nine back: the limit of five is met
    // and the match is not looked at until a byte of it is asked.
    let far = unhex("789c333034323601320000000001");
    let want = json!([[b(b"01234"), 1, 14], [["z", -3], 1, 14], [["z", -3], 1, 14]]);
    assert_eq!(run(&far, 1024, &[], &[('r', 5), ('r', 1), ('r', 1)]), want);
}

#[test]
fn after_a_raise_a_stream_feeds_what_it_held_or_reads_on() {
    // Nothing held: each call after the raise reads another chunk.
    let bad = unhex("789d000000000000000000");
    let want = json!([
        [["z", -3], 1, 3],
        [["z", -3], 2, 6],
        [["z", -3], 3, 9],
        [["z", -3], 4, 11]
    ]);
    assert_eq!(run(&bad, 3, &[], &[('r', 1); 4]), want);
    // The tail held: byte 20 meets the bad block, and no call reads on.
    let bad = unhex("789c001400ebff303132333435363738396162636465666768696a0735070605");
    let got = run(&bad, 1024, &[], &[('r', 1); 22]);
    let got = got.as_array().unwrap();
    assert_eq!(got[18], json!([b(b"i"), 1, 32]));
    for call in &got[19..] {
        assert_eq!(*call, json!([["z", -3], 1, 32]));
    }
}

#[test]
fn a_preset_dictionary_is_error_2() {
    let preset = unhex("7820010203043330343236313533b7b04c4c4a4e494d4bcfc8cc020035070605");
    let want = json!([[["z", 2], 1, 32], [["z", 2], 2, 32]]);
    assert_eq!(run(&preset, 1024, &[], &[('r', 1), ('r', 1)]), want);
}

#[test]
fn an_empty_read_ends_the_call_and_a_raise_loses_what_it_had() {
    let good = unhex(GOOD);
    let script = [8, 0, 8, 8];
    let want = json!([[b(b"01234"), 2, 8], [b(&PLAIN[5..]), 5, 28]]);
    assert_eq!(run(&good, 8, &script, &[('r', 20), ('r', 20)]), want);
    // The five bytes the first call held go with its raise.
    let script = [8, -1, 8, 8];
    let want = json!([[["x", "OSError"], 2, 8], [b(&PLAIN[5..]), 5, 28]]);
    assert_eq!(run(&good, 8, &script, &[('r', 20), ('r', 20)]), want);
    let want = json!([[["x", "PermissionError"], 1, 0]]);
    assert_eq!(run(&good, 8, &[-2], &[('e', 20)]), want);
    // No byte asked: the handle is not read.
    assert_eq!(run(&good, 8, &[-1], &[('r', 0)]), json!([[b(b""), 0, 0]]));
}

/// A pack of a base of 10 bytes at 12, and what `more` appends, with an
/// index of the names given (sorted here).
fn pack(more: &[u8], names: &[(&[u8; 20], Value)]) -> (Vec<u8>, PackIndex) {
    let mut data = b"PACK\x00\x00\x00\x02\x00\x00\x00\x00".to_vec();
    data.push(0x3a);
    data.extend(stored(b"0123456789"));
    assert_eq!(data.len(), 34);
    data.extend(more);
    let mut listed: Vec<(String, Value)> = names
        .iter()
        .map(|(name, at)| (common::hex(*name), at.clone()))
        .collect();
    listed.sort_by(|x, y| x.0.cmp(&y.0));
    let ix: Vec<Value> = listed.into_iter().map(|(n, at)| json!([n, at])).collect();
    (data, index(&json!({ "h": 20, "ix": ix })))
}

/// A delta entry by distance: its header and its stream.
fn ofs(back: u8, stream: &[u8]) -> Vec<u8> {
    assert!(back < 128);
    [&[0x62, back][..], stream].concat()
}

fn link(start: u64, size: u64, target: Option<u64>) -> Link {
    Link {
        start,
        size: Wide::from_u64(size),
        target: target.map(Wide::from_u64),
    }
}

#[test]
fn a_target_is_read_for_the_first_delta_and_under_a_cap_for_each() {
    // At 34 a delta whose stream fails its header check; at 49 a delta
    // on it.
    let good = stored(&[10, 12]);
    let mut bad = good.clone();
    bad[1] = 0x9d;
    let more = [ofs(22, &bad), ofs(15, &good)].concat();
    let (data, ix) = pack(&more, &[]);
    let mut fh = File::new(data.clone(), vec![]);
    let walked = chain(&mut fh, &ix, 49, None).unwrap().unwrap();
    assert_eq!(walked.0, 3);
    let want = vec![link(51, 2, Some(12)), link(36, 2, None), link(13, 10, None)];
    assert_eq!(walked.1, want);
    assert_eq!(fh.log, "s49 r64 s51 r1024 s34 r64 s12 r64");
    let mut fh = File::new(data.clone(), vec![]);
    assert_eq!(chain(&mut fh, &ix, 49, Some(100)), Err(Failure::Zlib(-3)));
    assert_eq!(fh.log, "s49 r64 s51 r1024 s34 r64 s36 r1024");
    // The first delta's own head is read with no cap too.
    let mut fh = File::new(data, vec![]);
    assert_eq!(chain(&mut fh, &ix, 34, None), Err(Failure::Zlib(-3)));
}

/// The fixture's ladder: a base of 11 bytes at 12 and `rungs` deltas of
/// 10 bytes stating a target of 10, each 23 bytes after the one it names.
fn ladder(rungs: usize) -> (Vec<u8>, PackIndex) {
    let mut data = b"PACK\x00\x00\x00\x02\x00\x00\x00\x00".to_vec();
    data.push(0x3b);
    data.extend(stored(b"0123456789a"));
    let delta = [11, 10, 0x90, 0x0a, 0, 0, 0, 0, 0, 0];
    let unit = [&[0x6a, 23][..], &stored(&delta)].concat();
    assert_eq!((data.len(), unit.len()), (35, 23));
    data.extend(unit.repeat(rungs));
    (data, index(&json!({ "h": 20, "ix": [] })))
}

fn rung(k: u64) -> u64 {
    35 + 23 * k
}

#[test]
fn a_cap_holds_a_size_at_it_and_a_total_of_sixteen_of_it() {
    let (data, ix) = ladder(12);
    let walk = |at: u64, cap: u64| {
        let mut fh = File::new(data.clone(), vec![]);
        let got = chain(&mut fh, &ix, at, Some(cap)).unwrap();
        (got.map(|(_, links)| links.len()), fh.log.split(' ').count())
    };
    // Rungs of 10 and 10, a base of 11. Under a cap of 11: 151 and 171
    // are within 176, 180 is not.
    assert_eq!(walk(rung(6), 11), (Some(8), 30));
    assert_eq!(walk(rung(7), 11), (Some(9), 34));
    assert_eq!(walk(rung(8), 11), (None, 36));
    // Under a cap of 10: 160 is not past 160, so the base is read, and
    // its 11 bytes are past the cap; 180 stops at the ninth rung.
    assert_eq!(walk(rung(7), 10), (None, 34));
    assert_eq!(walk(rung(8), 10), (None, 36));
    // A rung's size or target one past the cap stops at the first.
    assert_eq!(walk(rung(3), 9), (None, 4));
}

#[test]
fn a_chain_is_4096_entries_at_most() {
    let (data, ix) = ladder(4096);
    let mut fh = MemHandle::new(&data);
    let (code, links) = chain(&mut fh, &ix, rung(4094), None).unwrap().unwrap();
    assert_eq!((code, links.len()), (3, 4096));
    assert_eq!(links[0], link(rung(4094) + 2, 10, Some(10)));
    assert_eq!(links[4095], link(13, 11, None));
    assert_eq!(chain(&mut fh, &ix, rung(4095), None), Ok(None));
    let capped = chain(&mut fh, &ix, rung(4094), Some(4 << 20))
        .unwrap()
        .unwrap();
    assert!(capped.1[..4095]
        .iter()
        .all(|l| l.target == Some(Wide::from_u64(10))));
    assert_eq!(chain(&mut fh, &ix, rung(4095), Some(4 << 20)), Ok(None));
}

#[test]
fn a_base_is_before_its_delta_in_the_pack_or_listed_in_its_index() {
    let delta = stored(&[10, 12]);
    let (a, c, missing) = ([0x11u8; 20], [0x33u8; 20], [0x44u8; 20]);
    let by_name = |name: &[u8; 20]| [&[0x72][..], name, &delta].concat();
    // At 34 distance 0, at 49 distance 49 (its own offset), at 64 a
    // name the index lacks, at 98 its own name, at 132 the base's name.
    let more = [
        ofs(0, &delta),
        ofs(49, &delta),
        by_name(&missing),
        by_name(&c),
        by_name(&a),
    ]
    .concat();
    let (data, ix) = pack(&more, &[(&a, json!(12)), (&c, json!(98))]);
    let mut fh = MemHandle::new(&data);
    for at in [34, 49, 64, 98] {
        assert_eq!(chain(&mut fh, &ix, at, None), Ok(None), "at {at}");
    }
    let (code, links) = chain(&mut fh, &ix, 132, Some(12)).unwrap().unwrap();
    let want = vec![link(153, 2, Some(12)), link(13, 10, None)];
    assert_eq!((code, links), (3, want));
    // Distance 21 from 34 is offset 13, inside the base's stream: what
    // is read there as a header names a base the index lacks.
    let (data, ix) = pack(&ofs(21, &delta), &[]);
    let mut fh = MemHandle::new(&data);
    assert!(chain(&mut fh, &ix, 34, None).unwrap().is_none());
}

#[test]
fn an_offset_of_2_to_the_63_is_an_overflow_before_the_handle_is_asked() {
    let delta = stored(&[10, 12]);
    let far = [0x55u8; 20];
    let more = [&[0x72][..], &far, &delta].concat();
    // An index holds a 64-bit offset only past its first object.
    let (data, ix) = pack(
        &more,
        &[(&[0x11; 20], json!(12)), (&far, json!(1u64 << 63))],
    );
    for at in [1u64 << 63, u64::MAX] {
        let mut fh = File::new(data.clone(), vec![]);
        assert_eq!(chain(&mut fh, &ix, at, Some(1)), Err(Failure::Overflow));
        assert_eq!(fh.log, "");
    }
    // One below it is sought, and nothing is there.
    let mut fh = File::new(data.clone(), vec![]);
    assert_eq!(chain(&mut fh, &ix, (1 << 63) - 1, None), Ok(None));
    assert_eq!(fh.log, "s9223372036854775807 r64");
    // A base listed there: the walk raises on its way to it.
    let mut fh = File::new(data.clone(), vec![]);
    assert_eq!(chain(&mut fh, &ix, 34, None), Err(Failure::Overflow));
    assert_eq!(fh.log, "s34 r64 s55 r1024");
    let mut fh = File::new(data, vec![]);
    let at = link(1 << 63, 10, None);
    assert_eq!(inflate_at(&mut fh, &at), Err(Failure::Overflow));
    assert_eq!(fh.log, "");
}

#[test]
fn inflate_at_feeds_the_size_and_64_up_to_65536() {
    let (data, _) = pack(b"", &[]);
    let read = |start: u64, size: Wide| {
        let mut fh = File::new(data.clone(), vec![]);
        let at = Link {
            start,
            size,
            target: None,
        };
        (inflate_at(&mut fh, &at).unwrap(), fh.log)
    };
    let (got, log) = read(13, Wide::from_u64(10));
    assert_eq!(
        (got.as_deref(), log.as_str()),
        (Some(&b"0123456789"[..]), "s13 r74")
    );
    assert_eq!(read(13, Wide::from_u64(9)), (None, "s13 r73".into()));
    assert_eq!(read(13, Wide::from_u64(11)), (None, "s13 r75".into()));
    assert_eq!(read(13, Wide::from_u64(65471)), (None, "s13 r65535".into()));
    assert_eq!(read(13, Wide::from_u64(65472)), (None, "s13 r65536".into()));
    assert_eq!(
        read(13, Wide::from_u64(u64::MAX)),
        (None, "s13 r65536".into())
    );
    assert_eq!(
        read(13, wide("10000000000000000a")),
        (None, "s13 r65536".into())
    );
    // A cut stream: the second read comes back empty.
    let mut fh = File::new(data[..30].to_vec(), vec![]);
    assert_eq!(inflate_at(&mut fh, &link(13, 10, None)), Ok(None));
    assert_eq!(fh.log, "s13 r74 r74");
}

#[test]
fn a_name_is_listed_in_each_pack_that_holds_its_offset() {
    let (a, c, z) = ([0x11u8; 20], [0x33u8; 20], [0x77u8; 20]);
    let first = index(
        &json!({ "h": 20, "ix": [[common::hex(&a), 12], [common::hex(&c), ["raw", 0x8000_0003u32]]] }),
    );
    let second =
        index(&json!({ "h": 20, "ix": [[common::hex(&a), 1u64 << 40], [common::hex(&c), 9]] }));
    let packs = [first, second];
    let at = |raw: &[u8]| -> Vec<(usize, u64)> {
        let found = listed_in(&packs, raw).unwrap();
        found
            .iter()
            .map(|(pack, at)| {
                (
                    packs.iter().position(|p| std::ptr::eq(p, *pack)).unwrap(),
                    *at,
                )
            })
            .collect()
    };
    assert_eq!(at(&a), [(0, 12), (1, 1 << 40)]);
    // Listed in the first past its 64-bit table: that pack is left out.
    assert_eq!(at(&c), [(1, 9)]);
    assert_eq!(at(&z), []);
    assert_eq!(at(&a[..19]), []);
    assert_eq!(listed_in(&packs, b"").err(), Some(Raised::IndexError));
    assert_eq!(listed_in(&[], b"").map(|found| found.len()), Ok(0));

    // `take` moves each name to `found` from the first pack that lists
    // it, a name past the 64-bit table too, and keeps the rest in order.
    let want_of = |names: &[&[u8]]| -> Vec<(Vec<u8>, String)> {
        names.iter().map(|n| (n.to_vec(), common::hex(n))).collect()
    };
    let mut want = want_of(&[&z, &c, &[0x88; 20], &a]);
    let mut found = BTreeSet::from(["kept".to_string()]);
    assert_eq!(take(&packs, &mut want, &mut found), Ok(()));
    assert_eq!(want, want_of(&[&z, &[0x88; 20]]));
    let all: Vec<&str> = found.iter().map(String::as_str).collect();
    assert_eq!(all, [&common::hex(&a), &common::hex(&c), "kept"]);
    // An empty name raises before the pack it meets moves anything.
    let mut want = want_of(&[&a, b"", &c]);
    let mut found = BTreeSet::new();
    assert_eq!(take(&packs, &mut want, &mut found), Err(Raised::IndexError));
    assert_eq!((want.len(), found.len()), (3, 0));
    let mut want = want_of(&[b""]);
    assert_eq!(take(&[], &mut want, &mut found), Ok(()));
}

#[test]
fn a_handle_in_memory_reads_as_a_regular_file() {
    let data = b"0123456789";
    let mut fh = MemHandle::new(data);
    assert_eq!(fh.read(4), Ok(b"0123".to_vec()));
    assert_eq!(fh.read(100), Ok(b"456789".to_vec()));
    assert_eq!(fh.read(1), Ok(vec![]));
    fh.seek(8).unwrap();
    assert_eq!(fh.read(0), Ok(vec![]));
    assert_eq!(fh.read(usize::MAX), Ok(b"89".to_vec()));
    for past in [10, 11, u64::MAX] {
        fh.seek(past).unwrap();
        assert_eq!(fh.read(1), Ok::<_, PyErr>(vec![]));
    }
    fh.seek(0).unwrap();
    let mut stream = Stream::new(&mut fh, 3);
    assert_eq!(stream.read(5), Err(Failure::Zlib(-3)));
}
