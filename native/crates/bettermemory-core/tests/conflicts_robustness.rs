//! Hostile inputs to the conflict scan: 2,000 memories (1,999,000 pairs),
//! bodies of a million code points shaped against each pattern and the
//! tokenizer, values nested past `repr`'s depth, integers and floats at
//! their bounds. None may panic, in debug or release, and each grows with
//! its input as the reference's loops do (tests/timing).
//!
//! The reference (CPython 3.13.13, Apple M-series): `_find_dedup_with_skips`
//! over 200 such memories takes 0.14 s and over 2,000 8.85 s;
//! `_body_signals` of a million code points 0.02 to 0.42 s by shape; the
//! scan of two such bodies 0.78 s.

mod timing;

use bettermemory_core::conflicts::{
    body_hash, body_signals, find_conflict_candidates, find_dedup_with_skips, pair_id,
    ConflictCandidate, ConflictError,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::records::{Confidence, Memory, RecordFields, Source};
use bettermemory_core::unicode::cps;

const U: &str = "2024-03-01T12:00:00+00:00";

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).expect("an isoformat")
}

fn mem(id: &str, body: Vec<u32>) -> Memory {
    Memory {
        fields: RecordFields {
            id: cps(id),
            created: dt(U),
            updated: dt(U),
            scopes: vec![cps("global")],
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body,
            origin: None,
            actor: None,
            last_verified_at: None,
            category: None,
            verified_paths: Vec::new(),
            verified_commits: Vec::new(),
            verified_versions: Vec::new(),
            verified_absent_paths: Vec::new(),
            verified_path_targets: None,
            verified_absent_path_targets: None,
            claims: Vec::new(),
            verified_head: None,
        },
        links: Vec::new(),
        corroborations: "0".to_owned(),
        last_corroborated: None,
    }
}

/// `n` short bodies, a few of them near-identical: the reference's timing
/// rows build the same.
fn many(n: usize) -> Vec<Memory> {
    (0..n)
        .map(|i| {
            mem(
                &format!("m{i:05}"),
                cps(&format!("Use sudo w{i} w{} port {}.", i % 7, i % 13)),
            )
        })
        .collect()
}

#[test]
fn two_thousand_memories_grow_as_the_pairs() {
    let ms = many(2_000);
    let (cands, skips, _) = find_dedup_with_skips(&ms, None).expect("a scan");
    // As the reference: 147 candidates and no skipped pair.
    assert_eq!((cands.len(), skips.len()), (147, 0));
    let small = many(200);
    let (cands, _, _) = find_dedup_with_skips(&small, None).expect("a scan");
    assert_eq!(cands.len(), 14);
    // Ten times the memories is a hundred times the pairs: the reference's
    // quadratic loop, with the room `TENFOLD` leaves a linear claim.
    timing::grows_within(
        "2,000 memories against 200",
        timing::TENFOLD * 10,
        &mut || timing::once(|| find_dedup_with_skips(&small, None)),
        &mut || timing::once(|| find_dedup_with_skips(&ms, None)),
    );
}

fn repeat(piece: &str, total: usize) -> Vec<u32> {
    let piece = cps(piece);
    piece.iter().copied().cycle().take(total).collect()
}

#[test]
fn a_million_code_points_of_every_shape() {
    let shapes = [
        ("Do not use sudo, ", true),
        (".", false),
        ("a", false),
        ("no ", true),
        ("5", false),
        (". no", true),
        ("!?;\n", false),
        ("don\u{2019}t ", true),
        ("x-y_z.", false),
        ("3.27.0-", false),
    ];
    for (piece, negated) in shapes {
        let body = repeat(piece, 1_000_000);
        let sig = body_signals(&body).expect("signals");
        assert_eq!(sig.has_negation, negated, "{piece:?}");
        let _ = body_hash(&body);
    }
    // Lone surrogates: no token, no negator, and no hash.
    let lone = vec![0xD800u32; 1_000_000];
    let sig = body_signals(&lone).expect("signals");
    assert!(!sig.has_negation && sig.asserted.is_empty() && sig.numbers.is_empty());
    assert_eq!(body_hash(&lone), Err(ConflictError::Encode));
    // Growth: a tenth of the body against the whole, each shape a negator
    // in every clause.
    let small = repeat("Do not use sudo, now. ", 100_000);
    let large = repeat("Do not use sudo, now. ", 1_000_000);
    timing::grows_within(
        "body_signals of 1,000,000 code points against 100,000",
        timing::TENFOLD,
        &mut || timing::once(|| body_signals(&small)),
        &mut || timing::once(|| body_signals(&large)),
    );
}

