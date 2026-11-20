//! Cases a plausible wrong `location_stamps` fails: the mount table split
//! at ASCII whitespace only, or not at a lone `\r`; the dash searched from
//! the start of the line; `_DRVFS_ANAME` tried at its first match only or
//! with `$` read loosely; `dev_t` read as unsigned on macOS; NODEV and a
//! part of 0xffffffff read alike on glibc and musl; FILETIME's arithmetic
//! in 64 bits; the order of `capture_ticks`' reads.

mod common;

use bettermemory_core::fsdata::BasicInfo;
use bettermemory_core::location_stamps::{self as ls, Machine, Platform, StampError};
use common::location_stamps::{stat, TableFs};

const MOUNTS: &[u8] = b"/m";

fn drvfs_of(table: &[u8], device: i128) -> Result<bool, StampError> {
    let fs = TableFs {
        file: Some((MOUNTS.to_vec(), Ok(table.to_vec()))),
        ..TableFs::default()
    };
    let m = Machine {
        fs: &fs,
        platform: Platform::Linux,
        mountinfo: MOUNTS,
    };
    ls::drvfs(&m, device)
}

#[test]
fn the_table_splits_at_python_whitespace() {
    // U+00A0 and U+0085 split fields; a lone 0xA0 byte is U+FFFD and
    // does not.
    assert_eq!(
        drvfs_of(b"36\xc2\xa035\xc2\x850:7 / /m rw - drvfs\n", 7),
        Ok(true)
    );
    assert_eq!(drvfs_of(b"36\xa035 0:7 / /m rw - drvfs\n", 7), Ok(false));
    // The information separators 0x1C to 0x1F are whitespace too.
    assert_eq!(
        drvfs_of(b"36\x1c35\x1d0:7\x1e/\x1f/m rw - drvfs\n", 7),
        Ok(true)
    );
}

#[test]
fn a_lone_carriage_return_ends_a_line() {
    assert_eq!(drvfs_of(b"36 35 0:7 / /m\rrw - drvfs x\n", 7), Ok(false));
    assert_eq!(drvfs_of(b"36 35 0:7 / /m rw - drvfs x\r", 7), Ok(true));
    // `\r\n` split across the 8,192-byte chunks is one break.
    let mut t = b"36 35 0:9 / /m rw - ext4 ".to_vec();
    t.resize(8191, b'y');
    t.extend_from_slice(b"\r\n36 35 0:7 / /m rw - drvfs\n");
    assert_eq!(drvfs_of(&t, 7), Ok(true));
}

#[test]
fn the_dash_is_searched_from_the_seventh_field() {
    assert_eq!(drvfs_of(b"- - 0:7 - - - - drvfs\n", 7), Ok(true));
    assert_eq!(drvfs_of(b"36 35 0:7 / /m - - drvfs\n", 7), Ok(true));
    assert_eq!(drvfs_of(b"36 35 0:7 / /m rw - drvfs\n", 7), Ok(true));
    assert_eq!(drvfs_of(b"36 35 0:7 / /m rw -- drvfs\n", 7), Ok(false));
}

#[test]
fn the_aname_option_is_every_match_and_a_whole_word() {
    assert!(ls::aname_drvfs("xaname=drvfs,aname=drvfs"));
    assert!(ls::aname_drvfs("rw;aname=drvfs;x"));
    assert!(!ls::aname_drvfs("rw,aname=drvfs:"));
    assert!(!ls::aname_drvfs("aname=drvfsX"));
    assert!(!ls::aname_drvfs("aname=DRVFS"));
}

#[test]
fn darwin_dev_t_is_signed_32_bits() {
    let d = |v: i128| ls::device_name(Platform::Darwin, v);
    assert_eq!(d(-1), Ok("255:16777215".into()));
    assert_eq!(d((1 << 64) - (1 << 31)), Ok("128:0".into()));
    assert!(matches!(d(1 << 31), Err(StampError::Overflow(_))));
    assert!(matches!(d(-2), Err(StampError::Overflow(_))));
    assert_eq!(
        d(1 << 64),
        Err(StampError::Overflow("int too big to convert"))
    );
}

#[test]
fn linux_device_numbers_are_glibcs() {
    let d = |v: i128| ls::device_name(Platform::Linux, v);
    assert_eq!(d((1 << 32) + 0x12345), Ok("291:1048645".into()));
    assert_eq!(d(0x12345), Ok("291:69".into()));
    assert_eq!(d(0x0803), Ok("8:3".into()));
    // glibc's headers define NODEV: -1 is accepted, and a part of
    // 0xffffffff is answered -1 (`major_minor_conv`), the other part as
    // it is, as CPython 3.13 on glibc answers (location_stamps_platform).
    assert_eq!(d(-1), Ok("-1:-1".into()));
    assert_eq!(d((1 << 64) - 1), Ok("-1:-1".into()));
    assert_eq!(d(0xFFFF_F000_000F_FF00), Ok("-1:0".into()));
    assert_eq!(d(0x0000_0FFF_FFF0_00FF), Ok("0:-1".into()));
    assert_eq!(d((1 << 64) - 256), Ok("-1:4294967040".into()));
    assert_eq!(d(0xFFFF_F000_0000_0000), Ok("4294963200:0".into()));
    assert_eq!(
        d(-2),
        Err(StampError::Overflow(
            "can't convert negative int to unsigned"
        ))
    );
    assert_eq!(ls::device_name(Platform::Windows, -5), Ok(String::new()));
    assert!(matches!(
        ls::device_name(Platform::Other, -2),
        Err(StampError::NotPorted(_))
    ));
}

