//! Cases a plausible wrong expression grammar fails, each answer read
//! from CPython 3.13.13's `ast.parse`: a first-pass action left out, a
//! compiler check taken for the parser's, `**` read left to right, a
//! depth limit moved by one, a keyword matched after NFKC, a walrus
//! allowed on an attribute.

use bettermemory_core::pysyntax::module_expr::{
    parse_expression_module, recursion_answer, ExprError, Parser, Tree, C_RECURSION_LIMIT,
    MAXSTACK, START_BAND, START_DEPTH,
};
use bettermemory_core::pysyntax::module_lex::module_tokens;

const STACK: Option<ExprError> = Some(ExprError::Refused("stack"));
const BOUND: Option<ExprError> = Some(ExprError::Declined("stack bound"));

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn parse(s: &str) -> Result<Tree, ExprError> {
    parse_expression_module(&cps(s))
}

fn accepted(s: &str) {
    assert!(parse(s).is_ok(), "{s:?}: {:?}", parse(s).err());
}

fn refused(s: &str) {
    assert!(
        matches!(parse(s), Err(ExprError::Refused(_))),
        "{s:?}: {:?}",
        parse(s).map(|_| ())
    );
}

/// The tree as nested class names, operators and contexts left out.
fn outline(t: &Tree, n: u32) -> String {
    let kids: Vec<String> = t
        .kids(n)
        .iter()
        .filter(|&&k| t.nodes[k as usize].height > 0)
        .map(|&k| outline(t, k))
        .collect();
    let node = &t.nodes[n as usize];
    let name = node.name.as_ref().map(|s| {
        s.iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect::<String>()
    });
    let head = name.map_or(node.kind.to_string(), |s| format!("{}:{s}", node.kind));
    if kids.is_empty() {
        head
    } else {
        format!("{head}({})", kids.join(","))
    }
}

fn first(s: &str) -> String {
    let t = parse(s).unwrap();
    let e = t.kids(t.body[0])[0];
    outline(&t, e)
}

#[test]
fn first_pass_actions_refuse() {
    // `_PyPegen_concatenate_strings`: bytes beside str or an f-string.
    for s in ["b'a' 'b'", "'a' b'b'", "b'a' f'{x}'", "f'{x}' b''"] {
        refused(s);
    }
    accepted("b'a' rb'b'");
    accepted("'a' u'b' f'{x}'");
    // `_PyPegen_check_barry_as_flufl`: `<>` without the future flag.
    refused("x <> y");
    accepted("x != y");
    // `_PyPegen_new_identifier`: an identifier whose NFKC is a constant.
    for s in [
        "Ｔｒｕｅ",
        "x.Ｎｏｎｅ",
        "f(Ｆａｌｓｅ=1)",
        "lambda Ｔｒｕｅ: 0",
        "(Ｎｏｎｅ := 1)",
    ] {
        assert_eq!(parse(s).err(), Some(ExprError::Refused("value")), "{s}");
    }
    accepted("ＴＲＵＥ");
    // `_PyPegen_check_fstring_conversion`.
    for s in [
        "f'{x!z}'",
        "f'{x!rr}'",
        "f'{x! r}'",
        "f'{x!}'",
        "f'{x!r!s}'",
        "f'{x!r=}'",
        "f'{}'",
    ] {
        refused(s);
    }
    for s in [
        "f'{x!ｒ}'",
        "f'{x !r}'",
        "f'{x!r }'",
        "f'{x=!r:>{y}}'",
        "f'{x:}'",
        "f'{*x, y}'",
    ] {
        accepted(s);
    }
}

