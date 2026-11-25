//! Cases a plausible wrong port of `history`'s renderers fails. Every
//! answer is compared whole, as the reference writes it (read off
//! `history.render_text`, `render_json`, `_actor_text` and `_value_text`
//! under CPython 3.13.13).

use bettermemory_core::history_record::RecordHistory;
use bettermemory_core::history_render::{
    actor_text, render_json, render_text, value_text, RenderError,
};
use bettermemory_core::history_rows::HistoryRow;
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn dict(items: &[(&str, PyValue)]) -> PyValue {
    PyValue::Dict(items.iter().map(|(k, v)| (s(k), v.clone())).collect())
}

fn row(seq: i64) -> HistoryRow {
    HistoryRow {
        seq,
        ts: "t".to_owned(),
        session: None,
        kind: "memory_put".to_owned(),
        role: "record",
        action: "created".to_owned(),
        verified: Some(true),
        actor: None,
        actor_source: None,
        changed: Vec::new(),
        detail: Vec::new(),
        diff: None,
    }
}

fn hist(rows: Vec<HistoryRow>) -> RecordHistory {
    RecordHistory {
        record_id: cps("R"),
        record_kind: Some("memory"),
        state: "active",
        provenance: None,
        key_present: true,
        rows,
    }
}

fn text(h: &RecordHistory, diff: bool) -> String {
    let got = render_text(h, diff).expect("rendered");
    String::from_iter(
        got.iter()
            .map(|&c| char::from_u32(c).expect("no surrogate")),
    )
}

fn json(h: &RecordHistory, diff: bool) -> String {
    let got = render_json(h, diff).expect("rendered");
    String::from_iter(got.iter().map(|&c| char::from_u32(c).expect("ascii")))
}

fn header(n: &str) -> String {
    format!("bettermemory memory history R: memory, active, {n}\n")
}

const ROW: &str = "  seq 1 t memory_put created [mac ok]\n";

#[test]
fn row_and_rows_at_zero_one_and_two() {
    assert_eq!(text(&hist(vec![]), false), header("0 rows"));
    assert_eq!(text(&hist(vec![row(1)]), false), header("1 row") + ROW);
    assert_eq!(
        text(&hist(vec![row(1), row(1)]), false),
        header("2 rows") + ROW + ROW
    );
}

#[test]
fn the_record_word_stands_for_a_missing_or_empty_kind() {
    for kind in [None, Some("")] {
        let mut h = hist(vec![]);
        h.record_kind = kind;
        assert_eq!(
            text(&h, false),
            "bettermemory memory history R: record, active, 0 rows\n"
        );
    }
}

#[test]
fn the_unverified_note_is_for_an_active_record_without_the_key() {
    let key_line = "  key not held here: row MACs cannot be checked\n";
    let mut h = hist(vec![]);
    h.provenance = Some("local".to_owned());
    h.key_present = false;
    assert_eq!(
        text(&h, false),
        header("0 rows") + "  provenance local (unverified: no key)\n" + key_line
    );
    h.key_present = true;
    assert_eq!(text(&h, false), header("0 rows") + "  provenance local\n");
    // A tombstone's `unaccounted` rests on the log's shape, not the key.
    h.state = "tombstoned";
    h.provenance = Some("unaccounted".to_owned());
    h.key_present = false;
    assert_eq!(
        text(&h, false),
        "bettermemory memory history R: memory, tombstoned, 0 rows\n  provenance \
         unaccounted\n"
            .to_owned()
            + key_line
            + "  findings\n    the tombstoned record reads unaccounted: no log row this \
               store signed put it as it stands\n"
    );
}

#[test]
fn author_for_a_record_actor_and_actor_otherwise() {
    let actor = dict(&[("client", s("c"))]);
    let mut rows = Vec::new();
    for source in [Some("record"), Some("event"), None, Some("Record")] {
        let mut r = row(1);
        r.actor = Some(actor.clone());
        r.actor_source = source;
        rows.push(r);
    }
    let line = |label: &str| format!("  seq 1 t memory_put created [mac ok] {label} client=c\n");
    assert_eq!(
        text(&hist(rows), false),
        header("4 rows") + &line("author") + &line("actor") + &line("actor") + &line("actor")
    );
}

#[test]
fn an_empty_actor_mapping_is_undeclared_and_no_actor_prints_nothing() {
    let mut empty = row(1);
    empty.actor = Some(PyValue::Dict(Vec::new()));
    assert_eq!(
        text(&hist(vec![empty, row(1)]), false),
        header("2 rows") + "  seq 1 t memory_put created [mac ok] actor undeclared\n" + ROW
    );
}

