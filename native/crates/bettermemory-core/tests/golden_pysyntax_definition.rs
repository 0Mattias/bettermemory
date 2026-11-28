//! Every case of `pysyntax_definition.jsonl` (gated on the Python release)
//! against `pysyntax::module_expr::parse_module_blocks`, row by row as
//! `common/pysyntax_expr.rs` (`check_block`) compares them, with each
//! statement's, handler's, `arg`'s and `keyword`'s identifier and span
//! (`marks`), and the sweep by count and digest. The spelling is the
//! generator's (`tests/contract/gen_pysyntax_definition.py`).

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use bettermemory_core::pysyntax::module_expr::{parse_module_blocks, ExprError, Tree};
use bettermemory_core::pysyntax::module_lex::module_tokens;
use common::jsonl;
use common::parity::Ledger;
use expr::{check_block, ids, names, refused_as, shape, text, Counts};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// The classes of `ast.stmt` the port builds.
const STATEMENTS: &[&str] = &[
    "FunctionDef",
    "AsyncFunctionDef",
    "ClassDef",
    "Return",
    "Delete",
    "Assign",
    "AugAssign",
    "AnnAssign",
    "For",
    "AsyncFor",
    "While",
    "If",
    "With",
    "AsyncWith",
    "Raise",
    "Try",
    "TryStar",
    "Assert",
    "Import",
    "ImportFrom",
    "Global",
    "Nonlocal",
    "Expr",
    "Pass",
    "Break",
    "Continue",
];

fn ident(v: &[u32]) -> String {
    v.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

/// Each statement, handler, `arg` and `keyword` in preorder: its class,
/// its identifier and its span as `line:column-line:column`, columns in
/// code points of the translated line (`_marks`).
pub fn marks(t: &Tree, source: &[u32]) -> String {
    let src = module_tokens(source).expect("tokens").source;
    let starts: Vec<usize> = std::iter::once(0)
        .chain(
            src.iter()
                .enumerate()
                .filter(|c| *c.1 == 0x0A)
                .map(|c| c.0 + 1),
        )
        .collect();
    let at = |off: u32| {
        let line = starts.partition_point(|&s| s <= off as usize);
        format!("{line}:{}", off as usize - starts[line - 1])
    };
    let mut out = Vec::new();
    let mut stack: Vec<u32> = t.body.iter().rev().copied().collect();
    while let Some(n) = stack.pop() {
        let node = &t.nodes[n as usize];
        let named = matches!(
            node.kind,
            "FunctionDef" | "AsyncFunctionDef" | "ClassDef" | "ExceptHandler" | "arg" | "keyword"
        );
        if named || STATEMENTS.contains(&node.kind) {
            let name = match (&node.name, named) {
                (Some(v), true) => ident(v),
                _ => String::new(),
            };
            out.push(format!(
                "{} {name} {}-{}",
                node.kind,
                at(node.start),
                at(node.end)
            ));
        }
        stack.extend(t.kids(n).iter().rev());
    }
    out.join(";")
}

const PARAMS: &[&str] = &[
    "",
    "a",
    "a, b",
    "a=$",
    "a, b=$",
    "a: $",
    "a: $ = $",
    "a, /, b",
    "a=$, /, b=$",
    "*a",
    "*a: *$",
    "*, a",
    "*, a=$, b",
    "**k",
    "a, *b, c, **d",
    "*a, **k: $",
    "a=$, b",
    "*",
    "**k, a",
    "a, /",
];
const ARGLISTS: &[&str] = &[
    "",
    "a",
    "a, b",
    "*a",
    "a=$",
    "a, b=$, **c",
    "**a",
    "a=$, b",
    "x for x in y",
    "a := $",
];
const LEAVES: &[&str] = &["x", "1", "'s'", "None", "y.z", "a[0]", "f()"];
const EXPRS: &[&str] = &[
    "-$",
    "not $",
    "$ + $",
    "$ if $ else $",
    "lambda: $",
    "f($)",
    "($, $)",
    "(a := $)",
];
const SIMPLE: &[&str] = &[
    "$",
    "x = $",
    "pass",
    "return $",
    "yield $",
    "raise $",
    "del x",
    "import a as b",
];
const EDITS: &[&str] = &[
    ":", ";", ",", "*", "**", "/", "(", ")", "=", "->", "@", " as ", "def ", "class ", "try",
    "except", "except*", "else", "finally", "\n", "    ", "x", " ",
];

/// The generator's `sweep_cases`.
struct Sweep(u64);

impl Sweep {
    fn draw(&mut self) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 33) as usize
    }

    fn pick(&mut self, items: &[&'static str]) -> &'static str {
        items[self.draw() % items.len()]
    }

    fn expr(&mut self, depth: u32) -> String {
        if depth == 0 || self.draw().is_multiple_of(3) {
            return self.pick(LEAVES).to_string();
        }
        let template = self.pick(EXPRS);
        self.fill(template, depth - 1)
    }

    fn fill(&mut self, template: &str, depth: u32) -> String {
        let mut out = String::new();
        for c in template.chars() {
            if c == '$' {
                out.push_str(&self.expr(depth));
            } else {
                out.push(c);
            }
        }
        out
    }

    fn block(&mut self, depth: u32, indent: &str) -> Vec<String> {
        let mut lines = Vec::new();
        for _ in 0..1 + self.draw() % 2 {
            lines.extend(self.statement(depth, indent));
        }
        lines
    }

    fn branch(&mut self, head: &str, depth: u32, indent: &str) -> Vec<String> {
        if depth == 3 || self.draw().is_multiple_of(3) {
            let template = self.pick(SIMPLE);
            return vec![format!("{indent}{head} {}", self.fill(template, 2))];
        }
        let mut lines = vec![format!("{indent}{head}")];
        lines.extend(self.block(depth + 1, &format!("{indent}    ")));
        lines
    }

    fn statement(&mut self, depth: u32, indent: &str) -> Vec<String> {
        let kind = self.draw() % 5;
        if depth == 3 || kind == 0 {
            let template = self.pick(SIMPLE);
            return vec![format!("{indent}{}", self.fill(template, 2))];
        }
        if kind == 4 {
            let star = if self.draw().is_multiple_of(4) {
                "*"
            } else {
                ""
            };
            let mut lines = self.branch("try:", depth, indent);
            let handlers = self.draw() % 3;
            for _ in 0..handlers {
                let bare = self.draw().is_multiple_of(3);
                let typ = if bare && star.is_empty() {
                    String::new()
                } else {
                    format!(" {}", self.expr(1))
                };
                let alias = self.draw().is_multiple_of(2);
                let name = if !typ.is_empty() && alias {
                    " as e"
                } else {
                    ""
                };
                let head = format!("except{star}{typ}{name}:");
                lines.extend(self.branch(&head, depth, indent));
            }
            let (els, fin) = (self.draw().is_multiple_of(2), self.draw().is_multiple_of(2));
            if handlers > 0 && els {
                lines.extend(self.branch("else:", depth, indent));
            }
            if handlers == 0 || fin {
                lines.extend(self.branch("finally:", depth, indent));
            }
            return lines;
        }
        let decorated = self.draw().is_multiple_of(3);
        let mut lines = if decorated {
            vec![format!("{indent}@{}", self.expr(2))]
        } else {
            Vec::new()
        };
        let head = if kind == 3 {
            let args = self.pick(ARGLISTS);
            let bare = self.draw().is_multiple_of(2);
            if args.is_empty() && bare {
                "class C:".to_string()
            } else {
                format!("class C({}):", self.fill(args, 1))
            }
        } else {
            let returns = if self.draw().is_multiple_of(3) {
                format!(" -> {}", self.expr(1))
            } else {
                String::new()
            };
            let asynced = if kind == 2 { "async " } else { "" };
            let params = self.pick(PARAMS);
            format!("{asynced}def f({}){returns}:", self.fill(params, 1))
        };
        lines.extend(self.branch(&head, depth, indent));
        lines
    }
}

pub fn sweep_cases(count: usize) -> Vec<String> {
    let mut g = Sweep(0x6A09_E667_F3BC_C908);
    (0..count)
        .map(|_| {
            let mut lines = g.block(0, "");
            let edit = g.draw() % 12;
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
                body.insert_str(at, g.pick(EDITS));
            } else if edit == 1 {
                let at = g.draw() % body.len();
                body.remove(at);
            }
            body
        })
        .collect()
}

