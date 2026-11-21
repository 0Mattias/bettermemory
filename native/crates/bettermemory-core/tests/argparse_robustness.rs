//! Hostile inputs to `argparse`: very many and very long arguments, every
//! width an `i64` holds, surrogates. No panic, and time linear in argv.

mod timing;

use bettermemory_core::argparse::{Arg, ArgumentParser, Kind, Parsed, Unfilled};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn hook_like() -> ArgumentParser {
    let mut p = ArgumentParser::new("bettermemory audit-turn", Some("d"), true);
    p.add_argument(&["--transcript-path"], Arg::default())
        .unwrap();
    p.add_argument(&["--session-id"], Arg::default()).unwrap();
    let flag = Arg {
        kind: Some(Kind::StoreTrue),
        ..Arg::default()
    };
    p.add_argument(&["--quiet"], flag.clone()).unwrap();
    p.add_argument(&["--dry-run"], flag).unwrap();
    p
}

#[test]
fn every_width_answers() {
    let p = hook_like();
    for w in [i64::MIN, i64::MIN + 1, -1, 0, 1, 11, 78, i64::MAX] {
        let got = p.parse_args(&[cps("--bogus")], w, &Unfilled).unwrap();
        assert!(matches!(got, Parsed::Exit(ref o) if o.code == 2), "{w}");
    }
}

#[test]
fn long_and_odd_arguments_answer() {
    let p = hook_like();
    let long: PyStr = std::iter::once(0x2D)
        .chain(std::iter::repeat_n(0x2D, 100_000))
        .collect();
    let mut odd = cps("--session-id=");
    odd.extend([0xD800, 0xDFFF, 0x10FFFF, 0]);
    for arg in [
        long,
        odd,
        vec![0x2D; 2],
        vec![],
        cps("-"),
        cps("--="),
        cps("-="),
    ] {
        let _ = p.parse_args(&[arg.clone(), arg], 78, &Unfilled).unwrap();
    }
}

fn argv(n: usize) -> Vec<PyStr> {
    (0..n)
        .map(|i| cps(if i % 2 == 0 { "--session-id" } else { "s" }))
        .chain([cps("--quiet")])
        .collect()
}

#[test]
fn many_arguments_are_linear() {
    let p = hook_like();
    let (small, large) = (argv(20_000), argv(200_000));
    timing::grows_within(
        "argparse_many_options",
        timing::TENFOLD,
        &mut || timing::once(|| p.parse_args(&small, 78, &Unfilled).unwrap()),
        &mut || timing::once(|| p.parse_args(&large, 78, &Unfilled).unwrap()),
    );
}

#[test]
fn many_positional_words_are_linear() {
    let p = hook_like();
    let words = |n: usize| -> Vec<PyStr> { (0..n).map(|_| cps("w")).collect() };
    let (small, large) = (words(20_000), words(200_000));
    timing::grows_within(
        "argparse_many_extras",
        timing::TENFOLD,
        &mut || timing::once(|| p.parse_args(&small, 78, &Unfilled).unwrap()),
        &mut || timing::once(|| p.parse_args(&large, 78, &Unfilled).unwrap()),
    );
}