#[test]
fn actor_text_keeps_its_order_and_skips_what_is_not_text() {
    let a = dict(&[
        ("principal", s("p")),
        ("extra", s("e")),
        ("model", s("")),
        ("client", s("c")),
    ]);
    assert_eq!(actor_text(&a), Ok(cps("client=c principal=p")));
    let a = dict(&[
        ("client", PyValue::int(1)),
        ("model", PyValue::None),
        ("principal", PyValue::List(vec![s("p")])),
    ]);
    assert_eq!(actor_text(&a), Ok(cps("undeclared")));
    // A name given twice is the last, as `json.loads` keeps it.
    let a = dict(&[("client", s("a")), ("client", s("b"))]);
    assert_eq!(actor_text(&a), Ok(cps("client=b")));
    let a = dict(&[("client", s("a")), ("client", PyValue::int(2))]);
    assert_eq!(actor_text(&a), Ok(cps("undeclared")));
    // A key that is not a `str` is no name, whatever its text:
    // `actor.get("client")` misses a path or a tuple.
    let a = PyValue::Dict(vec![
        (PyValue::Path(cps("client")), s("x")),
        (PyValue::Tuple(vec![s("model")]), s("y")),
    ]);
    assert_eq!(actor_text(&a), Ok(cps("undeclared")));
    assert_eq!(
        actor_text(&PyValue::List(vec![])),
        Err(RenderError::ActorNotMapping)
    );
    assert_eq!(RenderError::ActorNotMapping.name(), "AttributeError");
}

#[test]
fn the_cut_counts_code_points_at_eighty() {
    let grin = |n: usize| PyValue::Str(vec![0x1F600; n]);
    assert_eq!(value_text(&grin(80)), Ok(vec![0x1F600; 80]));
    let mut cut = vec![0x1F600; 77];
    cut.extend(cps("..."));
    assert_eq!(value_text(&grin(81)), Ok(cut));
    // Latin-1 at the cut: two bytes each in UTF-8, one code point.
    assert_eq!(
        value_text(&PyValue::Str(vec![0xE9; 80])),
        Ok(vec![0xE9; 80])
    );
    // A lone surrogate counts one.
    let mut text = cps(&"a".repeat(79));
    text.push(0xD800);
    assert_eq!(value_text(&PyValue::Str(text.clone())), Ok(text.clone()));
    text.push(0xDC00);
    let mut cut = cps(&"a".repeat(77));
    cut.extend(cps("..."));
    assert_eq!(value_text(&PyValue::Str(text)), Ok(cut));
    // A dumped value is cut too: 77 code points of its JSON text.
    let list = PyValue::List((0..40).map(PyValue::int).collect());
    assert_eq!(
        value_text(&list),
        Ok(cps(
            "[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21..."
        ))
    );
}

#[test]
fn value_text_sorts_keeps_non_ascii_and_spaces_its_separators() {
    let d = dict(&[
        ("b", PyValue::int(1)),
        ("a", PyValue::List(vec![s("\u{e9}"), PyValue::Bool(true)])),
    ]);
    assert_eq!(
        value_text(&d),
        Ok(cps("{\"a\": [\"\u{e9}\", true], \"b\": 1}"))
    );
    assert_eq!(value_text(&PyValue::None), Ok(cps("null")));
    assert_eq!(value_text(&PyValue::Float(f64::NAN)), Ok(cps("NaN")));
    assert_eq!(value_text(&PyValue::Float(1e16)), Ok(cps("1e+16")));
    assert_eq!(value_text(&s("a\nb")), Ok(cps("a\nb")));
    assert_eq!(
        value_text(&PyValue::Dict(vec![
            (PyValue::int(1), s("a")),
            (s("b"), s("c"))
        ])),
        Err(PyError::TypeError)
    );
}

#[test]
fn render_json_keeps_insertion_order_and_escapes_non_ascii() {
    let mut r = row(1);
    r.detail = vec![(
        s("d"),
        dict(&[("b", PyValue::int(1)), ("a", s("\u{e9}\u{1F600}"))]),
    )];
    let h = hist(vec![r]);
    assert_eq!(
        text(&h, false),
        header("1 row")
            + "  seq 1 t memory_put created [mac ok] d={\"a\": \"\u{e9}\u{1F600}\", \"b\": 1}\n"
    );
    let want = r#"{
  "format": "bettermemory-history",
  "version": 1,
  "id": "R",
  "record_kind": "memory",
  "state": "active",
  "provenance": null,
  "key_present": true,
  "findings": [],
  "rows": [
    {
      "seq": 1,
      "ts": "t",
      "session": null,
      "kind": "memory_put",
      "role": "record",
      "action": "created",
      "verified": true,
      "actor": null,
      "actor_source": null,
      "changed": [],
      "detail": {
        "d": {
          "b": 1,
          "a": "\u00e9\ud83d\ude00"
        }
      }
    }
  ]
}
"#;
    assert_eq!(json(&h, false), want);
    let with_diff = want.replace(
        "        }\n      }\n    }",
        "        }\n      },\n      \"diff\": null\n    }",
    );
    assert_eq!(json(&h, true), with_diff);
}

