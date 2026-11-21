//! Inputs a plausible wrong `location::leaves` answers otherwise.

use std::sync::Arc;

use bettermemory_core::fsdata::{
    birthtime_float, BirthAttr, BirthFacts, Flavour, Fs, Kind, PyErr, StatResult, StdFs,
};
use bettermemory_core::location::leaves::{
    birth, evicted, fetch, file_url_path, int_of_float, object_key, statx_birth, unquote,
    url2pathname_nt, Platform, HOST_PLATFORM,
};
use bettermemory_core::location_reads::Board;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn st(flags: u32, attrs: u32) -> StatResult {
    StatResult {
        kind: Kind::Regular,
        mode: Some(0o100644),
        ino: Some(7),
        dev: Some(3),
        size: 0,
        mtime_ns: 0,
        ctime_ns: Some(0),
        flags,
        file_attributes: attrs,
        reparse_tag: Some(0),
    }
}

#[test]
fn unquote_decodes_each_ascii_run_alone() {
    // One UTF-8 sequence split by a non-ASCII character is two errors.
    assert_eq!(unquote(&cps("%C3\u{e9}%A9")), cps("\u{fffd}\u{e9}\u{fffd}"));
    assert_eq!(unquote(&cps("%C3%A9")), cps("\u{e9}"));
    // A lone surrogate's bytes are three replacements, not one.
    assert_eq!(unquote(&cps("%ED%A0%80")), cps("\u{fffd}\u{fffd}\u{fffd}"));
    assert_eq!(unquote(&cps("%zz%4")), cps("%zz%4"));
    assert_eq!(unquote(&cps("%2f%2F")), cps("//"));
    // U+0080 is the first character outside the ASCII runs.
    assert_eq!(unquote(&cps("\u{80}%41")), cps("\u{80}A"));
    assert_eq!(unquote(&cps("\u{7f}%41")), cps("\u{7f}A"));
}

#[test]
fn statx_seconds_are_signed_and_the_mask_decides() {
    let mut raw = vec![0u8; 256];
    raw[0..4].copy_from_slice(&0x800u32.to_le_bytes());
    raw[80..88].copy_from_slice(&(-1i64).to_le_bytes());
    raw[88..92].copy_from_slice(&5u32.to_le_bytes());
    assert_eq!(
        statx_birth(Platform::Linux, Some((0, &raw))),
        Some(-999_999_995)
    );
    assert_eq!(statx_birth(Platform::Other, Some((0, &raw))), None);
    assert_eq!(statx_birth(Platform::Linux, Some((-1, &raw))), None);
    raw[1] = 0;
    assert_eq!(statx_birth(Platform::Linux, Some((0, &raw))), None);
}

#[test]
fn birth_takes_the_attributes_in_the_reference_s_order() {
    let mut facts = BirthFacts {
        birthtime_ns: BirthAttr::Bool(true),
        birthtime: BirthAttr::Int(1),
        ctime_ns: Some(2),
        statx: Some(3),
    };
    assert_eq!(
        birth(true, &facts, Platform::Linux),
        Ok(Some("True".into()))
    );
    assert_eq!(birth(false, &facts, Platform::Linux), Ok(None));
    facts.birthtime_ns = BirthAttr::Float(9.0);
    assert_eq!(
        birth(true, &facts, Platform::Linux),
        Ok(Some("1000000000".into()))
    );
    facts.birthtime = BirthAttr::Absent;
    assert_eq!(birth(true, &facts, Platform::Windows), Ok(Some("2".into())));
    assert_eq!(birth(true, &facts, Platform::Linux), Ok(Some("3".into())));
    // The facts carry `_statx_birth`'s answer, which checks the platform itself.
    assert_eq!(birth(true, &facts, Platform::Other), Ok(Some("3".into())));
    facts.birthtime = BirthAttr::Float(1e30);
    assert_eq!(
        birth(true, &facts, Platform::Other),
        Ok(Some("1000000000000000090824893823431825424384".into()))
    );
}

