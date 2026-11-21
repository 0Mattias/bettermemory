//! Hostile input for `machine::facts`: large files, many line breaks,
//! deep JSON, the `ioreg` search over many near misses, extreme birth
//! times. No panic, and time grows linearly.

mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::machine::facts as f;

fn fs_with(bytes: Vec<u8>) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/f", Node::File(bytes));
    fs
}

#[test]
fn many_line_breaks_read_in_linear_time() {
    let small = fs_with("\r\n\x0b\r".repeat(50_000).into_bytes());
    let large = fs_with("\r\n\x0b\r".repeat(500_000).into_bytes());
    timing::grows_within(
        "read_id over line breaks",
        timing::TENFOLD,
        &mut || timing::once(|| f::read_id(&small, b"/f")),
        &mut || timing::once(|| f::read_id(&large, b"/f")),
    );
}

#[test]
fn ioreg_near_misses_search_in_linear_time() {
    let unit: Vec<u32> = "\"IOPlatformUUID\" = \"".chars().map(u32::from).collect();
    let small: Vec<u32> = unit
        .iter()
        .copied()
        .cycle()
        .take(unit.len() * 5_000)
        .collect();
    let large: Vec<u32> = unit
        .iter()
        .copied()
        .cycle()
        .take(unit.len() * 50_000)
        .collect();
    assert_eq!(f::ioreg_uuid(&small), None);
    timing::grows_within(
        "ioreg search over near misses",
        timing::TENFOLD,
        &mut || timing::once(|| f::ioreg_uuid(&small)),
        &mut || timing::once(|| f::ioreg_uuid(&large)),
    );
}

#[test]
fn an_unclosed_value_searches_in_linear_time() {
    // Each start finds no closing quote: a naive port scans to the end
    // from every start.
    let mut small: Vec<u32> = "\"IOPlatformUUID\" = \"".chars().map(u32::from).collect();
    let mut large = small.clone();
    small.extend(std::iter::repeat_n(u32::from(b'x'), 100_000));
    large.extend(std::iter::repeat_n(u32::from(b'x'), 1_000_000));
    timing::grows_within(
        "ioreg search over an unclosed value",
        timing::TENFOLD,
        &mut || timing::once(|| f::ioreg_uuid(&small)),
        &mut || timing::once(|| f::ioreg_uuid(&large)),
    );
}

#[test]
fn deep_and_large_records_do_not_panic() {
    for n in [1usize, 9_000, 10_000, 1_000_000] {
        let body = format!("{{\"a\":{}{}}}", "[".repeat(n), "]".repeat(n));
        let _ = f::read_record(&fs_with(body.into_bytes()), b"/f");
    }
    let wide = format!("{{{}\"z\":0}}", "\"k\":[],".repeat(200_000));
    assert!(f::read_record(&fs_with(wide.into_bytes()), b"/f")
        .expect("no raise")
        .is_some());
    let _ = f::read_id(&fs_with(vec![0xFF; 1 << 20]), b"/f");
}

#[test]
fn birth_times_at_the_integer_bounds() {
    for (sec, nsec) in [
        (i64::MIN, 0),
        (i64::MAX, 999_999_999),
        (0, 999_999_999),
        (-1, 1),
    ] {
        assert!(f::birth_text(Some((sec, nsec)), true).is_some());
        assert!(f::birth_text(Some((sec, nsec)), false).is_some());
    }
    for v in [1e290, -1e290, f64::MIN_POSITIVE, 1.7e29, -1.7e29, 1.8e19] {
        let text = f::ns_of_seconds(v).expect("finite");
        assert!(!text.is_empty());
    }
    assert_eq!(f::ns_of_seconds(1e290).expect("finite").len(), 300);
}
