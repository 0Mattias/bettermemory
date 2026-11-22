//! Cases a plausible wrong rung 2 fails: the order of `_rung_two`'s four
//! answers, the guard on a later checkout once one is held as a re-mint,
//! the fields `_token_now` replaces, the reads it skips, and what
//! `_token_read` makes of a hung, failed or evicted read.

mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;
#[path = "common/membership_rung.rs"]
mod rung;

use std::time::{Duration, Instant};

use bettermemory_core::checkout_token::read::Fault;
use bettermemory_core::fsdata::{OsError, PyErr, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::membership::rung::{token_file, token_read};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use common::membership_rows::{field, lines, name, same};

fn cases(f: &str) -> Vec<PyValue> {
    lines("membership_rung.jsonl")
        .into_iter()
        .filter(|c| name(field(c, "f")) == f)
        .collect()
}

fn answers(c: &PyValue) -> Vec<PyValue> {
    match field(c, "o") {
        PyValue::List(v) => v.clone(),
        other => panic!("an answer: {other:?}"),
    }
}

fn agrees(c: &PyValue) {
    let (o, calls) = rung::run(c);
    assert!(same(&o, field(c, "o")), "{o:?}");
    assert!(same(&calls, field(c, "calls")), "{calls:?}");
}

/// The checkout to retire into the first comes fourth and the earlier
/// one of a question third: a port that swaps them answers both rows
/// wrong.
#[test]
fn rung_two_keeps_the_order_of_its_four() {
    let all = cases("rung_two");
    let fourth: Vec<_> = all
        .iter()
        .filter(|c| answers(c)[3] != PyValue::None)
        .collect();
    let third: Vec<_> = all
        .iter()
        .filter(|c| answers(c)[2] != PyValue::None)
        .collect();
    assert!(!fourth.is_empty() && !third.is_empty());
    for c in fourth.into_iter().chain(third) {
        let o = answers(c);
        assert_ne!(o[2], o[3]);
        agrees(c);
    }
}

/// Once a checkout is held as a re-mint, a later unproven one is read
/// only where it records the token found: every row where that skip
/// decides is answered as the reference answers it.
#[test]
fn rung_two_skips_a_later_one_without_the_token_found() {
    let mut seen = 0;
    for c in cases("rung_two") {
        if c.get("remint") == Some(&PyValue::Bool(true)) {
            agrees(&c);
            seen += 1;
        }
    }
    assert!(seen > 5);
}

/// A replaced sighting holds no token written and the status `present`
/// whatever it held; one left alone keeps its status.
#[test]
fn token_now_replaces_written_and_status() {
    let mut replaced = 0;
    let mut kept = 0;
    for c in cases("token_now") {
        let o = answers(&c);
        if o[3] == PyValue::Bool(false) {
            assert_eq!(o[1], PyValue::None);
            assert_eq!(name(&o[2]), "present");
            replaced += 1;
        } else if field(&c, "loc").get("st").is_some() {
            kept += 1;
        }
        agrees(&c);
    }
    assert!(replaced > 3 && kept > 0);
}

/// A client's liveness reads no token, nor another machine's sighting.
#[test]
fn token_now_reads_only_here() {
    for c in cases("token_now") {
        let loc = field(&c, "loc");
        let other = loc.get("m").is_some() || c.get("here") == Some(&PyValue::Bool(false));
        if other {
            assert_eq!(field(&c, "calls"), &PyValue::List(Vec::new()));
            agrees(&c);
        }
    }
}

#[test]
fn token_read_of_a_hung_read_is_none_within_the_budget() {
    let board = rung::board();
    let started = Instant::now();
    let got = token_read(&board, "/hung/info/bettermemory", || {
        std::thread::sleep(Duration::from_secs(3));
        Ok(Some(cps("X")))
    });
    assert_eq!(got, Ok(None));
    assert!(started.elapsed() < Duration::from_millis(2500));
}

#[test]
fn token_read_passes_on_what_is_no_oserror() {
    let board = rung::board();
    let os = OsError::new(PyErr::Permission, "EACCES");
    assert_eq!(
        token_read(&board, "/a", move || Err(Fault::Raised(os))),
        Ok(None)
    );
    let nul = OsError {
        class: PyErr::Value,
        errno: None,
        winerror: None,
    };
    assert_eq!(
        token_read(&board, "/b", move || Err(Fault::Raised(nul))),
        Err(Fault::Raised(nul))
    );
    assert_eq!(
        token_read(&board, "/c", || Err(Fault::NotPorted)),
        Err(Fault::NotPorted)
    );
}

#[test]
fn token_file_never_reads_an_evicted_file_and_passes_the_stat_error() {
    let ctx = Ctx {
        fs: &StdFs,
        flavour: rung::HOST,
        cwd: rung::CWD,
    };
    let dir = std::env::temp_dir().join(format!("bm-rung-kill-{}", std::process::id()));
    let info = dir.join(".git").join("info");
    std::fs::create_dir_all(&info).expect("a directory");
    std::fs::write(
        info.join("bettermemory"),
        "bettermemory checkout token v1\n01ARZ3NDEKTSV4RRFFQ69G5FAV\n",
    )
    .expect("a token file");
    let git_dir = dir.join(".git");
    let git_dir = git_dir.to_str().expect("UTF-8");
    let asked = std::cell::Cell::new(0);
    let evicted = |_: &[u8]| {
        asked.set(asked.get() + 1);
        Ok(true)
    };
    assert_eq!(token_file(&ctx, git_dir, &evicted, &mut |_| {}), Ok(None));
    let here = |_: &[u8]| Ok(false);
    assert_eq!(
        token_file(&ctx, git_dir, &here, &mut |_| {}),
        Ok(Some(cps("01ARZ3NDEKTSV4RRFFQ69G5FAV")))
    );
    let gone = OsError::new(PyErr::FileNotFound, "ENOENT");
    let fails = move |_: &[u8]| Err(gone);
    assert!(matches!(
        token_file(&ctx, git_dir, &fails, &mut |_| {}),
        Err(Fault::Raised(_))
    ));
    assert_eq!(asked.get(), 1);
    let _ = std::fs::remove_dir_all(&dir);
}
