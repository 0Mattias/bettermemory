//! Cases a plausible wrong port of `witness_questions` fails, each named
//! by the mistake it catches. The answers are the reference's, as the
//! golden file pins them.

#[allow(dead_code)]
mod witness;

use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::witness_questions as wq;
use serde_json::{json, Value};
use witness::{capture, checkout, cps, dumps, location};

const T1: &str = "01ARZ3NDEKTSV4RRFFQ69G5FAV";
const T2: &str = "01BX5ZZKBKACTAV9WEVGEMMVRY";

fn load(text: &str) -> PyValue {
    witness::load(&Value::String(text.into()))
}

fn views(text: &str) -> Result<Vec<(Vec<u32>, wq::AskedView)>, PyError> {
    wq::asked_views(&load(text))
}

#[test]
fn an_empty_status_is_failed_io_not_the_empty_text() {
    let a = load(&format!(
        r#"{{"remint": {{"token": "{T2}", "status": ""}}}}"#
    ));
    let got = wq::AnsweredToken::new(&a, true).call(&checkout(&json!({"token": T1})));
    assert_eq!(got, Ok(Some((Some(cps(T2)), cps("failed:io")))));
}

#[test]
fn a_falsy_remint_ok_after_posting_reminting_later() {
    for a in [r#"{"remint_ok": 0}"#, r#"{"remint_ok": []}"#, "{}"] {
        let got = wq::AnsweredToken::new(&load(a), true).call(&checkout(&json!({})));
        assert_eq!(got, Ok(None), "{a}");
    }
    // A truthy one, or none posted, asks.
    let got = wq::AnsweredToken::new(&load(r#"{"remint_ok": "no"}"#), true)
        .call(&checkout(&json!({})))
        .unwrap_err();
    assert_eq!(dumps(&got), r#"{"remint": true, "replacing": null}"#);
}

#[test]
fn located_compares_device_and_inode_not_birth() {
    let a = load(&format!(
        r#"{{"remint": {{"token": "{T2}", "status": "written"}}}}"#
    ));
    let row = json!({"machine": "m1", "token": T1, "written": "W", "key": ["7", "42", "9"]});
    let loc = location(&row);
    let mut model = |_: &[u32]| vec![(cps("m1"), witness::key(&json!(["7", "42", null])))];
    let got = wq::AnsweredToken::new(&a, true).located(&loc, &mut model);
    assert_eq!(got.token, Some(cps(T2)));
    assert_eq!(got.token_written, None);
    assert_eq!(got.token_status, cps("present"));
    // Another machine's checkout at the same object does not count.
    let mut other = |_: &[u32]| vec![(cps("m2"), witness::key(&json!(["7", "42", "9"])))];
    let got = wq::AnsweredToken::new(&a, true).located(&loc, &mut other);
    assert_eq!(got, loc);
}

#[test]
fn located_asks_the_model_only_past_the_early_returns() {
    let a = load(&format!(
        r#"{{"remint": {{"token": "{T2}", "status": "written"}}}}"#
    ));
    let row = json!({"machine": "m1", "token": T2, "written": null, "key": ["7", "42", "9"]});
    let mut asked = 0;
    let mut model = |_: &[u32]| {
        asked += 1;
        Vec::new()
    };
    wq::AnsweredToken::new(&a, true).located(&location(&row), &mut model);
    assert_eq!(asked, 0);
}

#[test]
fn an_empty_capture_token_falls_to_the_written_one() {
    let cap = capture(&json!({"git_dir": "/r/.git", "token": "", "written": T2,
        "key": ["7", "42", null], "kind": "primary"}));
    let mut seen = None;
    let mut stub = |_: &[u32], _: bool, _: bool, r: Option<_>| {
        seen = Some(r);
        (None, "skipped:policy".to_owned())
    };
    wq::remint_answer(&cap, &load("{}"), &mut stub).unwrap();
    let r: Option<bettermemory_core::checkout_token::Lineage> = seen.unwrap();
    assert_eq!(r.unwrap().token, cps(T2));
}

#[test]
fn a_null_replacing_names_nothing_though_the_capture_has_a_lineage() {
    let cap = capture(&json!({"git_dir": "/r/.git", "token": T1, "written": null,
        "key": ["7", "42", null], "kind": "bare"}));
    let mut seen = Vec::new();
    let mut stub = |_: &[u32], p: bool, b: bool, r: Option<_>| {
        seen.push((p, b, r));
        (None, "skipped:bare".to_owned())
    };
    let got = wq::remint_answer(&cap, &load(r#"{"replacing": null}"#), &mut stub).unwrap();
    assert_eq!(seen, vec![(false, true, None)]);
    assert_eq!(dumps(&got), r#"{"token": null, "status": "skipped:bare"}"#);
}

#[test]
fn refused_needs_the_question_to_ask_and_the_exact_status() {
    let exists = load(r#"{"remint": {"status": "exists"}}"#);
    assert!(wq::remint_refused(&load(r#"{"remint": [0]}"#), &exists));
    assert!(!wq::remint_refused(&load(r#"{"remint": []}"#), &exists));
    assert!(!wq::remint_refused(
        &load(r#"{"remint": true}"#),
        &load(r#"{"remint": {"status": "EXISTS"}}"#)
    ));
}

#[test]
fn stamps_take_the_first_eight_then_leave_the_malformed_out() {
    let mut list = vec![r#"["bad"]"#.to_owned()];
    list.extend((0..9).map(|i| format!(r#"["n{i}", {i}, 0, 0, 0]"#)));
    let q = format!(
        r#"{{"views": [{{"path": "/r", "stamps": [{}]}}]}}"#,
        list.join(", ")
    );
    let got = views(&q).unwrap();
    assert_eq!(got[0].1.stamps.len(), 7);
}

#[test]
fn a_tuple_of_stamps_is_no_list() {
    let s = PyValue::Tuple(vec![PyValue::List(vec![
        PyValue::str("n"),
        PyValue::int(1),
        PyValue::int(2),
        PyValue::int(3),
        PyValue::int(4),
    ])]);
    assert_eq!(wq::asked_stamps(Some(&s)), Ok(Vec::new()));
}

#[test]
fn an_infinite_stamp_field_raises_and_nan_is_left_out() {
    let inf = r#"{"views": [{"path": "/r", "stamps": [["n", Infinity, 2, 3, 4]]}]}"#;
    assert_eq!(views(inf), Err(PyError::OverflowError));
    let nan = r#"{"views": [{"path": "/r", "stamps": [["n", NaN, 2, 3, 4]]}]}"#;
    assert!(views(nan).unwrap()[0].1.stamps.is_empty());
}

#[test]
fn a_truthy_views_that_is_not_iterable_raises() {
    for q in [r#"{"views": 3}"#, r#"{"views": true}"#, r#"{"views": 0.5}"#] {
        assert_eq!(views(q), Err(PyError::TypeError), "{q}");
    }
    for q in [
        r#"{"views": 0}"#,
        r#"{"views": "abc"}"#,
        r#"{"views": {"a": 1}}"#,
    ] {
        assert_eq!(views(q), Ok(Vec::new()), "{q}");
    }
}

#[test]
fn a_malformed_key_raises_after_an_earlier_view_was_read() {
    let q = r#"{"views": [{"path": "/r"}, {"path": "/s", "object": [1, 2]}]}"#;
    assert_eq!(views(q), Err(PyError::ValueError));
}

#[test]
fn heads_are_lowered_and_only_ascii_hex_is_a_hash() {
    let head = |h: &str| wq::asked_head(Some(&PyValue::str(h)));
    let h40 = "0123456789ABCDEF0123456789abcdef01234567";
    assert_eq!(head(h40), Some(cps(&h40.to_lowercase())));
    assert_eq!(head(&format!("{}\u{130}", &h40[..39])), None);
    assert_eq!(head(&"a".repeat(52)), None);
    assert_eq!(head(&"a".repeat(64)), Some(cps(&"a".repeat(64))));
}

#[test]
fn kind_falls_to_primary_only_when_falsy_and_superseded_only_when_true() {
    let q = r#"{"views": [{"path": "/r", "kind": "", "superseded": 1},
        {"path": "/s", "kind": true, "superseded": true}]}"#;
    let got = views(q).unwrap();
    assert_eq!(
        (got[0].1.kind.clone(), got[0].1.superseded),
        (cps("primary"), false)
    );
    assert_eq!(
        (got[1].1.kind.clone(), got[1].1.superseded),
        (cps("True"), true)
    );
}

#[test]
fn liveness_reads_answers_by_their_str() {
    let live = wq::AnsweredLiveness::new(&load(r#"{"views": {"/r": [1, "a"]}}"#)).unwrap();
    let got = live.call(&checkout(&json!({})), &witness::view("/r"));
    assert_eq!(got, Ok(cps("[1, 'a']")));
}

#[test]
fn a_path_views_is_not_iterable() {
    // `for v in PurePosixPath("a")` raises TypeError; a path is truthy.
    let q = PyValue::Dict(vec![(PyValue::str("views"), PyValue::Path(cps("a")))]);
    assert_eq!(wq::asked_views(&q), Err(PyError::TypeError));
}

#[test]
fn a_git_dir_no_encoding_spells_still_reaches_the_policy_skip() {
    // The reference hands the str on; `checkout_token.remint` skips on the
    // policy before it spells the path: {token: None, status: skipped:policy}.
    let mut cap = capture(&json!({"git_dir": "/r/.git", "token": T1, "written": null,
        "key": null, "kind": "primary"}));
    let dir = vec![0x2f, 0xd800, 0x72];
    cap.git_dir = Some(dir.clone());
    let mut seen = Vec::new();
    let mut stub = |g: &[u32], p: bool, _: bool, _: Option<_>| {
        seen.push((g.to_vec(), p));
        (None, "skipped:policy".to_owned())
    };
    let got = wq::remint_answer(&cap, &load("{}"), &mut stub).unwrap();
    assert_eq!(seen, vec![(dir, false)]);
    assert_eq!(
        dumps(&got),
        r#"{"token": null, "status": "skipped:policy"}"#
    );
}

#[test]
fn a_liveness_answer_keeps_the_last_value_of_a_key() {
    // {str(k): str(v) for k, v in {1: "a", "1": "b"}.items()} == {"1": "b"}.
    let answers = PyValue::Dict(vec![(
        PyValue::str("views"),
        PyValue::Dict(vec![
            (PyValue::Int("1".into()), PyValue::str("a")),
            (PyValue::str("1"), PyValue::str("b")),
        ]),
    )]);
    let live = wq::AnsweredLiveness::new(&answers).unwrap();
    let got = live.call(&checkout(&json!({})), &witness::view("1"));
    assert_eq!(got, Ok(cps("b")));
}