/// One sweep source's answer line (`gen_pysyntax_definition._line`).
fn line(t: &[u32]) -> String {
    match parse_module_blocks(t) {
        Ok(tree) => {
            let mut parts: Vec<String> = names(&tree).iter().map(|l| l.join(",")).collect();
            parts.push(ids(&tree));
            parts.push(shape(&tree));
            parts.push(marks(&tree, t));
            parts.join("|")
        }
        Err(ExprError::Refused(k)) => match refused_as(k) {
            "IndentationError" | "TabError" if module_tokens(t).is_err() => {
                "E SyntaxError".to_string()
            }
            class => format!("E {class}"),
        },
        Err(ExprError::Declined("statement")) => "D".to_string(),
        Err(e) => panic!("declined {}: {e:?}", ident(t)),
    }
}

fn sweep(row: &Value) {
    assert_eq!(row["sweep"], "definition_templates");
    let answers: Vec<String> = sweep_cases(10_000)
        .iter()
        .map(|s| line(&s.chars().map(u32::from).collect::<Vec<_>>()))
        .collect();
    assert_eq!(answers.len() as u64, row["count"].as_u64().unwrap());
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

fn case(row: &Value, n: &mut Counts) {
    check_block(row, n);
    if let Some(want) = row.get("marks") {
        let t = text(&row["t"]);
        let tree = parse_module_blocks(&t).expect("accepted");
        assert_eq!(marks(&tree, &t), want.as_str().unwrap(), "marks");
    }
}

#[test]
fn pysyntax_definition_every_case() {
    let name = "pysyntax_definition.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_definition_every_case", name);
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
            ledger.case(format!("{label} {short}"), || case(row, &mut n));
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
            accepted > 150 && refused > 300,
            "accepted {accepted}, refused {refused}"
        );
        assert!(statements >= 12, "declined {statements}");
        assert!(banded >= 20, "band {banded}");
    }
    ledger.finish(rows.len());
}
