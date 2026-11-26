//! Every case of `pysyntax_expr.jsonl` (gated on the Python release)
//! against `pysyntax::module_expr::parse_expression_module`, row by row as
//! `common/pysyntax_expr.rs` compares them; the rows whose answer follows
//! the C recursion limit are in `pysyntax_expr_posix.jsonl`
//! (`golden_pysyntax_expr_posix`). The two sweeps are checked by count and
//! digest. The spelling is the generator's
//! (`tests/contract/gen_pysyntax_expr.py`).

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use common::jsonl;
use common::parity::Ledger;
use expr::{answer_line, check, Counts};
use serde_json::Value;
use sha2::{Digest, Sha256};

const LEAVES: &[&str] = &[
    "x",
    "y",
    "1",
    "2.5",
    "3j",
    "'s'",
    "b'b'",
    "True",
    "None",
    "...",
    "f'a{x}'",
    "f'{x!r:>{y}}'",
    "f'{x=}'",
    "u's' 't'",
];
const TEMPLATES: &[&str] = &[
    "-$",
    "not $",
    "~$",
    "await $",
    "$ + $",
    "$ * $",
    "$ ** $",
    "$ // $",
    "$ @ $",
    "$ | $",
    "$ << $",
    "$ and $",
    "$ or $",
    "$ < $ <= $",
    "$ not in $",
    "$ is not $",
    "$ == $",
    "$ if $ else $",
    "lambda: $",
    "lambda a, *b, c=$, **d: $",
    "lambda a=$, /, b=$: $",
    "f($)",
    "f($, *$, k=$, **$)",
    "f($ for a in $)",
    "$.a",
    "$($)",
    "$[$]",
    "$[$:$:$]",
    "$[$, *$]",
    "($, $)",
    "($,)",
    "[$, *$]",
    "{$: $, **$}",
    "{$, $}",
    "[$ for a in $ if $]",
    "($ for a, *b in $)",
    "{a: $ for a in $}",
    "{$ async for a in $}",
    "(a := $)",
    "(yield $)",
    "(yield from $)",
    "(yield)",
    "f'{$}'",
    "f'{$:{$}}'",
    "f'{$!s}'",
    "($)",
    "[$]",
    "$ if $ else lambda: $",
    "b'x' $",
    "'a' $",
    "[a for a.b[$] in $]",
];
const EDITS: &[&str] = &[
    ",", ":", "*", "**", "=", ":=", "(", ")", "[", "]", " for ", " in ", " if ", "not ", "lambda ",
    "yield ", "<>", ".", "!r", "{", "}", " ", "await ", "x", "1", "-", "else ",
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
    if depth == 0 || g.draw().is_multiple_of(4) {
        return LEAVES[g.draw() % LEAVES.len()].to_string();
    }
    let template = TEMPLATES[g.draw() % TEMPLATES.len()];
    template
        .chars()
        .map(|c| {
            if c == '$' {
                expr(g, depth - 1)
            } else {
                c.to_string()
            }
        })
        .collect()
}

/// The generator's `sweep_cases`.
pub fn sweep_cases(count: usize) -> Vec<String> {
    let mut g = Lcg(0x2545_F491_4F6C_DD1D);
    (0..count)
        .map(|_| {
            let mut body = expr(&mut g, 3);
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
            format!("({body})\n")
        })
        .collect()
}

const OPENERS: &[&str] = &["f'", "f\"", "f'''", "rf'", "F\"\"\"", "fr'"];
const PIECES: &[&str] = &[
    "a",
    "a b",
    "{x}",
    "{x}",
    "{{",
    "}}",
    "{x!r}",
    "{x:>3}",
    "{x:{y}}",
    "{x=}",
    "{",
    "}",
    "{x",
    ":",
    "!",
    "!r",
    "=",
    "!=",
    ":=",
    "x",
    "(",
    ")",
    "[",
    "]",
    "'",
    "\"",
    "\\",
    "\\x41",
    "\\x",
    "\\{",
    "\n",
    " ",
    "# c",
    "{'a'}",
    "{f'{x}'}",
    "{f\"{x}\"}",
    "{lambda: 1}",
    "\\\n",
];
const TAILS: &[&str] = &["\n", " y\n", "", "\nz\n", " + f'{x}'\n", ")\n"];

/// `gen_pysyntax_fstring.sweep_cases`.
pub fn fstring_cases(count: usize) -> Vec<String> {
    let mut g = Lcg(0x9E37_79B9_7F4A_7C15);
    (0..count)
        .map(|_| {
            let opener = OPENERS[g.draw() % OPENERS.len()];
            let n = 1 + g.draw() % 8;
            let mut t = opener.to_string();
            for _ in 0..n {
                t.push_str(PIECES[g.draw() % PIECES.len()]);
            }
            if !g.draw().is_multiple_of(8) {
                t.push_str(opener.trim_start_matches(['f', 'F', 'r', 'R']));
            } else {
                t.push_str(PIECES[g.draw() % PIECES.len()]);
            }
            t.push_str(TAILS[g.draw() % TAILS.len()]);
            t
        })
        .collect()
}

fn sweep(row: &Value) {
    let sources = match row["sweep"].as_str().unwrap() {
        "fstring_pieces" => fstring_cases(20000),
        "expr_templates" => sweep_cases(20000),
        other => panic!("sweep {other}"),
    };
    let answers: Vec<String> = sources
        .iter()
        .map(|s| answer_line(&s.chars().map(u32::from).collect::<Vec<_>>()))
        .collect();
    assert_eq!(answers.len() as u64, row["count"].as_u64().unwrap());
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

#[test]
fn pysyntax_expr_every_case() {
    let name = "pysyntax_expr.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_expr_every_case", name);
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
            ledger.case(format!("{label} {short}"), || check(row, &mut n));
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
            "accepted {accepted}, refused {refused}, statements {statements}, \
             band {banded} ({band_declined} declined), name table {named}"
        );
        assert!(
            accepted > 400 && refused > 200,
            "accepted {accepted}, refused {refused}"
        );
        assert!(statements >= 20, "statements {statements}");
        // The boundary pairs near the stack may be declined; the bracket
        // stacks' sources 151 calls past their boundary and the refused
        // chains' sources below theirs were answered above.
        assert!(
            banded >= 40 && band_declined <= banded,
            "band {banded}, declined {band_declined}"
        );
        assert!(named <= 6, "name table {named}");
    }
    ledger.finish(rows.len());
}
