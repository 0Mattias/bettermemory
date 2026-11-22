//! Hostile project files: nesting past the reference's recursion, a name
//! and an id at the size bound, many keys; no panic, and time linear in
//! the input.

mod timing;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::project_file::{parse_raw, Fault, Found};
use bettermemory_core::pyvalue::PyStr;

fn path() -> PyStr {
    "/w/bettermemory.toml".chars().map(u32::from).collect()
}

#[test]
fn deep_nesting_declines_without_a_panic() {
    for depth in [400, 600, 20_000] {
        let doc = format!(
            "[project]\nid = {}{}\n",
            "[".repeat(depth),
            "]".repeat(depth)
        );
        let got = parse_raw(Flavour::Posix, &path(), doc.as_bytes());
        if depth < 497 {
            assert!(matches!(got, Ok(Found::Error(_))), "{depth}");
        } else {
            assert_eq!(got, Err(Fault::NotPorted), "{depth}");
        }
    }
}

#[test]
fn a_long_name_and_id_are_read_whole() {
    let name = "n".repeat(60_000);
    let doc = format!("[project]\nid = \"bmp_0123456789ABCDEFGHJKMNPQRS\"\nname = \"{name}\"\n");
    match parse_raw(Flavour::Posix, &path(), doc.as_bytes()) {
        Ok(Found::File(f)) => assert_eq!(f.name.len(), 60_000),
        other => panic!("{other:?}"),
    }
    let doc = format!("[project]\nid = \"{}\"\n", "\u{1b}".repeat(20_000));
    match parse_raw(Flavour::Posix, &path(), doc.as_bytes()) {
        Ok(Found::Error(e)) => assert!(e.detail.len() < 200),
        other => panic!("{other:?}"),
    }
}

#[test]
fn many_keys_parse_in_linear_time() {
    let doc = |n: usize| {
        let mut d = String::from("[project]\nid = \"bmp_0123456789ABCDEFGHJKMNPQRS\"\n");
        for i in 0..n {
            d.push_str(&format!("k{i} = {i}\n"));
        }
        d
    };
    let small = doc(500);
    let large = doc(5000);
    // The parse runs on a thread of its own (`toml::loads`): wall clock.
    timing::grows_within(
        "many keys",
        timing::TENFOLD,
        &mut || timing::wall(|| parse_raw(Flavour::Posix, &path(), small.as_bytes())),
        &mut || timing::wall(|| parse_raw(Flavour::Posix, &path(), large.as_bytes())),
    );
}
