//! Hostile input for `location::leaves`: very long URLs, every percent
//! form, the float bounds, short statx buffers; linear growth.

mod timing;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::location::leaves::{
    file_url_path, int_of_float, statx_birth, unquote, url2pathname_nt, Platform,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

#[test]
fn long_urls_answer() {
    let url = cps(&format!("file:///{}", "%E2%82%AC/a:b?#".repeat(200_000)));
    let posix = file_url_path(Flavour::Posix, &url);
    assert!(posix.len() > 1_000_000);
    let nt = file_url_path(Flavour::Windows, &url);
    assert!(nt.len() > 1_000_000);
    assert!(url2pathname_nt(&cps(&"|".repeat(100_000))).is_err());
}

#[test]
fn every_percent_pair() {
    for a in 0u32..128 {
        for b in 0u32..128 {
            let text = vec![u32::from(b'%'), a, b, 0xe9, u32::from(b'%')];
            let out = unquote(&text);
            assert!(!out.is_empty());
        }
    }
}

#[test]
fn float_bounds_and_short_buffers() {
    for f in [
        f64::MAX,
        f64::MIN,
        f64::MIN_POSITIVE,
        -0.0,
        1e38,
        -1e38,
        2f64.powi(127),
    ] {
        assert!(int_of_float(f).is_ok());
    }
    assert!(int_of_float(f64::NAN).is_err());
    for len in [0, 4, 80, 81, 87, 88, 91, 92, 256] {
        let mut raw = vec![0xffu8; len];
        if len >= 2 {
            raw[1] = 0x08;
        }
        let _ = statx_birth(Platform::Linux, Some((0, &raw)));
    }
}

#[test]
fn unquote_grows_linearly() {
    let small = cps(&"%C3%A9\u{e9}%zz".repeat(20_000));
    let large = cps(&"%C3%A9\u{e9}%zz".repeat(200_000));
    timing::grows_within(
        "20,000 and 200,000 units",
        timing::TENFOLD,
        &mut || timing::once(|| unquote(&small)),
        &mut || timing::once(|| unquote(&large)),
    );
}
