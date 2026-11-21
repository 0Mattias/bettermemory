//! Cases a plausible wrong `write` fails: each names the mistake it
//! catches. They run over the scripted OS of the golden file
//! (`ct_write_script`), so POSIX only, as that file is.

#![cfg(unix)]

mod ct_write_script;

use bettermemory_core::checkout_token::read::Fault;
use bettermemory_core::checkout_token::write::{failed, write, TokenWrite, WriteStatus};
use bettermemory_core::fsdata::{Flavour, OsError, PyErr};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use ct_write_script::{WMachine, WScript, CWD};
use serde_json::{json, Value};

const TOKEN: &str = "bettermemory checkout token v1\n0123456789ABCDEFGHJKMNPQRS\n";

fn run(row: Value) -> (Result<TokenWrite, Fault>, Vec<String>) {
    let script = WScript::new(&row);
    let ctx = Ctx {
        fs: &script,
        flavour: Flavour::Posix,
        cwd: CWD.as_bytes(),
    };
    let mut machine = WMachine { script: &script };
    let git = row.get("git").and_then(Value::as_str).unwrap_or("r/.git");
    let policy = row.get("policy").and_then(Value::as_bool).unwrap_or(true);
    let bare = row.get("bare").and_then(Value::as_bool).unwrap_or(false);
    let answer = write(&ctx, &mut machine, git, policy, bare);
    let log = script.read.log.borrow().clone();
    (answer, log)
}

fn absent() -> Value {
    json!({"open": "ENOENT"})
}

fn token() -> Value {
    json!({"reads": [{"t": TOKEN}]})
}

#[test]
fn the_create_fallback_losing_the_race_never_unlinks_the_token_name() {
    let (answer, log) = run(json!({
        "reads": [absent(), token()], "link": "EPERM", "creates": [{}, {"open": "EEXIST"}],
    }));
    assert_eq!(answer.unwrap().status, WriteStatus::Exists);
    assert!(
        !log.contains(&"unlink r/.git/info/bettermemory".to_owned()),
        "{log:?}"
    );
}

#[test]
fn a_fallback_create_that_fails_after_its_own_open_unlinks_the_token_name() {
    let (answer, log) = run(json!({
        "reads": [absent()], "link": "EPERM", "creates": [{}, {"writes": ["ENOSPC"]}],
    }));
    assert_eq!(answer.unwrap().status, WriteStatus::FailedIo);
    assert!(
        log.contains(&"unlink r/.git/info/bettermemory".to_owned()),
        "{log:?}"
    );
}

#[test]
fn the_finally_unlinks_the_temporary_file_even_after_success() {
    let (answer, log) = run(json!({"reads": [absent()]}));
    assert_eq!(answer.unwrap().status, WriteStatus::Written);
    let last = log.last().expect("a call");
    assert!(
        last.starts_with("unlink r/.git/info/bettermemory.tmp-"),
        "{last}"
    );
}

#[test]
fn only_eexist_of_mkdir_is_suppressed() {
    let (answer, _) = run(json!({"reads": [absent()], "mkdir": "EEXIST"}));
    assert_eq!(answer.unwrap().status, WriteStatus::Written);
    let (answer, log) = run(json!({"reads": [absent()], "mkdir": "EISDIR"}));
    let answer = answer.unwrap();
    assert_eq!(answer.status, WriteStatus::FailedIo);
    assert_eq!(answer.detail, "text of EISDIR (EISDIR)");
    assert!(!log.iter().any(|l| l.starts_with("create")), "{log:?}");
}

#[test]
fn readonly_is_exactly_eacces_eperm_erofs() {
    let script = WScript::new(&json!({}));
    for (name, status) in [
        ("EACCES", WriteStatus::FailedReadonly),
        ("EPERM", WriteStatus::FailedReadonly),
        ("EROFS", WriteStatus::FailedReadonly),
        ("ENOSPC", WriteStatus::FailedIo),
        ("EEXIST", WriteStatus::FailedIo),
        ("EIO", WriteStatus::FailedIo),
    ] {
        let got = failed(&script, &ct_write_script::fail(name)).unwrap();
        assert_eq!(got.status, status, "{name}");
        assert_eq!(got.detail, format!("text of {name} ({name})"));
    }
}

