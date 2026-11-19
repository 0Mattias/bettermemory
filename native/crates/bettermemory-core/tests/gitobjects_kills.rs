//! Inputs on which a plausible wrong port of `gitobjects`' byte parsers
//! answers otherwise than the reference. Each answer here is the
//! reference's, read from CPython 3.13.13.

use bettermemory_core::gitobjects::{self as g, Base, Raised, Wide};

fn index(names: &[[u8; 20]], offsets: &[u32], large: &[u64]) -> Vec<u8> {
    let mut out = b"\xfftOc\x00\x00\x00\x02".to_vec();
    for i in 0..256usize {
        let count = names.iter().filter(|n| usize::from(n[0]) <= i).count() as u32;
        out.extend(count.to_be_bytes());
    }
    for n in names {
        out.extend(n);
    }
    out.extend(vec![0u8; 4 * names.len()]);
    for o in offsets {
        out.extend(o.to_be_bytes());
    }
    for l in large {
        out.extend(l.to_be_bytes());
    }
    out.extend([0x11u8; 40]);
    out
}

#[test]
fn a_varint_past_64_bits_is_exact() {
    let mut data = vec![0xffu8; 10];
    data.push(0x01);
    let (v, next) = g::varint(&data, 0).unwrap();
    assert_eq!(v.hex(), "7fffffffffffffffff");
    assert_eq!(next, 11);
    // Groups of zeros at any shift leave the value small.
    let mut zeros = vec![0x80u8; 40];
    zeros.push(0);
    assert_eq!(g::varint(&zeros, 0).unwrap().0, Wide::from_u64(0));
    assert_eq!(g::varint(b"\x80", 0), None);
}

#[test]
fn an_ofs_distance_adds_one_before_each_shift() {
    let (code, size, base, len) = g::entry_header(&[0x60 | 3, 0x81, 0x00], 20).unwrap();
    assert_eq!((code, size.hex(), len), (6, "3".into(), 3));
    assert_eq!(base, Base::Distance(Wide::from_u64(256)));
    // A REF_DELTA needs the whole name; a code 0 or 5 is no entry.
    assert_eq!(g::entry_header(&[0x70, 1, 2, 3], 3).map(|e| e.3), Some(4));
    assert_eq!(g::entry_header(&[0x70, 1, 2, 3], 4), None);
    assert_eq!(g::entry_header(&[0x05], 20), None);
    assert_eq!(g::entry_header(&[0x55], 20), None);
}

#[test]
fn a_copy_of_size_zero_copies_65536_bytes() {
    let base = vec![7u8; 0x10000 + 1];
    let mut delta = vec![0x81, 0x80, 0x04, 0x80, 0x80, 0x04, 0x81, 0x01];
    assert_eq!(
        g::apply_delta(&base, &delta).map(|o| o.len()),
        Some(0x10000)
    );
    delta[6] = 0x81;
    delta[7] = 0x02;
    assert_eq!(g::apply_delta(&base, &delta), None);
}

#[test]
fn a_delta_that_overshoots_its_target_fails_at_once() {
    let base = b"abcdef";
    // Source 6, target 2, insert 3 bytes: over the target.
    assert_eq!(g::apply_delta(base, b"\x06\x02\x03xyz"), None);
    assert_eq!(
        g::apply_delta(base, b"\x06\x03\x03xyz"),
        Some(b"xyz".to_vec())
    );
    // Opcode 0 is reserved; a short target is no answer.
    assert_eq!(g::apply_delta(base, b"\x06\x03\x00"), None);
    assert_eq!(g::apply_delta(base, b"\x06\x04\x03xyz"), None);
    // The source size must be the base's.
    assert_eq!(g::apply_delta(base, b"\x05\x03\x03xyz"), None);
}

