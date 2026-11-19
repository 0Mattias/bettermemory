//! Hostile input for `gitobjects`' byte parsers: very large bodies, long
//! varints, integer boundaries in every count and index argument. No call
//! panics, in debug or release, and each answers within a bound well above
//! what the reference takes on the same input (timed with CPython 3.13.13
//! when the bounds were set; the README gives the figures).

use std::time::{Duration, Instant};

use bettermemory_core::gitobjects::{self as g, Raised};

fn within<T>(limit_ms: u64, f: impl FnOnce() -> T) -> T {
    let start = Instant::now();
    let out = f();
    let spent = start.elapsed();
    assert!(
        spent < Duration::from_millis(limit_ms),
        "took {spent:?}, over {limit_ms} ms"
    );
    out
}

#[test]
fn integer_bounds_in_every_argument() {
    let body = b"tree aaaa\nauthor a\ncommitter c 5\n";
    for big in [usize::MAX, usize::MAX - 5, usize::MAX / 2, 1 << 40] {
        assert_eq!(g::parent_lines(body, big), None);
        assert_eq!(g::committed_at(body, big), 0);
        assert!(!g::hex_at(body, big, 3));
        assert!(!g::hex_at(body, 3, big));
        assert_eq!(g::hex_name(b"aa", big), None);
        assert_eq!(g::parse_commit(body, big), None);
        assert_eq!(g::parse_tree(b"1 a\0xx", big), None);
        assert_eq!(g::unquote_c(b"\"a\"", big), None);
        assert_eq!(g::varint(b"\x01", big), None);
        assert_eq!(g::entry_header(&[0x70, 1, 2], big), None);
        assert!(g::parse_index(vec![0; 2000], vec![], big).is_none());
    }
    let mut idx = b"\xfftOc\x00\x00\x00\x02".to_vec();
    idx.extend(vec![0u8; 1024]);
    idx.extend(vec![0x11u8; 40]);
    let idx = g::parse_index(idx, vec![], 20).unwrap();
    assert_eq!(idx.offset(usize::MAX), Err(Raised::StructError));
    assert_eq!(idx.offset(usize::MAX / 4), Err(Raised::StructError));
    assert_eq!(idx.starting("ab", i64::MAX), Ok(vec![]));
    assert_eq!(idx.starting("ab", i64::MIN), Ok(vec![]));
    // A fanout that claims 2**32 - 1 objects in a short file.
    let mut huge = b"\xfftOc\x00\x00\x00\x02".to_vec();
    huge.extend([0xffu8; 1024]);
    assert!(g::parse_index(huge, vec![], 20).is_none());
}

#[test]
fn long_varints_and_headers() {
    let mut data = vec![0xffu8; 1 << 22];
    data.push(0x01);
    let (v, next) = within(5_000, || g::varint(&data, 0).unwrap());
    assert_eq!(next, data.len());
    assert_eq!(v.hex().len(), ((7 * (1 << 22) + 1) as usize).div_ceil(4));
    let mut zeros = vec![0x80u8; 1 << 22];
    zeros.push(0);
    assert!(within(5_000, || g::delta_sizes(&zeros)).is_none());
    // The OFS distance is quadratic in its length, in the reference too.
    let mut ofs = vec![0x60u8];
    ofs.extend(vec![0xffu8; 1 << 13]);
    ofs.push(0x7f);
    let (_, _, base, len) = within(5_000, || g::entry_header(&ofs, 20).unwrap());
    assert_eq!(len, ofs.len());
    assert!(matches!(base, g::Base::Distance(d) if d.to_u64().is_none()));
    let mut head = b"commit ".to_vec();
    head.extend(vec![b'9'; 4300]);
    head.push(0);
    let loose = within(5_000, || g::loose_header(&head)).unwrap().unwrap();
    assert!(loose.size.to_u64().is_none());
    let mut over = b"commit ".to_vec();
    over.extend(vec![b'9'; 1 << 20]);
    over.push(0);
    assert_eq!(
        within(5_000, || g::loose_header(&over)),
        Err(Raised::ValueError)
    );
}

