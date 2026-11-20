//! Every case of `checkout_token_read.jsonl`: `read` and `_read_at` over
//! a scripted OS, the answer and every call made, in order, as the
//! reference made them.
//!
//! POSIX only: on Windows the reference's logged open spells the path
//! with backslashes and an errno's name is that host's (`WSAELOOP`), so
//! the file is not made there; `checkout_token_read_not_posix.rs` records
//! the gap elsewhere.

#![cfg(unix)]

mod common;
mod ct_read_script;

use bettermemory_core::checkout_token::read::{read, read_at};
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use common::{assert_count, jsonl};
use ct_read_script::{expected, recorded, Script, CWD};
use serde_json::Value;

const FILE: &str = "checkout_token_read.jsonl";

#[test]
fn every_scripted_read_answers_and_calls_as_the_reference() {
    let lines = jsonl(FILE);
    let rows = &lines[1..];
    assert_eq!(lines[0]["count"].as_u64(), Some(rows.len() as u64));
    let mut checked = 0;
    for row in rows {
        let name = row["n"].as_str().expect("a name");
        let script = Script::new(&row["passes"], row["plat"] == "win32");
        let ctx = Ctx {
            fs: &script,
            flavour: Flavour::Posix,
            cwd: CWD.as_bytes(),
        };
        let mut pause = |seconds: f64| script.log.borrow_mut().push(format!("pause {seconds}"));
        let answer = match row.get("git") {
            Some(git) => read(&ctx, git.as_str().expect("text"), &mut pause),
            None => read_at(
                &ctx,
                row["path"].as_str().expect("text").as_bytes(),
                &mut pause,
            ),
        };
        assert_eq!(recorded(&answer), expected(row), "{name}");
        let log: Vec<Value> = script
            .log
            .borrow()
            .iter()
            .map(|s| s.clone().into())
            .collect();
        assert_eq!(Value::Array(log), row["log"], "{name}: the calls");
        assert_eq!(script.left(), 0, "{name}: every script used");
        checked += 1;
    }
    assert_count(FILE, checked, rows.len());
}
