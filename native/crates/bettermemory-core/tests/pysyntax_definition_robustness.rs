//! Definitions, decorators and `try` of
//! `pysyntax::module_expr::parse_module_blocks` over hostile input: 99
//! nested definitions and `try` blocks with a `-` chain at the pinned
//! boundary on a 64 KiB thread (nothing recurses), 100,000 handlers,
//! decorators, parameters and class arguments in time linear in the
//! source, a million operators in a default, and token soups and every
//! truncation of a deep module without a panic.

mod timing;

use bettermemory_core::pysyntax::module_expr::{parse_module_blocks, ExprError, C_RECURSION_LIMIT};

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

/// The heads nested a level each, with the calls each level adds to its
/// block's statements' place: a definition's block lies 7 past its own
/// place, a `try`'s 6.
const HEADS: [(&str, usize); 5] = [
    ("def f(a=1, *b, c, **d) -> e:", 7),
    ("class C(a, b=1):", 7),
    ("try:", 6),
    ("@d\n{}async def g():", 7),
    ("try:", 6),
];

/// One head a level, one space deeper each, `tail` innermost, then each
/// `try`'s handler after its block: the module and its innermost place.
fn nested(levels: usize, tail: &str) -> (String, usize) {
    let mut lines = Vec::new();
    let mut place = 0;
    for k in 0..levels {
        let (head, calls) = HEADS[k % HEADS.len()];
        lines.push(format!(
            "{}{}",
            " ".repeat(k),
            head.replace("{}", &" ".repeat(k))
        ));
        place += calls;
    }
    lines.push(format!("{}{tail}", " ".repeat(levels)));
    for k in (0..levels).rev() {
        if HEADS[k % HEADS.len()].0 == "try:" {
            lines.push(format!("{}except E as e: pass", " ".repeat(k)));
        }
    }
    (lines.join("\n") + "\n", place)
}

#[test]
fn ninety_nine_levels_of_definitions_need_no_stack() {
    for levels in [1, 2, 3, 4, 5, 50, 99] {
        let n = 5969 - nested(levels, "").1;
        let at = |k: usize| nested(levels, &format!("{}x", "-".repeat(k))).0;
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
    assert!(small_stack(nested(99, "pass").0).is_ok());
    // The tokenizer's 100 levels.
    assert!(small_stack(nested(100, "pass").0).is_err());
}

#[test]
fn a_million_operators_in_a_slot_need_no_stack() {
    for (head, tail) in [
        ("def f(a=", "x): pass\n"),
        ("@", "x\ndef f(): pass\n"),
        ("class C(", "x): pass\n"),
        ("try: pass\nexcept ", "x: pass\n"),
        ("def f() -> ", "x: pass\n"),
        ("def f(*a: *", "x): pass\n"),
    ] {
        let text = format!("{head}{}{tail}", "-".repeat(1_000_000));
        let answer = small_stack(text);
        assert!(
            matches!(
                answer,
                Err(ExprError::Refused("stack") | ExprError::Declined("stack bound"))
            ),
            "{head:?}: {answer:?}"
        );
    }
}

fn grows(name: &str, make: impl Fn(usize) -> String) {
    let (small, large) = (cps(&make(10_000)), cps(&make(100_000)));
    assert!(parse_module_blocks(&large).is_ok(), "{name}");
    timing::grows_within(
        name,
        timing::TENFOLD,
        &mut || timing::once(|| parse_module_blocks(&small).unwrap()),
        &mut || timing::once(|| parse_module_blocks(&large).unwrap()),
    );
}

#[test]
fn a_100000_handler_try_is_linear() {
    grows("definitions: 10,000 and 100,000 handlers", |n| {
        format!(
            "try: pass\n{}else: pass\nfinally: pass\n",
            "except E as e: pass\n".repeat(n)
        )
    });
}

#[test]
fn a_100000_decorator_definition_is_linear() {
    grows("definitions: 10,000 and 100,000 decorators", |n| {
        format!("{}def f(): pass\n", "@a.b(c, d=e)\n".repeat(n))
    });
}

#[test]
fn a_100000_parameter_definition_is_linear() {
    grows("definitions: 10,000 and 100,000 parameters", |n| {
        let params: String = (0..n).map(|k| format!("a{k}: int = {k}, ")).collect();
        format!("def f(b, /, {params}*c, d, **e) -> f: pass\n")
    });
}

#[test]
fn a_100000_argument_class_is_linear() {
    grows("definitions: 10,000 and 100,000 class arguments", |n| {
        format!("class C({}*b, c=1, **d): pass\n", "a, ".repeat(n))
    });
}

#[test]
fn a_100000_definition_module_is_linear() {
    grows("definitions: 10,000 and 100,000 sibling definitions", |n| {
        "@d\nclass C(B):\n    def f(self, a=1):\n        try:\n            return a\n        except E as e:\n            pass\n"
            .repeat(n / 10)
    });
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
    "**",
    "/",
    "=",
    "->",
    "@",
    "as",
    "def",
    "async",
    "class",
    "try",
    "except",
    "except*",
    "else",
    "finally",
    "if",
    "lambda",
    "yield",
    "pass",
    "\n",
    "\n    ",
    "\n        ",
    "\n\t",
    " ",
    ":=",
];

#[test]
fn token_soups_and_truncations_do_not_panic() {
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
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
        "def f(a, /, b: int = 1, *c: *d, e, f=2, **g) -> h: i; j",
    )
    .0);
    for k in 0..=whole.len() {
        let _ = parse_module_blocks(&whole[..k]);
    }
}