#[test]
fn grammar_placements() {
    // An unparenthesised generator beside other arguments.
    for s in [
        "f(x for x in y, 1)",
        "f(1, x for x in y)",
        "f(x for x in y,)",
    ] {
        refused(s);
    }
    accepted("f(x for x in y)");
    // Starred and double-starred placement.
    for s in [
        "f(**a, *b)",
        "f(a=1, b)",
        "f(**a, b)",
        "(*a)",
        "[*a or b]",
        "{**a or b}",
        "**a",
        "*a or b,",
    ] {
        refused(s);
    }
    for s in [
        "f(*a or b)",
        "x[*a or b]",
        "f(a=1, *b)",
        "*a",
        "*a | b,",
        "{**a, b: c}",
        "x[*a]",
    ] {
        accepted(s);
    }
    // Lambda parameter order.
    for s in [
        "lambda a=1, b: 0",
        "lambda *: 0",
        "lambda *, **k: 0",
        "lambda /: 0",
        "lambda **k, a: 0",
        "lambda a, *, /: 0",
    ] {
        refused(s);
    }
    for s in [
        "lambda a, /, b=1, *c, d, e=2, **f: 0",
        "lambda **k,: 0",
        "lambda *, a: 0",
        "lambda a=1, /, b=2: 0",
    ] {
        accepted(s);
    }
}

#[test]
fn compiler_checks_are_not_the_parsers() {
    // `ast.parse` never runs the compiler or the symbol table.
    for s in [
        "f(a=1, a=2)",
        "lambda a, a: 0",
        "*a",
        "(yield)",
        "await x",
        "[x async for x in y]",
        "(__debug__ := 1)",
        "[a for *a in b]",
        "f'{yield}'",
    ] {
        accepted(s);
    }
}

#[test]
fn walrus_targets_are_names() {
    for s in [
        "(x.y := 1)",
        "((x) := 1)",
        "(x[0] := 1)",
        "[x.y := 1]",
        "x := 1",
        "{a: b := 1}",
    ] {
        refused(s);
    }
    assert_eq!(first("(x := 1)"), "NamedExpr(Name:x,Constant)");
}

#[test]
fn precedence_and_associativity() {
    assert_eq!(first("x ** y ** z"), "BinOp(Name:x,BinOp(Name:y,Name:z))");
    assert_eq!(first("x - y - z"), "BinOp(BinOp(Name:x,Name:y),Name:z)");
    assert_eq!(first("-x ** -y"), "UnaryOp(BinOp(Name:x,UnaryOp(Name:y)))");
    assert_eq!(first("await x ** y"), "BinOp(Await(Name:x),Name:y)");
    assert_eq!(first("not x == y"), "UnaryOp(Compare(Name:x,Name:y))");
    assert_eq!(
        first("x or y and z"),
        "BoolOp(Name:x,BoolOp(Name:y,Name:z))"
    );
    assert_eq!(
        first("x and y or z"),
        "BoolOp(BoolOp(Name:x,Name:y),Name:z)"
    );
    assert_eq!(first("x < y < z"), "Compare(Name:x,Name:y,Name:z)");
    assert_eq!(
        first("a if b else c if d else e"),
        "IfExp(Name:b,Name:a,IfExp(Name:d,Name:c,Name:e))"
    );
    assert_eq!(
        first("lambda: x if y else z"),
        "Lambda(arguments,IfExp(Name:y,Name:x,Name:z))"
    );
    assert_eq!(first("x + y * z"), "BinOp(Name:x,BinOp(Name:y,Name:z))");
    refused("x if y");
    refused("-not x");
    refused("x < not y");
}

#[test]
fn keywords_are_raw_text() {
    // A NAME whose NFKC spells a keyword is a name, kept as its NFKC.
    assert_eq!(first("ｉｆ"), "Name:if");
    assert_eq!(first("x.ｉｆ"), "Attribute:if(Name:x)");
    refused("ｎｏｔ x");
    refused("ｉｆ x");
    accepted("(ｌａｍｂｄａ)");
}

/// The source `head + piece * n + tail`.
fn chain(head: &str, piece: &str, n: usize, tail: &str) -> Vec<u32> {
    cps(&format!("{head}{}{tail}", piece.repeat(n)))
}

/// What a chain `depth` deep that the parser reads answers: past the
/// depth `ast2obj` converts on this host, its refusal or decline.
fn read(depth: u32) -> Option<ExprError> {
    recursion_answer(depth, C_RECURSION_LIMIT).err()
}

