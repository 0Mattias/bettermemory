//! Hostile inputs to `argparse`'s subparsers: deep trees, long argv handed
//! to a child, every width. No panic, and time linear in argv.

mod timing;

use bettermemory_core::argparse::{Arg, ArgumentParser, Parsed, Unfilled};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn chain(depth: usize, width: i64) -> ArgumentParser {
    let mut p = ArgumentParser::new("deep", None, true);
    let mut c = &mut p;
    for _ in 0..depth {
        c.add_subparsers(Some("d"), width).unwrap();
        c = c.add_parser("s", None, None).unwrap();
    }
    p
}

#[test]
fn a_deep_tree_answers_below_the_bound() {
    // 3.13.13 answers 165 levels and raises RecursionError at 166; the port
    // answers below MAX_DEPTH (100).
    let p = chain(99, 78);
    let mut argv: Vec<PyStr> = vec![cps("s"); 99];
    assert!(matches!(
        p.parse_args(&argv, 78, &Unfilled).unwrap(),
        Parsed::Namespace(_)
    ));
    argv.push(cps("x"));
    assert!(matches!(
        p.parse_args(&argv, 78, &Unfilled).unwrap(),
        Parsed::Exit(ref o) if o.code == 2
    ));
}

#[test]
fn a_deeper_tree_declines_on_a_small_stack() {
    for depth in [100, 166, 20_000] {
        let got = std::thread::Builder::new()
            .stack_size(2 << 20)
            .spawn(move || {
                let p = chain(depth, 78);
                let argv: Vec<PyStr> = vec![cps("s"); depth];
                let got = p.parse_args(&argv, 78, &Unfilled).is_err();
                // The chain is dropped level by level on this thread.
                std::mem::forget(p);
                got
            })
            .unwrap()
            .join()
            .unwrap();
        assert!(got, "{depth}");
    }
    // The bound is given back: a shallow tree answers afterwards.
    let p = chain(3, 78);
    let got = p.parse_args(&vec![cps("s"); 3], 78, &Unfilled).unwrap();
    assert!(matches!(got, Parsed::Namespace(_)));
}

#[test]
fn every_width_answers() {
    for w in [i64::MIN, -1, 0, 1, 78, i64::MAX] {
        let p = chain(3, w);
        let got = p.parse_args(&[cps("s"), cps("t")], w, &Unfilled).unwrap();
        assert!(matches!(got, Parsed::Exit(ref o) if o.code == 2), "{w}");
    }
}

fn hook_tree() -> ArgumentParser {
    let mut p = ArgumentParser::new("bettermemory", None, true);
    p.add_subparsers(Some("cmd"), 78).unwrap();
    let h = p.add_parser("hook", None, None).unwrap();
    h.add_argument(&["event"], Arg::default()).unwrap();
    h.add_argument(&["--session-id"], Arg::default()).unwrap();
    p.add_argument(&["tail"], Arg::default()).unwrap();
    p
}

fn argv(n: usize) -> Vec<PyStr> {
    let mut v = vec![cps("hook"), cps("stop")];
    v.extend((0..n).map(|i| cps(if i % 3 == 0 { "--session-id" } else { "w" })));
    v
}

#[test]
fn long_argv_to_a_child_is_linear() {
    let p = hook_tree();
    let (small, large) = (argv(20_000), argv(200_000));
    timing::grows_within(
        "argparse_subparsers_long_argv",
        timing::TENFOLD,
        &mut || timing::once(|| p.parse_args(&small, 78, &Unfilled).unwrap()),
        &mut || timing::once(|| p.parse_args(&large, 78, &Unfilled).unwrap()),
    );
}
