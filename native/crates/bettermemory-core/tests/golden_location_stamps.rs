//! Every case of `location_stamps.jsonl`, `location_stamps_python.jsonl`
//! and `location_stamps_platform.jsonl` against `location_stamps`: `in_ticks`, `kept_in_ticks`, `_drvfs`,
//! `stat_each`, `_init_stamps`, `capture_ticks`, `filetime_ns`,
//! `basic_info_ns`, the two sweeps by digest, and `_device_name` in each
//! section of the second file, as the platform the section names answers.

mod common;

use bettermemory_core::fsdata::{BasicInfo, Flavour, PyErr};
use bettermemory_core::location_stamps::{self as ls, Machine, Platform, StampError};
use common::location_stamps::{bytes_of, int, stat, sweep_times, table_of, SplitMix, TableFs};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const MOUNTS: &[u8] = b"/proc/self/mountinfo";

fn err_value(e: &StampError) -> Value {
    match e {
        StampError::Overflow(m) => json!(["err", "OverflowError", m]),
        StampError::Value(m) => json!(["err", "ValueError", m]),
        StampError::Index => json!(["err", "IndexError", "list index out of range"]),
        StampError::NotPorted(what) => panic!("declined: {what}"),
    }
}

fn answer<T: Into<Value>>(got: Result<T, StampError>) -> Value {
    got.map_or_else(|e| err_value(&e), Into::into)
}

fn platform(windows: bool, linux: bool) -> Platform {
    if windows {
        Platform::Windows
    } else if linux {
        Platform::Linux
    } else {
        Platform::Darwin
    }
}

fn with_table(table: &Value) -> TableFs {
    let mut fs = TableFs::default();
    match table {
        Value::Null => {}
        Value::String(s) if s == "dir" => fs.file = Some((MOUNTS.to_vec(), Err(()))),
        pieces => fs.file = Some((MOUNTS.to_vec(), Ok(table_of(pieces)))),
    }
    fs
}

fn ints(v: &Value) -> Vec<i128> {
    v.as_array().unwrap().iter().map(int).collect()
}

fn st3(v: &Value) -> bettermemory_core::fsdata::StatResult {
    let f = ints(v);
    stat(0, f[0] as u64, f[1], f[2], 0)
}

fn sha(lines: &[String]) -> String {
    Sha256::digest(lines.join("\n").as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn check(row: &Value) {
    let r = row.as_array().unwrap();
    let got: Value = match r[0].as_str().unwrap() {
        "in" => ls::in_ticks(&ints(&r[1])).into(),
        "kept" => {
            let fs = with_table(&r[3]);
            let m = Machine {
                fs: &fs,
                platform: platform(r[1] == true, r[2] == true),
                mountinfo: MOUNTS,
            };
            let device = (!r[4].is_null()).then(|| int(&r[4]));
            answer(ls::kept_in_ticks(&m, &ints(&r[5]), device))
        }
        "drvfs" => {
            let fs = with_table(&r[1]);
            let m = Machine {
                fs: &fs,
                platform: Platform::Linux,
                mountinfo: MOUNTS,
            };
            answer(ls::drvfs(&m, int(&r[2])))
        }
        "stat" => {
            let mut fs = TableFs::default();
            for (path, s) in r[2].as_object().unwrap() {
                let entry = if s == "value" {
                    Err(PyErr::Value)
                } else {
                    let f = ints(s);
                    Ok(stat(f[0] as u64, f[1] as u64, f[2], f[3], f[4] as u64))
                };
                fs.stats.insert(path.as_bytes().to_vec(), entry);
            }
            let paths: Vec<Option<Vec<u8>>> = r[1]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| p.as_str().map(|s| s.as_bytes().to_vec()))
                .collect();
            let refs: Vec<Option<&[u8]>> = paths.iter().map(|p| p.as_deref()).collect();
            ls::stat_each(&fs, &refs)
                .iter()
                .map(|s| s.as_ref().map(|s| s.ino.unwrap().to_string()))
                .collect::<Vec<_>>()
                .into()
        }
        "init" => {
            let mut fs = TableFs {
                basic_fails: true,
                ..TableFs::default()
            };
            for (path, s) in r[5].as_object().unwrap() {
                let entry = if s == "value" {
                    Err(PyErr::Value)
                } else {
                    let f = ints(s);
                    Ok(stat(f[0] as u64, f[1] as u64, f[2], f[3], f[4] as u64))
                };
                fs.stats.insert(path.as_bytes().to_vec(), entry);
            }
            for (path, b) in r[6].as_object().unwrap() {
                let info = if b == "fail" {
                    BasicInfo::Failed
                } else {
                    let t = ints(b);
                    BasicInfo::Times([0, 1, 2, 3].map(|i| t[i] as i64))
                };
                fs.basic.insert(path.as_bytes().to_vec(), info);
            }
            let m = Machine {
                fs: &fs,
                platform: platform(r[2] == true, false),
                mountinfo: MOUNTS,
            };
            let flavour = if r[1] == true {
                Flavour::Windows
            } else {
                Flavour::Posix
            };
            let dir = r[3].as_str().unwrap().as_bytes();
            answer(
                ls::init_stamps(&m, flavour, dir, r[4] == true).map(|stamps| {
                    stamps
                        .iter()
                        .map(|s| {
                            json!([
                                s.name,
                                s.ino.to_string(),
                                s.mtime_ns.to_string(),
                                s.ctime_ns.to_string(),
                                s.size.to_string()
                            ])
                        })
                        .collect::<Vec<_>>()
                }),
            )
        }
        "cap" => {
            let fs = with_table(&r[3]);
            let m = Machine {
                fs: &fs,
                platform: platform(r[1] == true, r[2] == true),
                mountinfo: MOUNTS,
            };
            let git_st = (!r[4].is_null()).then(|| st3(&r[4]));
            let key: Option<Vec<bool>> = r[5]
                .as_array()
                .map(|k| k.iter().map(Value::is_null).collect());
            let read: Vec<_> = r[6]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| (!s.is_null()).then(|| st3(s)))
                .collect();
            let stamps: Vec<(i128, i128)> = r[7]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| (int(&s[2]), int(&s[3])))
                .collect();
            answer(ls::capture_ticks(
                &m,
                git_st.as_ref(),
                key.as_deref(),
                &read,
                &stamps,
            ))
        }
        "ft" => ls::filetime_ns(int(&r[1])).map_or(Value::Null, |v| v.to_string().into()),
        "basic" => {
            let mut fs = TableFs::default();
            let plat = if r[1] == "posix" {
                Platform::Darwin
            } else {
                Platform::Windows
            };
            let info = match &r[1] {
                Value::String(_) => BasicInfo::Failed,
                t => BasicInfo::Times([0, 1, 2, 3].map(|i| int(&t[i]) as i64)),
            };
            fs.basic.insert(b"C:\\g\\description".to_vec(), info);
            let m = Machine {
                fs: &fs,
                platform: plat,
                mountinfo: MOUNTS,
            };
            answer(ls::basic_info_ns(&m, b"C:\\g\\description").map(|got| {
                got.map_or(Value::Null, |t| {
                    t.iter().map(ToString::to_string).collect::<Vec<_>>().into()
                })
            }))
        }
        "digest" => {
            let mut gen = SplitMix(r[2].as_u64().unwrap());
            let count = r[3].as_u64().unwrap() as usize;
            let lines: Vec<String> = match r[1].as_str().unwrap() {
                "filetime" => (0..count)
                    .map(|_| {
                        let ticks = gen.next().unwrap() as i64;
                        ls::filetime_ns(i128::from(ticks)).unwrap().to_string()
                    })
                    .collect(),
                "in_ticks" => (0..count)
                    .map(|_| u8::from(ls::in_ticks(&sweep_times(&mut gen))).to_string())
                    .collect(),
                other => panic!("no sweep {other}"),
            };
            assert_eq!(lines.len(), count);
            sha(&lines).into()
        }
        other => panic!("unknown row kind {other}"),
    };
    let want = if r[0] == "digest" {
        &r[4]
    } else {
        r.last().unwrap()
    };
    assert_eq!(&got, want, "row {row}");
}

