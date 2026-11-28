//! Cases a plausible wrong `why` or `_decides` fails: the folded
//! checkouts left out, merges not followed, every import question taken,
//! the rows reordered, a payload that is no dict kept, an unhashable value
//! read as absent (a first home of a record without a checkout among
//! them), the two sentences of a moved home swapped, the projects read
//! once for good, and the short circuits of the reference's `or`; and
//! two answers of this build's shape: the log read whole, and the sorted
//! checkouts `decides` asserts in a debug build.

mod common;

use std::collections::{BTreeMap, HashSet};

use bettermemory_core::membership::{decides, Error};
use bettermemory_core::membership_records::outcomes::Why;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::{cps, to_string};
use common::membership_core::Model;
use common::membership_why::{home, log_row, project, question, why_model, LOG_ROWS};

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn none() -> PyValue {
    PyValue::None
}

/// The projects of the kills: P002 merged into P001, P004 into P002, P003
/// a child of P001, P006 apart.
fn projects() -> Vec<PyValue> {
    vec![
        project("P001", "alpha", &none(), &none()),
        project("P002", "beta", &s("P001"), &none()),
        project("P003", "gamma", &none(), &s("P001")),
        project("P004", "delta", &s("P002"), &none()),
        project("P006", "zeta", &none(), &none()),
    ]
}

fn imports() -> Vec<PyValue> {
    let ev = |p: &str| PyValue::Dict(vec![(s("imported_project"), s(p))]);
    vec![
        question("Q1", &s("K1"), "first_sight", PyValue::Dict(Vec::new())),
        question("Q3", &none(), "import", ev("P004")),
        question("Q4", &none(), "import", ev("P006")),
        question("Q5", &none(), "import", s("P001")),
    ]
}

/// A model over `projects()`, the homes given and a log whose rows are
/// numbered from 1 in the order given.
fn over(homes: Vec<PyValue>, proposals: Vec<PyValue>, log: &[(&str, &str)]) -> Model {
    let rows = log
        .iter()
        .enumerate()
        .map(|(i, (k, p))| log_row(i as i64 + 1, k, p))
        .collect();
    with_rows(homes, proposals, rows)
}

fn with_rows(homes: Vec<PyValue>, proposals: Vec<PyValue>, rows: Vec<PyValue>) -> Model {
    let tables = BTreeMap::from([
        ("projects".to_string(), projects()),
        ("homes".to_string(), homes),
        ("proposals".to_string(), proposals),
        (LOG_ROWS.to_string(), rows),
    ]);
    why_model(tables, None)
}

fn r1(checkout: &str) -> Vec<PyValue> {
    vec![home("R1", "P002", &s(checkout), "file")]
}

fn why(m: &mut Model, rid: &str) -> Why {
    m.why(&cps(rid)).expect("an answer")
}

fn class(m: &mut Model, rid: &str) -> String {
    match m.why(&cps(rid)) {
        Err(Error::Raised(r)) => r.class.to_string(),
        Err(Error::Store(_)) => "store".into(),
        Ok(w) => panic!("answered {:?}", text(&w)),
    }
}

fn seqs(w: &Why) -> Vec<i64> {
    w.rows.iter().map(|r| r.seq).collect()
}

fn text(w: &Why) -> String {
    to_string(&w.text).expect("ASCII")
}