#[test]
fn depth_limits_by_one() {
    // The deepest each chain parses in 3.13.13 and the first it refuses;
    // `extra` is what the tree holds besides the chain (Module, Expr, the
    // leaf, and a Tuple and Starred for the starred one).
    for (head, piece, tail, n, extra) in [
        ("", "-", "x\n", 5969, 3),
        ("", "not ", "x\n", 5969, 3),
        ("", "lambda: ", "x\n", 2984, 3),
        ("x", "**x", "\n", 2984, 3),
        ("", "x if y else ", "x\n", 5969, 3),
        ("*", "-", "x,\n", 5974, 5),
        ("", "-", "'s'\n", 5965, 3),
    ] {
        let got = parse_expression_module(&chain(head, piece, n, tail));
        assert_eq!(got.err(), read(n as u32 + extra), "{piece} {n}");
        assert_eq!(
            parse_expression_module(&chain(head, piece, n + 1, tail)).err(),
            Some(ExprError::Refused("stack")),
            "{piece} {}",
            n + 1
        );
    }
    assert_eq!(MAXSTACK, 6000);
    // `ast2obj`: a chain of n BinOps is n + 3 deep with Module and Expr.
    let sum = |n: u32| parse_expression_module(&chain("x", "+x", n as usize, "\n")).err();
    let edge = C_RECURSION_LIMIT - START_DEPTH - 3;
    assert_eq!(sum(edge - START_BAND), None);
    assert_eq!(
        sum(edge - START_BAND + 1),
        Some(ExprError::Declined("recursion bound"))
    );
    assert_eq!(sum(edge), Some(ExprError::Declined("recursion bound")));
    assert_eq!(sum(edge + 1), Some(ExprError::Refused("recursion")));
}

#[test]
fn the_recursion_limit_follows_the_host() {
    // `Py_C_RECURSION_LIMIT`: 10,000 on unix, 3,000 on Windows x64 and
    // 1,000 on Windows arm64; both branches of the rule hold here. A tree
    // is refused past the limit less a script's 3, declined within 100 of
    // that: a sum of 9,894 terms (9,897 deep) is converted, one of 9,895
    // declined, one of 9,995 refused.
    for limit in [10_000u32, 3_000, 1_000] {
        assert_eq!(
            recursion_answer(limit - 2, limit),
            Err(ExprError::Refused("recursion"))
        );
        for depth in [limit - 3, limit - 102] {
            assert_eq!(
                recursion_answer(depth, limit),
                Err(ExprError::Declined("recursion bound"))
            );
        }
        assert_eq!(recursion_answer(limit - 103, limit), Ok(()));
    }
    let want = if cfg!(all(windows, target_arch = "aarch64")) {
        1_000
    } else if cfg!(windows) {
        3_000
    } else {
        10_000
    };
    assert_eq!(C_RECURSION_LIMIT, want);
}

#[test]
fn statements_are_declined_not_refused() {
    for s in [
        "x = 1",
        "x: int",
        "x += 1",
        "x; y",
        "del x",
        "yield x",
        "@d\ndef f(): pass",
        "type X = int",
        "match x:\n    case 1: pass\n",
    ] {
        assert_eq!(
            parse(s).err(),
            Some(ExprError::Declined("statement")),
            "{s:?}"
        );
    }
    // A refusal on an earlier line comes first; `=` and `:` inside a
    // lambda's parameters or brackets do not make a statement.
    refused("x +\ny = 1\n");
    for s in [
        "lambda a=1: a",
        "f(a=1)",
        "{x: y}",
        "x[a:b]",
        "lambda: (x := 1)",
    ] {
        accepted(s);
    }
}

/// `-` * n + `x` inside `open` and `close`.
fn wrapped(open: &str, n: usize, close: &str) -> Vec<u32> {
    cps(&format!("{open}{}x{close}\n", "-".repeat(n)))
}