#[test]
fn printable_runs_on_each_line_after_the_split() {
    let mut r = row(1);
    r.kind = "k\n  seq 2 t k a [mac ok]".to_owned();
    r.detail = vec![(s("reason"), s("x\n  findings\u{1b}[2J\u{202e}"))];
    r.diff = Some(cps("a\u{1b}\nb"));
    let mut h = hist(vec![r]);
    h.provenance = Some("local\n  key\u{2028}".to_owned());
    h.record_id = cps("R\u{85}");
    let want = "bettermemory memory history R\\x85: memory, active, 1 row\n\
                \x20 provenance local\\n  key\\u2028\n\
                \x20 seq 1 t k\\n  seq 2 t k a [mac ok] created [mac ok] \
                reason=x\\n  findings\\x1b[2J\\u202e\n";
    assert_eq!(text(&h, false), want);
    assert_eq!(text(&h, true), want.to_owned() + "      a\\x1b\n      b\n");
}

#[test]
fn the_diff_s_last_empty_piece_is_a_line_and_an_empty_diff_none() {
    let with = |diff: &str| {
        let mut r = row(1);
        r.diff = Some(cps(diff));
        hist(vec![r])
    };
    assert_eq!(
        text(&with("a\n"), true),
        header("1 row") + ROW + "      a\n      \n"
    );
    assert_eq!(text(&with("a\n"), false), header("1 row") + ROW);
    assert_eq!(text(&with(""), true), header("1 row") + ROW);
    assert_eq!(
        text(&with("\n"), true),
        header("1 row") + ROW + "      \n      \n"
    );
    assert_eq!(
        text(&with("  \nb"), true),
        header("1 row") + ROW + "        \n      b\n"
    );
}

#[test]
fn changed_is_joined_by_commas_and_detail_follows_in_order() {
    let mut r = row(1);
    r.changed = vec![cps("body"), cps("scope"), cps("")];
    r.detail = vec![
        (s("z"), PyValue::int(1)),
        (PyValue::int(1), s("one")),
        (s("a"), PyValue::Bool(false)),
    ];
    r.verified = None;
    r.actor = Some(dict(&[("model", s("m"))]));
    r.actor_source = Some("event");
    assert_eq!(
        text(&hist(vec![r]), false),
        header("1 row")
            + "  seq 1 t memory_put created [mac unverifiable] actor model=m changed \
               body,scope, z=1 1=one a=false\n"
    );
}

#[test]
fn the_findings_block_is_there_only_with_findings() {
    let mut bad = row(3);
    bad.verified = Some(false);
    let h = hist(vec![row(1), bad.clone()]);
    assert_eq!(
        text(&h, false),
        header("2 rows")
            + ROW
            + "  seq 3 t memory_put created [MAC FAILS]\n  findings\n    seq 3 (memory_put) \
               fails its MAC\n"
    );
    let mut h = hist(vec![bad]);
    h.state = "missing";
    h.provenance = Some("unaccounted".to_owned());
    assert_eq!(
        text(&h, false),
        "bettermemory memory history R: memory, missing, 1 row\n  provenance unaccounted\n  \
         seq 3 t memory_put created [MAC FAILS]\n  findings\n    seq 3 (memory_put) fails \
         its MAC\n    the missing record reads unaccounted: no log row this store signed put \
         it as it stands\n    the log puts this record but no table holds it: it was deleted \
         outside the store\n"
    );
    assert_eq!(text(&hist(vec![row(1)]), false), header("1 row") + ROW);
}

