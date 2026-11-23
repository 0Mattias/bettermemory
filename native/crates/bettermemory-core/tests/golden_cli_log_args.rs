//! `log verify` declarations and parse outcomes from the public Python command tree.

mod common;

use bettermemory_core::argparse::{ArgumentParser, Kind, Namespace, SpecError, Unfilled};
use bettermemory_core::cli::log_args::add_verify_parser;
use bettermemory_core::cli::root::{self, Command, Formatted};
use bettermemory_core::cli::Exit;
use bettermemory_core::unicode::cps;
use common::argparse::{outcome, spell, text};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const WIDTH: i64 = 510;

// The sibling names supply the root and log usage. Every input targets verify.
struct Declaration(&'static str);

impl Command for Declaration {
    fn name(&self) -> &str {
        self.0
    }

    fn add_subparser(&self, parent: &mut ArgumentParser, width: i64) -> Result<(), SpecError> {
        let command = parent.add_parser(self.0, None, None)?;
        if self.0 == "log" {
            command.add_subparsers(Some("log_cmd"), width)?;
            add_verify_parser(command)?;
            for name in ["export", "anchor", "accept-history"] {
                command.add_parser(name, None, None)?;
            }
        }
        Ok(())
    }

    fn run(&self, _: &Namespace, _: Option<&Formatted<'_>>, _: Option<&Formatted<'_>>) -> Exit {
        unreachable!("the argument fixture only parses")
    }
}

#[test]
fn cli_log_args_every_case() {
    let declarations = root::COMMANDS.map(Declaration);
    let commands: Vec<_> = declarations.iter().map(|d| d as &dyn Command).collect();
    let root = root::build_parser("fixture", &commands, WIDTH).unwrap();
    let log = root::subparser(&root, &cps("log")).unwrap();
    let verify = root::subparser(log, &cps("verify")).unwrap();
    let name = "cli_log_args.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("cli_log_args_every_case", name);
    ledger.case("declaration", || {
        let header = &rows[0];
        assert_eq!(verify.description, Some(text(&header["description"])));
        let choices = &log.subparsers.as_ref().unwrap().choices_help;
        assert_eq!(choices[0], (cps("verify"), text(&header["choice_help"])));
        let options: Vec<_> = verify
            .actions
            .iter()
            .filter(|a| a.kind != Kind::Help)
            .map(|a| {
                let names: Vec<_> = a.option_strings.iter().map(|n| spell(n)).collect();
                json!([
                    names,
                    a.dest.as_deref().map(spell),
                    a.metavar.as_deref().map(spell),
                    a.help.as_deref().map(spell)
                ])
            })
            .collect();
        assert_eq!(json!(options), header["options"]);
    });
    let (sweep, cases) = rows[1..].split_last().unwrap();
    for (i, row) in cases.iter().enumerate() {
        ledger.case(i, || {
            let argv: Vec<_> = row["argv"].as_array().unwrap().iter().map(text).collect();
            let parsed = root.parse_args(&argv, WIDTH, &Unfilled).unwrap();
            assert_eq!(outcome(&parsed), row["outcome"], "{row}");
        });
    }
    ledger.case("three consecutive tokens", || {
        let tokens: Vec<_> = sweep["tokens"]
            .as_array()
            .unwrap()
            .iter()
            .map(text)
            .collect();
        let mut digest = Sha256::new();
        let mut count = 0;
        for first in &tokens {
            for second in &tokens {
                for third in &tokens {
                    let argv = [
                        cps("log"),
                        cps("verify"),
                        first.clone(),
                        second.clone(),
                        third.clone(),
                    ];
                    let parsed = root.parse_args(&argv, WIDTH, &Unfilled).unwrap();
                    digest.update(serde_json::to_vec(&outcome(&parsed)).unwrap());
                    digest.update(b"\n");
                    count += 1;
                }
            }
        }
        assert_eq!(json!(count), sweep["count"]);
        assert_eq!(
            Value::from(common::hex(&digest.finalize())),
            sweep["sha256"]
        );
    });
    ledger.finish(rows.len());
}
