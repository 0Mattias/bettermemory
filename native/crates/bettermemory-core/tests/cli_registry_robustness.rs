//! Deferred selections retain their order and use independent parser instances.

mod timing;

use bettermemory_core::argparse::{ArgumentParser, NotPorted, Parsed, Unfilled};
use bettermemory_core::cli::root::build_product_parser;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

#[test]
fn many_deferred_choices_keep_order_and_the_last_concrete_parser() {
    let mut p = ArgumentParser::new("many", None, false);
    p.add_subparsers(Some("cmd"), 78).unwrap();
    let names: Vec<_> = (0..1000).map(|i| format!("entry-{i}")).collect();
    for name in &names {
        p.add_deferred_parser(name, None).unwrap();
    }
    p.add_parser("last", Some("run"), None).unwrap();
    let group = p.subparsers.as_ref().unwrap();
    let choices = p.actions[group.action].choices.as_ref().unwrap();
    assert_eq!(choices.len(), names.len() + 1);
    for (choice, name) in choices.iter().zip(&names) {
        assert_eq!(choice, &cps(name));
    }
    assert_eq!(group.parsers.len(), 1);
    assert_eq!(group.choices_help, [(cps("last"), cps("run"))]);
    for i in [0, 499, 999] {
        assert_eq!(
            p.parse_args(&[cps(&names[i])], 78, &Unfilled),
            Err(NotPorted)
        );
    }
    assert!(matches!(
        p.parse_args(&[cps("last")], 78, &Unfilled),
        Ok(Parsed::Namespace(_))
    ));
}

#[test]
fn repeated_builds_preserve_raw_anchor_values_and_clear_defaults() {
    let value = vec![0xdcff, 0x1f642, 0xd800];
    let mut argv = vec![cps("log"), cps("verify")];
    for _ in 0..256 {
        argv.push(cps("--anchor"));
        argv.push(value.clone());
    }
    for width in [22, 78, 200] {
        let p = build_product_parser("version", width).unwrap();
        let Ok(Parsed::Namespace(ns)) = p.parse_args(&argv, width, &Unfilled) else {
            panic!("valid anchors")
        };
        assert!(ns.contains(&(
            cps("anchor"),
            PyValue::List(vec![PyValue::Str(value.clone()); 256])
        )));
        let Ok(Parsed::Namespace(fresh)) = p.parse_args(&argv[..2], width, &Unfilled) else {
            panic!("bare verify")
        };
        assert!(fresh.contains(&(cps("anchor"), PyValue::None)));
    }
}

#[test]
fn late_selection_scales_with_registered_choices() {
    fn sample(size: usize) -> (ArgumentParser, [Vec<u32>; 1]) {
        let mut p = ArgumentParser::new("sample", None, false);
        p.add_subparsers(None, 78).unwrap();
        for i in 0..size {
            p.add_deferred_parser(&format!("name-{i:04}"), None)
                .unwrap();
        }
        (p, [cps(&format!("name-{:04}", size - 1))])
    }
    let small = sample(100);
    let large = sample(1000);
    let measure = |(p, argv): &(ArgumentParser, [Vec<u32>; 1])| {
        timing::once(|| {
            for _ in 0..64 {
                assert_eq!(
                    p.parse_args(std::hint::black_box(argv), 78, &Unfilled),
                    Err(NotPorted)
                );
            }
        })
    };
    timing::grows_within(
        "deferred_last_choice",
        timing::TENFOLD,
        &mut || measure(&small),
        &mut || measure(&large),
    );
}