#[test]
fn errors_are_the_reference_s_in_its_order() {
    let mut r = row(1);
    r.actor = Some(s("client=c"));
    r.detail = vec![(
        s("d"),
        PyValue::Dict(vec![(PyValue::int(1), s("a")), (s("b"), s("c"))]),
    )];
    let h = hist(vec![r.clone()]);
    // The actor is spelled before any detail.
    assert_eq!(render_text(&h, false), Err(RenderError::ActorNotMapping));
    // `render_json` does not sort, so mixed keys are written.
    let want = r#"{
  "format": "bettermemory-history",
  "version": 1,
  "id": "R",
  "record_kind": "memory",
  "state": "active",
  "provenance": null,
  "key_present": true,
  "findings": [],
  "rows": [
    {
      "seq": 1,
      "ts": "t",
      "session": null,
      "kind": "memory_put",
      "role": "record",
      "action": "created",
      "verified": true,
      "actor": "client=c",
      "actor_source": null,
      "changed": [],
      "detail": {
        "d": {
          "1": "a",
          "b": "c"
        }
      }
    }
  ]
}
"#;
    assert_eq!(json(&h, false), want);
    r.actor = None;
    let h = hist(vec![r]);
    assert_eq!(
        render_text(&h, false),
        Err(RenderError::Raised(PyError::TypeError))
    );
    // A detail name past 4,300 digits is ValueError before its value.
    let mut r = row(1);
    r.detail = vec![(PyValue::Int("9".repeat(4301)), PyValue::Float(f64::NAN))];
    assert_eq!(
        render_text(&hist(vec![r.clone()]), false),
        Err(RenderError::Raised(PyError::ValueError))
    );
    r.detail = vec![(PyValue::Int("9".repeat(4300)), s("v"))];
    let got: PyStr = render_text(&hist(vec![r]), false).expect("4,300 digits are spelled");
    let want = header("1 row") + ROW.trim_end_matches('\n') + " " + &"9".repeat(4300) + "=v\n";
    assert_eq!(got, cps(&want));
}

fn with_detail(name: PyValue, value: PyValue) -> RecordHistory {
    let mut r = row(1);
    r.detail = vec![(name, value)];
    hist(vec![r])
}

fn raised(h: &RecordHistory) -> Result<String, &'static str> {
    render_text(h, false)
        .map(|t| String::from_iter(t.iter().map(|&c| char::from_u32(c).expect("no surrogate"))))
        .map_err(RenderError::name)
}

/// An `int` of 4,301 digits: `str()` of it is ValueError.
fn past_the_digits() -> PyValue {
    PyValue::Int(format!("1{}", "0".repeat(4300)))
}

/// `n` tuples of one item around 0, as `(((0,),),)`.
fn tuples(n: usize) -> PyValue {
    let mut value = PyValue::int(0);
    for _ in 0..n {
        value = PyValue::Tuple(vec![value]);
    }
    value
}

fn mixed_keys() -> PyValue {
    PyValue::Dict(vec![(PyValue::int(1), s("a")), (s("b"), PyValue::int(2))])
}

#[test]
fn a_detail_name_is_spelled_before_its_value() {
    // The name raises ValueError, the value would raise TypeError.
    assert_eq!(
        raised(&with_detail(past_the_digits(), mixed_keys())),
        Err("ValueError")
    );
    // The name raises RecursionError, the value would raise TypeError.
    assert_eq!(
        raised(&with_detail(tuples(10_000), mixed_keys())),
        Err("RecursionError")
    );
}

#[test]
fn a_tuple_name_s_items_are_spelled_first_to_last() {
    let pair = |a: PyValue, b: PyValue| with_detail(PyValue::Tuple(vec![a, b]), s("v"));
    assert_eq!(
        raised(&pair(past_the_digits(), tuples(10_000))),
        Err("ValueError")
    );
    assert_eq!(
        raised(&pair(tuples(10_000), past_the_digits())),
        Err("RecursionError")
    );
}

#[test]
fn a_negative_name_s_sign_is_not_a_digit() {
    let name = PyValue::Int(format!("-{}", "9".repeat(4300)));
    let want = header("1 row") + ROW.trim_end_matches('\n') + " -" + &"9".repeat(4300) + "=v\n";
    assert_eq!(raised(&with_detail(name, s("v"))), Ok(want));
    let name = PyValue::Int(format!("-{}", "9".repeat(4301)));
    assert_eq!(raised(&with_detail(name, s("v"))), Err("ValueError"));
}

#[test]
fn an_actor_of_none_is_no_actor_in_both_renderers() {
    // The reference's row holds a mapping or None; `to_dict` writes
    // `Some(PyValue::None)` as `null`, so the text reads it as None too.
    let mut none = row(1);
    none.actor = Some(PyValue::None);
    none.actor_source = Some("record");
    let mut absent = none.clone();
    absent.actor = None;
    assert_eq!(
        text(&hist(vec![none.clone()]), false),
        header("1 row") + ROW
    );
    assert_eq!(text(&hist(vec![none.clone()]), true), header("1 row") + ROW);
    for diff in [false, true] {
        assert_eq!(
            json(&hist(vec![none.clone()]), diff),
            json(&hist(vec![absent.clone()]), diff)
        );
    }
    assert!(json(&hist(vec![none]), false).contains("      \"actor\": null,\n"));
    // `_actor_text(None)` itself is the AttributeError of `None.get`.
    assert_eq!(
        actor_text(&PyValue::None),
        Err(RenderError::ActorNotMapping)
    );
}
