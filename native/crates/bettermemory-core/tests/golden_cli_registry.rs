//! The actual root, log, verify and hook declarations observed in Python.

mod common;

use bettermemory_core::argparse::{Action, ArgumentParser, Kind, Unfilled};
use bettermemory_core::cli::root::{build_product_parser, subparser};
use bettermemory_core::pyvalue::PyValue;
use common::argparse::{outcome, spell, text};
use common::parity::Ledger;
use serde_json::{json, Value};

const WIDTH: i64 = 510;

fn action(a: &Action) -> Value {
    let (kind, version) = match &a.kind {
        Kind::Help => ("_HelpAction", None),
        Kind::Version(value) => ("_VersionAction", Some(spell(value))),
        Kind::Parsers => ("_SubParsersAction", None),
        Kind::Store => ("_StoreAction", None),
        Kind::StoreTrue => ("_StoreTrueAction", None),
        Kind::Append => ("_AppendAction", None),
    };
    let default: Vec<_> = a
        .default
        .iter()
        .map(|value| match value {
            PyValue::None => Value::Null,
            PyValue::Bool(b) => json!(b),
            PyValue::Str(s) => spell(s),
            other => panic!("unexpected action default {other:?}"),
        })
        .collect();
    json!({
        "kind": kind,
        "version": version,
        "flags": a.option_strings.iter().map(|name| spell(name)).collect::<Vec<_>>(),
        "dest": a.dest.as_deref().map(spell),
        "default": default,
        "required": a.required,
        "metavar": a.metavar.as_deref().map(spell),
        "help": a.help.as_deref().map(spell),
        "choices": a.choices.as_ref().map(|names| names.iter().map(|n|spell(n)).collect::<Vec<_>>()),
    })
}

fn node(p: &ArgumentParser) -> Value {
    let rows: Vec<_> = p
        .subparsers
        .iter()
        .flat_map(|s| &s.choices_help)
        .map(|(n, h)| json!([spell(n), spell(h)]))
        .collect();
    json!({
        "prog": spell(&p.prog),
        "description": p.description.as_deref().map(spell),
        "actions": p.actions.iter().map(action).collect::<Vec<_>>(),
        "choice_help": rows,
    })
}

#[test]
fn cli_registry_every_case() {
    let name = "cli_registry.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("cli_registry_every_case", name);
    let root = build_product_parser("3.7-registry", WIDTH).unwrap();
    for (index, row) in rows.iter().enumerate() {
        ledger.case(index, || {
            if let Some(path) = row["path"].as_array() {
                let mut parser = &root;
                for name in path {
                    parser = subparser(parser, &text(name)).unwrap();
                }
                assert_eq!(node(parser), row["node"]);
            } else {
                let argv: Vec<_> = row["argv"].as_array().unwrap().iter().map(text).collect();
                let parsed = root.parse_args(&argv, WIDTH, &Unfilled).unwrap();
                assert_eq!(outcome(&parsed), row["outcome"]);
            }
        });
    }
    ledger.finish(rows.len());
}
