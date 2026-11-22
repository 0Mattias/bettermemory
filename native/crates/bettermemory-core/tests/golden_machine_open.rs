//! Every case of `machine_open.jsonl` and `machine_open_platform.jsonl`
//! against `machine::open`: `open_machine` over a real state directory
//! with the facts, the id file's object, the clock and the random bytes
//! scripted as the generator scripts them, the files compared as bytes
//! and modes. Both files are POSIX only; `machine_open_not_posix.rs`
//! records the Windows gap.

#![cfg(unix)]

mod common;

use std::cell::RefCell;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::StdFs;
use bettermemory_core::machine::facts::{self as f, IdFileStat};
use bettermemory_core::machine::identity_to_json;
use bettermemory_core::machine::open::{open_machine, OpenError, StdMachine};
use bettermemory_core::pyvalue::{PyError, PyValue};
use common::parity::Ledger;
use common::{jsonl, neutral, unhex};
use serde_json::{json, Value};

fn now(header: &Value) -> DateTime {
    let iso = header["now"].as_str().expect("now");
    DateTime::from_isoformat(&iso.chars().map(u32::from).collect::<Vec<_>>()).expect("a time")
}

fn token(header: &Value) -> [u8; 17] {
    unhex(header["token"].as_str().expect("token"))
        .try_into()
        .expect("17 bytes")
}

fn scratch(tag: &str, i: usize) -> PathBuf {
    let dir =
        std::env::temp_dir().join(format!("bm-machine-open-{tag}-{}-{i}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("a scratch folder");
    dir
}

fn read(path: &Path) -> Option<String> {
    std::fs::read(path)
        .ok()
        .map(|b| String::from_utf8(b).expect("ASCII"))
}

fn mode(path: &Path) -> Value {
    match std::fs::metadata(path) {
        Ok(md) => json!(format!("0o{:o}", md.permissions().mode() & 0o777)),
        Err(_) => Value::Null,
    }
}

fn text(v: &PyValue) -> Option<Vec<u32>> {
    match v {
        PyValue::None => None,
        PyValue::Str(s) => Some(s.clone()),
        other => panic!("a text {other:?}"),
    }
}

/// `_file_object` scripted: logs the files' state, answers the case's.
struct Scripted<'a> {
    answer: Option<(u64, u64)>,
    id_path: PathBuf,
    facts_path: PathBuf,
    first_facts: Option<String>,
    log: &'a RefCell<Vec<Value>>,
    seen_ids: &'a RefCell<Vec<Option<String>>>,
}

impl IdFileStat for Scripted<'_> {
    fn id_stat(&self, path: &[u8]) -> Result<f::IdStat, f::IdStatError> {
        assert_eq!(path, self.id_path.as_os_str().as_encoded_bytes());
        self.seen_ids.borrow_mut().push(read(&self.id_path));
        let untouched = read(&self.facts_path) == self.first_facts;
        self.log.borrow_mut().push(json!(["fo", null, untouched]));
        match self.answer {
            Some((dev, ino)) => Ok(f::IdStat {
                dev,
                ino,
                birth: None,
            }),
            None => Err(f::IdStatError::Os),
        }
    }
}

