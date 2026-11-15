//! Every case of `datetime.jsonl`: each formatter over each neutral
//! datetime, each parser over each text.
//!
//! A cell the fixture marks instead of pinning is not compared, and is
//! counted apart: `platform_dependent: true` (strftime `%Y` below year
//! 1000) and `local_time_dependent: true` (a naive value through
//! `astimezone`). A cell that records a result per Python version or per
//! platform is compared with the result of the reference, Python 3.13 on a
//! platform other than Windows.

mod common;

use bettermemory_core::datetime::{self, DateError, DateTime};
use bettermemory_core::unicode;
use common::{assert_count, jsonl};
use serde_json::{json, Value};

fn formatted(name: &str, dt: &DateTime) -> Value {
    let v = |text: String| json!({ "v": text });
    let err = |e: DateError| json!({ "error": e.name() });
    match name {
        "isoformat_utc" => v(dt.isoformat_utc()),
        "isoformat" => v(dt.isoformat()),
        "str" => v(dt.python_str()),
        "compact_utc" => match dt.compact_utc() {
            None => json!({ "local_time_dependent": true }),
            Some(Ok(text)) => v(text),
            Some(Err(e)) => err(e),
        },
        "date_only" => v(dt.strftime("%Y-%m-%d").expect("ported directives")),
        "seconds_z" => v(dt
            .strftime("%Y-%m-%dT%H:%M:%SZ")
            .expect("ported directives")),
        "pydantic_json" => v(dt.pydantic_json()),
        other => panic!("formatter {other} is not ported"),
    }
}

fn parsed(name: &str, text: &[u32]) -> Value {
    let iso = |d: Option<DateTime>| json!({ "v": d.map(|d| d.isoformat()) });
    match name {
        "parse_event_ts" => iso(datetime::parse_event_ts(text)),
        "store_rows_dt" | "v8_as_dt" => match datetime::parse_record_dt(text) {
            Ok(d) => iso(Some(d)),
            Err(e) => json!({ "error": e.name() }),
        },
        "git_instant" => iso(datetime::git_instant(text)),
        "git_seconds" => json!({ "v": datetime::git_seconds(text) }),
        "ulid_time" => match datetime::ulid_time(text) {
            Ok(d) => iso(d),
            Err(e) => json!({ "error": e.name() }),
        },
        other => panic!("parser {other} is not ported"),
    }
}

/// The result a cell pins for the reference (3.13, not Windows).
fn pinned(expected: &Value) -> &Value {
    if let Some(by_version) = expected.get("version_dependent") {
        let mut best: Option<((u32, u32), &Value)> = None;
        for (since, result) in by_version.as_object().expect("versions") {
            let (major, minor) = since.trim_end_matches('+').split_once('.').expect("x.y+");
            let version = (major.parse().unwrap(), minor.parse().unwrap());
            if version <= (3, 13) && best.is_none_or(|(b, _)| version > b) {
                best = Some((version, result));
            }
        }
        return best.expect("a result for 3.13").1;
    }
    if let Some(by_platform) = expected.get("platform_dependent") {
        if by_platform.is_object() {
            return &by_platform["other"];
        }
    }
    expected
}

#[test]
fn datetime_every_case() {
    let lines = jsonl("datetime.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "datetime");
    let mut checked = 0;
    let mut compared = 0;
    let mut marked = 0;
    let mut failures = Vec::new();
    for record in &lines[1..] {
        let out = record["out"].as_object().expect("out");
        if let Some(fields) = record.get("datetime") {
            assert_eq!(out.len(), header["formatters"].as_object().unwrap().len());
            let f: Vec<i64> = fields["fields"]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_i64().unwrap())
                .collect();
            let offset = fields["offset_seconds"].as_i64().map(|s| s * 1_000_000);
            let dt = DateTime::new(
                f[0],
                f[1] as u32,
                f[2] as u32,
                f[3] as u32,
                f[4] as u32,
                f[5] as u32,
                f[6] as u32,
                offset,
            )
            .expect("a valid datetime");
            for (name, expected) in out {
                if expected == &json!({ "platform_dependent": true })
                    || expected == &json!({ "local_time_dependent": true })
                        && formatted(name, &dt) == *expected
                {
                    marked += 1;
                    continue;
                }
                let got = formatted(name, &dt);
                if &got != expected {
                    failures.push(format!("{fields} / {name}: expected {expected}, got {got}"));
                }
                compared += 1;
            }
        } else {
            assert_eq!(out.len(), header["parsers"].as_object().unwrap().len());
            let text = unicode::cps(record["text"].as_str().expect("text"));
            for (name, expected) in out {
                let expected = pinned(expected);
                let got = parsed(name, &text);
                if &got != expected {
                    failures.push(format!(
                        "{:?} / {name}: expected {expected}, got {got}",
                        record["text"]
                    ));
                }
                compared += 1;
            }
        }
        checked += 1;
    }
    eprintln!(
        "datetime: {compared} cells compared, {marked} marked cells not pinned by the fixture"
    );
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count("datetime.jsonl", checked, lines.len() - 1);
}
