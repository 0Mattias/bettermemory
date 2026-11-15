//! Every case of `datetime_edges.jsonl` (Python 3.13's results): each
//! formatter over datetimes whose offsets fall below a minute or a
//! second, each parser over the spellings `fromisoformat` reads in ways a
//! port would not guess (ULIDs among them), and `datetime.fromtimestamp`
//! over doubles on and next to half a microsecond.

mod common;

use bettermemory_core::datetime::{self, DateError, DateTime};
use bettermemory_core::float_repr;
use common::{assert_count, jsonl, text_field};
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
    assert_eq!(header["python"], "3.13");
    let n_formatters = header["formatters"].as_object().unwrap().len();
    let n_parsers = header["parsers"].as_object().unwrap().len();
    let (mut formats, mut texts, mut stamps, mut cells) = (0, 0, 0, 0);
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
            if got != record["out"] {
                failures.push(format!(
                    "fromtimestamp({}): expected {}, got {got}",
                    stamp["repr"], record["out"]
                ));
            }
            cells += 1;
            stamps += 1;
        } else {
            let out = record["out"].as_object().expect("out");
            assert_eq!(out.len(), n_parsers);
            let text = text_field(record, "text");
            for (name, expected) in out {
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
        "datetime_edges: {formats} datetimes, {texts} texts, {stamps} timestamps, {cells} cells"
    );
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count(
        "datetime_edges.jsonl",
        formats + texts + stamps,
        lines.len() - 1,
    );
}