fn check_open(header: &Value, row: &Value, i: usize) {
    let root = scratch("g", i);
    let kind = row["k"].as_str();
    let state = if kind == Some("nested") {
        root.join("a").join("b").join("state")
    } else {
        root.join("state")
    };
    match kind {
        Some("file") => std::fs::write(&state, b"x").expect("a file"),
        Some("nested") => {}
        _ => {
            std::fs::create_dir(&state).expect("the state folder");
            if kind == Some("loose") {
                std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o755))
                    .expect("0755");
            }
        }
    }
    let id_path = state.join("machine-id");
    let facts_path = state.join("machine.json");
    for (key, path) in [("pre_id", &id_path), ("pre_facts", &facts_path)] {
        if let Some(t) = row[key].as_str() {
            std::fs::write(path, t).expect("a file");
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o644)).expect("0644");
        }
    }
    let log = RefCell::new(Vec::new());
    let seen_ids = RefCell::new(Vec::new());
    let stat = Scripted {
        answer: match row["fo"].as_str() {
            Some("A") => Some((1, 2)),
            Some("B") => Some((3, 4)),
            None => None,
            Some(other) => panic!("fo {other}"),
        },
        id_path: id_path.clone(),
        facts_path: facts_path.clone(),
        first_facts: row["pre_facts"].as_str().map(str::to_owned),
        log: &log,
        seen_ids: &seen_ids,
    };
    let raises = row["hw"].get("x").is_some();
    let hw = if raises {
        None
    } else {
        text(&neutral(&row["hw"]))
    };
    let host = text(&neutral(&row["host"]));
    let got = open_machine(
        &state,
        &StdFs,
        &stat,
        || {
            log.borrow_mut().push(json!("hw"));
            if raises {
                Err(PyError::UnicodeDecodeError)
            } else {
                Ok(hw.clone())
            }
        },
        || {
            log.borrow_mut().push(json!("host"));
            host.clone()
        },
        &now(header),
        || {
            log.borrow_mut().push(json!("tok"));
            token(header)
        },
    );
    let final_id = if state.is_dir() { read(&id_path) } else { None };
    let mut log = log.into_inner();
    let mut seen = seen_ids.into_inner().into_iter();
    for entry in log.iter_mut() {
        if entry.is_array() {
            let at = seen.next().expect("a stat");
            entry[1] = json!(at.is_some() && at == final_id);
        }
    }
    assert_eq!(Value::from(log), row["log"], "log of {row}");
    match got {
        Ok(ident) => assert_eq!(identity_to_json(&ident), neutral(&row["r"]), "{row}"),
        Err(OpenError::Raised(e)) => assert_eq!(row["x"], e.name(), "{row}"),
        Err(OpenError::Os(e)) => {
            assert_eq!(e.kind(), std::io::ErrorKind::AlreadyExists, "{row}");
            assert_eq!(row["x"], "FileExistsError", "{row}");
        }
        Err(OpenError::Declined(d)) => panic!("declined {d:?} on {row}"),
    }
    if state.is_dir() {
        assert_eq!(final_id.as_deref(), row["id"].as_str(), "id file of {row}");
        assert_eq!(
            read(&facts_path).as_deref(),
            row["facts"].as_str(),
            "facts of {row}"
        );
        let modes = json!([mode(&state), mode(&id_path), mode(&facts_path)]);
        assert_eq!(modes, row["modes"], "modes of {row}");
    } else {
        assert!(row.get("modes").is_none(), "{row}");
    }
    std::fs::remove_dir_all(&root).ok();
}

#[test]
fn machine_open_every_case() {
    let name = "machine_open.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    let mut ledger = Ledger::new("golden_machine_open", name);
    for (i, row) in lines[1..].iter().enumerate() {
        ledger.case(format!("{name} row {i}: {}", row["n"]), || {
            check_open(header, row, i);
        });
    }
    ledger.finish(header["count"].as_u64().expect("count") as usize);
}

/// A real open through `StdIdFileStat`: the facts' id file is the id
/// file's own stat, the birth time present as the reference found it,
/// and a second open keeps the id without a write.
#[test]
fn machine_open_platform_every_case() {
    let name = "machine_open_platform.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    let mut ledger = Ledger::new("golden_machine_open", name);
    for (i, row) in lines[1..].iter().enumerate() {
        ledger.case(format!("{name} row {i}"), || {
            let root = scratch("p", i);
            let state = root.join("state");
            let open = || {
                open_machine(
                    &state,
                    &StdFs,
                    &f::StdIdFileStat,
                    || Ok(Some("H".chars().map(u32::from).collect())),
                    || Some(vec![u32::from('h')]),
                    &now(header),
                    || token(header),
                )
                .expect("an open")
            };
            let first = open();
            let again = open();
            let id_path = state.join("machine-id");
            let raw = id_path.as_os_str().as_encoded_bytes().to_vec();
            let direct = f::file_object(&f::StdIdFileStat, &raw)
                .expect("a stat")
                .expect("an object");
            let (dev, ino, birth) = first.facts.id_file.clone().expect("an id file");
            let spell = |s: &str| s.chars().map(u32::from).collect::<Vec<_>>();
            assert_eq!(row["dev"], dev == spell(&direct.0));
            assert_eq!(row["ino"], ino == spell(&direct.1));
            assert_eq!(row["birth_present"], birth.is_some());
            assert_eq!(row["birth"], birth == direct.2.as_deref().map(spell));
            let kept = again.id == first.id && !again.minted && !again.facts_updated;
            assert_eq!(row["kept"], kept);
            std::fs::remove_dir_all(&root).ok();
        });
    }
    ledger.finish(header["count"].as_u64().expect("count") as usize);
}

/// The machine's own reads answer: on macOS a hardware id (from
/// `gethostuuid`) in the form `str(uuid.UUID(...)).upper()` spells, read
/// once; elsewhere the reference's platform branch (Linux's files) or
/// None, which this machine cannot show.
#[test]
fn the_machine_reads_its_own_hardware_id_once() {
    let machine = StdMachine::default();
    let first = machine.hardware().expect("a read");
    assert_eq!(machine.hardware().expect("a read"), first);
    if cfg!(target_os = "macos") {
        let id = first.expect("macOS has a hardware id");
        let text: String = id.iter().filter_map(|&c| char::from_u32(c)).collect();
        let raw = bettermemory_sys::gethostuuid().expect("gethostuuid");
        assert_eq!(text, f::uuid_text(&raw));
        assert_eq!(text.len(), 36);
        assert_eq!(text, text.to_uppercase());
    }
}
