//! Cases a plausible wrong port of `review`'s tables fails: the tau cell
//! and its guards (nesting, out-of-order drops, a panic, a second thread),
//! the length test of `related_text` in code points after the strip, the
//! sentence starts, the set's order and dedup, and the roster line.
//!
//! Every test that reads the thresholds holds a guard first: the tests of
//! this file run on threads of one process and share the cell.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::Duration;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::review::*;
use bettermemory_core::unicode;

fn pair() -> Option<(f64, f64)> {
    current_tau().map(|t| (t.r#unsafe, t.secret))
}

#[test]
fn nested_guards_dropped_out_of_order_restore_what_each_saved() {
    let _base = tau_override(0.5, None);
    let outer = tau_override(0.1, None);
    let inner = tau_override(0.2, Some(0.4));
    assert_eq!(pair(), Some((0.2, 0.4)));
    // The reference's `finally` puts back the pair each block found, so
    // the outer block's exit first and the inner one's after leaves the
    // pair the inner block found: the outer override's.
    drop(outer);
    assert_eq!(pair(), Some((0.5, 0.5)));
    drop(inner);
    assert_eq!(pair(), Some((0.1, 0.1)));
}

#[test]
fn nested_guards_in_order_restore_the_base() {
    let _base = tau_override(0.6, None);
    {
        let o = tau_override(0.7, Some(0.8));
        let t = o.tau().expect("set");
        assert_eq!((t.r#unsafe, t.secret), (0.7, 0.8));
        {
            let _u = tau_unset();
            assert_eq!(current_tau(), None);
        }
        assert_eq!(pair(), Some((0.7, 0.8)));
    }
    assert_eq!(pair(), Some((0.6, 0.6)));
}

#[test]
fn either_threshold_unset_is_no_tau() {
    let _base = tau_patched(None, Some(0.3));
    assert_eq!(current_tau(), None);
    let _two = tau_patched(Some(0.3), None);
    assert_eq!(current_tau(), None);
    let _three = tau_patched(Some(0.0), Some(1.0));
    assert_eq!(pair(), Some((0.0, 1.0)));
}

#[test]
fn a_panic_inside_an_override_restores_the_pair() {
    let _base = tau_override(0.55, None);
    let caught = std::panic::catch_unwind(|| {
        let _g = tau_override(0.9, None);
        panic!("inside the block");
    });
    assert!(caught.is_err());
    assert_eq!(pair(), Some((0.55, 0.55)));
}

#[test]
fn a_second_threads_override_waits_for_the_first() {
    let held = tau_override(0.11, None);
    let done = Arc::new(AtomicBool::new(false));
    let (tx, rx) = mpsc::channel();
    let flag = Arc::clone(&done);
    let other = thread::spawn(move || {
        tx.send(()).expect("send");
        let _g = tau_override(0.22, None);
        flag.store(true, Ordering::SeqCst);
        pair()
    });
    rx.recv().expect("started");
    thread::sleep(Duration::from_millis(150));
    assert!(
        !done.load(Ordering::SeqCst),
        "the second override did not wait"
    );
    assert_eq!(pair(), Some((0.11, 0.11)));
    drop(held);
    assert_eq!(other.join().expect("join"), Some((0.22, 0.22)));
}

#[test]
fn length_is_in_code_points_after_the_strip() {
    let whole: PyStr = std::iter::repeat_n(0xE9, 600).collect();
    assert_eq!(related_text(&[], &whole), whole);
    let mut padded = unicode::cps("  \n");
    padded.extend(std::iter::repeat_n(u32::from(b'x'), 600));
    padded.extend(unicode::cps("\t "));
    assert_eq!(related_text(&[], &padded).len(), 600);
    let over: PyStr = std::iter::repeat_n(u32::from(b'x'), 601).collect();
    assert!(related_text(&[], &over).len() <= 600);
}

#[test]
fn a_dot_inside_a_word_is_no_sentence_break() {
    let mut body = unicode::cps(&"word ".repeat(130));
    body.extend(unicode::cps("end.ruff here"));
    // No break before "ruff": the window starts at the head.
    let got = related_text(&unicode::cps("ruff"), &body);
    assert_eq!(&got[..5], &unicode::cps("word ")[..]);
}

#[test]
fn the_window_starts_past_the_whole_break() {
    let mut body = unicode::cps(&"word ".repeat(130));
    body.extend(unicode::cps("end!?  \t ruff here"));
    let got = related_text(&unicode::cps("ruff"), &body);
    assert_eq!(got, unicode::cps("...ruff here"));
}

#[test]
fn sentences_keep_first_order_and_drop_stopword_pieces() {
    let got = write_sentences(&unicode::cps("b x. a y.\nthe of.\n\nb x. a y."));
    assert_eq!(got, vec![unicode::cps("b x."), unicode::cps("a y.")]);
}

fn entry(i: &str) -> RelatedEntry {
    RelatedEntry {
        id: unicode::cps(i),
        scopes: Vec::new(),
        updated: String::new(),
        claim_sized: false,
        text: unicode::cps(i),
    }
}

#[test]
fn only_keeps_review_order_and_plus_dedups() {
    let set = RelatedSet {
        entries: vec![entry("a"), entry("b"), entry("c")],
        report: Some(CueReport {
            supersedes: vec![unicode::cps("z")],
            conflicts: vec![unicode::cps("c"), unicode::cps("q")],
        }),
    };
    let wanted = [unicode::cps("c"), unicode::cps("a")];
    let only = set.only(wanted.iter().map(Vec::as_slice));
    assert_eq!(only.ids(), vec![unicode::cps("a"), unicode::cps("c")]);
    assert_eq!(only.report, set.report);
    let plus = set.plus(vec![entry("c"), entry("d"), entry("d")]);
    assert_eq!(plus.ids().len(), 4);
    assert_eq!(set.cue_supersedes(), vec![unicode::cps("z")]);
    assert_eq!(set.cue_conflicts().len(), 2);
    let off = RelatedSet {
        report: None,
        ..set
    };
    assert!(off.cue_supersedes().is_empty() && off.cue_conflicts().is_empty());
}

#[test]
fn roster_line_and_label_choices() {
    let line = RosterLine {
        id: unicode::cps("i"),
        updated: unicode::cps("u"),
        text: unicode::cps("t"),
    };
    assert_eq!(line.to_line(), unicode::cps("i (u) t"));
    let built: Vec<String> = LABELS.iter().map(|l| format!("\"{l}\"")).collect();
    assert_eq!(LABEL_CHOICES, built.join(" | "));
    assert_ne!(questions_sha256(), constants_sha256());
}

#[test]
fn an_inner_guards_drop_keeps_the_lock_while_the_outer_lives() {
    let outer = tau_override(0.7, None);
    drop(tau_override(0.75, None));
    assert_eq!(pair(), Some((0.7, 0.7)));
    let entered = Arc::new(AtomicBool::new(false));
    let (tx, rx) = mpsc::channel();
    let flag = Arc::clone(&entered);
    let other = thread::spawn(move || {
        tx.send(()).expect("send");
        let _g = tau_override(0.9, None);
        flag.store(true, Ordering::SeqCst);
    });
    rx.recv().expect("started");
    thread::sleep(Duration::from_millis(150));
    assert!(
        !entered.load(Ordering::SeqCst),
        "the inner guard's drop freed the lock the outer guard holds"
    );
    assert_eq!(pair(), Some((0.7, 0.7)));
    drop(outer);
    other.join().expect("join");
}

#[test]
fn the_guard_yields_the_tau_it_set() {
    let g = tau_override(0.4, None);
    assert_eq!(g.tau(), current_tau());
    let p = tau_patched(Some(0.1), None);
    assert_eq!(p.tau(), None);
    let u = tau_unset();
    assert_eq!(u.tau(), None);
}