#[test]
fn the_folded_checkouts_are_read() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            ("checkout_retired", r#"{"checkout": "K0", "into": "K1"}"#),
            ("checkout_retired", r#"{"checkout": "K00", "into": "K0"}"#),
            ("checkout_moved", r#"{"checkout": "K00"}"#),
            (
                "home_put",
                r#"{"home": {"record_id": "R1", "checkout": "K00"}}"#,
            ),
            ("checkout_moved", r#"{"checkout": "K2"}"#),
        ],
    );
    let w = why(&mut m, "R1");
    assert_eq!(seqs(&w), vec![1, 2, 3, 4]);
    assert!(
        text(&w).contains("written in checkout K00 (retired into K1)"),
        "{}",
        text(&w)
    );
}

#[test]
fn a_question_at_a_folded_checkout_is_read() {
    let mut qs = imports();
    qs.push(question(
        "Q7",
        &s("K0"),
        "first_sight",
        PyValue::Dict(Vec::new()),
    ));
    let mut m = over(
        r1("K1"),
        qs,
        &[
            ("checkout_retired", r#"{"checkout": "K0", "into": "K1"}"#),
            ("proposal_answered", r#"{"proposal": "Q7"}"#),
        ],
    );
    assert_eq!(seqs(&why(&mut m, "R1")), vec![1, 2]);
}

#[test]
fn a_home_elsewhere_is_homed_now_at() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            ("checkout_retired", r#"{"checkout": "K0", "into": "K1"}"#),
            (
                "home_put",
                r#"{"home": {"record_id": "R1", "checkout": "K7"}}"#,
            ),
        ],
    );
    let w = why(&mut m, "R1");
    assert!(
        text(&w).contains("written in checkout K7 (homed now at K1)"),
        "{}",
        text(&w)
    );
}

#[test]
fn merges_are_followed_into_the_members() {
    // P004 is merged into P002, which is merged into P001: its canonical
    // project is the record's, so its rows and its import question count.
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            ("project_put", r#"{"project": {"id": "P004"}}"#),
            ("project_merged", r#"{"project": "P004", "into": "P002"}"#),
            ("proposal_answered", r#"{"proposal": "Q3"}"#),
            ("project_put", r#"{"project": {"id": "P003"}}"#),
            ("project_minted", r#"{"project": "P006"}"#),
        ],
    );
    let w = why(&mut m, "R1");
    assert_eq!(seqs(&w), vec![1, 2, 3]);
    assert_eq!(w.project, Some(cps("P001")));
}

#[test]
fn a_chain_parent_counts_and_its_members_too() {
    // R3 is homed in P003, a child of P001: P001 is on its chain, and P002
    // and P004 stand for P001.
    let homes = vec![home("R3", "P003", &s("K3"), "repo")];
    let mut m = over(
        homes,
        imports(),
        &[
            ("project_minted", r#"{"project": "P001"}"#),
            ("project_file_bound", r#"{"project": "P004"}"#),
            ("project_minted", r#"{"project": "P006"}"#),
        ],
    );
    assert_eq!(seqs(&why(&mut m, "R3")), vec![1, 2]);
}

#[test]
fn only_the_import_questions_about_the_project_count() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            ("proposal_answered", r#"{"proposal": "Q3"}"#),
            ("proposal_answered", r#"{"proposal": "Q4"}"#),
            ("proposal_answered", r#"{"proposal": "Q5"}"#),
            ("proposal_opened", r#"{"proposal": "Q1"}"#),
        ],
    );
    assert_eq!(seqs(&why(&mut m, "R1")), vec![1, 4]);
}

#[test]
fn the_rows_keep_the_order_the_log_answers() {
    let rows = vec![
        log_row(9, "checkout_moved", r#"{"checkout": "K1"}"#),
        log_row(
            2,
            "checkout_registered",
            r#"{"checkout": "K1", "rule": "a"}"#,
        ),
        log_row(5, "project_minted", r#"{"project": "P001"}"#),
        log_row(
            3,
            "checkout_registered",
            r#"{"checkout": "K1", "rule": "b"}"#,
        ),
    ];
    let mut m = with_rows(r1("K1"), imports(), rows);
    let w = why(&mut m, "R1");
    assert_eq!(seqs(&w), vec![9, 2, 5, 3]);
    assert!(text(&w).contains("joined by a;"), "{}", text(&w));
}

#[test]
fn a_payload_that_is_no_dict_is_never_kept() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            ("checkout_registered", r#"["K1"]"#),
            ("checkout_registered", r#"[{"checkout": "K1"}]"#),
            ("checkout_registered", r#""K1""#),
            ("checkout_registered", "null"),
            ("checkout_registered", "5"),
            ("checkout_registered", "{"),
            ("checkout_registered", ""),
        ],
    );
    let w = why(&mut m, "R1");
    assert!(w.rows.is_empty());
    assert!(
        text(&w).ends_with("written in checkout K1; rule file; 0 rows"),
        "{}",
        text(&w)
    );
}

#[test]
fn an_unhashable_value_in_a_set_is_type_error() {
    for (kind, payload) in [
        ("checkout_put", r#"{"checkout": {"id": ["K9"]}}"#),
        ("proposal_put", r#"{"proposal": {"id": {"a": 1}}}"#),
        ("project_put", r#"{"project": {"id": ["P001"]}}"#),
        ("project_file_put", r#"{"project_file": {"project": []}}"#),
        ("project_minted", r#"{"project": ["P001"]}"#),
        ("project_merged", r#"{"project": "PZ", "into": {}}"#),
        (
            "project_unmerged",
            r#"{"project": "PZ", "into": "PZ", "was_into": []}"#,
        ),
        (
            "checkout_moved",
            r#"{"checkout": "K9", "proposal": ["Q1"]}"#,
        ),
    ] {
        let mut m = over(r1("K1"), imports(), &[(kind, payload)]);
        assert_eq!(class(&mut m, "R1"), "TypeError", "{kind} {payload}");
    }
    let mut bad = imports();
    bad.push(question(
        "Q8",
        &none(),
        "import",
        PyValue::Dict(vec![(
            s("imported_project"),
            PyValue::List(vec![s("P001")]),
        )]),
    ));
    let mut m = over(r1("K1"), bad, &[]);
    assert_eq!(class(&mut m, "R1"), "TypeError");
}

#[test]
fn the_checkouts_list_compares_and_never_raises() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            (
                "checkout_view_put",
                r#"{"checkout_view": {"checkout": ["K1"]}}"#,
            ),
            (
                "checkout_view_put",
                r#"{"checkout_view": {"checkout": {"a": 1}}}"#,
            ),
            (
                "checkout_moved",
                r#"{"checkout": ["K1"], "proposal": "Q1"}"#,
            ),
        ],
    );
    assert_eq!(seqs(&why(&mut m, "R1")), vec![3]);
}

#[test]
fn the_or_stops_at_the_first_true() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            ("project_minted", r#"{"project": "P001", "into": ["x"]}"#),
            (
                "project_minted",
                r#"{"project": "PZ", "into": "PZ", "was_into": ["x"]}"#,
            ),
            (
                "checkout_moved",
                r#"{"checkout": "K1", "proposal": ["Q1"]}"#,
            ),
            (
                "checkout_put",
                r#"{"checkout": {"id": "K1"}, "proposal": ["Q1"]}"#,
            ),
        ],
    );
    assert_eq!(seqs(&why(&mut m, "R1")), vec![1, 3, 4]);
}

#[test]
fn the_kinds_read_what_they_read_and_no_more() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            // A project kind never reads the checkout.
            ("project_minted", r#"{"project": "P006", "checkout": "K1"}"#),
            // A mapping under another _put kind is never kept.
            (
                "machine_put",
                r#"{"machine": {"id": "K1"}, "checkout": "K1"}"#,
            ),
            // A _put kind whose body is no mapping reads the payload.
            ("checkout_put", r#"{"checkout": "K1"}"#),
            ("_put", r#"{"checkout": "K1"}"#),
            ("_put", r#"{"": {"id": "K1"}, "checkout": "K1"}"#),
            // The suppressions and witnesses are never kept.
            ("recall_suppressed", r#"{"checkout": "K1"}"#),
            ("witness_put", r#"{"witness": "w", "checkout": "K1"}"#),
            ("witness_released", r#"{"checkout": "K1"}"#),
            // import_homed reads the record only.
            ("import_homed", r#"{"record": "R2", "checkout": "K1"}"#),
            ("import_homed", r#"{"record": "R1"}"#),
            // was_into counts for project_unmerged only.
            (
                "project_absorbed",
                r#"{"project": "P006", "was_into": "P001"}"#,
            ),
            (
                "project_unmerged",
                r#"{"project": "P006", "was_into": "P001"}"#,
            ),
            // A repeated key: the last is read.
            ("checkout_moved", r#"{"checkout": "K1", "checkout": "K9"}"#),
            ("checkout_moved", r#"{"checkout": "K9", "checkout": "K1"}"#),
            // A checkout view by its checkout, a proposal by its id or a
            // checkout's id.
            ("checkout_view_put", r#"{"checkout_view": {"id": "K1"}}"#),
            ("proposal_put", r#"{"proposal": {"id": "K1"}}"#),
            ("checkout_put", r#"{"checkout": {"id": "Q1"}}"#),
            (
                "home_put",
                r#"{"home": {"record_id": "R2", "checkout": "K1"}}"#,
            ),
        ],
    );
    assert_eq!(seqs(&why(&mut m, "R1")), vec![3, 4, 10, 12, 14, 16, 17]);
}

#[test]
fn the_first_registration_decides_even_when_false() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            ("checkout_registered", r#"{"checkout": "K1", "rule": 0}"#),
            (
                "checkout_registered",
                r#"{"checkout": "K1", "rule": "later"}"#,
            ),
        ],
    );
    let w = why(&mut m, "R1");
    assert!(!text(&w).contains("joined by"), "{}", text(&w));
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            (
                "checkout_registered",
                r#"{"checkout": "K2", "rule": "other", "proposal": "Q1"}"#,
            ),
            (
                "checkout_registered",
                r#"{"checkout": "K1", "rule": ["a", 1.5]}"#,
            ),
        ],
    );
    let w = why(&mut m, "R1");
    assert!(
        text(&w).contains("; the checkout joined by ['a', 1.5]; 2 rows"),
        "{}",
        text(&w)
    );
}

#[test]
fn a_record_without_a_checkout() {
    let homes = vec![home("R4", "P001", &none(), "file")];
    let mut m = over(
        homes,
        imports(),
        &[
            (
                "checkout_registered",
                r#"{"proposal": "Q3", "rule": "viaq"}"#,
            ),
            (
                "home_put",
                r#"{"home": {"record_id": "R4", "checkout": null}}"#,
            ),
            ("checkout_retired", r#"{"into": "K1"}"#),
        ],
    );
    let w = why(&mut m, "R4");
    assert_eq!(
        text(&w),
        "R4: project \"alpha\" (P001), no checkout recorded; rule file; \
         the checkout joined by viaq; 2 rows"
    );
    // No checkout, so the folded ones are not read: the row that would
    // raise KeyError there is never asked, and the log is read once.
    let calls = m.store().0.borrow().log.clone();
    let reads = calls
        .iter()
        .filter(|c| matches!(c, PyValue::List(v) if v.first() == Some(&s("log"))))
        .count();
    assert_eq!(reads, 1);
}

#[test]
fn the_projects_are_read_anew_each_call() {
    let mut m = over(
        r1("K1"),
        imports(),
        &[("project_minted", r#"{"project": "P006"}"#)],
    );
    assert!(why(&mut m, "R1").rows.is_empty());
    m.store().0.borrow_mut().tables.insert(
        "projects".into(),
        vec![
            project("P001", "alpha", &none(), &none()),
            project("P002", "beta", &s("P001"), &none()),
            project("P006", "zeta", &s("P001"), &none()),
        ],
    );
    assert_eq!(seqs(&why(&mut m, "R1")), vec![1]);
}

#[test]
fn a_home_put_kept_without_a_mapping_raises() {
    for (payload, want) in [
        (r#"{"checkout": "K1"}"#, "KeyError"),
        (r#"{"home": "R1", "checkout": "K1"}"#, "AttributeError"),
        (r#"{"home": null, "proposal": "Q1"}"#, "AttributeError"),
    ] {
        let mut m = over(r1("K1"), imports(), &[("home_put", payload)]);
        assert_eq!(class(&mut m, "R1"), want, "{payload}");
    }
    // A later bad home_put raises after a good one.
    let mut m = over(
        r1("K1"),
        imports(),
        &[
            (
                "home_put",
                r#"{"home": {"record_id": "R1", "checkout": "K1"}}"#,
            ),
            ("home_put", r#"{"home": 5, "checkout": "K1"}"#),
        ],
    );
    assert_eq!(class(&mut m, "R1"), "AttributeError");
    // A global record reads no home_put.
    let homes = vec![home("R2", "global", &s("K1"), "outside")];
    let mut m = over(homes, imports(), &[("home_put", r#"{"checkout": "K1"}"#)]);
    let w = why(&mut m, "R2");
    assert_eq!(seqs(&w), vec![1]);
    assert!(text(&w).starts_with("R2: global (written outside"));
}

#[test]
fn an_unhashable_first_home_is_type_error_and_an_empty_one_reads_as_the_checkout() {
    let first = |c: &str| format!(r#"{{"home": {{"record_id": "R1", "checkout": {c}}}}}"#);
    for (c, want) in [
        ("[\"K9\"]", Err("TypeError")),
        ("{}", Ok("written in checkout K1;")),
    ] {
        let payload = first(c);
        let mut m = over(r1("K1"), imports(), &[("home_put", payload.as_str())]);
        match want {
            Err(cls) => assert_eq!(class(&mut m, "R1"), cls),
            Ok(said) => assert!(text(&why(&mut m, "R1")).contains(said)),
        }
    }
}

#[test]
fn a_home_without_a_checkout_hashes_its_first_home_too() {
    // No checkout, so the folded checkouts are the empty set, and
    // `at in folded` still hashes: a list or a dict raises TypeError, a
    // str is homed now at None.
    let first = |c: &str| format!(r#"{{"home": {{"record_id": "R1", "checkout": {c}}}}}"#);
    for (c, want) in [
        ("[\"K9\"]", Err("TypeError")),
        ("{\"a\": 1}", Err("TypeError")),
        (
            "\"K9\"",
            Ok(
                "R1: project \"alpha\" (P001), written in checkout K9 (homed now at None); \
                rule file; 1 rows",
            ),
        ),
    ] {
        let homes = vec![home("R1", "P001", &none(), "file")];
        let payload = first(c);
        let mut m = over(homes, imports(), &[("home_put", payload.as_str())]);
        match want {
            Err(cls) => assert_eq!(class(&mut m, "R1"), cls, "{c}"),
            Ok(said) => assert_eq!(text(&why(&mut m, "R1")), said),
        }
    }
}

#[test]
fn a_project_merged_into_global_is_global() {
    let mut ps = projects();
    ps.push(project("P008", "toglobal", &s("global"), &none()));
    let tables = BTreeMap::from([
        ("projects".to_string(), ps),
        (
            "homes".to_string(),
            vec![home("R6", "P008", &s("K1"), "file")],
        ),
        ("proposals".to_string(), Vec::new()),
        (
            LOG_ROWS.to_string(),
            vec![log_row(
                1,
                "project_merged",
                r#"{"project": "P008", "into": "global"}"#,
            )],
        ),
    ]);
    let mut m = why_model(tables, None);
    let w = why(&mut m, "R6");
    assert_eq!(w.project, Some(cps("global")));
    assert_eq!(seqs(&w), vec![1]);
}

#[test]
fn a_missing_project_and_a_missing_home() {
    let homes = vec![home("R5", "PZ", &s("K1"), "file")];
    let mut m = over(homes, imports(), &[]);
    match m.why(&cps("R5")) {
        Err(Error::Raised(r)) => assert_eq!((r.class, r.message.as_str()), ("KeyError", "'PZ'")),
        other => panic!("{other:?}"),
    }
    let w = why(&mut m, "R0");
    assert_eq!(text(&w), "R0: no home recorded");
    assert_eq!((w.project, w.checkout, w.rule), (None, None, None));
}

#[test]
fn a_home_checkout_that_is_no_str_declines() {
    let homes = vec![home("R1", "P001", &PyValue::int(5), "file")];
    let mut m = over(homes, imports(), &[]);
    assert_eq!(class(&mut m, "R1"), "NotPorted");
}

#[test]
fn a_failed_log_read_comes_before_a_row_that_raises() {
    // `MembershipLog` answers the log whole, so a read that fails (as the
    // store's does at a row it cannot read) raises the store's error even
    // where an earlier row raises TypeError in `_decides`. The reference
    // reads the store's generator and raises TypeError first; the two
    // agree once the store's `MembershipLog` yields its rows lazily.
    let log = [(
        "checkout_moved",
        r#"{"checkout": "KX", "proposal": ["Q1"]}"#,
    )];
    let mut m = over(r1("K1"), imports(), &log);
    assert_eq!(class(&mut m, "R1"), "TypeError");
    let rows = vec![log_row(1, log[0].0, log[0].1)];
    let tables = BTreeMap::from([
        ("projects".to_string(), projects()),
        ("homes".to_string(), r1("K1")),
        ("proposals".to_string(), imports()),
        (LOG_ROWS.to_string(), rows),
    ]);
    // The folded checkouts' read is the first, the log's own the second.
    let mut m = why_model(tables, Some(("log".to_string(), 2)));
    assert_eq!(class(&mut m, "R1"), "store");
}

#[test]
fn decides_alone() {
    let set = |v: &[&str]| v.iter().map(|x| cps(x)).collect::<HashSet<PyStr>>();
    let checkouts = vec![cps("K0"), cps("K1")];
    let (proposals, projects) = (set(&["Q1"]), set(&["P001"]));
    let ask = |kind: &str, payload: &str| -> Result<bool, Error<()>> {
        let p = bettermemory_core::json::loads(&cps(payload)).expect("JSON");
        decides(kind, &p, &cps("R1"), &checkouts, &proposals, &projects)
    };
    assert_eq!(
        ask("home_put", r#"{"home": {"record_id": "R1"}}"#),
        Ok(true)
    );
    assert_eq!(
        ask(
            "home_put",
            r#"{"home": {"record_id": "R2"}, "checkout": "K1"}"#
        ),
        Ok(false)
    );
    assert_eq!(ask("x_put", r#"{"checkout": "K0"}"#), Ok(true));
    assert_eq!(ask("x_put", r#"{"x": {}, "checkout": "K0"}"#), Ok(false));
    assert_eq!(ask("project_reparented", r#"{"into": "P001"}"#), Ok(true));
    assert_eq!(
        ask("proposal_put", r#"{"proposal": {"id": "K0"}}"#),
        Ok(true)
    );
    assert!(ask("proposal_put", r#"{"proposal": {"id": ["K0"]}}"#).is_err());
    assert_eq!(ask("whatever", r#"{"proposal": null}"#), Ok(false));
}

#[cfg(debug_assertions)]
#[test]
#[should_panic(expected = "not sorted in code point order")]
fn decides_asserts_its_checkouts_are_sorted() {
    let checkouts = vec![cps("K1"), cps("K0")];
    let p = bettermemory_core::json::loads(&cps(r#"{"checkout": "K0"}"#)).expect("JSON");
    let _ = decides::<()>(
        "checkout_moved",
        &p,
        &cps("R1"),
        &checkouts,
        &HashSet::new(),
        &HashSet::new(),
    );
}
