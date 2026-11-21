//! Every row of `origin_dates_reads.jsonl` against `origin_dates`:
//! `read_author_timestamps`, `refused_head`, `unborn_or_broken`,
//! `unread_entry`, `extended_author_timestamps`, `hashed_seconds` and
//! `marked_seconds`, each over a scripted git and scripted leaves; every
//! row checks the answer, the calls in order and the failures noted.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_dates as od;
use bettermemory_core::origin_history::{author_dates, HistoryUnread};
use common::origin_dates::{cut, spell, text, Script};
use common::{assert_count, jsonl};
use serde_json::{json, Value};

const CWD: &[u8] = b"R";

fn dates(d: &[DateTime]) -> Value {
    json!(d.iter().map(DateTime::isoformat).collect::<Vec<_>>())
}

fn why(w: Option<HistoryUnread>) -> Value {
    json!(w.map(HistoryUnread::value))
}

fn run(row: &Value) -> (Value, Script) {
    let s = Script::new(row);
    let (mut git, mut files) = (s.clone(), s.clone());
    let r = match row["f"].as_str().unwrap() {
        "read" => match od::read_author_timestamps(
            &mut git,
            &mut files,
            CWD,
            &text(&row["rev"]),
            &cut(&row["cut"]),
        ) {
            Ok(d) => dates(&d),
            Err(w) => why(w),
        },
        "refused" => why(od::refused_head(&mut git, &mut files, CWD)),
        "unborn" => why(od::unborn_or_broken(&mut git, &mut files, CWD)),
        "entry" => why(od::unread_entry(&mut files, CWD)),
        "extended" => {
            let base: Vec<i128> = row["base"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| i128::from(v.as_i64().unwrap()))
                .collect();
            let got = od::extended_author_timestamps(
                &mut git,
                CWD,
                &text(&row["base_head"]),
                &author_dates(&base),
                &text(&row["head"]),
                &cut(&row["cut"]),
            );
            match got {
                Ok(Some(d)) => dates(&d),
                Ok(None) => Value::Null,
                Err(w) => json!(w.value()),
            }
        }
        other => panic!("unknown function {other}"),
    };
    (r, s)
}

#[test]
fn every_row() {
    let rows = jsonl("origin_dates_reads.jsonl");
    let header = &rows[0];
    let probes: Vec<Vec<&str>> = od::HEAD_PROBES.iter().map(|p| p.to_vec()).collect();
    assert_eq!(header["probes"], json!(probes));
    assert_eq!(header["walk_mark"], json!(od::WALK_MARK));
    let mut checked = 0;
    for row in &rows[1..] {
        let f = row["f"].as_str().unwrap();
        if f == "hashed" || f == "marked" {
            let line = text(&row["line"]);
            let got = if f == "hashed" {
                od::hashed_seconds(&line).map(|(sha, v)| json!([spell(&sha), v]))
            } else {
                od::marked_seconds(&line).map(|(l, sha, v)| json!([l, spell(&sha), v]))
            };
            assert_eq!(got.unwrap_or(Value::Null), row["r"], "{row}");
            checked += 1;
            continue;
        }
        let (r, s) = run(row);
        let s = s.0.borrow();
        assert_eq!(r, row["r"], "{row}");
        assert_eq!(json!(s.calls), row["c"], "{row}");
        assert_eq!(json!(s.failed), row["n"], "{row}");
        assert!(s.answers.is_empty(), "{row}");
        checked += 1;
    }
    assert_count("origin_dates_reads.jsonl", checked, rows.len() - 1);
}
