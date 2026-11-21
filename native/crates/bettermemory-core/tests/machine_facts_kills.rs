//! Cases a plausible wrong port of `machine::facts` fails: each names the
//! slip it catches.

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::machine::facts as f;
use bettermemory_core::pyvalue::{PyError, PyValue};

fn id_file(bytes: &[u8]) -> Result<Option<String>, PyError> {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/f", Node::File(bytes.to_vec()));
    f::read_id(&fs, b"/f")
}

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

const GOOD: &str = "0123456789ABCDEFGHJKMNPQRS";

#[test]
fn the_excluded_letters_of_crockford_are_refused() {
    for c in ['I', 'L', 'O', 'U', 'a', 'z'] {
        let id = format!("{}{c}", &GOOD[..25]);
        let body = format!("bettermemory machine id v1\n{id}");
        assert_eq!(id_file(body.as_bytes()), Ok(None), "{c}");
    }
    let body = format!("bettermemory machine id v1\n{GOOD}");
    assert_eq!(id_file(body.as_bytes()), Ok(Some(GOOD.into())));
}

#[test]
fn a_lone_cr_ends_a_line_and_vertical_tab_too() {
    // A port splitting on `\n` alone misses both.
    for sep in ["\r", "\x0b", "\x1e"] {
        let body = format!("bettermemory machine id v1{sep}{GOOD}");
        assert_eq!(id_file(body.as_bytes()), Ok(Some(GOOD.into())), "{sep:?}");
    }
    // `\x1f` strips but does not end a line.
    let body = format!("bettermemory machine id v1\x1f{GOOD}");
    assert_eq!(id_file(body.as_bytes()), Ok(None));
}

#[test]
fn a_nul_in_the_path_raises_for_the_id_and_not_for_the_record() {
    let fs = MemFs::new(Flavour::Posix);
    assert_eq!(f::read_id(&fs, b"/a\0b"), Err(PyError::ValueError));
    assert_eq!(f::read_record(&fs, b"/a\0b"), Ok(None));
    assert_eq!(
        f::linux_hardware(&fs, &[b"/a\0b"]),
        Err(PyError::ValueError)
    );
}

#[test]
fn a_bom_is_kept_by_utf8() {
    let body = format!("\u{feff}bettermemory machine id v1\n{GOOD}");
    assert_eq!(id_file(body.as_bytes()), Ok(None));
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/r", Node::File(b"\xef\xbb\xbf{}".to_vec()));
    assert_eq!(f::read_record(&fs, b"/r"), Ok(None));
}

#[test]
fn deep_json_raises_recursion() {
    let mut fs = MemFs::new(Flavour::Posix);
    let body = format!("{{\"a\":{}{}}}", "[".repeat(100_000), "]".repeat(100_000));
    fs.insert(b"/r", Node::File(body.into_bytes()));
    assert_eq!(f::read_record(&fs, b"/r"), Err(PyError::RecursionError));
}

#[test]
fn the_uuid_is_upper_case_with_dashes() {
    let raw: [u8; 16] = core::array::from_fn(|i| 0xA0 + i as u8);
    assert_eq!(f::uuid_text(&raw), "A0A1A2A3-A4A5-A6A7-A8A9-AAABACADAEAF");
}

#[test]
fn ioreg_is_asked_only_without_gethostuuid_and_an_empty_value_moves_on() {
    let asked = std::cell::Cell::new(false);
    let got = f::darwin_hardware(Some([0; 16]), || {
        asked.set(true);
        Ok(None)
    });
    assert!(matches!(got, Ok(Some(_))) && !asked.get());
    // Output that does not decode raises out of `_darwin_hardware`.
    assert_eq!(
        f::darwin_hardware(None, || Err(PyError::UnicodeDecodeError)),
        Err(PyError::UnicodeDecodeError)
    );
    assert_eq!(f::darwin_hardware(None, || Ok(None)), Ok(None));
    let out = cps("\"IOPlatformUUID\" = \"\" \"IOPlatformUUID\" = \"B\"");
    assert_eq!(f::ioreg_uuid(&out), Some(cps("B")));
    assert_eq!(f::ioreg_uuid(&cps("\"IOPlatformUUID\" = \"open")), None);
}

