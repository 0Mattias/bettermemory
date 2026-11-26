//! The simple statements of `pysyntax::module_expr::parse_module` over
//! hostile input: each pinned stack boundary on a 64 KiB thread (nothing
//! recurses), 100,000 statements on one line joined by `;` and 100,000
//! lines in time linear in the source, and token soups and every
//! truncation of a long line without a panic.

mod timing;

use bettermemory_core::pysyntax::module_expr::{parse_module, ExprError, C_RECURSION_LIMIT};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// `parse_module` on a thread with a 64 KiB stack.
fn small_stack(text: String) -> Result<usize, ExprError> {
    std::thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(move || parse_module(&cps(&text)).map(|t| t.body.len()))
        .unwrap()
        .join()
        .unwrap()
}

#[test]
fn every_pinned_boundary_needs_no_stack() {
    for (head, tail, n) in [
        ("x = ", "x\n", 5967),
        ("x: ", "x\n", 5970),
        ("x: int = yield ", "x\n", 5965),
        ("x += ", "x\n", 5967),
        ("return *", "x,\n", 5973),
        ("raise y from ", "x\n", 5969),
        ("assert y, ", "x\n", 5969),
        ("yield from ", "x\n", 5969),
        ("x; ", "x\n", 5967),
        ("del ", "x\n", 5963),
    ] {
        let at = |k: usize| format!("{head}{}{tail}", "-".repeat(k));
        let deepest = small_stack(at(n)).err();
        // A tree about 5,970 deep: `ast2obj` converts it where the C
        // recursion limit is 10,000 and raises RecursionError under
        // Windows' 3,000 and 1,000.
        let answered = match deepest {
            None => C_RECURSION_LIMIT >= 10_000,
            Some(ExprError::Refused("recursion")) => C_RECURSION_LIMIT < 10_000,
            Some(e) => matches!(
                e,
                ExprError::Refused("syntax") | ExprError::Declined("stack bound")
            ),
        };
        assert!(answered, "{head:?}: {deepest:?}");
        assert_eq!(
            small_stack(at(n + 1)),
            Err(ExprError::Refused("stack")),
            "{head:?}"
        );
    }
    // Brackets to the tokenizer's limit in a target and a value.
    for s in [
        format!("x[{}y{}] = 1\n", "(".repeat(198), ")".repeat(198)),
        format!("x = {}y{}\n", "[".repeat(199), "]".repeat(199)),
        format!("del x[{}y{}]\n", "f(".repeat(198), ")".repeat(198)),
    ] {
        assert!(small_stack(s).is_ok());
    }
}

#[test]
fn statements_joined_by_semicolons_are_linear() {
    let line = |n: usize| {
        cps(&format!(
            "{}\n",
            "x = y; del z; import a.b as c; ".repeat(n / 3)
        ))
    };
    let (small, large) = (line(10_000), line(100_000));
    assert_eq!(parse_module(&large).unwrap().body.len(), 99_999);
    timing::grows_within(
        "statements: 10,000 and 100,000 on one line",
        timing::TENFOLD,
        &mut || timing::once(|| parse_module(&small).unwrap()),
        &mut || timing::once(|| parse_module(&large).unwrap()),
    );
}

fn lines(n: usize) -> Vec<u32> {
    cps(&"a.b[c], *d = e = (yield f(g, *h, k=[i for i in j]))\n".repeat(n))
}

#[test]
fn a_100000_line_module_is_linear() {
    let (small, large) = (lines(10_000), lines(100_000));
    assert_eq!(parse_module(&large).unwrap().body.len(), 100_000);
    timing::grows_within(
        "statements: 10,000 and 100,000 lines",
        timing::TENFOLD,
        &mut || timing::once(|| parse_module(&small).unwrap()),
        &mut || timing::once(|| parse_module(&large).unwrap()),
    );
}

const SOUP: &[&str] = &[
    "x",
    "1",
    "'s'",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ",",
    ":",
    ";",
    "*",
    "**",
    "=",
    "+=",
    "**=",
    ":=",
    "lambda",
    "yield",
    "from",
    "import",
    "as",
    ".",
    "...",
    "del",
    "return",
    "raise",
    "assert",
    "global",
    "nonlocal",
    "pass",
    "break",
    "if",
    "else",
    "for",
    "in",
    "not",
    "type",
    "match",
    "@",
    "\n",
    " ",
    "<>",
    "!=",
    "__future__",
    "barry_as_FLUFL",
];

#[test]
fn token_soup_and_truncations_do_not_panic() {
    let mut state: u64 = 0x0F1E_2D3C_4B5A_6978;
    let mut draw = || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (state >> 33) as usize
    };
    for _ in 0..20_000 {
        let n = 1 + draw() % 24;
        let text: String = (0..n)
            .map(|_| SOUP[draw() % SOUP.len()])
            .collect::<Vec<_>>()
            .join(" ");
        let _ = parse_module(&cps(&text));
    }
    let whole = "from ...a.b import (c as d, e,); import f.g as h, i; x: int = y = 1; a.b[c], *d = e = yield from f; del (g, [h.i]), j[k]; global l, m; raise n from o; assert p, q; r **= s\n";
    let whole = cps(whole);
    for k in 0..=whole.len() {
        let _ = parse_module(&whole[..k]);
    }
}