#[test]
fn bracket_bounds_by_one() {
    // Each bracket's range of rule calls, the first bracket's discount and
    // the later elements' shifts decide where the port stops answering and
    // where it refuses: (open, close, the last source answered, the first
    // refused). 3.13.13 reads the first and raises MemoryError for the
    // second; between the two the port declines. The tree read is between
    // n + 3 and n + 10 deep, which `ast2obj` converts where the C
    // recursion limit is 10,000 and refuses on Windows (3,000 or 1,000):
    // `read` answers alike for every depth in that range.
    for (open, close, last, refused) in [
        ("(", ")", 5940, 6008),
        ("f(", ")", 5942, 6006),
        ("f(a, ", ")", 5940, 6003),
        ("f'{", "}'", 5936, 6002),
        ("f'{a, ", "}'", 5932, 5997),
        ("f\"{a:{", "}}\"", 5927, 6012),
        ("[a for x[", "] in b]", 5903, 6023),
        ("x[a, ", "]", 5940, 6003),
        ("{a, ", "}", 5939, 6006),
        ("[(", ")]", 5911, 5985),
    ] {
        let at = |n| parse_expression_module(&wrapped(open, n, close)).err();
        assert_eq!(at(last), read(last as u32 + 3), "{open} {last}");
        assert_eq!(at(last + 1), BOUND, "{open} {}", last + 1);
        assert_eq!(at(refused - 1), BOUND, "{open} {}", refused - 1);
        assert_eq!(at(refused), STACK, "{open} {refused}");
    }
    // A bracket's first element is sought one call past its leaf's slot:
    // `-` * 5,940 + `(x)` is declined, though its leaf is not past the most
    // estimate (3.13.13 reads it, 5,942 deep; 5,942 `-` raise MemoryError).
    let inner = |n: usize| parse_expression_module(&cps(&format!("{}(x)\n", "-".repeat(n)))).err();
    assert_eq!(inner(5939), read(5942));
    assert_eq!(inner(5940), BOUND);
    assert_eq!(inner(5969), BOUND);
    assert_eq!(inner(5970), STACK);
}

#[test]
fn the_stack_is_met_before_the_leaf() {
    // The parser descends to an operand's atom whatever follows, so a
    // chain past `MAXSTACK` overflows with no leaf, at an operator, or
    // before a NAME's identifier or the strings' concatenation is made
    // (3.13.13: MemoryError for each).
    for s in [
        format!("{}\n", "-".repeat(5970)),
        format!("{}*\n", "-".repeat(5970)),
        format!("{}\n", "not ".repeat(5970)),
        format!("{}\n", "x if y else ".repeat(5970)),
        format!("{}\n", "lambda: ".repeat(2985)),
        format!("x{}**\n", "**x".repeat(2984)),
        format!("{}\u{ff34}rue\n", "-".repeat(5970)),
        format!("{}b'' 'a'\n", "-".repeat(5966)),
    ] {
        assert_eq!(
            parse(&s).err(),
            Some(ExprError::Refused("stack")),
            "{}",
            &s[s.len() - 12..]
        );
    }
    // A lambda's parameter is made one call past its slot's atom: at the
    // chain's own limit 3.13.13 raises MemoryError for it, one call short
    // ValueError.
    let param = |n: usize| {
        parse(&format!(
            "{}lambda \u{ff34}rue: 1\n",
            "x if y else ".repeat(n)
        ))
        .err()
    };
    assert_eq!(param(5968), Some(ExprError::Refused("value")));
    assert_eq!(param(5969), STACK);
    // One call less: the identifier and the concatenation refuse first.
    assert_eq!(
        parse(&format!("{}\u{ff34}rue\n", "-".repeat(5969))).err(),
        Some(ExprError::Refused("value"))
    );
    assert_eq!(
        parse(&format!("{}b'' 'a'\n", "-".repeat(5905))).err(),
        Some(ExprError::Refused("syntax"))
    );
    // At a slot `:=` may open the identifier is made before the atom is
    // sought: `(Ｔrue)` 5,963 `-` deep raises ValueError, 5,964 MemoryError,
    // where `(x)` overflows from 5,942. The port answers where its most
    // estimate of the atom is within the stack, declines up to the
    // chain's own limit, and refuses past it.
    let named = |n: usize| parse(&format!("{}(\u{ff34}rue)\n", "-".repeat(n))).err();
    assert_eq!(named(5939), Some(ExprError::Refused("value")));
    assert_eq!(named(5940), BOUND);
    assert_eq!(named(5969), BOUND);
    assert_eq!(named(5970), STACK);
    // The port refuses there only when the least estimate less those 22
    // calls passes the stack: in `[` + `-` * n + `(Ｔrue)]` (3.13.13:
    // ValueError to 5,953, MemoryError from 5,954) from 6,007, where the
    // chain's own least estimate passes too.
    let listed = |n: usize| parse(&format!("[{}(\u{ff34}rue)]\n", "-".repeat(n))).err();
    assert_eq!(listed(5910), Some(ExprError::Refused("value")));
    assert_eq!(listed(6006), BOUND);
    assert_eq!(listed(6007), STACK);
}