#[test]
fn an_error_without_an_errno_name_declines() {
    let script = WScript::new(&json!({}));
    let nameless = OsError {
        class: PyErr::Os,
        errno: None,
        winerror: None,
    };
    assert_eq!(failed(&script, &nameless), Err(Fault::NotPorted));
}

#[test]
fn the_sweep_cutoff_is_strict_and_reads_the_link_itself() {
    let (_, log) = run(json!({
        "reads": [absent()], "mtime": 5000.0,
        "scan": [{"name": "bettermemory.tmp-EDGE", "m": 1400.0},
                 {"name": "bettermemory.tmp-OLD", "m": 1399.5}],
    }));
    assert!(!log.contains(&"unlink r/.git/info/bettermemory.tmp-EDGE".to_owned()));
    assert!(log.contains(&"unlink r/.git/info/bettermemory.tmp-OLD".to_owned()));
}

#[test]
fn no_clock_sweeps_nothing() {
    let (_, log) = run(json!({
        "reads": [absent()], "utime": "EPERM",
        "scan": [{"name": "bettermemory.tmp-OLD", "m": 1.0}],
    }));
    assert!(!log
        .iter()
        .any(|l| l.starts_with("scandir") || l.starts_with("stat r/.git/info")));
}

#[test]
fn race_pauses_double_to_the_cap_and_end_on_the_deadline() {
    let mut reads = vec![absent()];
    reads.extend(std::iter::repeat_n(absent(), 13));
    let (answer, log) = run(json!({"reads": reads, "link": "EEXIST"}));
    let answer = answer.unwrap();
    assert_eq!(answer.detail, "the token another writer placed was removed");
    let pauses: Vec<&str> = log
        .iter()
        .filter_map(|l| l.strip_prefix("pause "))
        .collect();
    assert_eq!(
        &pauses[..7],
        ["0.01", "0.02", "0.04", "0.08", "0.16", "0.2", "0.2"]
    );
    assert_eq!(pauses.len(), 11);
    assert!(pauses[10].starts_with("0.189"), "{pauses:?}");
}

#[test]
fn a_malformed_file_other_than_unfinished_ends_the_race_at_once() {
    let (answer, log) = run(json!({
        "reads": [absent(), {"reads": [{"t": "hello\n"}]}], "link": "EEXIST",
    }));
    assert_eq!(answer.unwrap().status, WriteStatus::Malformed);
    assert!(!log.iter().any(|l| l.starts_with("pause")));
}

#[test]
fn win32_skips_the_owner_and_the_directory_fsync() {
    let (answer, log) = run(json!({"plat": "win32", "owner": 0, "reads": [absent()]}));
    assert!(answer.unwrap().ok());
    assert!(!log.iter().any(|l| l.starts_with("dopen") || l == "geteuid"));
}

#[test]
fn policy_is_asked_before_bare_and_ok_is_written_or_exists() {
    let (answer, log) = run(json!({"policy": false, "bare": true}));
    let answer = answer.unwrap();
    assert_eq!(answer.status, WriteStatus::SkippedPolicy);
    assert!(!answer.ok());
    assert!(log.is_empty());
}

#[test]
fn the_close_error_replaces_the_write_error() {
    let (answer, _) = run(json!({
        "reads": [absent()], "creates": [{"writes": ["ENOSPC"], "close": "EROFS"}],
    }));
    assert_eq!(answer.unwrap().status, WriteStatus::FailedReadonly);
}

#[test]
fn a_nul_raises_instead_of_failing() {
    let (answer, _) = run(json!({"git": "r/\u{0}"}));
    assert!(matches!(answer, Err(Fault::Raised(e)) if e.class == PyErr::Value));
}