#[test]
fn a_loose_size_is_canonical_decimal_of_any_width() {
    let l = g::loose_header(b"blob 0\0").unwrap().unwrap();
    assert_eq!((l.kind, l.size.hex(), l.start), ("blob", "0".into(), 7));
    assert_eq!(g::loose_header(b"blob 01\0"), Ok(None));
    assert_eq!(g::loose_header(b"blob 1 \0"), Ok(None));
    assert_eq!(g::loose_header(b"tree \0"), Ok(None));
    let wide = g::loose_header(b"blob 18446744073709551616\0")
        .unwrap()
        .unwrap();
    assert_eq!(wide.size.hex(), "10000000000000000");
    // The size is read nine digits at a time: every length across the
    // chunk edges answers the value u128 reads.
    let all = b"12345678909876543210123456789098765432";
    for n in 1..=all.len() {
        let mut head = b"blob ".to_vec();
        head.extend_from_slice(&all[..n]);
        head.push(0);
        let value: u128 = std::str::from_utf8(&all[..n]).unwrap().parse().unwrap();
        let l = g::loose_header(&head).unwrap().unwrap();
        assert_eq!(l.size.hex(), format!("{value:x}"), "{n} digits");
    }
    let mut long = b"blob ".to_vec();
    long.extend(vec![b'9'; 4301]);
    long.push(0);
    assert_eq!(g::loose_header(&long), Err(Raised::ValueError));
}

const T: &[u8] = b"tree aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n";
const A: &[u8] = b"author A <a> 1 +0000\n";

fn with_time(time: &[u8]) -> Vec<u8> {
    [T, A, b"committer C <c> ", time, b"\n"].concat()
}

#[test]
fn the_committer_time_wraps_and_saturates_as_strtoumax() {
    assert_eq!(g::committed_at(&with_time(b"-5"), 40), u64::MAX - 4);
    assert_eq!(g::committed_at(&with_time(b"-0"), 40), 0);
    assert_eq!(
        g::committed_at(&with_time(b"18446744073709551616"), 40),
        u64::MAX
    );
    let zeros = [&[b'0'; 30][..], b"42"].concat();
    assert_eq!(g::committed_at(&with_time(&zeros), 40), 42);
    assert_eq!(g::committed_at(&with_time(&[b'1'; 21]), 40), u64::MAX);
    assert_eq!(g::committed_at(&with_time(b"+5"), 40), 0);
    assert_eq!(g::committed_at(&with_time(b"\t\r 77x"), 40), 77);
    // The last `>` of the line is the mark.
    let body = [T, A, b"committer C <c> 5 > 6\n"].concat();
    assert_eq!(g::committed_at(&body, 40), 6);
}

#[test]
fn a_parent_line_at_the_very_end_refuses_the_commit() {
    let parent = b"parent bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n";
    assert_eq!(g::parent_lines(&[T, parent].concat(), 40), None);
    let (parents, at) = g::parent_lines(&[T, parent, A].concat(), 40).unwrap();
    assert_eq!((parents.len(), at), (1, T.len() + parent.len()));
    assert_eq!(g::parent_lines(T, 40), None);
}

#[test]
fn the_parsed_commit_lowercases_names_and_keeps_first_lines() {
    let body = [
        b"tree AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n".as_slice(),
        b"author x\nauthor y\ncommitter z\n\nmsg\n\nmore",
    ]
    .concat();
    let c = g::parse_commit(&body, 20).unwrap();
    assert_eq!(c.tree, "a".repeat(40));
    assert_eq!(
        (c.author.as_slice(), c.committer.as_slice()),
        (&b"x"[..], &b"z"[..])
    );
    assert_eq!(c.message, b"msg\n\nmore");
    assert_eq!(g::hex_name(b"", 0), None);
}

