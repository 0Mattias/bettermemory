//! Blocks and the compound statements of
//! `pysyntax::module_expr::parse_module_blocks` over hostile input: 99
//! nested blocks with a `-` chain at each pinned boundary on a 64 KiB
//! thread (nothing recurses), an `elif` chain thousands long, 100,000
//! statements in one block and 100,000 sibling blocks in time linear in
//! the source, and indentation soups and every truncation of a deep
//! module without a panic.

mod timing;

use bettermemory_core::pysyntax::module_expr::{
    parse_module_blocks, ExprError, Tree, C_RECURSION_LIMIT,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// `parse_module_blocks` on a thread with a 64 KiB stack.
fn small_stack(text: String) -> Result<usize, ExprError> {
    std::thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(move || parse_module_blocks(&cps(&text)).map(|t| t.nodes.len()))
        .unwrap()
        .join()
        .unwrap()
}

/// One head of each kind a level, one space deeper each, then `tail`.
fn nested(levels: usize, tail: &str) -> String {
    const HEADS: [&str; 6] = [
        "if x:",
        "while x:",
        "for x in y:",
        "with a as b:",
        "async for x in y:",
        "async with (a, b):",
    ];
    let heads: String = (0..levels)
        .map(|k| format!("{}{}\n", " ".repeat(k), HEADS[k % HEADS.len()]))
        .collect();
    format!("{heads}{}{tail}", " ".repeat(levels))
}

#[test]
fn ninety_nine_levels_need_no_stack() {
    // Six calls a level, whichever head opens it.
    for levels in [1, 50, 99] {
        let n = 5969 - 6 * levels;
        let at = |k: usize| nested(levels, &format!("{}x\n", "-".repeat(k)));
        // A tree about 5,970 deep: `ast2obj` converts it where the C
        // recursion limit is 10,000 and raises RecursionError under
        // Windows' 3,000 and 1,000.
        let deep = if C_RECURSION_LIMIT >= 10_000 {
            None
        } else {
            Some(ExprError::Refused("recursion"))
        };
        assert_eq!(small_stack(at(n)).err(), deep, "{levels}");
        assert_eq!(
            small_stack(at(n + 1)),
            Err(ExprError::Refused("stack")),
            "{levels}"
        );
    }
    assert!(small_stack(nested(99, "pass\n")).is_ok());
}

#[test]
fn a_long_elif_chain_needs_no_stack() {
    let chain = |k: usize, body: &str| format!("if x: pass\n{}else: pass\n", body.repeat(k));
    for body in ["elif x: pass\n", "elif x:\n    y\n"] {
        let short = small_stack(chain(500, body));
        assert!(short.is_ok(), "{short:?}");
        assert_eq!(
            small_stack(chain(100_000, body)),
            Err(ExprError::Declined("stack bound"))
        );
    }
}

fn block(n: usize) -> Vec<u32> {
    cps(&format!(
        "if a:\n{}",
        "    b.c[d], *e = f = (yield g(h, *i, k=[j for j in l]))\n".repeat(n)
    ))
}

fn siblings(n: usize) -> Vec<u32> {
    cps(&"for a in b:\n    with c as d: e\nelse:\n    f\n".repeat(n))
}

fn statements(t: &Tree) -> usize {
    t.body.len()
}

#[test]
fn a_100000_statement_block_is_linear() {
    let (small, large) = (block(10_000), block(100_000));
    let t = parse_module_blocks(&large).unwrap();
    assert_eq!(statements(&t), 1);
    timing::grows_within(
        "blocks: 10,000 and 100,000 statements in one",
        timing::TENFOLD,
        &mut || timing::once(|| parse_module_blocks(&small).unwrap()),
        &mut || timing::once(|| parse_module_blocks(&large).unwrap()),
    );
}

#[test]
fn a_100000_block_module_is_linear() {
    let (small, large) = (siblings(10_000), siblings(100_000));
    assert_eq!(statements(&parse_module_blocks(&large).unwrap()), 100_000);
    timing::grows_within(
        "blocks: 10,000 and 100,000 sibling blocks",
        timing::TENFOLD,
        &mut || timing::once(|| parse_module_blocks(&small).unwrap()),
        &mut || timing::once(|| parse_module_blocks(&large).unwrap()),
    );
}

const SOUP: &[&str] = &[
    "x",
    "1",
    "(",
    ")",
    "[",
    "]",
    ",",
    ":",
    ";",
    "*",
    "=",
    "as",
    "in",
    "if",
    "elif",
    "else",
    "while",
    "for",
    "with",
    "async",
    "def",
    "match",
    "lambda",
    "yield",
    "pass",
    "\n",
    "\n    ",
    "\n        ",
    "\n\t",
    " ",
    "@",
];

#[test]
fn indentation_soup_and_truncations_do_not_panic() {
    let mut state: u64 = 0x5BE0_CD19_137E_2179;
    let mut draw = || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (state >> 33) as usize
    };
    for _ in 0..20_000 {
        let n = 1 + draw() % 30;
        let text: String = (0..n).map(|_| SOUP[draw() % SOUP.len()]).collect();
        let _ = parse_module_blocks(&cps(&text));
    }
    let whole = cps(&nested(
        30,
        "if (a := b): c; d = e\n else:\n  for f, *g in h, *i:\n   with (j as k, l): m\n  else: n\n",
    ));
    for k in 0..=whole.len() {
        let _ = parse_module_blocks(&whole[..k]);
    }
}
