//! `log::ring` over hostile input: ids of a million characters, anchors
//! and bindings nested past the JSON limit, integers past i64, files of
//! megabytes. No panic, and reading a
//! binding is linear in its size: ten times the bytes cost under sixty
//! times the time.

mod timing;

use std::path::Path;

use bettermemory_core::log::ring::{BadId, KeyFiles};
use bettermemory_core::pyvalue::PyValue;

const ID: &str = "0123456789abcdef0123456789abcdef";

#[test]
fn huge_and_odd_ids_are_refused_with_a_short_message() {
    let huge = PyValue::str(&"\u{e9}".repeat(1_000_000));
    let Err(BadId::ValueError(text)) = KeyFiles::new(Path::new("k"), &huge) else {
        panic!("a ValueError");
    };
    assert!(text.len() < 300, "{}", text.len());
    let mut nested = PyValue::List(vec![]);
    for _ in 0..50 {
        nested = PyValue::List(vec![nested]);
    }
    assert!(KeyFiles::new(Path::new("k"), &nested).is_err());
    let surrogate = PyValue::Str(
        [0xD800]
            .into_iter()
            .chain(ID.chars().skip(1).map(u32::from))
            .collect(),
    );
    assert!(KeyFiles::new(Path::new("k"), &surrogate).is_err());
}

#[cfg(unix)]
#[test]
fn files_nested_or_large_raise_or_read_without_panic() {
    use std::fs;
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("bm-kf-robust-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(root.join("keys")).unwrap();
    let r = KeyFiles::new(&root.join("keys"), &PyValue::str(ID)).unwrap();
    for depth in [9_998usize, 9_999, 1_000_000] {
        let text = format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        fs::write(r.origin_path(), &text).unwrap();
        let _ = r.read_origin();
        fs::write(r.binding_path(), &text).unwrap();
        let _ = r.read_binding();
    }
    let anchor = format!(
        "{{\"seq\": {}, \"mac\": \"{}\"}}",
        "9".repeat(5_000),
        "a".repeat(64)
    );
    fs::write(r.origin_path(), anchor).unwrap();
    assert_eq!(r.read_origin(), Ok(None));
    assert_eq!(
        r.write_origin(i64::MAX, &"a".repeat(64), None).ok(),
        Some(true)
    );
    assert_eq!(r.read_origin().unwrap().map(|o| o.seq), Some(i64::MAX));
    let size = |n: usize| {
        let body = format!("{{\"path\": \"/{}\"}}", "x".repeat(n));
        fs::write(r.binding_path(), body).unwrap();
        timing::wall(|| r.read_binding().unwrap())
    };
    timing::grows_within(
        "read_binding: 200 KB and 2 MB",
        timing::TENFOLD,
        &mut || size(200_000),
        &mut || size(2_000_000),
    );
    fs::remove_dir_all(&root).unwrap();
}
