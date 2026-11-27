//! Cases a plausible wrong reading of blocks and the compound statements
//! fails (`pysyntax::module_expr::parse_module_blocks`): an
//! IndentationError answered as SyntaxError or the reverse, a missing
//! block accepted, a branch after `else`, a `with` item's target read
//! without its lookahead, the parenthesised `with` misread, `async for`
//! refused, each slot's stack offset by one, the second pass's bound
//! counted from the module's level, the forced `:` of `else` given a
//! second pass, an `elif` chain read past the place bound, and the two
//! decline-only files answering anything else. Each answer is CPython
//! 3.13.13's unless a test says it is a stated difference.

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use bettermemory_core::pysyntax::module_expr::{
    parse_module, parse_module_blocks, Ctx, ExprError, Node, Tree, C_RECURSION_LIMIT,
};
use expr::{refused_as, shape};

const NONE: &str = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn read(s: &str) -> Result<Tree, ExprError> {
    parse_module_blocks(&cps(s))
}

fn all<'t>(t: &'t Tree, kind: &str) -> Vec<&'t Node> {
    t.nodes.iter().filter(|n| n.kind == kind).collect()
}

/// `if x:` nested `levels` deep, one space a level, then `tail`.
fn nested(levels: usize, tail: &str) -> String {
    let heads: String = (0..levels)
        .map(|k| format!("{}if x:\n", " ".repeat(k)))
        .collect();
    format!("{heads}{}{tail}", " ".repeat(levels))
}

/// A tree about 5,970 deep: `ast2obj` converts it where the C recursion
/// limit is 10,000 (POSIX) and raises RecursionError under Windows' 3,000
/// (x64) and 1,000 (arm64).
fn deep_answer() -> Option<ExprError> {
    if C_RECURSION_LIMIT >= 10_000 {
        None
    } else {
        Some(ExprError::Refused("recursion"))
    }
}

#[test]
fn a_missing_block_is_an_indentation_error() {
    for s in [
        "if x:",
        "if x:\n",
        "if x:\npass\n",
        "if x:\n\n# c\n",
        "while x:\n",
        "for x in y:\n",
        "with a as b:\n",
        "with (a, b):\n",
        "async with a:\n",
        "if x: pass\nelse:\n",
        "if x: pass\nelif y:\npass",
        "if a:\n    if b:\nc\n",
    ] {
        let e = read(s).err();
        assert_eq!(
            e,
            Some(ExprError::Refused("expected an indented block")),
            "{s:?}"
        );
        assert_eq!(refused_as("expected an indented block"), "IndentationError");
    }
    // A head without its `:` is a SyntaxError (`invalid_if_stmt`'s
    // "expected ':'"), and so is a block the head's line refuses.
    for s in [
        "if x\n    pass\n",
        "while x\n",
        "for x in y\n",
        "with a\n",
        "with (a as b)\n",
        "if x: pass\nelif y\n",
        "if x pass",
        "if x: ; pass",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s:?}");
    }
    // An unexpected indent (the parser's IndentationError) and the
    // tokenizer's.
    for (s, k) in [
        ("if x:\n  pass\n    pass", "indent"),
        ("if x: pass\n    y", "indent"),
        ("if x: pass\n  elif y: pass", "indent"),
        (
            "if x:\n    pass\n  pass",
            "unindent does not match any outer indentation level",
        ),
        (
            "if x:\n\tpass\n        pass",
            "inconsistent use of tabs and spaces",
        ),
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused(k)), "{s:?}");
    }
    assert_eq!(refused_as("indent"), "IndentationError");
    assert_eq!(
        refused_as("inconsistent use of tabs and spaces"),
        "TabError"
    );
    // 99 levels are read; the tokenizer refuses the 100th.
    assert!(read(&nested(99, "pass\n")).is_ok());
    assert_eq!(
        read(&nested(100, "pass\n")).err(),
        Some(ExprError::Refused("too many levels of indentation"))
    );
}

