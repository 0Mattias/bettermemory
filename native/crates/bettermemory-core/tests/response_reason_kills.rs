//! Cases a plausible wrong `response_reason` or `response_trust` fails:
//! each pins one reading of the reference that a simpler port would get
//! wrong.

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::response_reason::{
    check_reason, claim_at_index, verdict_and_reason, with_objection, CheckReason, Raised,
};
use bettermemory_core::response_trust::{
    apply_trust, apply_trust_unavailable, attach_provenance, get, AttachError, Row, TrustRows,
};
use bettermemory_core::store::provenance::TrustRow;
use bettermemory_core::unicode;

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn list(xs: &[&str]) -> PyValue {
    PyValue::List(xs.iter().map(|x| s(x)).collect())
}

fn dict(pairs: Vec<(&str, PyValue)>) -> Row {
    pairs.into_iter().map(|(k, v)| (s(k), v)).collect()
}

fn reason(a: CheckReason) -> String {
    unicode::to_string(&check_reason(&a).unwrap().unwrap()).unwrap()
}

#[test]
fn three_paths_are_named_and_the_rest_counted() {
    let got = reason(CheckReason {
        missing: list(&["a", "b", "c", "d"]),
        ..Default::default()
    });
    assert_eq!(got, "Cited path missing: a, b, c and 1 more.");
    let got = reason(CheckReason {
        missing: list(&["a", "b", "c"]),
        ..Default::default()
    });
    assert_eq!(got, "Cited path missing: a, b, c.");
}

#[test]
fn anchored_paths_keep_the_missing_order_then_the_rest() {
    let got = reason(CheckReason {
        missing: list(&["p", "q", "r"]),
        anchored_missing: list(&["z", "r", "p"]),
        ..Default::default()
    });
    assert_eq!(
        got,
        "Attested or anchored path missing: p, r, z. Cited path missing: q."
    );
}

#[test]
fn a_bool_commit_count_is_spelled_true_and_singular() {
    let got = reason(CheckReason {
        commits: PyValue::Bool(true),
        ..Default::default()
    });
    assert_eq!(
        got,
        "True commit since the last verify touched the paths it cites."
    );
}

#[test]
fn a_string_is_named_by_its_characters() {
    let got = reason(CheckReason {
        contradicted_by: s("abcde"),
        ..Default::default()
    });
    assert!(got.starts_with("A recorded link says it contradicts a, b, c and 2 more: read it"));
}

#[test]
fn a_float_count_of_one_reads_once() {
    let entry = PyValue::Dict(vec![
        (s("count_in_window"), PyValue::Float(1.0)),
        (s("most_recent_ts"), s("2026-01-02T03:04:05Z")),
        (s("outcome"), s("ignored")),
    ]);
    let got = reason(CheckReason {
        rejections: PyValue::List(vec![entry]),
        ..Default::default()
    });
    assert_eq!(
        got,
        "A use marked it ignored on 2026-01-02. It clears once a later use applies it."
    );
}

#[test]
fn a_non_text_leg_note_raises_at_the_join() {
    let got = check_reason(&CheckReason {
        leg_note: PyValue::int(5),
        ..Default::default()
    });
    assert_eq!(got, Err(Raised::TypeError));
}

#[test]
fn a_float_commit_count_is_not_counted() {
    let row = dict(vec![
        ("staleness_verdict", s("stale")),
        (
            "commit_drift",
            PyValue::Dict(vec![(s("commits_since_verify"), PyValue::Float(2.0))]),
        ),
    ]);
    assert_eq!(verdict_and_reason(&row).unwrap(), (s("stale"), None));
}

#[test]
fn a_stamp_on_an_unaccounted_row_requires_a_check() {
    let row = dict(vec![
        ("staleness_verdict", s("fresh")),
        ("provenance", s("unaccounted")),
        (
            "verification",
            PyValue::Dict(vec![(s("status"), s("fresh"))]),
        ),
    ]);
    let (verdict, reason) = verdict_and_reason(&row).unwrap();
    assert_eq!(verdict, s("spot_check_required"));
    assert!(reason.is_some());
}

#[test]
fn an_objection_raises_only_fresh() {
    assert_eq!(with_objection(&s("stale"), true), s("stale"));
    assert_eq!(
        with_objection(&s("fresh"), true),
        s("spot_check_recommended")
    );
    assert_eq!(with_objection(&s("fresh"), false), s("fresh"));
}

