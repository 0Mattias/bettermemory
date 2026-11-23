//! Column transitions, caller-supplied metadata and public parser outcomes.
use bettermemory_core::argparse::{
    help::DefaultHelp, Arg, ArgumentParser, HelpText, Kind, Parsed, Unfilled,
};
use bettermemory_core::unicode::cps;

fn printed(parser: &ArgumentParser, argv: &[&str], width: i64) -> (i32, String, String) {
    let args = argv.iter().map(|s| cps(s)).collect::<Vec<_>>();
    let Parsed::Exit(out) = parser.parse_args(&args, width, &DefaultHelp).unwrap() else {
        panic!("expected an exit")
    };
    let text = |v: Vec<u32>| v.into_iter().map(|c| char::from_u32(c).unwrap()).collect();
    (out.code, text(out.stdout), text(out.stderr))
}

#[test]
fn registration_order_and_two_indentation_levels() {
    let mut p = ArgumentParser::new("db", None, false);
    p.add_argument(
        &["--v"],
        Arg {
            kind: Some(Kind::StoreTrue),
            help: Some("enabled".into()),
            ..Arg::default()
        },
    )
    .unwrap();
    p.add_subparsers(Some("cmd"), 78).unwrap();
    p.add_parser("show", Some("print it"), None).unwrap();
    p.add_parser("other", None, None).unwrap();
    p.add_parser("s", Some(""), None).unwrap();
    let expect="usage: db [--v] {show,other,s} ...\n\npositional arguments:\n  {show,other,s}\n    show          print it\n    s\n\noptions:\n  --v             enabled\n";
    assert_eq!(DefaultHelp.format_help(&p, 78), Some(cps(expect)));
    assert_eq!(DefaultHelp.format_help(&p, i64::MAX), Some(cps(expect)));
    assert!(DefaultHelp.format_help(&p, i64::MIN).is_some());
    assert_eq!(
        DefaultHelp.format_help(&ArgumentParser::new("empty", None, false), 0),
        Some(cps("usage: empty\n"))
    );
}

#[test]
fn help_aliases_and_value_errors_use_parser_precedence() {
    let mut p = ArgumentParser::new("alpha", None, true);
    p.add_argument(
        &["-o", "--output"],
        Arg {
            metavar: Some("FILE".into()),
            help: Some("two words".into()),
            ..Arg::default()
        },
    )
    .unwrap();
    let (code, out, err) = printed(&p, &["--help", "--output"], 78);
    assert_eq!(code, 0);
    assert!(err.is_empty());
    assert!(out.contains("-o, --output FILE"));
    let (code, out, err) = printed(&p, &["--output", "--help"], 78);
    assert_eq!(code, 2);
    assert!(out.is_empty());
    assert!(err.contains("expected one argument"));
    assert!(p.parse_args(&[cps("--help")], 78, &Unfilled).is_err());
    p.actions.last_mut().unwrap().help = Some(cps("%(unsupported)s"));
    assert!(p.parse_args(&[cps("--help")], 78, &DefaultHelp).is_err());
}

#[test]
fn version_fill_preserves_the_parser_contract_and_raw_codepoints() {
    let mut p = ArgumentParser::new("p", Some(""), true);
    p.add_argument(
        &["--version"],
        Arg {
            kind: Some(Kind::Version(cps("up-to-date\ttool ---- version"))),
            ..Arg::default()
        },
    )
    .unwrap();
    assert_eq!(
        printed(&p, &["--version", "--help"], 12),
        (0, "up-to-date\ntool ----\nversion\n".into(), String::new())
    );
    let mut raw = vec![0xdc80];
    raw.extend(cps("xx YYYY"));
    assert_eq!(
        DefaultHelp.fill(&raw, 3),
        Some([&raw[..3], &cps("\nYYY\nY")].concat())
    );
    assert_eq!(DefaultHelp.fill(&raw, 0), None);
}

#[test]
fn expansion_that_cannot_produce_a_line_declines() {
    let mut parser = ArgumentParser::new("  ", None, false);
    parser
        .add_argument(
            &["--show"],
            Arg {
                kind: Some(Kind::StoreTrue),
                help: Some("%(prog)s".into()),
                ..Arg::default()
            },
        )
        .unwrap();
    assert!(DefaultHelp.format_help(&parser, 78).is_none());
}

#[test]
fn long_word_padding_and_description_width() {
    // A run that starts with dashes needs a non-dash before a hyphen can
    // become the preferred cut within a long word.
    assert_eq!(DefaultHelp.fill(&cps("--xyz"), 3), Some(cps("--x\nyz")));

    // A truthy help string that strips to nothing retains the header's
    // column padding.
    let mut parser = ArgumentParser::new("p", None, false);
    parser
        .add_argument(
            &["-x"],
            Arg {
                kind: Some(Kind::StoreTrue),
                help: Some(" ".into()),
                ..Arg::default()
            },
        )
        .unwrap();
    assert_eq!(
        DefaultHelp.format_help(&parser, 78),
        Some(cps("usage: p [-x]\n\noptions:\n  -x  \n"))
    );

    let narrow = ArgumentParser::new("p", Some("abcdefghijkx"), false);
    assert_eq!(
        DefaultHelp.format_help(&narrow, 5),
        Some(cps("usage: p\n\nabcdefghijk\nx\n"))
    );
}