#[test]
fn a_tree_mode_is_one_to_six_octal_digits() {
    let sha = [0u8; 20];
    let one = [b"40000 d\0".as_slice(), &sha].concat();
    assert_eq!(g::parse_tree(&one, 20).unwrap()[0].mode, 0o40000);
    assert_eq!(
        g::parse_tree(&[b"1006440 d\0".as_slice(), &sha].concat(), 20),
        None
    );
    assert_eq!(
        g::parse_tree(&[b"100644 \0".as_slice(), &sha].concat(), 20),
        None
    );
    let spaced = g::parse_tree(&[b"100644 a b\0".as_slice(), &sha].concat(), 20).unwrap();
    assert_eq!(spaced[0].name, b"a b");
}

#[test]
fn the_fingerprint_wants_exactly_two_lines() {
    let two = g::commit_fingerprint(b"author a\ncommitter c\n\nm").unwrap();
    assert_eq!(two.len(), 32);
    assert_eq!(
        g::commit_fingerprint(b"author a\nauthor b\ncommitter c\n\nm"),
        None
    );
    assert_eq!(g::commit_fingerprint(b"author a\n\ncommitter c\n"), None);
}

#[test]
fn quoting_and_alternates() {
    assert_eq!(g::unquote_c(b"\"\\400\"", 0), None);
    assert_eq!(g::unquote_c(b"\"\\377x\"", 0), Some((vec![0xff, b'x'], 7)));
    // The byte after a closing quote is skipped, whatever it is.
    assert_eq!(
        g::alternate_entries(b"\"/q\"x/b\n"),
        vec![b"/q".to_vec(), b"/b".to_vec()]
    );
    assert_eq!(
        g::alternate_entries(b"\"/bad\\q\"\n/b"),
        vec![b"\"/bad\\q\"".to_vec(), b"/b".to_vec()]
    );
    assert_eq!(g::alternate_entries(b"#c\n\n/a"), vec![b"/a".to_vec()]);
}

#[test]
fn an_index_size_admits_at_most_count_minus_one_large_offsets() {
    let names = [[1u8; 20], [2u8; 20], [3u8; 20]];
    assert!(g::parse_index(index(&names, &[0, 1, 2], &[5, 6]), vec![], 20).is_some());
    assert!(g::parse_index(index(&names, &[0, 1, 2], &[5, 6, 7]), vec![], 20).is_none());
    let one = g::parse_index(index(&names, &[0, 0x8000_0001, 2], &[5, 6]), vec![], 20).unwrap();
    assert_eq!(one.offset(1), Ok(Some(6)));
    assert_eq!(one.find(&[2u8; 20]), Ok(Some(1)));
    assert_eq!(one.find(&[]), Err(Raised::IndexError));
    let none = g::parse_index(index(&names, &[0, 0x8000_0002, 2], &[5, 6]), vec![], 20).unwrap();
    assert_eq!(none.offset(1), Ok(None));
}

#[test]
fn a_one_digit_prefix_searches_the_bucket_of_its_value() {
    let mut name = [0u8; 20];
    name[0] = 0xa0;
    let mut low = [0u8; 20];
    low[0] = 0x0a;
    let idx = g::parse_index(index(&[low, name], &[1, 2], &[]), vec![], 20).unwrap();
    assert_eq!(idx.starting("a", 2), Ok(vec![]));
    assert_eq!(idx.starting("a0", 2), Ok(vec![name.to_vec()]));
    assert_eq!(idx.starting("0", 2), Ok(vec![]));
    assert_eq!(idx.starting("0a", 2), Ok(vec![low.to_vec()]));
    assert_eq!(idx.starting("A0", 2), Ok(vec![]));
    assert_eq!(idx.starting(" a", 2), Err(Raised::ValueError));
    assert_eq!(idx.starting("a0 0a ", 2), Ok(vec![]));
    assert_eq!(idx.starting("", 2), Err(Raised::ValueError));
    // Outside ASCII: `int` reads Unicode digits, `bytes.fromhex` raises.
    for p in [
        "\u{663}",
        "\u{661}\u{662}",
        "-\u{661}",
        "a\u{e9}",
        "ab\u{e9}",
    ] {
        assert_eq!(idx.starting(p, 2), Err(Raised::ValueError));
    }
}
