//! Default help through the normal parser, from the shipped command metadata.
mod common;

use bettermemory_core::argparse::{
    help::DefaultHelp, Arg, ArgumentParser, HelpText, Namespace, SpecError,
};
use bettermemory_core::cli::log_args::add_verify_parser;
use bettermemory_core::cli::root::{build_parser, Command, Formatted};
use bettermemory_core::cli::Exit;
use common::argparse::{outcome, text};
use common::parity::Ledger;
use serde_json::Value;

struct Registration<'a> {
    name: &'a str,
    help: &'a str,
    declarations: &'a Value,
}

impl Command for Registration<'_> {
    fn name(&self) -> &str {
        self.name
    }

    fn add_subparser(&self, root: &mut ArgumentParser, width: i64) -> Result<(), SpecError> {
        if self.name != "log" {
            root.add_parser(self.name, Some(self.help), None)?;
            return Ok(());
        }
        let log = root.add_parser(
            "log",
            Some(self.help),
            self.declarations["description"].as_str(),
        )?;
        log.add_subparsers(Some("log_cmd"), width)?;
        for child in self.declarations["log"].as_array().unwrap() {
            let name = child[0].as_str().unwrap();
            if name == "verify" {
                add_verify_parser(log)?;
            } else {
                log.add_parser(name, child[1].as_str(), None)?;
            }
        }
        Ok(())
    }

    fn run(&self, _: &Namespace, _: Option<&Formatted<'_>>, _: Option<&Formatted<'_>>) -> Exit {
        panic!("asking for help dispatched a command")
    }
}

fn alias_parser() -> ArgumentParser {
    let mut p = ArgumentParser::new(
        "archive",
        Some(" %(prog)s\tuses  A\u{a0}B and x\u{2003}y. "),
        true,
    );
    p.add_argument(
        &["-I", "--include"],
        Arg {
            metavar: Some("FILE".into()),
            help: Some("A\u{2003}B,  100%% for %(prog)s.".into()),
            ..Arg::default()
        },
    )
    .unwrap();
    p
}

fn fill(row: &Value) {
    assert_eq!(
        DefaultHelp
            .fill(&text(&row["fill"]), row["width"].as_i64().unwrap())
            .unwrap(),
        text(&row["out"])
    );
}

#[test]
fn registered_help_pages() {
    let name = "argparse_help_python.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("registered_help_pages", name);
    let header = &rows[0];
    let entries = header["root"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| Registration {
            name: row[0].as_str().unwrap(),
            help: row[1].as_str().unwrap(),
            declarations: header,
        })
        .collect::<Vec<_>>();
    let commands = entries
        .iter()
        .map(|e| e as &dyn Command)
        .collect::<Vec<_>>();
    ledger.case("ordered declarations", || {
        assert_eq!(entries.len(), 31);
        assert_eq!(header["log"].as_array().unwrap().len(), 4);
    });
    let p = build_parser("0", &commands, 78).unwrap();
    let alias = alias_parser();
    for (i, row) in rows[1..].iter().enumerate() {
        ledger.case(i + 1, || {
            if row.get("fill").is_some() {
                fill(row);
            } else {
                let selected = if row.get("alias").is_some() {
                    &alias
                } else {
                    &p
                };
                let argv = row["argv"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(text)
                    .collect::<Vec<_>>();
                let got = selected
                    .parse_args(&argv, row["width"].as_i64().unwrap(), &DefaultHelp)
                    .unwrap();
                assert_eq!(outcome(&got), row["exit"]);
            }
        });
    }
    assert_eq!(rows.len(), 60);
    ledger.finish(rows.len());
}

#[test]
fn default_fill_matches_reference() {
    let name = "argparse_help.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("default_fill_matches_reference", name);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(i, || fill(row));
    }
    ledger.finish(rows.len());
}