#[test]
fn a_branch_belongs_to_its_own_statement() {
    for s in [
        "if x: pass\nelse: pass\nelse: pass",
        "if x: pass\nelse: pass\nelif y: pass",
        "while x: pass\nelif y: pass",
        "for a in b: pass\nelif c: pass",
        "with a: pass\nelse: pass",
        "else: pass",
        "elif x: pass",
        "if x: if y: pass",
        "x; if y: pass",
        "if x: pass; if y: pass",
        "if x: def f(): pass",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s:?}");
    }
    // An `elif` is an If alone in its parent's `orelse`; an `else` dedented
    // past an inner statement is the outer's.
    let t = read("if a: b\nelif c: d\nelse: e").unwrap();
    assert_eq!(
        shape(&t),
        "Module(If(Name(Load),Expr(Name(Load)),If(Name(Load),Expr(Name(Load)),Expr(Name(Load)))))"
    );
    let t = read("if a:\n    if b:\n        c\nelse:\n    d\n").unwrap();
    assert_eq!(
        shape(&t),
        "Module(If(Name(Load),If(Name(Load),Expr(Name(Load))),Expr(Name(Load))))"
    );
    let t = read("for x in y:\n    a\nelse:\n    b\nwhile c: d\nelse: e\n").unwrap();
    assert_eq!(
        shape(&t),
        "Module(For(Name(Store),Name(Load),Expr(Name(Load)),Expr(Name(Load))),\
         While(Name(Load),Expr(Name(Load)),Expr(Name(Load))))"
    );
    // A compound statement ends with its last statement.
    let t = read("if x:\n    y\n").unwrap();
    let node = all(&t, "If")[0];
    assert_eq!((node.start, node.end), (0, 11));
}

#[test]
fn targets_take_store_and_their_lookahead() {
    let t = read("for a.b, c[0], *d in e: pass").unwrap();
    for kind in ["Attribute", "Subscript", "Starred", "Tuple"] {
        assert!(all(&t, kind).iter().all(|n| n.ctx == Ctx::Store), "{kind}");
    }
    let t = read("with a as (b, [c]): pass").unwrap();
    assert!(all(&t, "Name")
        .iter()
        .filter(|n| n.start > 9)
        .all(|n| n.ctx == Ctx::Store));
    for s in [
        "with a as *b: pass",
        "with a as b, c as d: pass",
        "with (a as *b): pass",
        "for x in *a, b: pass",
        "for x in lambda: y: pass",
        "for x, in y: pass",
        "for () in x: pass",
        "for 'a'.b in c: pass",
    ] {
        assert!(read(s).is_ok(), "{s:?}");
    }
    // The target's lookahead `&(',' | ')' | ':')`, and targets that are
    // none.
    for s in [
        "with a as b.c(): pass",
        "with a as b c: pass",
        "with a as b + c: pass",
        "with (a as b) as c: pass",
        "with a as b, *c: pass",
        "with a as (*b): pass",
        "for f() in y: pass",
        "for (*a) in b: pass",
        "for a + b in c: pass",
        "for x in y := 1: pass",
        "for x: pass",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s:?}");
    }
    // The second pass reads on past a refused target: a constant's
    // identifier there is a ValueError.
    for s in [
        format!("for -{NONE} in x: pass"),
        format!("for f() in {NONE}: pass"),
        format!("with a as b + {NONE}: pass"),
        format!("with (a as b + {NONE}): pass"),
        format!("with (a as b + {NONE}), c: pass"),
    ] {
        assert_eq!(read(&s).err(), Some(ExprError::Refused("value")), "{s:?}");
    }
}

#[test]
fn a_parenthesised_with_is_tried_first() {
    for (s, items) in [
        ("with (a, b): pass", 2),
        ("with (a, b) as c: pass", 1),
        ("with (a as b, c as d,): pass", 2),
        ("with (a, ): pass", 1),
        ("with (yield): pass", 1),
        ("with (a), (b): pass", 2),
        ("with (a := 1): pass", 1),
        ("with (a := 1, b := 2): pass", 1),
        ("with (a, *b): pass", 1),
        ("with (): pass", 1),
        ("with (a)(b): pass", 1),
    ] {
        let t = read(s).unwrap();
        assert_eq!(all(&t, "withitem").len(), items, "{s:?}");
    }
    let t = read("with (a, b) as c: pass").unwrap();
    assert_eq!(
        shape(&t),
        "Module(With(withitem(Tuple(Name(Load),Name(Load),Load),Name(Store)),Pass))"
    );
    for s in ["with (a, yield): pass", "with (a, b for b in c): pass"] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s:?}");
    }
}