#[test]
fn the_second_pass_is_declined_near_the_stack() {
    // A source the first pass refuses is read again by the error rules,
    // deeper and past the first failure: 3.13.13 raises MemoryError for
    // `-` * 5,963 + `x $` (and SyntaxError for 5,962), for `-` * 5,964
    // alone, and for `[*` + `lambda: ` * 2,979 + `1]`, which the first pass
    // refuses at its first `lambda`. The port declines near the stack and
    // answers below it.
    for s in [
        format!("{}x $\n", "-".repeat(5963)),
        format!("{}x $\n", "-".repeat(5962)),
        format!("{}\n", "-".repeat(5964)),
        format!("{}x\n  y\n", "-".repeat(5963)),
        format!("[*{}1]\n", "lambda: ".repeat(2979)),
        format!("(a.b := {}1)\n", "-".repeat(5956)),
    ] {
        assert_eq!(parse(&s).err(), BOUND, "{}", &s[s.len() - 12..]);
    }
    // Where the port's bound starts: the line's reach (each `-` one call,
    // a lambda's parameters eight, a leaf seven) and 20 calls of the
    // second pass past 6,000 less 31; 3.13.13 raises SyntaxError for all
    // four sources.
    let chain = |n: usize| parse(&format!("{}x $\n", "-".repeat(n))).err();
    assert_eq!(chain(5940), Some(ExprError::Refused("syntax")));
    assert_eq!(chain(5941), BOUND);
    let default = |n: usize| parse(&format!("lambda a, b={}x $: 1\n", "-".repeat(n))).err();
    assert_eq!(default(5932), Some(ExprError::Refused("syntax")));
    assert_eq!(default(5933), BOUND);
    // The error rules nest on a flat line too: `invalid_legacy_expression`
    // reads the rest of the line inside each element that opens with a
    // NAME a possible operand follows (six calls each), and
    // `invalid_arithmetic` the inversion after each `+ not` (eleven).
    // 3.13.13 raises MemoryError from 994 elements and 543 pieces, and
    // SyntaxError below; the port declines from 991 and 540 (its leading
    // `x +` counted as a NAME-led element). Elements
    // whose NAME no operand follows, or that open otherwise, do not nest.
    let legacy = |piece: &str, n: usize| parse(&format!("{}x $\n", piece.repeat(n))).err();
    for piece in [
        "x + x, ",
        "x - 1, ",
        "x [1], ",
        "x + x + x, ",
        "x if y else x - 1, ",
    ] {
        assert_eq!(legacy(piece, 994), BOUND, "{piece}");
    }
    assert_eq!(legacy("x + x, ", 990), Some(ExprError::Refused("syntax")));
    assert_eq!(legacy("x + x, ", 991), BOUND);
    // A comprehension target's elements nest alike (`invalid_for_target`
    // reads them as `star_expressions`), and a comma there keeps the
    // depth they reached: 3.13.13 raises MemoryError for 992 elements and
    // SyntaxError for 991; the port declines from 982.
    let target = |n: usize| parse(&format!("[a for {}x in b $]\n", "x + x, ".repeat(n))).err();
    assert_eq!(target(981), Some(ExprError::Refused("syntax")));
    assert_eq!(target(982), BOUND);
    assert_eq!(target(992), BOUND);
    for piece in ["-x - 1, ", "1 + x, ", "x.y, ", "f(x), ", "x | x, "] {
        assert_eq!(
            legacy(piece, 1500),
            Some(ExprError::Refused("syntax")),
            "{piece}"
        );
    }
    let arithmetic = |n: usize| parse(&format!("x{}\n", " + not x".repeat(n))).err();
    assert_eq!(arithmetic(539), Some(ExprError::Refused("syntax")));
    assert_eq!(arithmetic(540), BOUND);
    assert_eq!(arithmetic(543), BOUND);
    assert_eq!(
        parse(&format!("{}x\n", "x * not x * not ".repeat(272))).err(),
        BOUND
    );
    // The bound reads only a source the first pass refuses.
    accepted(&format!("{}x", "x + x, ".repeat(3000)));
    for (s, k) in [
        (format!("{}x $\n", "-".repeat(5902)), "syntax"),
        (format!("{}x\n  y\n", "-".repeat(5902)), "indent"),
        (format!("[*{}1]\n", "lambda: ".repeat(578)), "syntax"),
    ] {
        assert_eq!(
            parse(&s).err(),
            Some(ExprError::Refused(k)),
            "{}",
            &s[s.len() - 12..]
        );
    }
}

