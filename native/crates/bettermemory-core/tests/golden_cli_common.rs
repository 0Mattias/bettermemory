//! Every row of `cli_common.jsonl` and `cli_common_python.jsonl` against
//! `entry` (the hook words and their routing) and `cli::common` (the
//! refusals, the key-unreadable line, the migration line and
//! `parse_iso_cutoff`).

mod common;

use std::path::PathBuf;

use bettermemory_core::cli::common::{
    exit_if_bundle_file, exit_if_key_unreadable, key_unreadable_line, migration_line,
    parse_iso_cutoff, refuse, CutoffFault, MigrationTotals,
};
use bettermemory_core::cli::{Exit, UsageError};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::entry::{main, route_hook_word, Routed, HOOK_WORDS};
use bettermemory_core::log::ring::{KeyUnreadable, RingError};
use bettermemory_core::pyvalue::PyStr;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

/// A `str` field, or its `*_cps` form.
fn text(row: &Value, name: &str) -> PyStr {
    if let Some(v) = row.get(name).and_then(Value::as_str) {
        return cps(v);
    }
    row[format!("{name}_cps")]
        .as_array()
        .unwrap_or_else(|| panic!("no field {name}"))
        .iter()
        .map(|c| c.as_u64().unwrap() as u32)
        .collect()
}

/// A PyStr as the generator spells it: a JSON string, or code points.
fn spelled(name: &str, t: &[u32]) -> (String, Value) {
    let chars: Option<String> = t.iter().map(|&c| char::from_u32(c)).collect();
    match chars {
        Some(s) => (name.to_owned(), json!(s)),
        None => (format!("{name}_cps"), json!(t)),
    }
}

fn str_of(t: &[u32]) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

/// `argparse.ArgumentParser(prog=prog)`'s `error`: its usage line, then
/// the message.
pub struct Parser(PyStr);

impl UsageError for Parser {
    fn prog(&self) -> &PyStr {
        &self.0
    }
    fn error(&self, message: &PyStr) -> Exit {
        let mut stderr = cps("usage: ");
        stderr.extend_from_slice(&self.0);
        stderr.extend(cps(" [-h]\n"));
        stderr.extend_from_slice(&self.0);
        stderr.extend(cps(": error: "));
        stderr.extend_from_slice(message);
        stderr.extend(cps("\n"));
        Exit { code: 2, stderr }
    }
}

fn key_path(row: &Value) -> PathBuf {
    let t = text(row, "path");
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;
        // `os.fsencode`: a lone surrogate U+DC80..U+DCFF is its byte.
        let mut bytes = Vec::new();
        for &c in &t {
            if (0xDC80..=0xDCFF).contains(&c) {
                bytes.push((c - 0xDC00) as u8);
            } else {
                let mut buf = [0; 4];
                bytes
                    .extend_from_slice(char::from_u32(c).unwrap().encode_utf8(&mut buf).as_bytes());
            }
        }
        PathBuf::from(std::ffi::OsString::from_vec(bytes))
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStringExt;
        let mut units = Vec::new();
        for &c in &t {
            match char::from_u32(c) {
                Some(ch) => {
                    let mut buf = [0; 2];
                    units.extend_from_slice(ch.encode_utf16(&mut buf));
                }
                None => units.push(c as u16),
            }
        }
        PathBuf::from(std::ffi::OsString::from_wide(&units))
    }
}

fn exit_value(e: &Option<Exit>) -> (Value, PyStr) {
    match e {
        Some(e) => (json!(e.code), e.stderr.clone()),
        None => (Value::Null, PyStr::new()),
    }
}