#[test]
fn two_bodies_of_a_million_code_points_scan() {
    let a = repeat("Do not use sudo, ", 1_000_008);
    let b: Vec<u32> = repeat("Do  use sudo, ", 1_000_008);
    let ms = [mem("a", a), mem("b", b)];
    let (cands, skips, _) = find_dedup_with_skips(&ms, None).expect("a scan");
    // As the reference: one skipped pair, by polarity.
    assert_eq!((cands.len(), skips.len()), (0, 1));
    assert_eq!(skips[0].detector, "polarity");
    let rows = find_conflict_candidates(&ms, None, &dt(U)).expect("rows");
    assert_eq!(rows.len(), 1);
    // The reference's summary backs off to a word: 98 code points.
    assert_eq!(rows[0].summary_a.len(), 98);
}

#[test]
fn every_threshold_bound() {
    let ms = many(40);
    for th in [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::MIN_POSITIVE,
        -0.0,
        1.0,
        f64::MAX,
    ] {
        let (cands, skips, _) = find_dedup_with_skips(&ms, Some(th)).expect("a scan");
        let admitted = th.is_nan() || th <= 0.0;
        if admitted {
            assert_eq!(cands.len() + skips.len(), 40 * 39 / 2, "{th}");
        }
        assert!(cands.windows(2).all(|w| w[0].similarity >= w[1].similarity));
    }
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn from_dict_at_every_bound() {
    let base = |key: &str, value: PyValue| {
        let mut out = vec![
            (PyValue::str("id"), PyValue::str("i")),
            (PyValue::str("a_id"), PyValue::str("a")),
            (PyValue::str("b_id"), PyValue::str("b")),
        ];
        out.push((PyValue::str(key), value));
        out
    };
    // `str()` past the recursion limit and past the digits limit; float()
    // past the float range; a str of a million digits.
    assert_eq!(
        ConflictCandidate::from_dict(&base("note", nested(100_000))).map(|_| ()),
        Err(ConflictError::Value(PyError::RecursionError))
    );
    assert_eq!(
        ConflictCandidate::from_dict(&base("note", PyValue::Int("9".repeat(100_000)))).map(|_| ()),
        Err(ConflictError::Value(PyError::ValueError))
    );
    assert_eq!(
        ConflictCandidate::from_dict(&base("similarity", PyValue::Int("9".repeat(4_300))))
            .map(|_| ()),
        Err(ConflictError::Value(PyError::OverflowError))
    );
    let digits = PyValue::Str(repeat("1", 1_000_000));
    let row = ConflictCandidate::from_dict(&base("similarity", digits)).expect("a row");
    assert_eq!(row.similarity, f64::INFINITY);
    let row = ConflictCandidate::from_dict(&base(
        "similarity",
        PyValue::Int(format!("-{}", "9".repeat(308))),
    ))
    .expect("a row");
    assert_eq!(row.similarity, -1e308);
    // The dict form of a row with extreme similarities.
    for sim in [f64::MAX, f64::MIN_POSITIVE, f64::NAN, -f64::INFINITY] {
        let mut row = ConflictCandidate::from_dict(&base("x", PyValue::None)).expect("a row");
        row.similarity = sim;
        let _ = row.to_dict();
    }
    // A pair id over ids of a million code points.
    let long = repeat("\u{1F600}", 1_000_000);
    assert!(pair_id(&long, &long).expect("an id").starts_with("cf-"));
}
