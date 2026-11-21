//! Hostile index bytes: the reader never panics, a count that runs past
//! the data declines at once, and the time grows linearly with the
//! number of entries and with a name's length.

mod timing;

use bettermemory_core::verify_anchoring::sparse::{index_varint, parse_skip_index, SkipIndex};

fn index(version: u32, count: u32, body: &[u8], hash_len: usize) -> Vec<u8> {
    let mut d = b"DIRC".to_vec();
    d.extend(version.to_be_bytes());
    d.extend(count.to_be_bytes());
    d.extend_from_slice(body);
    d.extend(std::iter::repeat_n(0xEEu8, hash_len));
    d
}

fn entries(n: usize) -> Vec<u8> {
    let mut body = Vec::new();
    for i in 0..n {
        let name = format!("d{}/f{i:08}", i % 97);
        let mut e = vec![0u8; 40];
        e.extend([0xABu8; 20]);
        e.extend((0x4000u16 | name.len() as u16).to_be_bytes());
        e.extend((if i % 2 == 0 { 0x4000u16 } else { 0 }).to_be_bytes());
        e.extend(name.as_bytes());
        let pad = 8 - e.len() % 8;
        e.extend(std::iter::repeat_n(0u8, pad));
        body.extend(e);
    }
    body
}

#[test]
fn no_panic_on_boundaries() {
    for h in [0usize, 1, 20, 32, usize::MAX] {
        for v in [2u32, 3, 4] {
            for count in [0u32, 1, u32::MAX] {
                for body in [
                    &b""[..],
                    &[0xFF; 100][..],
                    &[0x00; 100][..],
                    &[0x80; 70][..],
                ] {
                    let _ = parse_skip_index(&index(v, count, body, h.min(64)), h);
                }
            }
        }
    }
    assert!(index_varint(&[], usize::MAX).is_err());
    assert!(index_varint(&[0x80; 1000], 0).is_err());
    // Extension sizes at the u32 bound.
    let mut ext = b"TREE".to_vec();
    ext.extend(u32::MAX.to_be_bytes());
    assert!(parse_skip_index(&index(2, 0, &ext, 20), 20).is_ok());
    // A huge count declines at the end of the data.
    assert!(parse_skip_index(&index(2, u32::MAX, &entries(3), 20), 20).is_err());
}

#[test]
fn time_grows_linearly_with_entries() {
    let small = index(3, 20_000, &entries(20_000), 20);
    let large = index(3, 200_000, &entries(200_000), 20);
    assert_eq!(parse_skip_index(&large, 20).unwrap().skipped.len(), 100_000);
    timing::grows_within(
        "20k and 200k entries",
        timing::TENFOLD,
        &mut || timing::once(|| parse_skip_index(&small, 20).map(|i| i.kept.len())),
        &mut || timing::once(|| parse_skip_index(&large, 20).map(|i| i.kept.len())),
    );
}

#[test]
fn skips_answers_at_depth() {
    let index = SkipIndex {
        skipped: vec![b"p/".to_vec()],
        kept: vec![],
    };
    let deep = vec![b"p".to_vec(); 5_000];
    assert!(index.skips(&deep));
    let other = vec![b"q".to_vec(); 5_000];
    assert!(!index.skips(&other));
}
