//! Hostile input for `response_write`: a million similar hits, origin
//! fields of a million code points with surrogates, every code point past
//! U+10FFFF refused rather than encoded. No panic in debug or release, and
//! time linear in the input.

mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::{Confidence, Origin};
use bettermemory_core::response_write::{add_similar, fsencode_as, origin_to_dict_as, FsEncoding};
use bettermemory_core::search_dedup::SimilarHit;
use bettermemory_core::unicode;

fn hit() -> SimilarHit {
    let at = DateTime::from_isoformat(&unicode::cps("0001-01-01T00:00:00")).expect("dt");
    SimilarHit {
        id: vec![0x41; 26],
        scopes: Vec::new(),
        confidence: Confidence::Low,
        snippet: vec![0xDCFF; 1000],
        similarity: f64::NAN,
        relevance: "high",
        created: at,
        updated: at,
        removed_at: Some(at),
        removed_reason: Some(Vec::new()),
    }
}

#[test]
fn a_million_hits_show_five() {
    let hits = vec![hit(); 1_000_000];
    let mut out = Vec::new();
    add_similar(&mut out, "related", &hits);
    assert_eq!(out.len(), 2);
    assert_eq!(out[1].1, PyValue::Int("999995".to_owned()));
}

fn origin(n: usize) -> Origin {
    let field: Vec<u32> = (0..n)
        .map(|i| if i % 2 == 0 { 0xDCFF } else { 0x10FFFF })
        .collect();
    Origin {
        cwd: Some(field.clone()),
        repo: Some(field.clone()),
        branch: Some(field.clone()),
        worktree_root: Some(field),
        source: None,
    }
}

#[test]
fn huge_origins_encode_under_both_flavours() {
    let o = origin(1_000_000);
    for enc in [FsEncoding::SurrogateEscape, FsEncoding::SurrogatePass] {
        assert!(origin_to_dict_as(Some(&o), enc).expect("ok").is_some());
    }
}

#[test]
fn code_points_past_the_range_are_refused() {
    for enc in [FsEncoding::SurrogateEscape, FsEncoding::SurrogatePass] {
        assert_eq!(fsencode_as(&[0x110000], enc), None);
        assert_eq!(fsencode_as(&[u32::MAX], enc), None);
    }
}

#[test]
fn origin_grows_linearly() {
    let small = origin(20_000);
    let large = origin(200_000);
    timing::grows_within(
        "origin_to_dict: 20,000 and 200,000 code points per field",
        timing::TENFOLD,
        &mut || timing::once(|| origin_to_dict_as(Some(&small), FsEncoding::SurrogatePass)),
        &mut || timing::once(|| origin_to_dict_as(Some(&large), FsEncoding::SurrogatePass)),
    );
}