#[test]
fn musl_defines_no_nodev() {
    // Without NODEV, -1 is a negative int like any other and every part
    // is unsigned, as CPython 3.13 on musl answers.
    let d = |v: i128| ls::device_name(Platform::LinuxMusl, v);
    assert_eq!(
        d(-1),
        Err(StampError::Overflow(
            "can't convert negative int to unsigned"
        ))
    );
    assert_eq!(d((1 << 64) - 1), Ok("4294967295:4294967295".into()));
    assert_eq!(d(0xFFFF_F000_000F_FF00), Ok("4294967295:0".into()));
    assert_eq!(d((1 << 32) + 0x12345), Ok("291:1048645".into()));
    // musl's Linux is `_LINUX` all the same: its mount table is read.
    let fs = TableFs {
        file: Some((MOUNTS.to_vec(), Ok(b"36 35 0:7 / /m rw - drvfs\n".to_vec()))),
        ..TableFs::default()
    };
    let m = Machine {
        fs: &fs,
        platform: Platform::LinuxMusl,
        mountinfo: MOUNTS,
    };
    assert_eq!(ls::drvfs(&m, 7), Ok(true));
    assert_eq!(ls::kept_in_ticks(&m, &[1_000_000_000], Some(7)), Ok(true));
    assert_eq!(
        ls::drvfs(&m, -1),
        Err(StampError::Overflow(
            "can't convert negative int to unsigned"
        ))
    );
}

#[test]
fn filetime_is_not_64_bit_arithmetic() {
    assert_eq!(
        ls::filetime_ns(i128::from(i64::MIN)),
        Some(-933_981_677_285_477_580_800)
    );
    assert_eq!(ls::filetime_ns(0), Some(-11_644_473_600_000_000_000));
    assert_eq!(ls::filetime_ns(i128::MAX), None);
}

#[test]
fn ticks_need_one_time_off_the_second() {
    assert!(!ls::in_ticks(&[]));
    assert!(!ls::in_ticks(&[1_000_000_000, 150]));
    assert!(ls::in_ticks(&[1_000_000_000, -100]));
    assert!(!ls::in_ticks(&[-1_000_000_000]));
}

#[test]
fn capture_ticks_reads_the_key_before_the_platform() {
    let fs = TableFs::default();
    let m = Machine {
        fs: &fs,
        platform: Platform::Windows,
        mountinfo: MOUNTS,
    };
    let st = stat(1, 7, 100, 100, 0);
    let got = ls::capture_ticks(&m, Some(&st), Some(&[false, false]), &[], &[]);
    assert_eq!(got, Err(StampError::Index));
    assert_eq!(ls::capture_ticks(&m, None, Some(&[]), &[], &[]), Ok(None));
}

#[test]
fn windows_stamps_skip_a_file_without_change_time() {
    let mut fs = TableFs {
        basic_fails: true,
        ..TableFs::default()
    };
    fs.stats
        .insert(b"g/description".to_vec(), Ok(stat(1, 1, 5, 6, 7)));
    fs.stats
        .insert(b"g/info/exclude".to_vec(), Ok(stat(2, 1, 5, 6, 7)));
    fs.basic.insert(
        b"g/info/exclude".to_vec(),
        BasicInfo::Times([0, 0, 0, 116_444_736_000_000_001]),
    );
    let m = Machine {
        fs: &fs,
        platform: Platform::Windows,
        mountinfo: MOUNTS,
    };
    let got = ls::init_stamps(&m, bettermemory_core::fsdata::Flavour::Posix, b"g", false).unwrap();
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].name, "info/exclude");
    assert_eq!(got[0].ctime_ns, 100);
}

#[test]
fn a_read_error_after_the_first_chunk_keeps_a_match_in_it() {
    // A drvfs line ending past byte 4,096 but inside the first 8,192: the
    // first read holds it whole, and the failing second read is never
    // reached. A line ending past 8,192 meets the error and answers False.
    let table_of = |len: usize| {
        let mut t = b"36 35 0:7 / /m rw - drvfs ".to_vec();
        t.resize(len, b'y');
        t.push(b'\n');
        t
    };
    for (len, want) in [(6000, true), (9000, false)] {
        let fs = TableFs {
            file: Some((MOUNTS.to_vec(), Ok(table_of(len)))),
            reads_ok: Some(1),
            ..TableFs::default()
        };
        let m = Machine {
            fs: &fs,
            platform: Platform::Linux,
            mountinfo: MOUNTS,
        };
        assert_eq!(ls::drvfs(&m, 7), Ok(want), "line of {len}");
    }
}

#[test]
fn a_windows_read_stat_without_change_time_still_answers_true() {
    let fs = TableFs::default();
    let m = Machine {
        fs: &fs,
        platform: Platform::Windows,
        mountinfo: MOUNTS,
    };
    let git = stat(1, 7, 100, 100, 0);
    let mut read = stat(2, 7, 100, 100, 0);
    read.ctime_ns = None;
    assert_eq!(
        ls::capture_ticks(&m, Some(&git), None, &[Some(read)], &[]),
        Ok(Some(true))
    );
}
