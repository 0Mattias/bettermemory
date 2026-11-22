//! Hostile input for `_first_file_root` and `unquote`: megabytes of
//! escapes, invalid bytes and code points past ASCII, many roots. No panic
//! in debug or release, and each call linear in its input.

mod timing;

use bettermemory_core::identity as id;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn uri(piece: &str, n: usize) -> PyStr {
    cps(&format!("file:///{}", piece.repeat(n)))
}

#[test]
fn megabytes_of_escapes_are_refused_without_panic() {
    for piece in ["%41", "%FF", "%E2%82", "%", "%4", "\u{e9}%C3", "\u{10ffff}"] {
        let got = id::first_file_root(&[Some(uri(piece, 1 << 18))]);
        assert_eq!(got, Ok(None), "{piece}");
    }
    let raw: PyStr = (0..0x11_0000u32).collect();
    let _ = id::unquote(&raw);
    let _ = id::first_file_root(&[Some(raw)]);
}

#[test]
fn every_byte_escaped_decodes_to_one_code_point_or_one_escape_each() {
    let text: String = (0..=255u8).map(|b| format!("%{b:02X}")).collect();
    let out = id::unquote(&cps(&text));
    assert_eq!(out.len(), 256);
    assert!(out[128..].iter().all(|&c| (0xDC80..=0xDCFF).contains(&c)));
}

#[test]
fn many_roots_are_walked() {
    let mut roots: Vec<Option<PyStr>> = vec![None; 200_000];
    roots.push(Some(cps("file:///end")));
    assert_eq!(id::first_file_root(&roots), Ok(Some(cps("/end"))));
}

#[test]
fn unquote_is_linear() {
    for piece in ["%41", "%FF", "%E2\u{e9}"] {
        let small = cps(&piece.repeat(20_000));
        let large = cps(&piece.repeat(200_000));
        timing::grows_within(
            "unquote",
            30,
            &mut || timing::once(|| id::unquote(&small)),
            &mut || timing::once(|| id::unquote(&large)),
        );
    }
}

#[test]
fn first_file_root_is_linear() {
    let small = vec![Some(uri("%FF", 20_000))];
    let large = vec![Some(uri("%FF", 200_000))];
    timing::grows_within(
        "first_file_root",
        30,
        &mut || timing::once(|| id::first_file_root(&small)),
        &mut || timing::once(|| id::first_file_root(&large)),
    );
}