#[test]
fn a_negative_index_counts_from_the_end() {
    let ev = dict(vec![("claim_excerpts", list(&["x", "y", "z"]))]);
    assert_eq!(claim_at_index(&ev, -1), Ok(Some(unicode::cps("z"))));
    assert_eq!(claim_at_index(&ev, -4), Err(Raised::IndexError));
    assert_eq!(claim_at_index(&ev, 3), Ok(None));
}

#[test]
fn a_local_stamp_lifts_the_synced_rule() {
    let mut row = dict(vec![("last_verified_at", s("2026-01-01T00:00:00Z"))]);
    apply_trust(&mut row, Some("synced"), Some("2026-01-02T00:00:00Z"), 30);
    assert_eq!(row.len(), 2);
    assert_eq!(get(&row, "provenance"), Some(&s("synced")));
    let mut row = dict(vec![("last_verified_at", s("2026-01-01T00:00:00Z"))]);
    apply_trust(&mut row, Some("synced"), None, 30);
    assert_eq!(
        get(&row, "staleness_verdict"),
        Some(&s("spot_check_required"))
    );
}

#[test]
fn an_unstamped_row_only_says_trust_is_unavailable() {
    let mut row = dict(vec![("staleness_verdict", s("never"))]);
    apply_trust_unavailable(&mut row, "R.");
    assert_eq!(get(&row, "staleness_verdict"), Some(&s("never")));
    assert_eq!(get(&row, "trust_unavailable"), Some(&PyValue::Bool(true)));
}

#[test]
fn a_dict_named_by_slice_misses_as_from_python_3_12() {
    // `{...}[:3]`: `KeyError` from 3.12 on, `TypeError` on 3.11. The port
    // follows 3.12 and later.
    let got = check_reason(&CheckReason {
        contradicted_by: PyValue::Dict(vec![(s("k"), PyValue::int(1))]),
        ..Default::default()
    });
    assert_eq!(got, Err(Raised::KeyError));
}

#[test]
fn a_row_reads_the_last_pair_of_a_key() {
    let row = dict(vec![
        ("staleness_verdict", s("stale")),
        ("staleness_verdict", s("fresh")),
    ]);
    let (verdict, _) = verdict_and_reason(&row).unwrap();
    assert_eq!(verdict, s("fresh"));
    assert_eq!(get(&row, "staleness_verdict"), Some(&s("fresh")));
}

struct Answer {
    rows: Vec<(String, &'static str)>,
    verifies: bool,
}

impl TrustRows for Answer {
    type Error = ();
    fn trust_rows(&self, _: &[String]) -> Result<Option<Vec<(String, TrustRow)>>, ()> {
        if !self.verifies {
            return Ok(None);
        }
        Ok(Some(
            self.rows
                .iter()
                .map(|(k, l)| {
                    let t = TrustRow {
                        label: (*l).into(),
                        verified_at: None,
                    };
                    (k.clone(), t)
                })
                .collect(),
        ))
    }
    fn verifies(&self) -> bool {
        self.verifies
    }
}

#[test]
fn a_duplicated_id_in_the_store_answer_takes_the_last_row() {
    let store = Answer {
        rows: vec![("m".into(), "local"), ("m".into(), "synced")],
        verifies: true,
    };
    let mut rows = vec![dict(vec![("id", s("m"))])];
    attach_provenance(&mut rows, &store, 30).unwrap();
    assert_eq!(get(&rows[0], "provenance"), Some(&s("synced")));
}

#[test]
fn a_surrogate_id_raises_only_when_the_store_holds_its_key() {
    let surrogate = PyValue::Str(vec![0xD800]);
    let fresh = || {
        vec![dict(vec![
            ("id", surrogate.clone()),
            ("last_verified_at", s("x")),
            ("staleness_verdict", s("fresh")),
        ])]
    };
    let keyless = Answer {
        rows: Vec::new(),
        verifies: false,
    };
    let mut rows = fresh();
    attach_provenance(&mut rows, &keyless, 30).unwrap();
    assert_eq!(
        get(&rows[0], "trust_unavailable"),
        Some(&PyValue::Bool(true))
    );
    assert_eq!(
        get(&rows[0], "staleness_verdict"),
        Some(&s("spot_check_required"))
    );
    let keyed = Answer {
        rows: Vec::new(),
        verifies: true,
    };
    let mut rows = fresh();
    assert_eq!(
        attach_provenance(&mut rows, &keyed, 30),
        Err(AttachError::Encode)
    );
    assert_eq!(rows, fresh());
}