#[test]
fn the_async_forms_are_read_where_the_parser_takes_them() {
    let t = read("async for x in y: pass\nelse: pass\nasync with a as b, c: pass").unwrap();
    assert_eq!(
        shape(&t),
        "Module(AsyncFor(Name(Store),Name(Load),Pass,Pass),\
         AsyncWith(withitem(Name(Load),Name(Store)),withitem(Name(Load)),Pass))"
    );
    for s in [
        "async x",
        "async if x: pass",
        "async = 1",
        "async\nfor x in y: pass",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s:?}");
    }
}

/// Each slot's offset past the expression statement's path to a name:
/// `head` + `-` * (5969 - offset) + `tail` is read and one more `-`
/// overflows the parser's stack.
const SLOTS: &[(&str, &str, i32)] = &[
    ("if ", "x: pass\n", -1),
    ("while ", "x: pass\n", -1),
    ("if x: pass\nelif ", "x: pass\n", 0),
    ("if x: pass\nelif y: pass\nelif ", "x: pass\n", 1),
    ("for x in ", "x: pass\n", 0),
    ("for x in y, ", "x: pass\n", 2),
    ("for x in *", "x: pass\n", -5),
    ("async for x in ", "x: pass\n", 0),
    ("if x: ", "x\n", 3),
    ("if x: y; ", "x\n", 5),
    ("if x:\n    ", "x\n", 6),
    ("if x:\n    y\n    ", "x\n", 6),
    ("if x:\n if x:\n  ", "x\n", 12),
    ("if x:\n if ", "x: pass\n", 5),
    ("if x:\n if y: ", "x\n", 9),
    ("if x: pass\nelse: ", "x\n", 4),
    ("if x: pass\nelse:\n    ", "x\n", 7),
    ("if x: pass\nelif y: ", "x\n", 4),
    ("if x: pass\nelif y: pass\nelse: ", "x\n", 5),
    ("if x: pass\nelif y: pass\nelse:\n    ", "x\n", 8),
    ("while x: pass\nelse: ", "x\n", 4),
    ("for x in y:\n    ", "x\n", 6),
    ("with ", "x: pass\n", 0),
    ("with a, ", "x: pass\n", 1),
    ("with a as b, ", "x: pass\n", 1),
    ("with (", "x): pass\n", 0),
    ("with (a, ", "x): pass\n", 1),
    ("with x: ", "x\n", 3),
    ("with (a, b):\n    ", "x\n", 6),
];

#[test]
fn each_slot_overflows_where_the_reference_does() {
    let deep = deep_answer();
    let mut slots: Vec<(String, String, i32)> = SLOTS
        .iter()
        .map(|&(h, t, o)| (h.to_owned(), t.to_owned(), o))
        .collect();
    // Six calls a level, to the tokenizer's 99.
    slots.push((nested(98, ""), "x\n".to_owned(), 588));
    slots.push((nested(99, ""), "x\n".to_owned(), 594));
    for (head, tail, offset) in slots {
        let n = (5969 - offset) as usize;
        let at = |k: usize| format!("{head}{}{tail}", "-".repeat(k));
        assert_eq!(read(&at(n)).err(), deep, "{offset} at {n}");
        assert_eq!(
            read(&at(n + 1)).err(),
            Some(ExprError::Refused("stack")),
            "{offset} at {}",
            n + 1
        );
    }
}

