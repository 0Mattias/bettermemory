//! Every case of `pysyntax_compound.jsonl` (gated on the Python release)
//! against `pysyntax::module_expr::parse_module_blocks`, row by row as
//! `common/pysyntax_expr.rs` (`check_block`) compares them, and the sweep
//! by count and digest; then every case of `pysyntax_stmt.jsonl` read again
//! by the same entry (`check_stmt_as_block`). The spelling is the
//! generator's (`tests/contract/gen_pysyntax_compound.py`); the rows whose
//! trees are deeper than Windows' `ast2obj` converts are
//! `golden_pysyntax_compound_posix.rs`'s.

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use common::jsonl;
use common::parity::Ledger;
use expr::{block_line, check_block, check_stmt_as_block, Counts};
use serde_json::Value;
use sha2::{Digest, Sha256};

const LEAVES: &[&str] = &["x", "1", "'s'", "None", "y.z", "a[0]", "f()"];
const EXPRS: &[&str] = &[
    "-$",
    "not $",
    "$ + $",
    "$ if $ else $",
    "lambda: $",
    "f($)",
    "($, $)",
    "[$, *$]",
    "(a := $)",
    "(yield $)",
];
const TARGETS: &[&str] = &["a", "b.c", "d[$]", "(e)", "[f, g]", "(h, *i)", "*j", "$"];
const HEADS: &[&str] = &[
    "if $:",
    "while $:",
    "for T in $:",
    "for T, T in $, $:",
    "for *T, T in *$, $:",
    "with $:",
    "with $ as T:",
    "with $, $ as T:",
    "with ($, $ as T):",
    "with ($ as T,):",
    "async for T in $:",
    "async with $ as T, $:",
    "if $ if $ else $:",
    "while (a := $):",
];
const SIMPLE: &[&str] = &[
    "$",
    "T = $",
    "T += $",
    "T: $",
    "pass",
    "break",
    "return $",
    "del T",
    "import a.b as c",
    "x; T = $",
    "global a",
    "yield $",
    "def f(): pass",
    "match $:",
    "type T = $",
];
const EDITS: &[&str] = &[
    ":", ";", ",", "*", "(", ")", " as ", " in ", "for ", "if ", "else", "elif ", "with ",
    "while ", "\n", "    ", "x", " ",
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
                let target = TARGETS[g.draw() % TARGETS.len()];
                out.push_str(&fill(g, target, 1));
            }
            c => out.push(c),
        }
    }
    out
}

fn block(g: &mut Lcg, depth: u32, indent: &str) -> Vec<String> {
    let mut lines = Vec::new();
    for _ in 0..1 + g.draw() % 2 {
        lines.extend(statement(g, depth, indent));
    }
    lines
}

fn statement(g: &mut Lcg, depth: u32, indent: &str) -> Vec<String> {
    if depth == 3 || g.draw().is_multiple_of(2) {
        let template = SIMPLE[g.draw() % SIMPLE.len()];
        return vec![format!("{indent}{}", fill(g, template, 2))];
    }
    let template = HEADS[g.draw() % HEADS.len()];
    let head = fill(g, template, 2);
    let mut lines = branch(g, &head, depth, indent);
    if ["if", "while", "for", "async for"]
        .iter()
        .any(|h| head.starts_with(h))
    {
        let follow = g.draw() % 4;
        if follow == 0 && head.starts_with("if") {
            let elif = format!("elif {}:", expr(g, 2));
            lines.extend(branch(g, &elif, depth, indent));
        }
        if follow <= 1 {
            lines.extend(branch(g, "else:", depth, indent));
        }
    }
    lines
}

fn branch(g: &mut Lcg, head: &str, depth: u32, indent: &str) -> Vec<String> {
    if g.draw().is_multiple_of(3) {
        let template = SIMPLE[g.draw() % SIMPLE.len()];
        return vec![format!("{indent}{head} {}", fill(g, template, 2))];
    }
    let mut lines = vec![format!("{indent}{head}")];
    lines.extend(block(g, depth + 1, &format!("{indent}    ")));
    lines
}

/// The generator's `sweep_cases`.
pub fn sweep_cases(count: usize) -> Vec<String> {
    let mut g = Lcg(0x3C6E_F372_FE94_F82B);
    (0..count)
        .map(|_| {
            let mut lines = block(&mut g, 0, "");
            let edit = g.draw() % 9;
            if edit == 2 {
                let at = g.draw() % lines.len();
                lines[at] = format!("    {}", lines[at]);
            } else if edit == 3 {
                let at = g.draw() % lines.len();
                if let Some(rest) = lines[at].strip_prefix("    ") {
                    lines[at] = rest.to_string();
                }
            }
            let mut body = lines.join("\n") + "\n";
            if edit == 0 {
                let at = g.draw() % (body.len() + 1);
                body.insert_str(at, EDITS[g.draw() % EDITS.len()]);
            } else if edit == 1 {
                let at = g.draw() % body.len();
                body.remove(at);
            }
            body
        })
        .collect()
}

fn sweep(row: &Value) {
    assert_eq!(row["sweep"], "block_templates");
    let answers: Vec<String> = sweep_cases(20000)
        .iter()
        .map(|s| block_line(&s.chars().map(u32::from).collect::<Vec<_>>()))
        .collect();
    assert_eq!(answers.len() as u64, row["count"].as_u64().unwrap());
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

fn header(row: &Value, rows: usize) {
    assert_eq!(row["python"], "3.13.13");
    assert!(row.get("declined").is_none(), "declined: {row}");
    assert_eq!(row["rows"].as_u64().unwrap() as usize, rows - 1);
}

#[test]
fn pysyntax_compound_every_case() {
    let name = "pysyntax_compound.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_compound_every_case", name);
    let mut n = Counts::default();
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || header(row, rows.len()));
        } else if row.get("sweep").is_some() {
            ledger.case(label, || sweep(row));
        } else {
            let short: String = row["t"].to_string().chars().take(120).collect();
            ledger.case(format!("{label} {short}"), || check_block(row, &mut n));
        }
    }
    if ledger.strict() {
        let Counts {
            accepted,
            refused,
            statements,
            banded,
            band_declined,
            ..
        } = n;
        eprintln!(
            "accepted {accepted}, refused {refused}, declined {statements}, \
             band {banded} ({band_declined} declined)"
        );
        assert!(
            accepted > 150 && refused > 200,
            "accepted {accepted}, refused {refused}"
        );
        assert!(statements >= 4, "declined {statements}");
        assert!(
            banded >= 60 && band_declined <= banded,
            "band {banded}, declined {band_declined}"
        );
    }
    ledger.finish(rows.len());
}

/// The simple statements' fixture read by the block entry: what
/// `parse_module` reads is read alike, and a compound statement it
/// declines is read or declined as this unit's scope says.
#[test]
fn pysyntax_stmt_read_as_blocks() {
    let name = "pysyntax_stmt.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_stmt_read_as_blocks", name);
    let mut n = Counts::default();
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || header(row, rows.len()));
        } else if row.get("sweep").is_some() {
            // The sweep's digest holds `parse_module`'s declines.
            ledger.case(label, || assert_eq!(row["sweep"], "stmt_templates"));
        } else {
            let short: String = row["t"].to_string().chars().take(120).collect();
            ledger.case(format!("{label} {short}"), || {
                check_stmt_as_block(row, &mut n)
            });
        }
    }
    if ledger.strict() {
        assert!(n.accepted > 450 && n.statements >= 20);
    }
    ledger.finish(rows.len());
}