#[test]
fn a_blank_first_file_falls_through() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/a", Node::File(b" \n\x1c".to_vec()));
    fs.insert(b"/b", Node::File(b" id \n".to_vec()));
    assert_eq!(f::linux_hardware(&fs, &[b"/a", b"/b"]), Ok(Some(cps("id"))));
    fs.insert(b"/a", Node::File(b"\xc3\xa9".to_vec()));
    assert_eq!(f::linux_hardware(&fs, &[b"/a", b"/b"]), Ok(Some(cps("id"))));
}

#[test]
fn a_false_guid_is_none_and_a_number_is_spelled() {
    assert_eq!(
        f::windows_hardware(Some(&PyValue::Int("0".into()))),
        Ok(None)
    );
    assert_eq!(
        f::windows_hardware(Some(&PyValue::Int("7".into()))),
        Ok(Some(cps("7")))
    );
    assert_eq!(f::windows_hardware(None), Ok(None));
}

#[test]
#[allow(clippy::excessive_precision)]
fn birth_from_the_float_truncates_and_wraps_before_1970() {
    // A double holding 1700000000.123456789 loses its last digits.
    assert_eq!(
        f::ns_of_seconds(1_700_000_000.123_456_789),
        Ok("1700000000123456768".into())
    );
    assert_eq!(f::ns_of_seconds(-1.5), Ok("-1500000000".into()));
    assert_eq!(
        f::ns_of_seconds(1e30),
        Ok("1000000000000000090824893823431825424384".into())
    );
    // int(7.5e-10 * 1e9) == 0 and int(-7.5e-10 * 1e9) == 0: truncated,
    // not rounded.
    assert_eq!(f::ns_of_seconds(7.5e-10), Ok("0".into()));
    assert_eq!(f::ns_of_seconds(-7.5e-10), Ok("0".into()));
    assert_eq!(f::ns_of_seconds(1.9e-9), Ok("1".into()));
    // int(nan) raises ValueError, int(inf) OverflowError.
    assert_eq!(f::ns_of_seconds(f64::NAN), Err(PyError::ValueError));
    assert_eq!(f::ns_of_seconds(f64::INFINITY), Err(PyError::OverflowError));
    assert_eq!(
        f::ns_of_seconds(f64::NEG_INFINITY),
        Err(PyError::OverflowError)
    );
    assert_eq!(
        f::birth_text(Some((-1, 0)), true),
        Some("-1000000000".into())
    );
    // `unsigned long` seconds: -1 is 2**64 - 1.
    assert_eq!(
        f::birth_text(Some((-1, 0)), false),
        Some("18446744073709551616000000000".into())
    );
}

#[test]
fn a_time_before_1970_floors_its_seconds() {
    use std::time::{Duration, UNIX_EPOCH};
    let half = Duration::from_millis(500);
    assert_eq!(f::seconds_of_time(UNIX_EPOCH - half), (-1, 500_000_000));
    assert_eq!(
        f::seconds_of_time(UNIX_EPOCH - Duration::from_nanos(2_000_000_001)),
        (-3, 999_999_999)
    );
    assert_eq!(
        f::seconds_of_time(UNIX_EPOCH - Duration::from_secs(2)),
        (-2, 0)
    );
    assert_eq!(f::seconds_of_time(UNIX_EPOCH + half), (0, 500_000_000));
}

#[test]
fn the_memo_keeps_none_too() {
    let mut memo = f::HardwareMemo::default();
    let mut n = 0;
    for _ in 0..3 {
        let _ = memo.get_or_fill(|| {
            n += 1;
            Ok::<_, ()>(None)
        });
    }
    assert_eq!(n, 1);
}