#[test]
fn a_tokenizer_refusal_comes_first() {
    // Stated difference: the port reads every token before the grammar,
    // so a tokenizer refusal later in the source is answered where
    // 3.13.13, whose tokenizer the parser drives, overflows the stack
    // first and raises MemoryError.
    let parens = format!("{}x{}\n", "[a, ".repeat(201), "]".repeat(201));
    assert_eq!(
        parse(&parens).err(),
        Some(ExprError::Refused("too many nested parentheses"))
    );
    assert_eq!(
        parse(&format!("{}x '\n", "-".repeat(5970))).err(),
        Some(ExprError::Refused("unterminated string"))
    );
    assert_eq!(
        parse(&format!("{}x)\n", "-".repeat(5970))).err(),
        Some(ExprError::Refused("unmatched bracket"))
    );
}

/// Each node's class and span, operators, `arguments` and `comprehension`
/// (which `ast` gives no position) and an f-string's literal parts left out.
fn spans(s: &str) -> Vec<String> {
    let t = parse(s).unwrap();
    let mut out: Vec<String> = t
        .nodes
        .iter()
        .filter(|n| n.height > 0 && !matches!(n.kind, "arguments" | "comprehension"))
        .filter(|n| !(s.starts_with("f'") && n.kind == "Constant"))
        .map(|n| format!("{}:{}-{}", n.kind, n.start, n.end))
        .collect();
    out.sort();
    out
}

