//! Cases a plausible wrong `response_render` fails, each with what the
//! reference (`_response.py`) answers.

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Memory;
use bettermemory_core::response_render::{
    compact_origin, grouped_links, path_states, render_hit, render_show_sep, ClaimRejected, Fault,
    ParsedClaim, PathDriftLists, NATIVE_SEP,
};
use bettermemory_core::unicode::cps;

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn dict(items: &[(&str, PyValue)]) -> Vec<(PyValue, PyValue)> {
    items.iter().map(|(k, v)| (s(k), v.clone())).collect()
}

fn keys(row: &[(PyValue, PyValue)]) -> Vec<String> {
    row.iter()
        .map(|(k, _)| match k {
            PyValue::Str(t) => {
                String::from_utf16_lossy(&t.iter().map(|&c| c as u16).collect::<Vec<_>>())
            }
            _ => panic!("a str key"),
        })
        .collect()
}

fn hit(extra: &[(&str, PyValue)]) -> Vec<(PyValue, PyValue)> {
    let mut r = dict(&[
        ("id", s("a")),
        ("snippet", s("s")),
        ("relevance", PyValue::Float(0.5)),
        ("staleness_verdict", s("fresh")),
    ]);
    r.extend(dict(extra));
    r
}

fn memory(f: &[(&str, PyValue)]) -> Memory {
    let mut m = dict(&[
        ("id", s("01ARZ3NDEKTSV4RRFFQ69G5FAV")),
        ("created", s("2026-01-02T03:04:05Z")),
        ("updated", s("2026-01-02T03:04:05Z")),
        ("scopes", PyValue::List(vec![s("global")])),
        ("confidence", s("medium")),
        ("source", s("explicit-statement")),
        ("body", s("b")),
    ]);
    m.extend(dict(f));
    Memory::from_payload(&PyValue::Dict(m)).unwrap()
}

fn no_claims(_: &[u32]) -> ParsedClaim {
    Err(ClaimRejected)
}

fn ps(m: &Memory, d: &PathDriftLists, sep: Option<char>) -> Vec<(String, String)> {
    let (paths, _) = path_states(m, d, &mut no_claims, sep).unwrap();
    paths
        .into_iter()
        .map(|(p, f)| (text(&p), text(&f)))
        .collect()
}

fn text(t: &PyStr) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn strs(v: &[&str]) -> Vec<PyStr> {
    v.iter().map(|t| cps(t)).collect()
}

#[test]
fn a_hit_without_a_trust_row_says_provenance_null() {
    // `row.get("provenance")` is None: not "local", so it is shown.
    let out = render_hit(&hit(&[])).unwrap();
    assert_eq!(keys(&out).last().unwrap(), "provenance");
    let out = render_hit(&hit(&[("provenance", s("local"))])).unwrap();
    assert!(!keys(&out).contains(&"provenance".to_owned()));
}

#[test]
fn a_present_contradicted_by_of_none_raises() {
    // `"_contradicted_by" in row` holds for None, and `list(None)` raises.
    let r = render_hit(&hit(&[("_contradicted_by", PyValue::None)]));
    assert_eq!(r, Err(Fault::TypeError));
    // A str is listed by code point.
    let out = render_hit(&hit(&[("_contradicted_by", s("xy"))])).unwrap();
    let c = out.iter().find(|(k, _)| *k == s("contradicts")).unwrap();
    assert_eq!(c.1, PyValue::List(vec![s("x"), s("y")]));
}

fn show_state(extra: &[(&str, PyValue)]) -> Vec<(PyValue, PyValue)> {
    let mut r = dict(&[
        ("id", s("a")),
        ("staleness_verdict", s("fresh")),
        ("use_token", s("t")),
        ("scopes", PyValue::List(vec![])),
        ("body", s("b")),
        ("created", s("c")),
        ("updated", s("c")),
        ("provenance", s("local")),
        ("verification", PyValue::Dict(vec![])),
    ]);
    r.extend(dict(extra));
    r
}

fn show(state: &[(PyValue, PyValue)], m: &Memory) -> Result<Vec<String>, Fault> {
    render_show_sep(state, m, &PathDriftLists::default(), &mut no_claims, None).map(|o| keys(&o))
}

#[test]
fn updated_is_compared_by_python_equality() {
    let m = memory(&[]);
    let k = show(
        &show_state(&[
            ("created", PyValue::int(1)),
            ("updated", PyValue::Float(1.0)),
        ]),
        &m,
    )
    .unwrap();
    assert!(!k.contains(&"updated".to_owned()));
    let k = show(
        &show_state(&[
            ("updated", PyValue::Bool(true)),
            ("created", PyValue::int(1)),
        ]),
        &m,
    )
    .unwrap();
    assert!(!k.contains(&"updated".to_owned()));
}