#[test]
fn int_of_a_float_is_exact_past_i128() {
    assert_eq!(int_of_float(-1.9), Ok("-1".into()));
    assert_eq!(
        int_of_float(2f64.powi(200)),
        Ok("1606938044258990275541962092341162602522202993782792835301376".into())
    );
    // Python's int() of the largest floats.
    assert_eq!(
        int_of_float(f64::MAX),
        Ok("179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368".into())
    );
    assert_eq!(
        int_of_float(1.7e308),
        Ok("169999999999999993883079578865998174333346074304075874502773119193537729178160565864330091787584707988572262467983188919169916105593357174268369962062473635296474636515660464935663040684957844303524367815028553272712298986386310828644513212353921123253311675499856875650512437415429217994623324794855339589632".into())
    );
    assert_eq!(
        int_of_float(-(2f64.powi(1023) + 2f64.powi(971))),
        Ok("-89884656743115815344868354886649353244625979315500989311227644933441752928222905938473157350844425860139711866292707572253863326332160750970803599255673669580601929560746945838207839311479812235103755919672374710952642786610471374045061106479117507470227939822734724191182691726800274576002630468252211347456".into())
    );
}

#[test]
fn int_of_float_at_the_largest_float_is_quick() {
    let start = std::time::Instant::now();
    for _ in 0..100 {
        int_of_float(f64::MAX).unwrap();
    }
    // About 30 passes over 35 limbs each; the doubling it replaced took
    // 971 passes and 9 ms a call in a debug build.
    assert!(start.elapsed().as_secs_f64() < 5.0);
}

#[test]
fn macos_birth_time_is_cpython_s_float() {
    // CPython: (double)(unsigned long)tv_sec + 1e-9 * tv_nsec, then
    // `_birth`'s int(seconds * 1e9) (values read from CPython 3.13.13).
    let f = birthtime_float(1_795_267_021_692_880_901);
    assert_eq!(f, 1_795_267_021.692_880_9);
    let facts = BirthFacts {
        birthtime: BirthAttr::Float(f),
        ..BirthFacts::default()
    };
    assert_eq!(
        birth(true, &facts, Platform::Other),
        Ok(Some("1795267021692880896".into()))
    );
    // Two seconds before the epoch wraps through unsigned long.
    let facts = BirthFacts {
        birthtime: BirthAttr::Float(birthtime_float(-1_500_000_000)),
        ..BirthFacts::default()
    };
    assert_eq!(
        birth(true, &facts, Platform::Other),
        Ok(Some("18446744073709551616000000000".into()))
    );
}

#[test]
fn file_urls_off_windows_keep_query_and_fragment() {
    assert_eq!(
        file_url_path(Flavour::Posix, &cps("file://host/a?b#c")),
        cps("/a?b#c")
    );
    assert_eq!(file_url_path(Flavour::Posix, &cps("file://host")), cps(""));
}

#[test]
fn file_urls_on_windows() {
    assert_eq!(
        file_url_path(Flavour::Windows, &cps("file://D|/x")),
        cps("\\\\D|\\x")
    );
    assert_eq!(
        file_url_path(Flavour::Windows, &cps("file://d:/x")),
        cps("D:\\x")
    );
    assert_eq!(
        file_url_path(Flavour::Windows, &cps("file:///1:/x")),
        cps("\\1:\\x")
    );
    assert!(url2pathname_nt(&cps("///C:/a:b")).is_err());
}

#[test]
fn windows_drive_authority_is_two_characters() {
    // `_DRIVE.fullmatch`: a longer authority is a UNC host.
    assert_eq!(
        file_url_path(Flavour::Windows, &cps("file://a:b/x")),
        cps("\\\\a:b\\x")
    );
    assert_eq!(
        file_url_path(Flavour::Windows, &cps("file://ab:/x")),
        cps("\\\\ab:\\x")
    );
}

#[test]
fn nturl2path_drops_one_slash_of_a_fifth() {
    assert_eq!(url2pathname_nt(&cps("/////x")), Ok(cps("\\\\x")));
    assert_eq!(
        url2pathname_nt(&cps("//localhost////x")),
        Ok(cps("\\\\\\x"))
    );
}

