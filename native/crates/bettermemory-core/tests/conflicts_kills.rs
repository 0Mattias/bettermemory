//! Inputs on which a plausible wrong conflict scan answers otherwise than
//! the reference. Each expected value is the reference's (CPython
//! 3.13.13), read from `conflicts.py` as `gen_conflicts` runs it.

use bettermemory_core::conflicts::{
    body_hash, body_signals, clause_polarity, conflict_signal, find_conflict_candidates,
    find_dedup_with_skips, pair_id, pick_keeper, ConflictCandidate, ConflictError,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{Confidence, Memory, RecordFields, Source};
use bettermemory_core::search_dedup::raw_content_token_set;
use bettermemory_core::unicode::cps;

const SUDO: &str = "Do not use sudo for deploys.";
const SUDO_POS: &str = "Use sudo for deploys.";
const PORT_A: &str = "The auth service listens on port 5432 in the cluster.";
const PORT_B: &str = "The auth service listens on port 5433 in the cluster.";
const PORT_DETAIL: &str = "The auth service listens on port 5432 in the cluster since 2026-07-20.";
const NEG_STAMP: &str = "\n\n_(consolidate --llm --from-transcript: do not use sudo, never)_";
const U: &str = "2024-03-01T12:00:00+00:00";

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).expect("an isoformat")
}

