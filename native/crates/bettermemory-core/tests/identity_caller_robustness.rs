//! Hostile input for `identity`: very long values, many headers, deep
//! JSON, every code point. No panic in debug or release, and each call
//! linear in its input.

use std::time::{Duration, Instant};

use bettermemory_core::identity::{self as id, Actor, Request};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn timed<T>(limit_ms: u64, f: impl FnOnce() -> T) -> T {
    let start = Instant::now();
    let out = f();
    let took = start.elapsed();
    // Generous for a debug build; the reference takes about 200 ms for a
    // 4 MB value it walks, and refuses these unread.
    assert!(took < Duration::from_millis(limit_ms), "took {took:?}");
    out
}

#[test]
fn a_four_megabyte_value_is_refused_quickly() {
    let ascii: PyStr = vec![0x61; 4 << 20];
    assert_eq!(timed(2000, || id::clean(Some(&ascii), true)), None);
    let wide: PyStr = vec![0xE9; 4 << 20];
    assert_eq!(timed(2000, || id::clean(Some(&wide), true)), None);
    let spaces: PyStr = vec![0x20; 4 << 20];
    assert_eq!(timed(2000, || id::clean(Some(&spaces), false)), None);
}

#[test]
fn deep_and_long_json_headers_are_refused() {
    for text in [
        "[".repeat(1_000_000),
        format!("{}{}", "[".repeat(769), "]".repeat(769)),
        format!("\"{}\"", "\\u0000".repeat(300_000)),
        "\"".repeat(1539),
    ] {
        assert_eq!(timed(2000, || id::json_workspace(Some(&cps(&text)))), None);
    }
}

#[test]
fn many_headers_and_a_huge_environment() {
    let mut headers: Vec<(PyStr, PyStr)> = (0..200_000)
        .map(|i| (cps(&format!("X-Noise-{i}")), cps("v")))
        .collect();
    headers.push((cps("X-BETTERMEMORY-CLIENT"), vec![0x10FFFF; 300]));
    headers.push((cps("x-bettermemory-workspace"), vec![0xDC80; 256]));
    let r = Request {
        headers: Some(headers),
        client_info: Some((Some(vec![0x200B; 5_000_000]), Some(vec![0; 10]))),
        principal: Some(vec![0x20; 3_000_000]),
        request_id: Some(vec![0xD800; 1000]),
        root: Some(Vec::new()),
    };
    let caller = timed(10_000, || {
        id::resolve(Some(&r), &mut |_| Some(vec![0x61; 1 << 20]), &mut || None)
    });
    assert_eq!(caller.actor.client, None);
    assert_eq!(caller.workspace.path, Some(vec![0xDC80; 256]));
    assert_eq!(caller.actor.principal, None);
}

#[test]
fn every_code_point_alone_and_at_the_ends() {
    for cp in [
        0u32,
        0x7F,
        0xD800,
        0xDFFF,
        0xFFFF,
        0x10FFFF,
        0x11_0000,
        u32::MAX,
    ] {
        for sf in [true, false] {
            let _ = id::clean(Some(&[cp]), sf);
            let _ = id::clean(Some(&[0x20, cp, 0x20]), sf);
            let _ = id::strip_format_characters(&[cp, cp]);
        }
    }
}

#[test]
fn a_registry_key_of_long_fields() {
    let long: PyStr = vec![0x7C; 1 << 20];
    let mut a = Actor {
        principal: Some(long.clone()),
        session: Some(long.clone()),
        client: Some(long.clone()),
        model: Some(long),
        ..Actor::default()
    };
    for f in ["session", "client", "model"] {
        a.set_source(f, "header");
    }
    let key = timed(2000, || id::registry_key(&a)).expect("a key");
    assert_eq!(
        key.len(),
        4 * (1 << 20) + 3 + "principal=session=client=model=".len()
    );
    let _ = a.to_record();
}

#[test]
fn a_long_ascii_value_and_a_long_header_key() {
    let value: String = "a".repeat(4_000_000);
    let req = Request {
        headers: Some(vec![(cps("x-bettermemory-client"), cps(&value))]),
        ..Request::default()
    };
    let caller = timed(2000, || {
        id::resolve(Some(&req), &mut |_| None, &mut || None)
    });
    assert!(caller.actor.client.is_none());
    let key: String = "X".repeat(4_000_000);
    let req = Request {
        headers: Some(vec![(cps(&key), cps("v"))]),
        ..Request::default()
    };
    let caller = timed(2000, || {
        id::resolve(Some(&req), &mut |_| None, &mut || None)
    });
    assert!(caller.actor.client.is_none());
}