#[test]
fn large_deltas() {
    let base = vec![b'x'; 1 << 20];
    let mut delta = vec![0x80, 0x80, 0x40];
    let target: usize = 1 << 24;
    let mut t = target;
    loop {
        let c = (t & 0x7f) as u8;
        t >>= 7;
        if t == 0 {
            delta.push(c);
            break;
        }
        delta.push(c | 0x80);
    }
    delta.extend(std::iter::repeat_n(0x80u8, target / 0x10000));
    let out = within(10_000, || g::apply_delta(&base, &delta)).unwrap();
    assert_eq!(out.len(), target);
    let mut inserts = vec![0x80, 0x80, 0x40, 0x80, 0x80, 0x80, 0x01];
    for _ in 0..((1 << 21) / 127) {
        inserts.push(127);
        inserts.extend([b'y'; 127]);
    }
    assert_eq!(within(10_000, || g::apply_delta(&base, &inserts)), None);
    // A target past 64 bits is never met.
    let mut wide = vec![0x80, 0x80, 0x40];
    wide.extend([0xffu8; 12]);
    wide.push(0x01);
    wide.extend([0x03, b'a', b'b', b'c']);
    assert_eq!(g::apply_delta(&base, &wide), None);
}

#[test]
fn a_delta_past_its_target_stops_at_the_first_instruction_over() {
    // Target 1, then 100,000 copies of 65,536 bytes: the first copy passes
    // the target, so the answer comes before 6.5 GB are built.
    let base = vec![b'x'; 0x10000];
    let mut delta = vec![0x80, 0x80, 0x04, 0x01];
    delta.extend(std::iter::repeat_n(0x80u8, 100_000));
    assert_eq!(within(1_000, || g::apply_delta(&base, &delta)), None);
}

#[test]
fn large_commits_trees_and_files() {
    let mut body = b"tree ".to_vec();
    body.extend([b'a'; 40]);
    body.push(b'\n');
    for _ in 0..20_000 {
        body.extend(b"parent ");
        body.extend([b'b'; 40]);
        body.push(b'\n');
    }
    body.extend(b"author a\ncommitter c >");
    body.extend(vec![b'0'; 1 << 20]);
    body.extend(b"7\n\n");
    body.extend(vec![b'\n'; 1 << 20]);
    let at = within(5_000, || g::committed_at(&body, 40));
    assert_eq!(at, 7);
    let commit = within(5_000, || g::parse_commit(&body, 20)).unwrap();
    assert_eq!(commit.parents.len(), 20_000);
    assert!(within(5_000, || g::commit_fingerprint(&body)).is_some());

    let mut tree = Vec::new();
    for i in 0..200_000u32 {
        tree.extend(b"100644 ");
        tree.extend(i.to_string().as_bytes());
        tree.push(0);
        tree.extend([7u8; 20]);
    }
    assert_eq!(
        within(5_000, || g::parse_tree(&tree, 20)).unwrap().len(),
        200_000
    );
    let spaces = vec![b' '; 1 << 20];
    assert_eq!(within(5_000, || g::parse_tree(&spaces, 20)), None);

    // Every line opens a quote that never closes: each scans to the end,
    // in the reference too.
    let alternates = b"\"aaaaaaaaa\n".repeat(2_000);
    let entries = within(10_000, || g::alternate_entries(&alternates));
    assert_eq!(entries.len(), 2_000);
    let escapes = [b"\"".as_slice(), &b"\\101".repeat(1 << 18)].concat();
    assert_eq!(within(5_000, || g::unquote_c(&escapes, 0)), None);
}

#[test]
fn a_large_index() {
    let count = 200_000usize;
    let names: Vec<[u8; 20]> = (0..count)
        .map(|i| {
            let mut n = [0u8; 20];
            n[..8].copy_from_slice(&((i as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15)).to_be_bytes());
            n
        })
        .collect();
    let mut sorted = names.clone();
    sorted.sort();
    let mut data = b"\xfftOc\x00\x00\x00\x02".to_vec();
    for b in 0..256usize {
        let c = sorted.iter().take_while(|n| usize::from(n[0]) <= b).count() as u32;
        data.extend(c.to_be_bytes());
    }
    for n in &sorted {
        data.extend(n);
    }
    data.extend(vec![0u8; 4 * count]);
    for i in 0..count as u32 {
        data.extend((i | 0x8000_0000).to_be_bytes());
    }
    for i in 0..(count as u64 - 1) {
        data.extend(i.to_be_bytes());
    }
    data.extend([0u8; 40]);
    let idx = within(5_000, || g::parse_index(data, vec![], 20)).unwrap();
    within(10_000, || {
        for (i, n) in sorted.iter().enumerate() {
            let p = idx.find(n).unwrap().unwrap();
            assert_eq!(p, i);
            let expect = if i < count - 1 { Some(i as u64) } else { None };
            assert_eq!(idx.offset(p).unwrap(), expect);
        }
    });
    let hex: String = sorted[77].iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(
        idx.starting(&hex[..10], 2).unwrap(),
        vec![sorted[77].to_vec()]
    );
}
