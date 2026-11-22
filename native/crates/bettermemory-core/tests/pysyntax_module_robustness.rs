//! `pysyntax::module_lex` over hostile input: 100,000 lines, indentation
//! at the limit, long runs of brackets, dedents and continuations. No
//! function recurses; time is linear, as the reference's tokenizer is.

mod timing;

use bettermemory_core::pysyntax::module_lex::module_tokens;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn lines(n: usize) -> Vec<u32> {
    cps(&"def f(a, b=1):\n    if a:\n        return (a, 'x')  # c\n\n".repeat(n / 4))
}

#[test]
fn a_100000_line_module_is_linear() {
    let (small, large) = (lines(10_000), lines(100_000));
    assert_eq!(module_tokens(&large).unwrap().tokens.len(), 25_000 * 28 + 1);
    timing::grows_within(
        "module: 10,000 and 100,000 lines",
        timing::TENFOLD,
        &mut || timing::once(|| module_tokens(&small).unwrap()),
        &mut || timing::once(|| module_tokens(&large).unwrap()),
    );
}

fn deep(n: usize) -> Vec<u32> {
    let mut s = String::new();
    for _ in 0..n {
        for k in 0..99 {
            s += &" ".repeat(k);
            s += "if x:\n";
        }
        s += &" ".repeat(99);
        s += "pass\n";
    }
    cps(&s)
}

#[test]
fn indentation_at_the_limit_over_and_over_is_linear() {
    let (small, large) = (deep(20), deep(200));
    let m = module_tokens(&large).unwrap();
    let dedents = m
        .tokens
        .iter()
        .filter(|t| t.type_name() == "DEDENT")
        .count();
    assert_eq!(dedents, 99 * 200);
    timing::grows_within(
        "indentation: 20 and 200 descents to 99 levels",
        timing::TENFOLD,
        &mut || timing::once(|| module_tokens(&small).unwrap()),
        &mut || timing::once(|| module_tokens(&large).unwrap()),
    );
}

#[test]
fn hostile_shapes_do_not_panic() {
    let cases = [
        "(".repeat(200) + &")".repeat(200),
        "(".repeat(201),
        "(".repeat(100_000),
        ")".repeat(100_000),
        "\\\n".repeat(100_000) + "x",
        "\\\n".repeat(100_000),
        "\n".repeat(100_000),
        " ".repeat(100_000) + "x",
        "\t".repeat(100_000) + "x",
        "\x0c".repeat(100_000),
        "#".repeat(100_000),
        "'".repeat(100_001),
        "f'".repeat(50_000),
        "1".repeat(100_000),
        "0x".to_string() + &"f".repeat(100_000),
        "x".repeat(100_000),
        "\u{e9}".repeat(100_000),
        "if x:\n".to_string() + &" \t".repeat(50_000) + "y\n",
    ];
    for c in &cases {
        let _ = module_tokens(&cps(c));
    }
    let mut bytes = Vec::new();
    for c in 0..0x300u32 {
        bytes.push(c);
        bytes.push(0x0A);
        bytes.push(0x20);
        bytes.push(c);
    }
    let _ = module_tokens(&bytes);
    let _ = module_tokens(&[0x10_FFFF, 0xD800, 0x0D]);
}
