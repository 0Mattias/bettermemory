//! Cases a plausible wrong reading of the index or the switch fails, each
//! answer the reference's (`verify_anchoring.py`, read at 9b4a365a).

use bettermemory_core::verify_anchoring::sparse::{
    config_switch, index_varint, parse_skip_index, SkipIndex,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn entry(name: &[u8], flags_extra: u16, ext: Option<u16>) -> Vec<u8> {
    let mut e = vec![0u8; 40];
    e.extend([0xABu8; 20]);
    let flags = flags_extra | if ext.is_some() { 0x4000 } else { 0 } | name.len() as u16;
    e.extend(flags.to_be_bytes());
    if let Some(x) = ext {
        e.extend(x.to_be_bytes());
    }
    e.extend_from_slice(name);
    let pad = 8 - e.len() % 8;
    e.extend(std::iter::repeat_n(0u8, pad));
    e
}

fn index(version: u32, entries: &[Vec<u8>], tail: &[u8]) -> Vec<u8> {
    let mut d = b"DIRC".to_vec();
    d.extend(version.to_be_bytes());
    d.extend((entries.len() as u32).to_be_bytes());
    for e in entries {
        d.extend_from_slice(e);
    }
    d.extend_from_slice(tail);
    d.extend([0xEEu8; 20]);
    d
}

#[test]
fn varint_adds_one_per_continuation() {
    // Not plain base-128: 0x80 0x00 is 128, not 0.
    assert_eq!(index_varint(&[0x80, 0x00], 0), Ok((128, 2)));
    assert_eq!(index_varint(&[0x81, 0x00], 0), Ok((256, 2)));
    assert!(index_varint(&[0x80], 0).is_err());
    // Nine continuations of 0xff pass 57 bits before the last shift.
    let mut over = vec![0xFFu8; 9];
    over.push(0x7F);
    assert!(index_varint(&over, 0).is_err());
    let mut top = vec![0x80u8];
    top.extend([0xFEu8; 8]);
    top.push(0x7F);
    assert_eq!(index_varint(&top, 0), Ok((u64::MAX, 10)));
}

#[test]
fn the_last_assignment_wins_and_a_bad_value_unsets() {
    let t = cps("[core]\n\tsparseCheckout = true\n\tsparseCheckout = maybe\n");
    assert_eq!(
        config_switch(&t, &cps("core"), &cps("sparsecheckout")),
        Ok(None)
    );
    let t = cps("[core \"x\"]\n\tsparseCheckout = true\n");
    assert_eq!(
        config_switch(&t, &cps("core"), &cps("sparsecheckout")),
        Ok(None)
    );
    // The name is lowered as text: the Kelvin sign is k.
    let t = cps("[core]\n\tkey = true\n");
    assert_eq!(
        config_switch(&t, &cps("core"), &cps("\u{212a}ey")),
        Ok(Some(true))
    );
    // A text git refuses answers None before the name is encoded.
    assert_eq!(
        config_switch(&cps("[core"), &cps("c\u{f6}re"), &cps("x")),
        Ok(None)
    );
    assert!(config_switch(&cps("[core]\n"), &cps("c\u{f6}re"), &cps("x")).is_err());
}

#[test]
fn conflicted_entries_are_passed_over_and_the_bit_is_the_second_word() {
    let d = index(
        3,
        &[
            entry(b"a", 0, Some(0x4000)),
            entry(b"b", 0, Some(0x2000)),
            entry(b"c", 0x1000, Some(0x4000)),
        ],
        b"",
    );
    let got = parse_skip_index(&d, 20).unwrap();
    assert_eq!(got.skipped, vec![b"a".to_vec()]);
    assert_eq!(got.kept, vec![b"b".to_vec()]);
    // An unknown extended bit declines.
    let d = index(3, &[entry(b"a", 0, Some(0x1000))], b"");
    assert!(parse_skip_index(&d, 20).is_err());
}

#[test]
fn a_link_extension_declines_and_others_are_skipped() {
    let mut tree = b"TREE".to_vec();
    tree.extend(3u32.to_be_bytes());
    tree.extend(b"xyz");
    let mut link = b"link".to_vec();
    link.extend(0u32.to_be_bytes());
    let mut both = tree.clone();
    both.extend(&link);
    assert!(parse_skip_index(&index(2, &[entry(b"a", 0, None)], &tree), 20).is_ok());
    assert!(parse_skip_index(&index(2, &[entry(b"a", 0, None)], &both), 20).is_err());
}

#[test]
fn skips_needs_every_entry_below_to_carry_the_bit() {
    let i = SkipIndex {
        skipped: vec![b"a/b".to_vec(), b"a/c".to_vec()],
        kept: vec![b"a/d".to_vec()],
    };
    assert!(!i.skips(&[b"a"]));
    assert!(i.skips(&[b"a", b"b"]));
    let i = SkipIndex {
        skipped: vec![b"a-b".to_vec(), b"a/b".to_vec()],
        kept: vec![b"a.b".to_vec()],
    };
    assert!(i.skips(&[b"a"]));
    let dir = SkipIndex {
        skipped: vec![b"a/".to_vec()],
        kept: vec![],
    };
    assert!(dir.skips(&[b"a".as_slice(), b"x", b"y"]));
    assert!(dir.skips(&[b"a"]));
    assert!(!dir.skips(&[b"ab"]));
}

#[test]
fn a_running_value_between_57_and_58_bits_declines() {
    // At the last continuation the running value is in [2^57, 2^58): the
    // reference declines, where a 58-bit guard would overflow the shift.
    let mut data = vec![0x80u8];
    data.extend([0xFFu8; 8]);
    data.push(0x00);
    assert!(index_varint(&data, 0).is_err());
}

#[test]
fn a_body_under_twelve_bytes_declines() {
    // end = 8 with a 20-byte trailer of zeros that would read as a count.
    let mut data = b"DIRC\0\0\0\x02".to_vec();
    data.extend([0u8; 20]);
    assert!(parse_skip_index(&data, 20).is_err());
    assert!(parse_skip_index(b"DIRC\0\0\0\x02\0\0", 0).is_err());
    // end = 12 with no trailer reads an empty index.
    let empty = parse_skip_index(b"DIRC\0\0\0\x02\0\0\0\0", 0).unwrap();
    assert!(empty.skipped.is_empty() && empty.kept.is_empty());
}