#[test]
fn spans_are_ast_s() {
    // `col_offset` and `end_col_offset` of 3.13.13's nodes.
    for (s, want) in [
        (
            "f(x)[y].z",
            &[
                "Attribute:0-9",
                "Call:0-4",
                "Expr:0-9",
                "Name:0-1",
                "Name:2-3",
                "Name:5-6",
                "Subscript:0-7",
            ][..],
        ),
        (
            "(a, b) + -c",
            &[
                "BinOp:0-11",
                "Expr:0-11",
                "Name:1-2",
                "Name:10-11",
                "Name:4-5",
                "Tuple:0-6",
                "UnaryOp:9-11",
            ],
        ),
        (
            "[a, *b,] or {k: v, **d} and {e}",
            &[
                "BoolOp:0-31",
                "BoolOp:12-31",
                "Dict:12-23",
                "Expr:0-31",
                "List:0-8",
                "Name:1-2",
                "Name:13-14",
                "Name:16-17",
                "Name:21-22",
                "Name:29-30",
                "Name:5-6",
                "Set:28-31",
                "Starred:4-6",
            ],
        ),
        (
            "x[1:2, ::3, a,]",
            &[
                "Constant:2-3",
                "Constant:4-5",
                "Constant:9-10",
                "Expr:0-15",
                "Name:0-1",
                "Name:12-13",
                "Slice:2-5",
                "Slice:7-10",
                "Subscript:0-15",
                "Tuple:2-14",
            ],
        ),
        (
            "x[:]",
            &["Expr:0-4", "Name:0-1", "Slice:2-3", "Subscript:0-4"],
        ),
        (
            "f(a, k=1, *s, **d)",
            &[
                "Call:0-18",
                "Constant:7-8",
                "Expr:0-18",
                "Name:0-1",
                "Name:11-12",
                "Name:16-17",
                "Name:2-3",
                "Starred:10-12",
                "keyword:14-17",
                "keyword:5-8",
            ],
        ),
        (
            "f(i for i in j)",
            &[
                "Call:0-15",
                "Expr:0-15",
                "GeneratorExp:1-15",
                "Name:0-1",
                "Name:13-14",
                "Name:2-3",
                "Name:8-9",
            ],
        ),
        (
            "lambda a, *b, c=1: (yield a, b)",
            &[
                "Constant:16-17",
                "Expr:0-31",
                "Lambda:0-31",
                "Name:26-27",
                "Name:29-30",
                "Tuple:26-30",
                "Yield:20-30",
                "arg:11-12",
                "arg:14-15",
                "arg:7-8",
            ],
        ),
        (
            "a if not b else c < d",
            &[
                "Compare:16-21",
                "Expr:0-21",
                "IfExp:0-21",
                "Name:0-1",
                "Name:16-17",
                "Name:20-21",
                "Name:9-10",
                "UnaryOp:5-10",
            ],
        ),
        (
            "(x := await y), x.y",
            &[
                "Attribute:16-19",
                "Await:6-13",
                "Expr:0-19",
                "Name:1-2",
                "Name:12-13",
                "Name:16-17",
                "NamedExpr:1-13",
                "Tuple:0-19",
            ],
        ),
        (
            "[i async for i, *j in k if i]",
            &[
                "Expr:0-29",
                "ListComp:0-29",
                "Name:1-2",
                "Name:13-14",
                "Name:17-18",
                "Name:22-23",
                "Name:27-28",
                "Starred:16-18",
                "Tuple:13-18",
            ],
        ),
        // A node built on a parenthesised first part starts at its `(`;
        // the part keeps its own span.
        ("(x)", &["Expr:0-3", "Name:1-2"]),
        (
            "(x) * y",
            &["BinOp:0-7", "Expr:0-7", "Name:1-2", "Name:6-7"],
        ),
        ("(a).b", &["Attribute:0-5", "Expr:0-5", "Name:1-2"]),
        (
            "((x)) < y < z",
            &[
                "Compare:0-13",
                "Expr:0-13",
                "Name:12-13",
                "Name:2-3",
                "Name:8-9",
            ],
        ),
        (
            "(f)(x)[(y)]",
            &[
                "Call:0-6",
                "Expr:0-11",
                "Name:1-2",
                "Name:4-5",
                "Name:8-9",
                "Subscript:0-11",
            ],
        ),
        (
            "(a) if b else c",
            &[
                "Expr:0-15",
                "IfExp:0-15",
                "Name:1-2",
                "Name:14-15",
                "Name:7-8",
            ],
        ),
        (
            "x[(a), b]",
            &[
                "Expr:0-9",
                "Name:0-1",
                "Name:3-4",
                "Name:7-8",
                "Subscript:0-9",
                "Tuple:2-8",
            ],
        ),
        ("'s' 't'", &["Constant:0-7", "Expr:0-7"]),
        // An f-string's literal parts and a specification carry the
        // string's or the field's start, not `ast`'s.
        (
            "f'a{x!r:>{y}}b'",
            &[
                "Expr:0-15",
                "FormattedValue:3-13",
                "FormattedValue:9-12",
                "JoinedStr:0-15",
                "JoinedStr:3-13",
                "Name:10-11",
                "Name:4-5",
            ],
        ),
        (
            "f'{a, b=}'",
            &[
                "Expr:0-10",
                "FormattedValue:2-9",
                "JoinedStr:0-10",
                "Name:3-4",
                "Name:6-7",
                "Tuple:3-7",
            ],
        ),
    ] {
        assert_eq!(spans(s), want, "{s}");
    }
}

#[test]
fn the_entries_for_statements() {
    // `expression(false)` (`expression`) stops before `:=`;
    // `expression(true)` (`named_expression`) reads it.
    let m = module_tokens(&cps("a := 1\n")).unwrap();
    let mut p = Parser::new(&m);
    let v = p.expression(false).unwrap();
    assert_eq!((p.tree.nodes[v as usize].kind, p.pos), ("Name", 1));
    let mut p = Parser::new(&m);
    let v = p.expression(true).unwrap();
    assert_eq!((p.tree.nodes[v as usize].kind, p.pos), ("NamedExpr", 3));
    let mut p = Parser::new(&m);
    let v = p.star_expressions().unwrap();
    assert_eq!((p.tree.nodes[v as usize].kind, p.pos), ("Name", 1));
}
