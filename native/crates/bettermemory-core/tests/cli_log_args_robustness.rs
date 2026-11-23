//! Repeated anchor values stay ordered as the command line grows.

mod timing;

use bettermemory_core::argparse::{ArgumentParser, Parsed, Unfilled};
use bettermemory_core::cli::log_args::add_verify_parser;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn arguments(count: usize) -> Vec<PyStr> {
    let mut argv = vec![cps("verify")];
    argv.extend((0..count).map(|i| cps(&format!("--anchor={i}"))));
    argv
}

#[test]
fn ordered_appends_grow_with_the_argument_list() {
    let mut log = ArgumentParser::new("bettermemory log", None, true);
    log.add_subparsers(Some("log_cmd"), 78).unwrap();
    add_verify_parser(&mut log).unwrap();
    let small = arguments(300);
    let large = arguments(3000);
    let Parsed::Namespace(ns) = log.parse_args(&large, 78, &Unfilled).unwrap() else {
        panic!("valid repeated anchors should parse");
    };
    let (_, PyValue::List(paths)) = ns.iter().find(|(k, _)| k == &cps("anchor")).unwrap() else {
        panic!("expected a list of anchors");
    };
    assert_eq!(paths.len(), 3000);
    for (i, path) in paths.iter().enumerate() {
        assert_eq!(path, &PyValue::str(&i.to_string()));
    }
    timing::grows_within(
        "repeated_anchor_arguments",
        timing::TENFOLD,
        &mut || timing::once(|| log.parse_args(&small, 78, &Unfilled).unwrap()),
        &mut || timing::once(|| log.parse_args(&large, 78, &Unfilled).unwrap()),
    );
}