#[test]
fn every_case_answers_as_the_reference() {
    let name = "location_stamps.jsonl";
    let rows = common::jsonl(name);
    let kinds = rows[0]["kinds"].as_object().unwrap();
    let in_file: u64 = kinds.values().map(|v| v.as_u64().unwrap()).sum();
    assert_eq!(in_file as usize, rows.len() - 1);
    let mut checked = 0;
    for row in &rows[1..] {
        check(row);
        checked += 1;
    }
    common::assert_count(name, checked, rows.len() - 1);
}

/// `location_stamps_python.jsonl` (class ii, Python): the rows whose
/// answer is the message `os.stat` raises for a NUL in a path, which is
/// the release's.
#[test]
fn every_release_bound_case_answers_as_the_reference() {
    let name = "location_stamps_python.jsonl";
    let rows = common::jsonl(name);
    let kinds = rows[0]["kinds"].as_object().unwrap();
    let in_file: u64 = kinds.values().map(|v| v.as_u64().unwrap()).sum();
    assert_eq!(in_file as usize, rows.len() - 1);
    let mut ledger = Ledger::new("golden_location_stamps", name);
    for (i, row) in rows[1..].iter().enumerate() {
        ledger.case(format!("{name}:{i}"), || check(row));
    }
    ledger.finish(rows.len() - 1);
}

/// The encoding of `os.major` a section of the platform file names.
fn encoding(name: &str) -> Platform {
    match name {
        "darwin" => Platform::Darwin,
        "linux-gnu" => Platform::Linux,
        "linux-musl" => Platform::LinuxMusl,
        "win32" => Platform::Windows,
        other => panic!("no device encoding for {other}"),
    }
}

#[test]
fn device_names_answer_as_each_platform_that_made_them() {
    let name = "location_stamps_platform.jsonl";
    let rows = common::jsonl(name);
    let listed: Vec<&str> = rows[0]["sections"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap())
        .collect();
    // macOS's section and both Linux C libraries' are in every set: the
    // running platform's made there, the others carried as recorded.
    assert_eq!(&listed[..3], ["darwin", "linux-gnu", "linux-musl"]);
    let mut seen: Vec<&str> = Vec::new();
    let mut devices: Vec<Vec<&Value>> = Vec::new();
    let mut plat = None;
    let mut checked = 0;
    for row in &rows[1..] {
        if row[0] == "section" {
            seen.push(row[1].as_str().unwrap());
            devices.push(Vec::new());
            plat = Some(encoding(row[1].as_str().unwrap()));
            continue;
        }
        assert_eq!(row[0], "dev", "row {row}");
        let plat = plat.expect("a row before any section");
        let got = answer(ls::device_name(plat, int(&row[1])));
        assert_eq!(got, row[2], "{} row {row}", seen.last().unwrap());
        devices.last_mut().unwrap().push(&row[1]);
        checked += 1;
    }
    assert_eq!(seen, listed);
    // Every section names the same devices in the same order.
    assert!(devices.iter().all(|d| d == &devices[0]), "sections differ");
    common::assert_count(name, checked, rows.len() - 1 - seen.len());
}

#[test]
fn spellings_read_back() {
    assert_eq!(bytes_of("=a b"), b"a b");
    assert_eq!(bytes_of("5c00"), b"\\\0");
}