/// The native answer to one row, in the row's own spelling.
fn answer(row: &Value) -> Value {
    let mut out = row.clone();
    let f = row["f"].as_str().unwrap();
    match f {
        "words" => {
            let mut w: Vec<&str> = HOOK_WORDS.to_vec();
            w.sort_unstable();
            out["v"] = json!(w);
        }
        "hook" => {
            let argv: Vec<PyStr> = row["argv"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| cps(a.as_str().unwrap()))
                .collect();
            let ret = row["ret"].as_i64().unwrap() as i32;
            let mut seen = None;
            let code = route_hook_word(&argv, |a| {
                seen = Some(a.iter().map(|w| str_of(w)).collect::<Vec<_>>());
                ret
            });
            out["exit"] = json!(code);
            out["called"] = json!(seen);
            // `main` routes the same way and otherwise calls the CLI.
            let routed = main(&argv, |_| ret, || "cli");
            let want = match code {
                Some(c) => Routed::Hook(c),
                None => Routed::Cli("cli"),
            };
            assert_eq!(routed, want);
        }
        "refuse" => {
            let parser = Parser(cps(row["prog"].as_str().unwrap()));
            let e = refuse(
                &parser,
                &text(row, "message"),
                row["code"].as_i64().unwrap() as i32,
            );
            out["exit"] = json!(e.code);
            out["err"] = json!(str_of(&e.stderr));
        }
        "bundle" => {
            let refusal = row["refusal"].as_str().map(cps);
            let e = exit_if_bundle_file(
                refusal,
                row["code"].as_i64().unwrap() as i32,
                &text(row, "prefix"),
            )
            .err();
            let (code, err) = exit_value(&e);
            out["exit"] = code;
            out["err"] = json!(str_of(&err));
        }
        "key" => {
            let make = || KeyUnreadable {
                path: key_path(row),
                errno: Some(13),
                reason: row["reason"].as_str().unwrap().to_owned(),
            };
            let prefix = text(row, "prefix");
            let line = key_unreadable_line(&make(), &prefix);
            let (k, v) = spelled("line", &line);
            out[k] = v;
            let code = row["code"].as_i64().unwrap() as i32;
            let e =
                exit_if_key_unreadable::<()>(Err(RingError::KeyUnreadable(make())), code, &prefix)
                    .err();
            let (c, err) = exit_value(&e);
            out["exit"] = c;
            let (k, v) = spelled("err", &err);
            out[k] = v;
            // Anything else passes through.
            assert!(matches!(
                exit_if_key_unreadable(Ok::<i32, RingError>(5), code, &prefix),
                Ok(Ok(5))
            ));
        }
        "migration" => {
            let t = &row["t"];
            let g = |n: &str| t.get(n).and_then(Value::as_i64).unwrap_or(0);
            let totals = MigrationTotals {
                projects: g("projects"),
                checkouts: g("checkouts"),
                memory_homes: g("memory_homes"),
                tombstone_homes: g("tombstone_homes"),
                episode_homes: g("episode_homes"),
                proposals: g("proposals"),
                log_rows: g("log_rows"),
            };
            out["line"] = json!(str_of(&migration_line(&totals)));
        }
        "cutoff" => {
            let now = DateTime::from_isoformat(&cps(row["now"].as_str().unwrap())).unwrap();
            let got = parse_iso_cutoff(
                &text(row, "value"),
                &text(row, "flag"),
                row["far"].as_str().unwrap(),
                &now,
            );
            let obj = out.as_object_mut().unwrap();
            obj.remove("ok");
            obj.remove("err");
            obj.remove("err_cps");
            let (exit, raised, err) = match got {
                Ok(c) => {
                    obj.insert(
                        "ok".into(),
                        json!([c.parsed.isoformat(), str_of(&c.canonical)]),
                    );
                    (Value::Null, Value::Null, c.warning)
                }
                Err(CutoffFault::Exit(e)) => (json!(e.code), Value::Null, e.stderr),
                Err(CutoffFault::Raised { error, stderr }) => {
                    (Value::Null, json!(error.name()), stderr)
                }
            };
            obj.insert("exit".into(), exit);
            obj.insert("raised".into(), raised);
            let (k, v) = spelled("err", &err);
            obj.insert(k, v);
        }
        other => panic!("unknown row kind {other}"),
    }
    out
}

fn file(test: &str, name: &str) {
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    assert!(!lines.is_empty(), "{name}: no case");
    let ledger = Ledger::new(test, name);
    let mut failures = Vec::new();
    for (i, row) in lines.iter().enumerate() {
        let got = answer(row);
        if got != *row {
            failures.push(format!("#{i}: native {got}\n reference {row}"));
        }
    }
    ledger.settle(lines.len(), &failures);
}

#[test]
fn cli_common_every_case() {
    file("golden_cli_common", "cli_common.jsonl");
}

#[test]
fn cli_common_python_every_case() {
    file("golden_cli_common_python", "cli_common_python.jsonl");
}