#[cfg(unix)]
#[test]
fn local_joins_only_a_relative_path_onto_a_base() {
    use bettermemory_core::location::leaves::local;

    let dir = scratch("local");
    let real = std::fs::canonicalize(&dir).unwrap();
    let cwd = real.to_str().unwrap().as_bytes();
    let at = |s: &str| cps(&format!("{}{s}", real.to_str().unwrap()));
    // posixpath.join("", "x") is "x", read against the working directory.
    assert_eq!(
        local(&StdFs, Flavour::Posix, cwd, &cps("x"), Some(&[])),
        Ok(at("/x"))
    );
    // An absolute path ignores the base.
    assert_eq!(
        local(&StdFs, Flavour::Posix, cwd, &at("/y"), Some(&cps("/base"))),
        Ok(at("/y"))
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn eviction_is_either_mark() {
    assert!(!evicted(&st(0, 0)));
    assert!(evicted(&st(0x4000_0000, 0)));
    assert!(evicted(&st(0, 0x0004_0000)));
    assert!(evicted(&st(0, 0x0040_0000)));
}

fn scratch(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("bm-leaves-k-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn object_key_on_disk() {
    let dir = scratch("key");
    let file = dir.join("f");
    std::fs::write(&file, b"x").unwrap();
    let path = file.to_str().unwrap().as_bytes();
    let missing = dir.join("none");
    assert_eq!(
        object_key(
            &StdFs,
            missing.to_str().unwrap().as_bytes(),
            None,
            true,
            HOST_PLATFORM
        ),
        Ok(None)
    );
    let got = object_key(&StdFs, path, None, false, HOST_PLATFORM);
    if cfg!(windows) {
        // Windows' st_dev and st_ino are not ported.
        assert!(got.is_err());
    } else {
        let (dev, ino, born) = got.unwrap().unwrap();
        assert!(!dev.is_empty() && !ino.is_empty());
        assert_eq!(born, None);
        let (_, _, born) = object_key(&StdFs, path, None, true, HOST_PLATFORM)
            .unwrap()
            .unwrap();
        // CPython 3.13 on macOS has the float st_birthtime and no
        // st_birthtime_ns, so the key is int(st_birthtime * 1e9) (seen
        // here); Linux reads statx where the file system keeps a birth
        // time (ext4, btrfs, tmpfs from 5.x), so it may be None there;
        // Windows declines above.
        let facts = StdFs.birth_facts(path);
        if cfg!(target_os = "macos") {
            let BirthAttr::Float(f) = facts.birthtime else {
                panic!("{facts:?}")
            };
            assert_eq!(facts.birthtime_ns, BirthAttr::Absent);
            let ns: String = born
                .unwrap()
                .iter()
                .map(|&c| char::from_u32(c).unwrap())
                .collect();
            assert_eq!(ns, int_of_float(f * 1e9).unwrap());
            assert!(ns.parse::<i128>().unwrap() > 1_000_000_000_000_000_000);
        }
        if cfg!(target_os = "linux") {
            assert_eq!(facts.birthtime, BirthAttr::Absent);
            assert_eq!(facts.birthtime_ns, BirthAttr::Absent);
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn fetch_reads_and_stats() {
    let dir = scratch("fetch");
    let file = dir.join("f");
    std::fs::write(&file, b"x").unwrap();
    let start = std::time::Instant::now();
    let board = Board::new(Arc::new(move || start.elapsed().as_secs_f64()));
    assert_eq!(fetch(&StdFs, &board, file.to_str().unwrap(), 5.0), Ok(true));
    assert_eq!(
        fetch(&StdFs, &board, dir.join("none").to_str().unwrap(), 5.0),
        Ok(false)
    );
    assert_eq!(fetch(&StdFs, &board, "a\0b", 5.0), Err(PyErr::Value));
    let _ = std::fs::remove_dir_all(&dir);
}
