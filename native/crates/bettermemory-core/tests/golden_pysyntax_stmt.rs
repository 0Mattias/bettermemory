//! Every case of `pysyntax_stmt.jsonl` (gated on the Python release)
//! against `pysyntax::module_expr::parse_module`, row by row as
//! `common/pysyntax_expr.rs` (`check_stmt`) compares them, and the sweep
//! by count and digest. The spelling is the generator's
//! (`tests/contract/gen_pysyntax_stmt.py`); the rows whose trees are
//! deeper than Windows' `ast2obj` converts are
//! `golden_pysyntax_stmt_posix.rs`'s.

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use common::jsonl;
use common::parity::Ledger;
use expr::{check_stmt, stmt_line, Counts};
use serde_json::Value;
use sha2::{Digest, Sha256};

const LEAVES: &[&str] = &[
    "x", "1", "'s'", "f'{x}'", "None", "...", "y.z", "a[0]", "f()",
];
const EXPRS: &[&str] = &[
    "-$",
    "not $",
    "$ + $",
    "$ if $ else $",
    "lambda: $",
    "f($, k=$)",
    "$.a",
    "$[$]",
    "($, $)",
    "[$, *$]",
    "{$: $}",
    "(a := $)",
    "(yield $)",
    "[$ for a in $]",
    "*$",
    "await $",
];
const TARGETS: &[&str] = &[
    "a", "b.c", "d[$]", "e[$:$]", "f($).g", "(h)", "[i, j]", "(k, *l)", "*m",
];
const STMTS: &[&str] = &[
    "$",
    "T = $",
    "T = T = $",
    "T, T = $",
    "T: $",
    "T: $ = $",
    "T += $",
    "T -= $",
    "T **= $",
    "T = yield $",
    "T: $ = yield",
    "T += yield from $",
    "return",
    "return $",
    "return $, *$",
    "raise",
    "raise $",
    "raise $ from $",
    "assert $",
    "assert $, $",
    "del T",
    "del T, T",
    "del (T, [T])",
    "pass",
    "break",
    "continue",
    "global a, b",
    "nonlocal a",
    "import a.b as c",
    "import a, b.c",
    "from . import *",
    "from .a import (b as c, d,)",
    "from a import b, c as d",
    "yield",
    "yield $",
    "yield from $",
    "*T, T = $",
    "[T, *T] = $",
    "(T) = $",
    "(T): $",
    "$; $",
    "T = $; $",
    "$;",
    "if $: pass",
    "match $:",
    "type T = $",
];
const EDITS: &[&str] = &[
    " = ", ":", ";", ",", "*", "del ", "yield ", "(", ")", "import ", " as ", ".", " += ",
    "return ", " from ", "lambda ", "x", "1", " ", "global ", "[", "]",
];

struct Lcg(u64);

impl Lcg {
    fn draw(&mut self) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 33) as usize
    }
}

fn expr(g: &mut Lcg, depth: u32) -> String {
    if depth == 0 || g.draw().is_multiple_of(3) {
        return LEAVES[g.draw() % LEAVES.len()].to_string();
    }
    let template = EXPRS[g.draw() % EXPRS.len()];
    fill(g, template, depth - 1)
}

fn fill(g: &mut Lcg, template: &str, depth: u32) -> String {
    let mut out = String::new();
    for c in template.chars() {
        match c {
            '$' => out.push_str(&expr(g, depth)),
            'T' => {
                if g.draw().is_multiple_of(4) {
                    out.push_str(&expr(g, 1));
                } else {
                    let target = TARGETS[g.draw() % TARGETS.len()];
                    out.push_str(&fill(g, target, 1));
                }
            }
            c => out.push(c),
        }
    }
    out
}

/// The generator's `sweep_cases`.
pub fn sweep_cases(count: usize) -> Vec<String> {
    let mut g = Lcg(0xBB67_AE85_84CA_A73B);
    (0..count)
        .map(|_| {
            let mut out = String::new();
            for _ in 0..1 + g.draw() % 2 {
                let template = STMTS[g.draw() % STMTS.len()];
                let mut body = fill(&mut g, template, 2);
                match g.draw() % 6 {
                    0 => {
                        let at = g.draw() % (body.len() + 1);
                        body.insert_str(at, EDITS[g.draw() % EDITS.len()]);
                    }
                    1 => {
                        let at = g.draw() % body.len();
                        body.remove(at);
                    }
                    _ => {}
                }
                out.push_str(&body);
                out.push('\n');
            }
            out
        })
        .collect()
}

fn sweep(row: &Value) {
    assert_eq!(row["sweep"], "stmt_templates");
    let answers: Vec<String> = sweep_cases(20000)
        .iter()
        .map(|s| stmt_line(&s.chars().map(u32::from).collect::<Vec<_>>()))
        .collect();
    assert_eq!(answers.len() as u64, row["count"].as_u64().unwrap());
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

#[test]
fn pysyntax_stmt_every_case() {
    let name = "pysyntax_stmt.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_stmt_every_case", name);
    let mut n = Counts::default();
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["python"], "3.13.13");
                assert!(row.get("declined").is_none(), "declined: {row}");
                assert_eq!(row["rows"].as_u64().unwrap() as usize, rows.len() - 1);
            });
        } else if row.get("sweep").is_some() {
            ledger.case(label, || sweep(row));
        } else {
            let short: String = row["t"].to_string().chars().take(120).collect();
            ledger.case(format!("{label} {short}"), || check_stmt(row, &mut n));
        }
    }
    if ledger.strict() {
        let Counts {
            accepted,
            refused,
            statements,
            banded,
            band_declined,
            named,
        } = n;
        eprintln!(
            "accepted {accepted}, refused {refused}, compound {statements}, \
             band {banded} ({band_declined} declined), name table {named}"
        );
        assert!(
            accepted > 450 && refused > 400,
            "accepted {accepted}, refused {refused}"
        );
        assert!(statements >= 20, "compound {statements}");
        assert!(
            banded >= 50 && band_declined <= banded,
            "band {banded}, declined {band_declined}"
        );
        assert!(named <= 6, "name table {named}");
    }
    ledger.finish(rows.len());
}