/// The second pass reads the module again with its error rules, a
/// statement in a block from its place: `if x:` nested 20 deep, then
/// 5,842 `-` and `x $`, raises SyntaxError, one more MemoryError. The port's
/// bound counts the deepest place read; from the module's level it would
/// answer SyntaxError up to 5,930.
#[test]
fn the_second_pass_counts_from_the_block() {
    let line = |k: usize| nested(20, &format!("{}x $\n", "-".repeat(k)));
    assert_eq!(read(&line(5810)).err(), Some(ExprError::Refused("syntax")));
    assert_eq!(
        read(&line(5811)).err(),
        Some(ExprError::Declined("stack bound"))
    );
    for k in [5842, 5843, 5849] {
        assert_eq!(
            read(&line(k)).err(),
            Some(ExprError::Declined("stack bound")),
            "{k}"
        );
    }
    // The first pass overflows past 5,849 at that place.
    assert_eq!(read(&line(5850)).err(), Some(ExprError::Refused("stack")));
    // The forced `:` of `else` raises in the first pass, which no second
    // pass follows: SyntaxError up to the first pass's own overflow.
    let forced = |k: usize| format!("if {}x: pass\nelse pass\n", "-".repeat(k));
    assert_eq!(
        read(&forced(5970)).err(),
        Some(ExprError::Refused("syntax"))
    );
    assert_eq!(read(&forced(5971)).err(), Some(ExprError::Refused("stack")));
    // `elif` without its `:` has a second pass.
    let elif = |k: usize| format!("if x: pass\nelif {}x\n", "-".repeat(k));
    assert_eq!(read(&elif(5800)).err(), Some(ExprError::Refused("syntax")));
    assert_eq!(
        read(&elif(5964)).err(),
        Some(ExprError::Declined("stack bound"))
    );
}

/// Each `elif`'s block lies a call deeper: a chain of 5,966 `elif x: pass`
/// overflows the reference's stack. A statement within `STATEMENT_PASS` of
/// it is declined, since the calls a statement makes without an operand
/// are not counted.
#[test]
fn a_long_elif_chain_is_declined_near_the_stack() {
    let chain = |k: usize| format!("if x: pass\n{}", "elif x: pass\n".repeat(k));
    assert_eq!(read(&chain(5956)).err(), deep_answer());
    assert_eq!(
        read(&chain(5957)).err(),
        Some(ExprError::Declined("stack bound"))
    );
    assert_eq!(
        read(&chain(5967)).err(),
        Some(ExprError::Declined("stack bound"))
    );
}

#[test]
fn definitions_try_and_match_are_declined() {
    let statement = Some(ExprError::Declined("statement"));
    for s in [
        "def f(): pass",
        "async def f(): pass",
        "class C: pass",
        "try:\n    pass\nfinally:\n    pass\n",
        "@d\ndef f(): pass",
        "match x:\n    case 1: pass\n",
        "type X = int",
        "if x:\n    def f(): pass\n",
        "while x:\n    class C: pass\n",
        "for x in y:\n    @d\n    def f(): pass\n",
        "with a:\n    match y:\n        case 1: pass\n",
        "if x: type X = int",
        "if x: pass\nelse:\n    async def f(): pass\n",
    ] {
        assert_eq!(read(s).err(), statement, "{s:?}");
    }
    // A statement before that the port refuses comes first.
    assert_eq!(
        read("if x:\n    y $\n    def f(): pass\n").err(),
        Some(ExprError::Refused("syntax"))
    );
    assert_eq!(
        read("if x:\ndef f(): pass\n").err(),
        Some(ExprError::Refused("expected an indented block"))
    );
    // The simple statements' entry declines every compound statement, as
    // unit claims-5c1 built it.
    for s in [
        "if x: pass",
        "for a in b: pass",
        "with a: pass",
        "while x: pass",
    ] {
        assert_eq!(parse_module(&cps(s)).err(), statement, "{s:?}");
    }
}

/// `block` on a head's line is `simple_stmts`: no `match_stmt` subject is
/// read there, so its second pass refuses nothing; an indented line's is
/// read.
#[test]
fn a_heads_line_reads_no_match_subject() {
    assert_eq!(
        read(&format!("if x: match(*a)\nf() = {NONE}\n")).err(),
        Some(ExprError::Refused("value"))
    );
    assert_eq!(
        read(&format!("if x:\n    match(*a)\nf() = {NONE}\n")).err(),
        Some(ExprError::Refused("syntax"))
    );
}

