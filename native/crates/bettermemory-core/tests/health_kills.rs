//! Cases a plausible wrong `health_summary`, `health_roots` or
//! `admission` fails, each with the reference's answer.

use bettermemory_core::admission::{admitted_nothing, CountedRecords, DriftScope, EVERY_RECORD};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_roots::{EstateProgress, LAST_CHECKED_CAP};
use bettermemory_core::health_summary::{health_section, health_summary, section_names};
use bettermemory_core::in_force::RecordView;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(items.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn chars(summary: &PyValue) -> Vec<String> {
    let PyValue::Dict(sc) = summary.get("section_chars").expect("section_chars") else {
        panic!("a dict");
    };
    sc.iter()
        .map(|(k, _)| match k {
            PyValue::Str(t) => String::from_utf8(t.iter().map(|&c| c as u8).collect()).unwrap(),
            _ => panic!(),
        })
        .collect()
}

#[test]
fn zero_false_and_blank_are_sections_none_and_empties_are_not() {
    let report = dict(vec![
        ("zero", PyValue::int(0)),
        ("f", PyValue::Bool(false)),
        ("blank", s("")),
        ("none", PyValue::None),
        ("l", PyValue::List(vec![])),
        ("d", PyValue::Dict(vec![])),
        ("t", PyValue::Tuple(vec![])),
    ]);
    let out = health_summary(&report).expect("a summary");
    assert_eq!(chars(&out), ["zero", "f", "blank", "t"]);
}

#[test]
fn scope_wins_over_memory_ids_and_both_take_three() {
    let rec = dict(vec![
        ("kind", s("k")),
        ("count", PyValue::int(1)),
        ("action", s("go")),
        (
            "memory_ids",
            PyValue::List(vec![s("a"), s("b"), s("c"), s("d")]),
        ),
        ("scope", s("p, q, r, s")),
    ]);
    let out = health_summary(&dict(vec![("recommendations", PyValue::List(vec![rec]))])).unwrap();
    assert_eq!(
        out.get("attention"),
        Some(&PyValue::List(vec![s("k 1: go [p, q, r]")]))
    );
}

#[test]
fn headline_keys_come_in_their_own_order() {
    let report = dict(vec![
        ("distinct_sessions", PyValue::int(2)),
        ("generated_at", s("g")),
    ]);
    let PyValue::Dict(ref out) = health_summary(&report).unwrap() else {
        panic!()
    };
    assert_eq!(out[0].0, s("generated_at"));
    assert_eq!(out[1].0, s("distinct_sessions"));
}

#[test]
fn section_strip_is_python_whitespace_and_duplicates_keep_first_place() {
    let report = dict(vec![("a", PyValue::int(1)), ("b", PyValue::int(2))]);
    let got = health_section(&report, &cps("\u{3000}b\u{85}, a ,b")).unwrap();
    assert_eq!(
        got,
        dict(vec![("b", PyValue::int(2)), ("a", PyValue::int(1))])
    );
    let err = health_section(&report, &cps("\u{200b}a")).unwrap_err();
    assert_eq!(err.name, "ValueError");
    assert_eq!(
        err.text,
        Some(cps(
            "health has no section '\\u200ba'; the sections are a, b"
        ))
    );
    let err = health_section(&report, &cps(" , ")).unwrap_err();
    assert_eq!(
        err.text,
        Some(cps("health has no section ' , '; the sections are a, b"))
    );
}

#[test]
fn headline_keys_are_not_sections() {
    let report = dict(vec![("window_days", PyValue::int(1))]);
    let err = health_section(&report, &cps("window_days")).unwrap_err();
    assert_eq!(
        err.text,
        Some(cps(
            "health has no section 'window_days'; the sections are "
        ))
    );
}

fn t(i: i64) -> DateTime {
    DateTime::from_unix_micros_utc(i * 1_000_000).unwrap()
}

fn roots(items: &[&str]) -> Vec<PyStr> {
    items.iter().map(|r| cps(r)).collect()
}

#[test]
fn retry_starts_at_itself_cursor_starts_after_itself() {
    let all = roots(&["c", "a", "b", "a"]);
    let mut p = EstateProgress {
        cursor: Some(cps("a")),
        ..EstateProgress::default()
    };
    assert_eq!(p.ordered(&all), roots(&["b", "c", "a"]));
    p.retry_root = Some(cps("a"));
    assert_eq!(p.ordered(&all), roots(&["a", "b", "c"]));
    p.retry_root = Some(cps("ab"));
    assert_eq!(p.ordered(&all), roots(&["b", "c", "a"]));
    p.cursor = Some(cps("ab"));
    assert_eq!(p.ordered(&all), roots(&["b", "c", "a"]));
    p.attempt(cps("c"), t(0));
    assert_eq!(p.retry_root, None);
    assert_eq!(p.ordered(&all), roots(&["a", "b", "c"]));
    assert!(p.caller_first);
}

#[test]
fn code_point_order_not_utf16() {
    let p = EstateProgress::default();
    let all = vec![vec![0x1F600], vec![0xFFFF]];
    assert_eq!(p.ordered(&all), vec![vec![0xFFFF], vec![0x1F600]]);
}

#[test]
fn reattempt_moves_to_end_so_the_oldest_is_evicted() {
    let mut p = EstateProgress::default();
    for i in 0..LAST_CHECKED_CAP as i64 {
        p.attempt(cps(&format!("r{i}")), t(i));
    }
    assert_eq!(p.checked.len(), LAST_CHECKED_CAP);
    p.attempt(cps("r0"), t(5000));
    assert_eq!(p.checked.len(), LAST_CHECKED_CAP);
    p.attempt(cps("new"), t(5001));
    assert_eq!(p.checked.len(), LAST_CHECKED_CAP);
    assert_eq!(p.checked_at(&cps("r0")), Some(&t(5000)));
    assert_eq!(p.checked_at(&cps("r1")), None);
    assert_eq!(p.checked.back().unwrap().0, cps("new"));
}

#[test]
fn admission_vocabulary() {
    assert_eq!(admitted_nothing("deferred"), "admitted nothing: deferred");
    let mut every = EVERY_RECORD;
    assert!(every.admits_id("anything"));
    let mut counted = CountedRecords::default();
    assert!(!counted.drift_counts("a"));
}

fn int(i: i64) -> PyValue {
    PyValue::int(i128::from(i))
}

/// The error class, or the ValueError text, or "ok".
fn outcome<T>(r: Result<T, bettermemory_core::health_summary::HealthError>) -> String {
    match r {
        Ok(_) => "ok".into(),
        Err(e) => match e.text {
            Some(t) if e.name == "ValueError" => {
                String::from_utf16_lossy(&t.iter().map(|&c| c as u16).collect::<Vec<_>>())
            }
            _ => e.name.to_string(),
        },
    }
}

#[test]
fn a_report_that_is_not_a_dict_is_iterated_as_the_reference_iterates_it() {
    // Each row: report, then section_names, health_summary,
    // health_section(report, "zz"), health_section(report, "a"), as the
    // reference answers under CPython 3.13.
    let rows: Vec<(PyValue, [&str; 4])> = vec![
        (
            PyValue::List(vec![s("a")]),
            [
                "ok",
                "TypeError",
                "health has no section 'zz'; the sections are a",
                "TypeError",
            ],
        ),
        (
            PyValue::List(vec![]),
            [
                "ok",
                "AttributeError",
                "health has no section 'zz'; the sections are ",
                "health has no section 'a'; the sections are ",
            ],
        ),
        (
            PyValue::Tuple(vec![]),
            [
                "ok",
                "AttributeError",
                "health has no section 'zz'; the sections are ",
                "health has no section 'a'; the sections are ",
            ],
        ),
        (
            s(""),
            [
                "ok",
                "AttributeError",
                "health has no section 'zz'; the sections are ",
                "health has no section 'a'; the sections are ",
            ],
        ),
        (
            PyValue::List(vec![int(0)]),
            ["ok", "AttributeError", "TypeError", "TypeError"],
        ),
        (
            PyValue::List(vec![int(5)]),
            ["ok", "IndexError", "TypeError", "TypeError"],
        ),
        (
            PyValue::List(vec![s("window_days")]),
            [
                "ok",
                "TypeError",
                "health has no section 'zz'; the sections are ",
                "health has no section 'a'; the sections are ",
            ],
        ),
        (
            s("ab"),
            [
                "ok",
                "TypeError",
                "health has no section 'zz'; the sections are a, b",
                "TypeError",
            ],
        ),
        (int(5), ["TypeError", "TypeError", "TypeError", "TypeError"]),
        (
            PyValue::None,
            ["TypeError", "TypeError", "TypeError", "TypeError"],
        ),
    ];
    for (report, want) in rows {
        let got = [
            outcome(section_names(&report)),
            outcome(health_summary(&report)),
            outcome(health_section(&report, &cps("zz"))),
            outcome(health_section(&report, &cps("a"))),
        ];
        assert_eq!(got, want.map(String::from), "{report:?}");
    }
    assert_eq!(section_names(&s("ab")).unwrap(), vec![s("a"), s("b")]);
    assert_eq!(
        section_names(&PyValue::List(vec![s("a")])).unwrap(),
        vec![s("a")]
    );
}

#[test]
fn a_repeated_key_answers_its_last_value() {
    // A Python dict holds one value per key, the last one set; a
    // `PyValue::Dict` with a repeated key reads the same way.
    let report = PyValue::Dict(vec![
        (s("window_days"), int(1)),
        (s("x"), int(1)),
        (s("window_days"), int(2)),
        (s("x"), int(22)),
    ]);
    let PyValue::Dict(ref out) = health_summary(&report).unwrap() else {
        panic!("a dict");
    };
    assert_eq!(out[0], (s("window_days"), int(2)));
    let PyValue::Dict(ref sec) = health_section(&report, &cps("x")).unwrap() else {
        panic!("a dict");
    };
    assert_eq!(*sec, vec![(s("x"), int(22))]);
}

#[test]
fn a_repeated_recommendation_key_answers_its_last_value() {
    // The attention lines read `recommendations` and each record's fields
    // through the same last-value rule as a Python dict.
    let rec = PyValue::Dict(vec![
        (s("kind"), s("old")),
        (s("count"), int(1)),
        (s("action"), s("a")),
        (s("kind"), s("new")),
    ]);
    let report = PyValue::Dict(vec![
        (s("recommendations"), PyValue::List(vec![])),
        (s("recommendations"), PyValue::List(vec![rec])),
    ]);
    let PyValue::Dict(ref out) = health_summary(&report).unwrap() else {
        panic!("a dict");
    };
    let attention = out
        .iter()
        .find(|(k, _)| *k == s("attention"))
        .map(|(_, v)| v.clone());
    assert_eq!(attention, Some(PyValue::List(vec![s("new 1: a")])));
}