#[test]
fn provenance_null_only_without_trust_unavailable() {
    let m = memory(&[]);
    let k = show(&show_state(&[("provenance", PyValue::None)]), &m).unwrap();
    assert!(k.contains(&"provenance".to_owned()));
    let k = show(
        &show_state(&[
            ("provenance", PyValue::None),
            ("trust_unavailable", PyValue::Bool(true)),
        ]),
        &m,
    )
    .unwrap();
    assert!(!k.contains(&"provenance".to_owned()));
}

#[test]
fn a_verification_that_is_not_a_dict_is_an_attribute_error() {
    let m = memory(&[]);
    for v in [PyValue::None, PyValue::List(vec![]), s("")] {
        assert_eq!(
            show(&show_state(&[("verification", v)]), &m),
            Err(Fault::AttributeError)
        );
    }
}

#[test]
fn corroborations_of_zero_leave_the_stamp_out() {
    let m = memory(&[("last_corroborated", s("2026-03-04T05:06:07Z"))]);
    let k = show(&show_state(&[]), &m).unwrap();
    assert!(!k.contains(&"last_corroborated".to_owned()));
    let m = memory(&[("corroborations", PyValue::int(1))]);
    let k = show(&show_state(&[]), &m).unwrap();
    assert!(k.ends_with(&["corroborations".to_owned(), "last_corroborated".to_owned()]));
}

#[test]
fn links_group_by_python_equality_keeping_the_first_spelling() {
    let e = |t: PyValue, id: &str| PyValue::Dict(dict(&[("type", t), ("s", s(id))]));
    let entries = PyValue::List(vec![
        e(PyValue::Bool(true), "1"),
        e(PyValue::int(1), "2"),
        e(PyValue::Float(1.0), "3"),
    ]);
    let g = grouped_links(&entries, "s").unwrap();
    let PyValue::List(groups) = &g else { panic!() };
    assert_eq!(groups.len(), 1);
    assert_eq!(groups[0].get("type"), Some(&PyValue::Bool(true)));
    // The key is hashed before the id is read: an unhashable type raises
    // TypeError though the entry has no id either.
    let bad = PyValue::List(vec![PyValue::Dict(dict(&[(
        "type",
        PyValue::List(vec![]),
    )]))]);
    assert_eq!(grouped_links(&bad, "s"), Err(Fault::TypeError));
}

#[test]
fn findings_are_kept_once_and_checked_adds_only_new_paths() {
    let d = PathDriftLists {
        missing: strs(&["/r/m", "/r/m"]),
        claim_anchored_missing: strs(&["/r/m"]),
        checked: strs(&["/r/m", "/r/c"]),
        ..Default::default()
    };
    assert_eq!(
        ps(&memory(&[]), &d, None),
        vec![
            ("/r/m".into(), "missing, attested or anchored".into()),
            ("/r/c".into(), "present".into())
        ]
    );
}

#[test]
fn the_unconfirmed_reasons_are_zipped_strictly() {
    let d = PathDriftLists {
        unconfirmed: strs(&["a", "b"]),
        unconfirmed_reasons: strs(&["r"]),
        ..Default::default()
    };
    let r = path_states(&memory(&[]), &d, &mut no_claims, None);
    assert_eq!(r, Err(Fault::ValueError));
}

#[test]
fn a_spelling_is_stripped_of_every_leading_dot_and_slash() {
    // `lstrip("./")` strips characters, not the prefix `./`.
    let d = PathDriftLists {
        checked: strs(&["/r/a"]),
        ..Default::default()
    };
    let m = memory(&[("verified_paths", PyValue::List(vec![s("..//.a")]))]);
    assert_eq!(ps(&m, &d, None), vec![("/r/a".into(), "present".into())]);
}

#[test]
fn a_backslash_is_a_separator_only_under_one() {
    let d = PathDriftLists {
        checked: strs(&["C:\\r\\b\\c"]),
        ..Default::default()
    };
    let m = memory(&[("verified_paths", PyValue::List(vec![s("b\\c")]))]);
    assert_eq!(ps(&m, &d, Some('\\')).len(), 1);
    assert_eq!(ps(&m, &d, None).len(), 2);
}

#[test]
fn the_native_separator_follows_the_target() {
    // `os.sep` is `\` on Windows and `/` on macOS and Linux.
    if cfg!(windows) {
        assert_eq!(NATIVE_SEP, Some('\\'));
    } else {
        assert_eq!(NATIVE_SEP, None);
    }
}

#[test]
fn a_cwd_equal_to_the_root_is_dropped_and_the_rest_kept() {
    let o = PyValue::Dict(dict(&[
        ("cwd", s("/w")),
        ("worktree_root", s("/w")),
        ("host", PyValue::None),
    ]));
    assert_eq!(
        compact_origin(Some(&o)),
        Some(dict(&[("worktree_root", s("/w"))]))
    );
    let o = PyValue::Dict(dict(&[("host", PyValue::None)]));
    assert_eq!(compact_origin(Some(&o)), None);
}
