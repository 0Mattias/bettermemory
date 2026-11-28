//! Every row of `datetime_offsets.jsonl`, stated difference D1: texts whose
//! offset is zero hours, minutes and seconds with a non-zero fraction
//! after the seconds, read with the offset kept, as CPython 3.13.18 and
//! later read them (3.13.13 reads UTC and drops the fraction). A text row goes through each
//! reader as `datetime_edges.jsonl`'s rows do; a bound or window row
//! through `eval_model`'s `parse_window_bound` or `show_in_window` as
//! `eval_model_python.jsonl`'s rows do. The file is not gated: its
//! generator never asks the interpreter's parser, so it is the same under
//! every Python. Each row carries `"difference": "D1"` and none carries
//! `"reference"`: the reference's answer on these texts is not one value.

mod common;

use bettermemory_core::datetime::{self, DateError, DateTime};
use bettermemory_core::eval_model::{self as em, Bound, EvalModelError};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{neutral, text_field};
use serde_json::{json, Map, Value};

const NAME: &str = "datetime_offsets.jsonl";

fn parsed(name: &str, text: &[u32]) -> Value {
    let iso = |d: Option<DateTime>| json!({ "v": d.map(|d| d.isoformat()) });
    let err = |e: DateError| json!({ "error": e.name() });
    match name {
        "parse_event_ts" => iso(datetime::parse_event_ts(text)),
        "store_rows_dt" | "v8_as_dt" => match datetime::parse_record_dt(text) {
            Ok(d) => iso(Some(d)),
            Err(e) => err(e),
        },
        "git_instant" => iso(datetime::git_instant(text)),
        "git_seconds" => json!({ "v": datetime::git_seconds(text) }),
        "ulid_time" => match datetime::ulid_time(text) {
            Ok(d) => iso(d),
            Err(e) => err(e),
        },
        other => panic!("parser {other} is not ported"),
    }
}

fn fields(dt: &DateTime) -> Value {
    json!([
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.microsecond,
        dt.offset_us
    ])
}

fn opt_dt(v: &Value) -> Option<DateTime> {
    let f = v.as_array()?;
    let u = |i: usize| f[i].as_u64().expect("a field") as u32;
    Some(
        DateTime::new(
            f[0].as_i64().expect("year"),
            u(1),
            u(2),
            u(3),
            u(4),
            u(5),
            u(6),
            f[7].as_i64(),
        )
        .expect("a datetime"),
    )
}

/// An exception as `gen_eval_model._err` writes it (every message here is
/// under its 300-character digest bound).
fn err(e: &EvalModelError) -> Value {
    let text = e.message();
    assert!(text.len() <= 300, "a short message");
    let mut m = Map::new();
    m.insert("x".into(), json!(e.name()));
    match unicode::to_string(&text) {
        Some(t) => m.insert("msg".into(), json!(t)),
        None => m.insert("msg_cps".into(), json!(text)),
    };
    Value::Object(m)
}

fn answer<T>(r: Result<T, EvalModelError>, spell: impl FnOnce(T) -> Value) -> Value {
    match r {
        Ok(v) => json!({ "r": spell(v) }),
        Err(e) => err(&e),
    }
}

/// An eval_model row's answer keys.
fn want(row: &Value) -> Value {
    let keys = ["r", "x", "msg", "msg_cps"];
    let m: Map<String, Value> = keys
        .iter()
        .filter_map(|k| row.get(*k).map(|v| ((*k).to_owned(), v.clone())))
        .collect();
    Value::Object(m)
}

fn bound_json(b: Option<Bound>) -> Value {
    match b {
        None => Value::Null,
        Some(Bound::Span(us)) => json!({"span": us.to_string()}),
        Some(Bound::At(at)) => json!({"at": fields(&at)}),
    }
}

/// The port's answer and the row's, in the row's own answer keys, and
/// the row's kind.
fn check(row: &Value, n_parsers: usize) -> (&'static str, Value, Value) {
    if row.get("text").is_some() || row.get("text_cps").is_some() {
        let out = row["out"].as_object().expect("out");
        assert_eq!(out.len(), n_parsers);
        let text = text_field(row, "text");
        let got: Map<String, Value> = out
            .keys()
            .map(|name| (name.clone(), parsed(name, &text)))
            .collect();
        return ("text", Value::Object(got), row["out"].clone());
    }
    match row["k"].as_str().expect("a kind") {
        "bound" => {
            let now = opt_dt(&row["now"]).expect("now");
            let flag = row["flag"].as_str().expect("a flag");
            let spec = text_field(row, "spec");
            let got = answer(
                em::parse_window_bound(Some(spec.as_slice()), flag, &now),
                bound_json,
            );
            ("bound", got, want(row))
        }
        "window" => {
            let got = answer(
                em::show_in_window(
                    &neutral(&row["show"]),
                    opt_dt(&row["start"]).as_ref(),
                    opt_dt(&row["end"]).as_ref(),
                ),
                |b| json!(b),
            );
            ("window", got, want(row))
        }
        other => panic!("unknown kind {other}"),
    }
}

#[test]
fn datetime_offsets_every_row() {
    let lines = common::jsonl(NAME);
    let header = &lines[0];
    assert_eq!(header["fixture"], "datetime_offsets");
    let rows = &lines[1..];
    assert_eq!(
        header["cases"].as_u64().expect("cases") as usize,
        rows.len()
    );
    let stated = header["stated_differences"]
        .as_object()
        .expect("stated_differences");
    assert!(stated.contains_key("D1"), "the header names D1");
    let n_parsers = header["parsers"].as_object().expect("parsers").len();
    let mut ledger = Ledger::new("golden_datetime_offsets", NAME);
    let mut failures = Vec::new();
    let (mut texts, mut bounds, mut windows, mut differing) = (0, 0, 0, 0);
    for (i, row) in rows.iter().enumerate() {
        assert!(
            row.get("reference").is_none(),
            "row {i} carries a reference answer, which follows the release here"
        );
        if let Some(id) = row.get("difference") {
            let id = id.as_str().expect("an id");
            assert!(
                stated.contains_key(id),
                "row {i}: {id} is not in the header"
            );
            differing += 1;
        }
        let (kind, got, want) = check(row, n_parsers);
        match kind {
            "text" => texts += 1,
            "bound" => bounds += 1,
            _ => windows += 1,
        }
        if ledger.differs_fatally(format!("row {i}"), got == want) {
            failures.push(format!("row {i} {row}\n native {got}\n file   {want}"));
        }
    }
    assert!(
        failures.is_empty(),
        "{NAME}: {} rows differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert!(differing > 0, "the header names D1 and no row carries it");
    assert_eq!(differing, rows.len(), "every row is a D1 row");
    // Three texts from datetime_edges.jsonl, four more and four of the comma
    // and compact spellings; six bounds from eval_model_python.jsonl and
    // three more; three window tests.
    assert_eq!((texts, bounds, windows), (11, 9, 3));
    ledger.finish(rows.len());
}
