//! The expression grammar of `pysyntax::module_expr` over hostile input:
//! every depth boundary on a 64 KiB thread (nothing recurses: one loop
//! drives a vector of frames), 100,000 expression lines and a
//! 1,000,000-term sum in time linear in the source, as the reference's
//! parser is, a million prefix operators read no further than the
//! parser's stack, and token soups, truncations and unbalanced brackets
//! without a panic.

mod timing;

use bettermemory_core::pysyntax::module_expr::{
    parse_expression_module, recursion_answer, ExprError, Parser, C_RECURSION_LIMIT,
};
use bettermemory_core::pysyntax::module_lex::{module_tokens, Module};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// `parse_expression_module` on a thread with a 64 KiB stack.
fn small_stack(text: String) -> Result<usize, ExprError> {
    std::thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(move || parse_expression_module(&cps(&text)).map(|t| t.nodes.len()))
        .unwrap()
        .join()
        .unwrap()
}

#[test]
fn every_depth_boundary_needs_no_stack() {
    // `extra`: the tree's depth besides the chain, which `ast2obj` holds
    // to the host's C recursion limit.
    for (head, piece, tail, n, extra) in [
        ("", "-", "x\n", 5969, 3),
        ("", "not ", "x\n", 5969, 3),
        ("", "~", "x\n", 5969, 3),
        ("", "lambda: ", "x\n", 2984, 3),
        ("x", "**x", "\n", 2984, 3),
        ("", "x if y else ", "x\n", 5969, 3),
        ("*", "-", "x,\n", 5974, 5),
    ] {
        let at = |k: usize| format!("{head}{}{tail}", piece.repeat(k));
        let want = recursion_answer(n as u32 + extra, C_RECURSION_LIMIT).err();
        assert_eq!(small_stack(at(n)).err(), want, "{piece} {n}");
        assert_eq!(
            small_stack(at(n + 1)),
            Err(ExprError::Refused("stack")),
            "{piece}"
        );
    }
    // Brackets to the tokenizer's limit, f-strings to theirs, and a sum
    // past the depth `ast2obj` converts.
    for (o, c) in [("(", ")"), ("[", "]"), ("{", "}"), ("f(", ")"), ("x[", "]")] {
        assert!(
            small_stack(format!("{}x{}\n", o.repeat(199), c.repeat(199))).is_ok(),
            "{o}"
        );
    }
    let fstrings = format!("{}x{}\n", "f'{".repeat(149), "}'".repeat(149));
    assert!(small_stack(fstrings).is_ok());
    let sum = format!("x{}\n", "+x".repeat(20_000));
    assert_eq!(small_stack(sum), Err(ExprError::Refused("recursion")));
}

fn lines(n: usize) -> Vec<u32> {
    cps(&"f(a, *b, c=d.e[1:2], **{g: [h for h in i if j]})\n".repeat(n))
}

#[test]
fn a_100000_line_module_is_linear() {
    let (small, large) = (lines(10_000), lines(100_000));
    assert_eq!(parse_expression_module(&large).unwrap().body.len(), 100_000);
    timing::grows_within(
        "expressions: 10,000 and 100,000 lines",
        timing::TENFOLD,
        &mut || timing::once(|| parse_expression_module(&small).unwrap()),
        &mut || timing::once(|| parse_expression_module(&large).unwrap()),
    );
}

#[test]
fn a_million_term_sum_is_linear() {
    let sum = |n: usize| cps(&format!("x{}\n", "+x".repeat(n - 1)));
    let (small, large) = (sum(100_000), sum(1_000_000));
    // Past the depth `ast2obj` converts: read whole, then refused.
    assert_eq!(
        parse_expression_module(&large).err(),
        Some(ExprError::Refused("recursion"))
    );
    timing::grows_within(
        "expressions: 100,000 and 1,000,000 terms",
        timing::TENFOLD,
        &mut || timing::once(|| parse_expression_module(&small).err()),
        &mut || timing::once(|| parse_expression_module(&large).err()),
    );
}

#[test]
fn past_the_stack_nothing_more_is_read() {
    // The parser stops where its stack overflows, as 3.13.13's does: past
    // the tokens, a million prefix operators cost what ten thousand do
    // (ten parses each, the tokens read beforehand), not a frame each.
    for piece in ["-", "not "] {
        let source = |n: usize| cps(&format!("{}x\n", piece.repeat(n)));
        let (small, large) = (source(10_000), source(1_000_000));
        assert_eq!(
            parse_expression_module(&large).err(),
            Some(ExprError::Refused("stack"))
        );
        let (small, large) = (
            module_tokens(&small).unwrap(),
            module_tokens(&large).unwrap(),
        );
        let ten = |m: &Module| {
            let mut parsers: Vec<Parser> = (0..10).map(|_| Parser::new(m)).collect();
            timing::once(|| {
                for p in &mut parsers {
                    assert_eq!(
                        p.star_expressions().err(),
                        Some(ExprError::Refused("stack"))
                    );
                }
            })
        };
        timing::grows_within(
            &format!("expressions: {piece:?} 10,000 and 1,000,000 deep"),
            2,
            &mut || ten(&small),
            &mut || ten(&large),
        );
    }
}

const SOUP: &[&str] = &[
    "x", "1", "'s'", "f'{", "}'", "f'{x:{", "(", ")", "[", "]", "{", "}", ",", ":", "*", "**", "=",
    ":=", "lambda", "yield", "await", "not", "in", "is", "if", "else", "for", "async", "and", "or",
    ".", "!r", "-", "+", "<", "==", "@", ";", "\n", " ", "/", "from",
];

#[test]
fn token_soup_and_truncations_do_not_panic() {
    let mut state: u64 = 0x1234_5678_9ABC_DEF1;
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
        let _ = parse_expression_module(&cps(&text));
    }
    let whole = "f(a, *b, c=lambda x, /, y=1, *z, w, **v: (yield x), **{k: [i async for i, *j in l if m]})[n:o:p, *q].r if s else t or not u\n";
    let whole = cps(whole);
    for k in 0..=whole.len() {
        let _ = parse_expression_module(&whole[..k]);
    }
    for o in ["(", "[", "{", "f'{"] {
        assert!(parse_expression_module(&cps(&o.repeat(150))).is_err());
    }
}
