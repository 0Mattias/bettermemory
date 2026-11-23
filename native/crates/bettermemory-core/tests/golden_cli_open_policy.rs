//! The same stat, bundle and reader observations as the shipped CLI.
mod common;

use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::rc::Rc;

use bettermemory_core::argparse::ArgumentParser;
use bettermemory_core::cli::common::{
    open_read_only_with, Diagnostic, ReadOnlyError, ReaderFailure, StatFailure,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use serde_json::{json, Value};

#[derive(Clone)]
struct Source<'a> {
    identity: Rc<()>,
    replacement: Option<Rc<()>>,
    formatting: &'a str,
    message: PyStr,
    trace: &'a RefCell<Vec<&'static str>>,
}

impl Diagnostic for Source<'_> {
    fn format_message(&self) -> Result<PyStr, Self> {
        if self.formatting.ends_with("_custom_format") {
            self.trace.borrow_mut().push("format");
            Ok(cps("formatted"))
        } else if self.formatting.ends_with("_bad_format") {
            self.trace.borrow_mut().push("format");
            Err(Self {
                identity: self.replacement.clone().unwrap(),
                ..self.clone()
            })
        } else {
            self.message()
        }
    }
    fn message(&self) -> Result<PyStr, Self> {
        self.trace.borrow_mut().push("str");
        match &self.replacement {
            Some(other) if self.formatting.ends_with("_bad_str") => Err(Self {
                identity: other.clone(),
                ..self.clone()
            }),
            _ => Ok(self.message.clone()),
        }
    }
}

fn raw_path() -> PathBuf {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        Path::new(std::ffi::OsStr::from_bytes(b"raw-\xff")).to_owned()
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStringExt;
        PathBuf::from(std::ffi::OsString::from_wide(&[114, 97, 119, 45, 0xdcff]))
    }
}

fn check(row: &Value) {
    let parser = ArgumentParser::new("bettermemory log", None, true);
    let phase = row["phase"].as_str().unwrap();
    let behavior = row["behavior"].as_str().unwrap();
    let key = behavior
        .trim_end_matches("_bad_str")
        .trim_end_matches("_custom_format")
        .trim_end_matches("_bad_format");
    let raw = row["raw"].as_bool().unwrap();
    let path = if raw {
        raw_path()
    } else {
        "memory.sqlite".into()
    };
    let trace = RefCell::new(Vec::new());
    let mut message = cps("reason");
    if raw {
        message.extend([45, 0xdcfe]);
    }
    let original = Source {
        identity: Rc::new(()),
        replacement: (behavior.ends_with("_bad_str") || behavior.ends_with("_bad_format"))
            .then(|| Rc::new(())),
        formatting: behavior,
        message,
        trace: &trace,
    };
    let owner = Rc::new(());
    let begin = |name, supplied: &Path| {
        assert!(std::ptr::eq(supplied, path.as_path()));
        trace.borrow_mut().push(name);
    };
    let result = open_read_only_with(
        &parser,
        &path,
        row["code"].as_i64().unwrap() as i32,
        |p| {
            begin("stat", p);
            if phase != "stat" {
                return Ok(true);
            }
            match key {
                "non_regular" => Ok(false),
                "missing" | "not_directory" => Err(StatFailure::Missing),
                "os" => Err(StatFailure::Os(original.clone())),
                _ => Err(StatFailure::Raised(original.clone())),
            }
        },
        |p| {
            begin("bundle", p);
            if phase != "bundle" {
                return Ok(None);
            }
            match key {
                "empty" => Ok(Some(vec![])),
                "refuse" => {
                    let mut message = cps("bundle");
                    if raw {
                        message.extend([45, 0xdcfe]);
                    }
                    Ok(Some(message))
                }
                _ => Err(original.clone()),
            }
        },
        |p| {
            begin("open", p);
            match key {
                "ok" => Ok(owner.clone()),
                "restore" => Err(ReaderFailure::RestoreRunning(original.clone())),
                "value" | "os" | "database" | "missing" | "not_directory" => {
                    Err(ReaderFailure::Unreadable(original.clone()))
                }
                _ => Err(ReaderFailure::Raised(original.clone())),
            }
        },
    );
    let mut stderr = vec![];
    let outcome = match result {
        Ok(actual) => {
            assert!(Rc::ptr_eq(&actual, &owner));
            json!("owner")
        }
        Err(ReadOnlyError::Exit(exit)) => {
            stderr = exit.stderr;
            json!(exit.code)
        }
        Err(error) => {
            let (stage, error) = match error {
                ReadOnlyError::Stat(e) => ("stat", e),
                ReadOnlyError::Bundle(e) => ("bundle", e),
                ReadOnlyError::Open(e) => ("open", e),
                ReadOnlyError::Exit(_) => unreachable!(),
            };
            assert_eq!(stage, phase);
            if Rc::ptr_eq(&error.identity, &original.identity) {
                json!("original")
            } else {
                assert!(Rc::ptr_eq(
                    &error.identity,
                    original.replacement.as_ref().unwrap()
                ));
                json!("format")
            }
        }
    };
    assert_eq!(outcome, row["result"], "{row}");
    assert_eq!(stderr, common::text_field(row, "stderr"), "{row}");
    assert_eq!(json!(trace.into_inner()), row["trace"], "{row}");
}

#[test]
fn supplied_store_policy_matches_python() {
    let rows = common::jsonl("cli_open_policy.jsonl");
    for row in &rows {
        check(row);
    }
}
