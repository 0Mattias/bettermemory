//! Every case of `checkout_token_remint.jsonl`: `remint` over a scripted
//! OS, the answer and every call made, in order, as the reference made
//! them.
//!
//! POSIX only: on Windows the reference's logged calls spell the path
//! with backslashes and an errno's name is that host's, so the file is not
//! made there; `checkout_token_remint_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;
mod ct_remint_script;

use common::{assert_count, jsonl};
use ct_remint_script::{expected, recorded, run, RScript};

const FILE: &str = "checkout_token_remint.jsonl";

#[test]
fn every_scripted_remint_answers_and_calls_as_the_reference() {
    let lines = jsonl(FILE);
    let rows = &lines[1..];
    assert_eq!(lines[0]["count"].as_u64(), Some(rows.len() as u64));
    let mut checked = 0;
    for row in rows {
        let name = row["n"].as_str().expect("a name");
        let script = RScript::new(row);
        let answer = run(row, &script);
        assert_eq!(recorded(&answer), expected(row), "{name}");
        assert_eq!(script.w.logged(), row["log"], "{name}: the calls");
        assert!(script.spent(), "{name}: every scripted answer used");
        checked += 1;
    }
    assert_count(FILE, checked, rows.len());
}