/// A block's statement is a level below its head in `ast2obj`'s count,
/// and an `elif` a level below its `if`.
#[test]
fn a_block_is_a_level_below_its_head() {
    let t = read("if x:\n    y = z\n").unwrap();
    let (assign, node) = (all(&t, "Assign")[0], all(&t, "If")[0]);
    assert_eq!(node.height, assign.height + 1);
    let t = read("if a: b\nelif c: d = e\n").unwrap();
    // The `elif`'s If is built first, then the `if`'s around it.
    let heights: Vec<u32> = all(&t, "If").iter().map(|n| n.height).collect();
    assert_eq!(heights[1], heights[0] + 1);
    let t = read("with a as b[c]: pass").unwrap();
    assert_eq!(all(&t, "With")[0].height, 4);
    // A sum deeper than every host converts, in a block.
    let sum = format!("if x:\n    y = x{}\n", "+x".repeat(10_500));
    assert_eq!(read(&sum).err(), Some(ExprError::Refused("recursion")));
}

/// Stated difference: a constant's identifier only the expression
/// grammar's second pass reads (`invalid_legacy_expression` over the
/// `star_expressions` `invalid_for_target` reads) is refused as `syntax`,
/// where CPython 3.13.13 raises ValueError; `check_claim` catches both.
#[test]
fn a_constant_only_the_legacy_rule_reads_is_syntax() {
    assert_eq!(
        read(&format!("for x {NONE}: pass")).err(),
        Some(ExprError::Refused("syntax"))
    );
}

/// The reference's second pass reads the module again from its start, and
/// a missing block's IndentationError (`invalid_block` and its kin) and
/// an unexpected indent (`_PyPegen_set_syntax_error`) come only after it
/// raises nothing. A `match` line before, read as an expression statement,
/// whose subject that pass refuses (`invalid_match_stmt`'s "expected ':'",
/// or the subject's own error rules) raises SyntaxError first; a legacy
/// `print` or `exec` before (`invalid_legacy_expression`) is declined
/// (`second pass`).
#[test]
fn an_earlier_line_the_second_pass_refuses_comes_first() {
    let syntax = Some(ExprError::Refused("syntax"));
    for s in [
        "match(x)\nwhile y:\n",
        "match(x)\n    y\n",
        "match(*a)\nif y:\n",
        "if a:\n    match(x)\nwhile z:\nw\n",
        "match(x)\nif y: pass\n  z\n",
        "match -x\nfor a in b:\n",
        "match[x]\nwith a as b:\n",
        "match(x).y\nif a:\n    b\nelse:\nc\n",
        "match(x) + 1\nasync for a in b:\n",
        "match((x))\nif a:\n    b\n        c\n",
        "match(x)(y)\nwhile y:\n",
        "match [*a]\n    y\n",
        "match(*a, b)\nwhile y:\n",
        "match(x, *y)\nwhile y:\n",
        "if q:\n    match(x)\n    while y:\n",
        "if q:\n    match(x)\n        y\n",
    ] {
        assert_eq!(read(s).err(), syntax, "{s:?}");
    }
    // The simple statements' entry: the unexpected indent likewise.
    assert_eq!(parse_module(&cps("match(x)\n    y\n")).err(), syntax);
    assert_eq!(parse_module(&cps("match -x\n    y\n")).err(), syntax);
    // No subject the second pass refuses, or none read: the IndentationError
    // stands.
    for (s, e) in [
        ("match * x\nwhile y:\n", "expected an indented block"),
        ("match.x\nwhile y:\n", "expected an indented block"),
        ("match(x); y\nwhile y:\n", "expected an indented block"),
        ("if a: match(x)\nwhile y:\n", "expected an indented block"),
        ("print(x)\nwhile y:\n", "expected an indented block"),
        ("match = 1\n    y\n", "indent"),
        ("print.x\n    y\n", "indent"),
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused(e)), "{s:?}");
    }
    assert_eq!(
        parse_module(&cps("match = 1\n    y\n")).err(),
        Some(ExprError::Refused("indent"))
    );
    // A legacy `print` or `exec` before: the reference raises SyntaxError
    // ("Missing parentheses"), and the port declines.
    let second = Some(ExprError::Declined("second pass"));
    for s in [
        "print -x\nwhile y:\n",
        "print -x\n    y\n",
        "exec -x\nif y:\n",
        "if a:\n    print -x\nwhile z:\nw\n",
    ] {
        assert_eq!(read(s).err(), second, "{s:?}");
    }
    assert_eq!(parse_module(&cps("print -x\n    y\n")).err(), second);
}

