//! Every case of `checkout_token_write.jsonl`: `write` over a scripted OS,
//! the answer and every call made, in order, as the reference made them.
//!
//! POSIX only: on Windows the reference's logged calls spell the path
//! with backslashes and an errno's name is that host's, so the file is not
//! made there; `checkout_token_write_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;
mod ct_write_script;

use bettermemory_core::checkout_token::write::write;
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use common::{assert_count, jsonl};
use ct_write_script::{expected, recorded, WMachine, WScript, CWD};

const FILE: &str = "checkout_token_write.jsonl";

#[test]
fn every_scripted_write_answers_and_calls_as_the_reference() {
    let lines = jsonl(FILE);
    let rows = &lines[1..];
    assert_eq!(lines[0]["count"].as_u64(), Some(rows.len() as u64));
    let mut checked = 0;
    for row in rows {
        let name = row["n"].as_str().expect("a name");
        let script = WScript::new(row);
        let ctx = Ctx {
            fs: &script,
            flavour: Flavour::Posix,
            cwd: CWD.as_bytes(),
        };
        let mut machine = WMachine { script: &script };
        let git = row.get("git").and_then(|g| g.as_str()).unwrap_or("r/.git");
        let policy = row.get("policy").and_then(|v| v.as_bool()).unwrap_or(true);
        let bare = row.get("bare").and_then(|v| v.as_bool()).unwrap_or(false);
        let answer = write(&ctx, &mut machine, git, policy, bare);
        assert_eq!(recorded(&answer), expected(row), "{name}");
        assert_eq!(script.logged(), row["log"], "{name}: the calls");
        assert_eq!(script.read.left(), 0, "{name}: every read script used");
        checked += 1;
    }
    assert_count(FILE, checked, rows.len());
}