fn mem(id: &str, body: &str, updated: &str, paths: &[&str]) -> Memory {
    Memory {
        fields: RecordFields {
            id: cps(id),
            created: dt(U),
            updated: dt(updated),
            scopes: vec![cps("global")],
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body: cps(body),
            origin: None,
            actor: None,
            last_verified_at: None,
            category: None,
            verified_paths: paths.iter().map(|p| cps(p)).collect(),
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

fn signal(a: &str, b: &str) -> Option<&'static str> {
    conflict_signal(
        &body_signals(&cps(a)).expect("signals"),
        &body_signals(&cps(b)).expect("signals"),
    )
}

/// The keeper's id of a pair the guard lets through.
fn keeper(a: &Memory, b: &Memory) -> PyStr {
    let (k, _) = pick_keeper(a, b, None, None).expect("a keeper");
    k.fields.id.clone()
}

#[test]
fn the_guard_runs_before_attestation() {
    // A port that checks the tiers first crowns the attested side and
    // tombstones one half of a contradiction.
    let a = mem("a", SUDO, U, &["src/a.py"]);
    let b = mem("b", SUDO_POS, U, &[]);
    assert_eq!(
        pick_keeper(&a, &b, None, None),
        Err(ConflictError::ConflictingPair("polarity"))
    );
    let a = mem("a", PORT_A, U, &["src/a.py"]);
    let b = mem("b", PORT_B, U, &[]);
    assert_eq!(
        pick_keeper(&a, &b, None, None),
        Err(ConflictError::ConflictingPair("numeric"))
    );
}

#[test]
fn negation_is_read_off_the_raw_body() {
    // The dedup token sets drop the negators: both sides are {sudo, use,
    // deploi}, so negation read from them sees no flip.
    assert_eq!(
        raw_content_token_set(&cps(SUDO)),
        raw_content_token_set(&cps(SUDO_POS))
    );
    assert!(body_signals(&cps(SUDO)).expect("signals").has_negation);
    assert_eq!(signal(SUDO, SUDO_POS), Some("polarity"));
    // Contractions with either apostrophe; a left quote is no apostrophe.
    assert!(
        body_signals(&cps("DON\u{2019}T use sudo"))
            .expect("s")
            .has_negation
    );
    assert!(
        body_signals(&cps("don't use sudo"))
            .expect("s")
            .has_negation
    );
    assert!(
        !body_signals(&cps("don\u{2018}t use sudo"))
            .expect("s")
            .has_negation
    );
}

#[test]
fn a_comma_is_no_clause_boundary() {
    // Split at the comma, "beta" and "gamma" become asserted and the pair
    // flips mutually; the reference negates the whole clause.
    let x = "Do not use alpha, beta, or gamma.";
    let y = "Do not use beta; use alpha.";
    let (asserted, negated) = clause_polarity(&cps(x)).expect("polarity");
    assert!(asserted.is_empty());
    assert_eq!(
        negated.into_iter().collect::<Vec<_>>(),
        ["alpha", "beta", "gamma", "use"]
    );
    assert_eq!(signal(x, y), None);
    // The inverse-clause pair does flip, on its semicolon.
    assert_eq!(
        signal(
            "Deploy with the blue-green strategy; never do in-place.",
            "Never deploy with the blue-green strategy; do in-place."
        ),
        Some("polarity")
    );
}

#[test]
fn numeric_divergence_is_mutual() {
    assert_eq!(signal(PORT_A, PORT_DETAIL), None);
    assert_eq!(signal(PORT_DETAIL, PORT_A), None);
    assert_eq!(signal(PORT_A, PORT_B), Some("numeric"));
    // Both signals: polarity is named first.
    assert_eq!(
        signal(&format!("{PORT_A} Not on 5434."), PORT_B),
        Some("polarity")
    );
}

#[test]
fn the_stamp_is_out_of_the_signals() {
    let stamped = format!("{SUDO_POS}{NEG_STAMP}");
    assert_eq!(signal(&stamped, SUDO_POS), None);
    // The same words outside a stamp do carry a negator.
    assert_eq!(
        signal(&format!("{SUDO_POS} do not use sudo, never"), SUDO_POS),
        Some("polarity")
    );
    // The scan merges the stamped pair as duplicates.
    let ms = [mem("a", &stamped, U, &[]), mem("b", SUDO_POS, U, &[])];
    let (cands, skips, _) = find_dedup_with_skips(&ms, None).expect("a scan");
    assert_eq!((cands.len(), skips.len()), (1, 0));
}

#[test]
fn equal_similarities_keep_scan_order() {
    // Twelve memories of one token set: every pair scores 1.0. The pairs
    // of opposite polarity are skipped, the rest are candidates, each list
    // in the order the scan met them, as `list.sort` keeps it.
    let ms: Vec<Memory> = (0..12)
        .map(|i| {
            let body = if i % 3 == 0 { SUDO } else { SUDO_POS };
            mem(&format!("m{i:02}"), body, U, &[])
        })
        .collect();
    let (cands, skips, _) = find_dedup_with_skips(&ms, None).expect("a scan");
    let mut want_skips = Vec::new();
    let mut want_cands = 0;
    for i in 0..12 {
        for j in i + 1..12 {
            if (i % 3 == 0) != (j % 3 == 0) {
                want_skips.push((cps(&format!("m{i:02}")), cps(&format!("m{j:02}"))));
            } else {
                want_cands += 1;
            }
        }
    }
    let got: Vec<(PyStr, PyStr)> = skips
        .iter()
        .map(|p| (p.memory_id_a.clone(), p.memory_id_b.clone()))
        .collect();
    assert_eq!(got, want_skips);
    assert_eq!(cands.len(), want_cands);
    // A higher similarity moves ahead of every 1.0 pair only by its score.
    let mixed = [
        mem("a", PORT_A, U, &[]),
        mem("b", PORT_DETAIL, U, &[]),
        mem("c", PORT_A, U, &[]),
    ];
    let (cands, _, _) = find_dedup_with_skips(&mixed, Some(0.0)).expect("a scan");
    let order: Vec<f64> = cands.iter().map(|c| c.similarity).collect();
    assert_eq!(order[0], 1.0);
    assert!(order[1] < 1.0 && order[1] == order[2]);
    // a-b came before b-c in the scan; the tie keeps it.
    assert_eq!(cands[1].duplicate_id, cps("a"));
    assert_eq!(cands[2].keeper_id, cps("c"));
    // 24 memories of two bodies: 276 candidates whose two scores
    // interleave in scan order. Sorted, the 1.0 pairs come first and each
    // score keeps the scan's order; an unstable sort reorders the ties.
    let ms: Vec<Memory> = (0..24)
        .map(|i| {
            let body = if i % 2 == 0 { PORT_A } else { PORT_DETAIL };
            mem(&format!("m{i:02}"), body, U, &[])
        })
        .collect();
    let (cands, skips, _) = find_dedup_with_skips(&ms, Some(0.0)).expect("a scan");
    assert!(skips.is_empty());
    let mut want = Vec::new();
    for same in [true, false] {
        for i in 0..24 {
            for j in i + 1..24 {
                if (i % 2 == j % 2) == same {
                    // Every tier ties but the id: the larger keeps.
                    want.push((cps(&format!("m{j:02}")), cps(&format!("m{i:02}"))));
                }
            }
        }
    }
    let got: Vec<(PyStr, PyStr)> = cands
        .iter()
        .map(|c| (c.keeper_id.clone(), c.duplicate_id.clone()))
        .collect();
    assert_eq!(got, want);
}

#[test]
fn updated_is_compared_as_an_instant() {
    // 12:30+01:00 is 11:30Z: earlier than 12:00Z though its wall clock is
    // later.
    let a = mem("a", PORT_A, "2024-03-01T12:30:00+01:00", &[]);
    let b = mem("b", PORT_A, U, &[]);
    assert_eq!(keeper(&a, &b), cps("b"));
    // One instant at two offsets is a tie, decided by the id.
    let a = mem("b", PORT_A, "2024-03-01T13:00:00+01:00", &[]);
    let b = mem("a", PORT_A, U, &[]);
    assert_eq!(keeper(&a, &b), cps("b"));
    // A naive value against an aware one raises only at that tier.
    let a = mem("a", PORT_A, "2024-03-01T12:00:00", &[]);
    assert_eq!(
        pick_keeper(&a, &b, None, None),
        Err(ConflictError::NaiveAware)
    );
    let attested = mem("a", PORT_A, "2024-03-01T12:00:00", &["p"]);
    assert_eq!(keeper(&attested, &b), cps("a"));
}

#[test]
fn ties_fall_to_the_id_in_code_point_order() {
    // UTF-16 order puts U+1F600 (a surrogate pair) before U+FFFF.
    let a = mem("\u{1F600}", PORT_A, U, &[]);
    let b = mem("\u{FFFF}", PORT_A, U, &[]);
    assert_eq!(keeper(&a, &b), cps("\u{1F600}"));
    assert_eq!(
        pair_id(&cps("\u{1F600}"), &cps("\u{FFFF}")).expect("an id"),
        "cf-2f16b7c1edcc"
    );
    // An equal id answers (b, a).
    let a = mem("a", PORT_A, U, &[]);
    let b = mem("a", PORT_A, U, &[]);
    let (k, _) = pick_keeper(&a, &b, None, None).expect("a keeper");
    assert!(std::ptr::eq(k, &b));
}

#[test]
fn an_unknown_status_reads_pending() {
    let raw = |status: PyValue| {
        vec![
            (PyValue::str("id"), PyValue::str("cf-1")),
            (PyValue::str("a_id"), PyValue::str("a")),
            (PyValue::str("b_id"), PyValue::str("b")),
            (PyValue::str("status"), status),
        ]
    };
    for (status, want) in [
        (PyValue::str("bogus"), "pending"),
        (PyValue::str("Dismissed"), "pending"),
        (PyValue::None, "pending"),
        (PyValue::str("dismissed"), "dismissed"),
    ] {
        let row = ConflictCandidate::from_dict(&raw(status)).expect("a row");
        assert_eq!(row.status, cps(want));
    }
    // `status` is read before `id`: its error comes first.
    let huge = PyValue::Int("1".repeat(4301));
    assert_eq!(
        ConflictCandidate::from_dict(&[(PyValue::str("status"), huge)]).map(|_| ()),
        Err(ConflictError::Value(
            bettermemory_core::pyvalue::PyError::ValueError
        ))
    );
}

#[test]
fn a_surrogate_is_never_hashed_by_replacement() {
    let lone: PyStr = vec![u32::from(b'a'), 0xD800, u32::from(b'b')];
    assert_eq!(body_hash(&lone), Err(ConflictError::Encode));
    assert_eq!(
        body_hash(&cps("a\u{FFFD}b")).expect("a hash"),
        "05087813392efc16"
    );
    assert_eq!(pair_id(&lone, &cps("a")), Err(ConflictError::Encode));
    // The scan files the pair and then fails lifting it into a row.
    let ms = [mem("a", SUDO, U, &[]), {
        let mut m = mem("b", SUDO_POS, U, &[]);
        m.fields.id = lone.clone();
        m
    }];
    assert_eq!(
        find_conflict_candidates(&ms, None, &dt(U)),
        Err(ConflictError::Encode)
    );
}

#[test]
fn a_nan_threshold_admits_every_pair() {
    let ms = [mem("a", PORT_A, U, &[]), mem("b", "zzz qqq", U, &[])];
    let (cands, _, _) = find_dedup_with_skips(&ms, Some(f64::NAN)).expect("a scan");
    assert_eq!(cands.len(), 1);
    assert_eq!(cands[0].similarity, 0.0);
    let (cands, _, _) = find_dedup_with_skips(&ms, None).expect("a scan");
    assert!(cands.is_empty());
}
