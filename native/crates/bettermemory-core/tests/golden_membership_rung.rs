//! Every case of `membership_rung.jsonl` (not gated) against rung 2 of
//! the resolution ladder: `_rung_two`, `_by_object` and `_token_now` over
//! the native `Membership` on the generator's recording store (the answer
//! and the store calls, in order, compared), and `_token_read` over token
//! files written anew in a temporary directory.
//!
//! Off POSIX the row whose token path is a directory is not compared:
//! opening a directory fails there and `StdFs` ports no `strerror` text
//! for Windows, so the read declines (`Fault::NotPorted`) where the
//! reference answers None. `membership_rung_not_posix.rs` records it.

mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;
#[path = "common/membership_rung.rs"]
mod rung;

use std::fs;

use bettermemory_core::pyvalue::PyValue;
use common::membership_rows::{enc, field, lines, name, same};
use common::parity::Ledger;

const FILE: &str = "membership_rung.jsonl";

fn token_case(root: &std::path::Path, i: usize, case: &PyValue) -> PyValue {
    let git_dir = root.join(format!("g{i}")).join(".git");
    if case.get("git_dir") != Some(&PyValue::Bool(false)) {
        fs::create_dir_all(git_dir.join("info")).expect("a git directory");
    }
    let path = git_dir.join("info").join("bettermemory");
    match field(case, "file") {
        PyValue::None => {}
        f if name(f) == "dir" => fs::create_dir_all(&path).expect("a directory"),
        f => fs::write(&path, name(f)).expect("a token file"),
    }
    let evicted = case.get("evicted") == Some(&PyValue::Bool(true));
    let got = rung::read_token(git_dir.to_str().expect("a UTF-8 path"), evicted).expect("no fault");
    enc(&got.map_or(PyValue::None, PyValue::Str))
}

#[test]
fn membership_rung_every_case() {
    let all = lines(FILE);
    let root = std::env::temp_dir().join(format!("bm-rung-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    let mut ledger = Ledger::new("membership_rung_every_case", FILE);
    let mut checks = 0;
    let mut skipped = 0;
    for (i, case) in all.iter().enumerate() {
        let f = name(field(case, "f"));
        let label = format!("case {i} {f}");
        if f == "token_read" {
            if cfg!(not(unix))
                && field(case, "file") != &PyValue::None
                && name(field(case, "file")) == "dir"
            {
                skipped += 1;
                continue;
            }
            let got = token_case(&root, i, case);
            let want = field(case, "o");
            checks += 1;
            assert!(
                !ledger.differs_fatally(format!("{label} o"), same(&got, want)),
                "{label}: got {got:?}, want {want:?}"
            );
            continue;
        }
        let (o, calls) = rung::run(case);
        for (what, got, want) in [
            ("o", &o, field(case, "o")),
            ("calls", &calls, field(case, "calls")),
        ] {
            checks += 1;
            assert!(
                !ledger.differs_fatally(format!("{label} {what}"), same(got, want)),
                "{label} {what}: got {got:?}, want {want:?}"
            );
        }
    }
    let _ = fs::remove_dir_all(&root);
    let reads = all
        .iter()
        .filter(|c| name(field(c, "f")) == "token_read")
        .count();
    assert!(reads > 0 && reads < all.len());
    assert_eq!(checks + skipped, reads + 2 * (all.len() - reads));
    assert_eq!(skipped, usize::from(cfg!(not(unix))));
    ledger.finish(checks);
}