/// A bracket in a statement 90 levels deep keeps its range from the
/// statement's place, not the module's: the reference raises MemoryError
/// from 5,425 `-` in `for a[...] in y` (5,424 in `with a as b[...]`,
/// 5,422 in `x = a[...]`), and the port declines from 5,404 (5,403 and
/// 5,402) and refuses from 5,466 (5,465 and 5,464). Both edges are decided
/// before `ast2obj`; below them the tree is about 5,500 deep.
#[test]
fn a_bracket_deep_in_blocks_counts_from_its_statement() {
    let deep = deep_answer();
    let bound = Some(ExprError::Declined("stack bound"));
    let stack = Some(ExprError::Refused("stack"));
    let mut wrong = Vec::new();
    for (head, tail, lo, hi) in [
        ("for a[", "x] in y: pass\n", 5404, 5466),
        ("x = a[", "x]\n", 5402, 5464),
        ("with a as b[", "x]: pass\n", 5403, 5465),
    ] {
        let at = |k: usize| read(&nested(90, &format!("{head}{}{tail}", "-".repeat(k)))).err();
        for (k, want) in [
            (lo - 1, &deep),
            (lo, &bound),
            (5425, &bound),
            (hi - 1, &bound),
            (hi, &stack),
        ] {
            if at(k) != *want {
                wrong.push(format!("{head} at {k}: {:?}", at(k)));
            }
        }
    }
    assert!(wrong.is_empty(), "{wrong:?}");
}

/// A later `for` target lies two calls past the first, a starred one two
/// past its star, and a `with` item's target one past its item: the
/// reference raises MemoryError from 5,965 `-` in `for a[...] in y`, from
/// 5,963 in a later or starred target and from 5,964 in `with a as
/// b[...]`, and each offset moves the edges of the port's band around
/// that boundary by as much. Below the lower edge the port answers as the
/// reference, between them it declines, from the upper one it refuses.
#[test]
fn later_targets_move_the_bracket_band() {
    let deep = deep_answer();
    let bound = Some(ExprError::Declined("stack bound"));
    let stack = Some(ExprError::Refused("stack"));
    let mut wrong = Vec::new();
    for (head, tail, lo, hi) in [
        ("for a[", "x] in y: pass\n", 5944, 6006),
        ("for a, b[", "x] in y: pass\n", 5942, 6004),
        ("for *a[", "x] in y: pass\n", 5942, 6004),
        ("with a as b[", "x]: pass\n", 5943, 6005),
    ] {
        let at = |k: usize| read(&format!("{head}{}{tail}", "-".repeat(k))).err();
        for (k, want) in [
            (lo - 1, &deep),
            (lo, &bound),
            (hi - 1, &bound),
            (hi, &stack),
        ] {
            if at(k) != *want {
                wrong.push(format!("{head} at {k}: {:?}", at(k)));
            }
        }
    }
    assert!(wrong.is_empty(), "{wrong:?}");
}

/// Stated difference: the port reads every token first, so the
/// tokenizer's TabError stands where the reference raises ValueError at
/// a constant's identifier on an earlier line; `check_claim` catches
/// both.
#[test]
fn a_tokenizer_refusal_comes_first() {
    assert_eq!(
        read(&format!("if {NONE}: pass\nif y:\n\tz\n        w\n")).err(),
        Some(ExprError::Refused("inconsistent use of tabs and spaces"))
    );
    assert_eq!(
        read(&format!("m(atch -{NONE}\n\n")).err(),
        Some(ExprError::Refused("bracket never closed"))
    );
}

/// `invalid_for_target`'s reading after `for` (`FOR_PASS`) changes no
/// answer: the second pass's bound declines first, where the reference
/// raises SyntaxError to 5,964 and MemoryError from 5,965.
#[test]
fn a_for_target_that_is_none_is_declined_near_the_stack() {
    let line = |k: usize| read(&format!("for {}x in y: pass\n", "-".repeat(k))).err();
    assert_eq!(line(5919), Some(ExprError::Refused("syntax")));
    for k in [5920, 5964, 5965, 6214] {
        assert_eq!(line(k), Some(ExprError::Declined("stack bound")), "{k}");
    }
}
