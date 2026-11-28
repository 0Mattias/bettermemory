//! Every case of `datetime_edges.jsonl` (Python 3.13's results): each
//! formatter over datetimes whose offsets fall below a minute or a
//! second, each parser over the spellings `fromisoformat` reads in ways a
//! port would not guess (ULIDs among them), and `datetime.fromtimestamp`
//! over doubles on and next to half a microsecond. A text whose offset is
//! zero hours, minutes and seconds with a fraction after the seconds is
//! not here: its answer follows the release, and the port's (stated
//! difference D1) is `golden_datetime_offsets`'. One whose fraction
//! follows the offset's hour or minute or a fourth `:`, or whose time
//! holds a fraction after its hour or minute or a fourth `:`, is: the
//! port answers it as 3.13.13 does.
//!
//! Marks as in `datetime.jsonl`: a cell recording a result per platform
//! (`fromtimestamp`, and `ulid_time` through it, outside 1970..3000, which
//! Windows refuses) is compared with the result on a platform other than
//! Windows, which the port follows; a bare `platform_dependent: true` or
//! `local_time_dependent: true` is not compared and is counted apart.

mod common;

use bettermemory_core::datetime::{self, DateError, DateTime};
use bettermemory_core::float_repr;
use common::parity::{self, Ledger};
use common::{jsonl, text_field};
use serde_json::{json, Value};

fn formatted(name: &str, dt: &DateTime) -> Value {
    let v = |text: String| json!({ "v": text });
    match name {
        "isoformat_utc" => v(dt.isoformat_utc()),
        "isoformat" => v(dt.isoformat()),
        "str" => v(dt.python_str()),
        "compact_utc" => match dt.compact_utc() {
            None => json!({ "local_time_dependent": true }),
            Some(Ok(text)) => v(text),
            Some(Err(e)) => json!({ "error": e.name() }),
        },
        "date_only" => v(dt.strftime("%Y-%m-%d").expect("ported directives")),
        "seconds_z" => v(dt
            .strftime("%Y-%m-%dT%H:%M:%SZ")
            .expect("ported directives")),
        "pydantic_json" => v(dt.pydantic_json()),
        other => panic!("formatter {other} is not ported"),
    }
}

/// The result a cell pins for the reference (3.13, not Windows), and
/// whether the cell recorded it per platform.
fn pinned(expected: &Value) -> (&Value, bool) {
    match expected.get("platform_dependent") {
        Some(by_platform) if by_platform.is_object() => (&by_platform["other"], true),
        _ => (expected, false),
    }
}

/// A cell the fixture marks instead of pinning.
fn bare_mark(expected: &Value) -> bool {
    expected == &json!({ "platform_dependent": true })
        || expected == &json!({ "local_time_dependent": true })
}

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

#[test]
fn datetime_edges_every_case() {
    let lines = jsonl("datetime_edges.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "datetime_edges");
    // The CPython minor that made the file (3.13 for the committed one).
    assert_eq!(header["python"], parity::python_minor().as_str());
    let n_formatters = header["formatters"].as_object().unwrap().len();
    let n_parsers = header["parsers"].as_object().unwrap().len();
    let (mut formats, mut texts, mut stamps, mut cells) = (0, 0, 0, 0);
    let (mut per_platform, mut marked) = (0, 0);
    let mut failures = Vec::new();
    for record in &lines[1..] {
        if let Some(fields) = record.get("datetime") {
            let out = record["out"].as_object().expect("out");
            assert_eq!(out.len(), n_formatters);
            let f: Vec<i64> = fields["fields"]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_i64().unwrap())
                .collect();
            let dt = DateTime::new(
                f[0],
                f[1] as u32,
                f[2] as u32,
                f[3] as u32,
                f[4] as u32,
                f[5] as u32,
                f[6] as u32,
                Some(fields["offset_us"].as_i64().expect("offset_us")),
            )
            .expect("a valid datetime");
            for (name, expected) in out {
                if bare_mark(expected) {
                    marked += 1;
                    continue;
                }
                let got = formatted(name, &dt);
                if &got != expected {
                    failures.push(format!("{fields} / {name}: expected {expected}, got {got}"));
                }
                cells += 1;
            }
            formats += 1;
        } else if let Some(stamp) = record.get("timestamp") {
            let bits = u64::from_str_radix(stamp["bits"].as_str().unwrap(), 16).unwrap();
            let x = f64::from_bits(bits);
            assert_eq!(float_repr::repr(x), stamp["repr"].as_str().unwrap());
            let got = match DateTime::from_timestamp_utc(x) {
                Ok(d) => json!({ "v": d.isoformat() }),
                Err(e) => json!({ "error": e.name() }),
            };
            let (expected, by_platform) = pinned(&record["out"]);
            per_platform += usize::from(by_platform);
            if &got != expected {
                failures.push(format!(
                    "fromtimestamp({}): expected {expected}, got {got}",
                    stamp["repr"]
                ));
            }
            cells += 1;
            stamps += 1;
        } else {
            let out = record["out"].as_object().expect("out");
            assert_eq!(out.len(), n_parsers);
            let text = text_field(record, "text");
            for (name, expected) in out {
                let (expected, by_platform) = pinned(expected);
                per_platform += usize::from(by_platform);
                let got = parsed(name, &text);
                if &got != expected {
                    failures.push(format!(
                        "{text:x?} / {name}: expected {expected}, got {got}"
                    ));
                }
                cells += 1;
            }
            texts += 1;
        }
    }
    eprintln!(
        "datetime_edges: {formats} datetimes, {texts} texts, {stamps} timestamps, \
         {cells} cells compared ({per_platform} of them per platform), \
         {marked} marked cells not pinned by the fixture"
    );
    // CPython 3.13.13's behaviour (class ii): Ledger sets a differing cell
    // aside, named, where the file was made under another Python.
    Ledger::new("datetime_edges_every_case", "datetime_edges.jsonl").settle(cells, &failures);
    assert_eq!(
        formats + texts + stamps,
        lines.len() - 1,
        "datetime_edges.jsonl cases"
    );
    // 32 datetimes x 7 formatters + 199 texts x 6 parsers + 105 timestamps.
    // The three texts whose offset is zero whole seconds with a fraction
    // after the seconds are `datetime_offsets.jsonl`'s (stated difference
    // D1); the seven with the offset's fraction after its hour, its minute
    // or a fourth `:`, and the six with such an offset after a time whose
    // fraction follows its hour or minute or a fourth `:`, are here.
    assert_eq!((formats, texts, stamps), (32, 199, 105));
    assert_eq!(cells + marked, 32 * 7 + 199 * 6 + 105);
    // 46 ULIDs past 3000 through ulid_time, 31 timestamps outside 1970..3000.
    assert_eq!(per_platform, 77, "cells recorded per platform");
    assert_eq!(marked, 0, "bare marks");
}
