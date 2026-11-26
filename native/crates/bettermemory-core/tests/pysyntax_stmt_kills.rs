//! Cases a plausible wrong reading of the simple statements fails
//! (`pysyntax::module_expr::parse_module`): a target the grammar refuses
//! taken, an error only the compiler raises refused, `AnnAssign.simple`
//! for a parenthesised name, a starred annotated target, `del` read as
//! Store, a dotted import name split, each statement slot's stack offset
//! by one, the second pass's bound dropped, the future import that turns
//! `<>` into `!=`, the statements declined, an annotated target read past
//! its parenthesised first part, what the second pass reads past a target
//! it refuses, a SyntaxError the second pass meets before it (in a
//! `match` line's subject too), a `match` line's subject read before the
//! line and its memo, a MemoryError past the subject's tokens, and the
//! module's depth taken from its deepest statement.

use bettermemory_core::pysyntax::module_expr::{
    parse_expression_module, parse_module, Ctx, ExprError, Tree, C_RECURSION_LIMIT,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn read(s: &str) -> Result<Tree, ExprError> {
    parse_module(&cps(s))
}

fn text(v: &[u32]) -> String {
    v.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

/// The nodes of `kind` in the arena, in the order they were built.
fn all<'t>(t: &'t Tree, kind: &str) -> Vec<&'t bettermemory_core::pysyntax::module_expr::Node> {
    t.nodes.iter().filter(|n| n.kind == kind).collect()
}

#[test]
fn a_target_the_grammar_refuses_is_refused() {
    for s in [
        "f() = 1",
        "a + 1 = 2",
        "None = 1",
        "x = 1 = 2",
        "(x := 1) = 2",
        "lambda: x = 1",
        "(yield) = 1",
        "a = yield = c",
        "a + 1 += 2",
        "f() += 1",
        "(a, b) += 1",
        "[a] += 1",
        "*a += 1",
        "del f()",
        "del a + 1",
        "del *a",
        "del (*a,)",
        "a, b: int",
        "[a]: int",
        "*a: int",
        "(*a): int",
        "True: int",
        "x: *a",
        "import a as b.c",
        "from a import b,",
        "from a import *, b",
        "from a import ()",
        "global a,",
        "x;;",
        "x; if y: pass",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s}");
    }
}

/// `assignment` reads an annotated target that is no bare name as the
/// group `'(' single_target ')' | single_subscript_attribute_target`; once
/// the first branch matches, the `:` must follow it (CPython 3.13.13:
/// SyntaxError "illegal target for annotation"). Augmented, Store and Del
/// targets try the attribute or subscript first.
#[test]
fn an_annotated_target_commits_to_its_parenthesised_part() {
    for s in [
        "(a).b: c",
        "(a)[0]: c",
        "(a)(b).c: d",
        "((a)).b: c",
        "(a.b).c: d",
        "(a[0]).b: c",
        "(a).b: c = 1",
        "x; (a).b: int",
        "(a) .b: int",
        "(match).b: int",
        "((a).b).c: d",
        "(a)\\\n.b: c",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s}");
    }
    for (s, simple) in [
        ("((a).b): c", 0),
        ("(f()).b: c", 0),
        ("(a,).b: c", 0),
        ("[a].b: c", 0),
        ("(1).b: c", 0),
        ("(a): c", 0),
        ("((a)): c", 0),
        ("a.b: c", 0),
        ("a: c", 1),
    ] {
        let t = read(s).unwrap_or_else(|e| panic!("{s}: {e:?}"));
        assert_eq!(all(&t, "AnnAssign")[0].num, simple, "{s}");
    }
    for s in ["(a).b = 1", "(a).b += 1", "del (a).b", "(a)[0], (b).c = d"] {
        assert!(read(s).is_ok(), "{s}");
    }
}

/// Where the first pass refuses a target, the second pass reads on past
/// it (`invalid_assignment`, `invalid_del_stmt`), and a constant's
/// identifier there is a ValueError; past what it reads, the SyntaxError
/// stands. A line opening with `match` has its subject read first.
#[test]
fn the_second_pass_reads_on_past_a_refused_target() {
    let none = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";
    for (s, kind) in [
        // The annotation, after any target but a starred one.
        ("[a]: #", "value"),
        ("(a).b: #", "value"),
        ("*a, b: #", "value"),
        ("f(): lambda: #", "value"),
        ("*a: #", "syntax"),
        ("f(): int = #", "syntax"),
        ("(a).b: int = #", "syntax"),
        ("f(): a := #", "syntax"),
        // The augmented value.
        ("a + 1 += #", "value"),
        ("(a, b) += #", "value"),
        ("*a += #", "value"),
        ("f() += yield #", "value"),
        // A `bitwise_or` after the `=` that follows the first target's
        // last element, unless a list, tuple, generator or keyword
        // constant opens it or it is no `bitwise_or`.
        ("f() = #", "value"),
        ("-x = #", "value"),
        ("(a) + 1 = #", "value"),
        ("([a]) + 1 = #", "value"),
        ("((a, b)) + 1 = #", "value"),
        ("[x for x in y] + 1 = #", "value"),
        ("await x = #", "value"),
        ("f(), x = #", "value"),
        ("x, f() = #", "value"),
        ("f() = # = 1", "value"),
        ("f() = a | #", "value"),
        ("x; f() = #", "value"),
        ("f() = a if # else b", "syntax"),
        ("f() = a < #", "syntax"),
        ("f() = yield #", "syntax"),
        ("[a] + 1 = #", "syntax"),
        ("() + 1 = #", "syntax"),
        ("(x for x in y) + 1 = #", "syntax"),
        ("True + 1 = #", "syntax"),
        ("x == y = #", "syntax"),
        ("not x = #", "syntax"),
        ("lambda: x = #", "syntax"),
        ("x, f(), = #", "syntax"),
        ("f(), *x = #", "syntax"),
        ("f(), None = #", "syntax"),
        ("*f() = #", "syntax"),
        ("x = f() = #", "syntax"),
        // The rest of a `del` list.
        ("del f(), #", "value"),
        ("del x, f(), #", "value"),
        ("del f(), *#", "value"),
        ("del *a, #", "value"),
        ("del f(); #", "syntax"),
        // `match_stmt`'s subject.
        ("match #: pass", "value"),
        ("match #", "value"),
        ("match x, *#", "value"),
        ("match a := #", "value"),
    ] {
        let s = s.replace('#', none);
        assert_eq!(read(&s).err(), Some(ExprError::Refused(kind)), "{s}");
    }
    // The subject read is undone: the line is read as simple statements.
    for s in [
        "match = 1",
        "match(x)",
        "match.x: int = 1",
        "match, x = 1, 2",
        "match -x",
    ] {
        assert!(read(s).is_ok(), "{s}");
    }
    let t = read("match (a), b\n").unwrap();
    assert_eq!(all(&t, "Tuple").len(), 1);
    assert_eq!(all(&t, "Call").len(), 1);
}

#[test]
fn errors_only_the_compiler_raises_are_accepted() {
    for s in [
        "*a, *b = c",
        "*a = b",
        "x = *a",
        "return",
        "return *a, b",
        "yield",
        "x = yield",
        "nonlocal x",
        "global x",
        "break",
        "continue",
        "from __future__ import braces",
        "x = 1\nfrom __future__ import annotations\n",
        "() = x",
    ] {
        assert!(read(s).is_ok(), "{s}");
    }
}

#[test]
fn simple_is_one_for_a_bare_name_only() {
    for (s, simple) in [
        ("a: int", 1),
        ("a: int = 1", 1),
        ("(a): int", 0),
        ("((a)): int = 1", 0),
        ("a.b: int", 0),
        ("(a.b): int", 0),
        ("a[0]: int", 0),
    ] {
        let t = read(s).unwrap();
        let ann = all(&t, "AnnAssign");
        assert_eq!(ann.len(), 1, "{s}");
        assert_eq!(ann[0].num, simple, "{s}");
    }
}

#[test]
fn targets_take_their_context() {
    let t = read("del a, (b, [c.d]), e[0]").unwrap();
    let ctx: Vec<(&str, Ctx)> = t
        .nodes
        .iter()
        .filter(|n| n.ctx != Ctx::None)
        .map(|n| (n.kind, n.ctx))
        .collect();
    assert!(ctx.contains(&("Name", Ctx::Del)));
    assert!(ctx.contains(&("Tuple", Ctx::Del)));
    assert!(ctx.contains(&("List", Ctx::Del)));
    assert!(ctx.contains(&("Attribute", Ctx::Del)));
    assert!(ctx.contains(&("Subscript", Ctx::Del)));
    assert!(!ctx.iter().any(|&(_, c)| c == Ctx::Store));
    // The value under an Attribute or Subscript target is read.
    let t = read("a.b[c], *d = e = f").unwrap();
    let store: Vec<String> = t
        .nodes
        .iter()
        .filter(|n| n.kind == "Name" && n.ctx == Ctx::Store)
        .map(|n| text(n.name.as_deref().unwrap()))
        .collect();
    assert_eq!(store, ["d", "e"]);
    assert_eq!(all(&t, "Assign").len(), 1);
    // `x: int` and `x += 1` store their single target.
    for s in ["x: int", "x += 1"] {
        let t = read(s).unwrap();
        assert_eq!(all(&t, "Name")[0].ctx, Ctx::Store, "{s}");
    }
}

#[test]
fn an_import_keeps_its_dotted_name_whole() {
    let t = read("import a.b.c as d, e . f\nfrom ... g.h import (i as j, k,)\n").unwrap();
    let aliases: Vec<(String, Option<String>)> = all(&t, "alias")
        .iter()
        .map(|n| {
            (
                text(n.name.as_deref().unwrap()),
                n.asname.as_deref().map(text),
            )
        })
        .collect();
    assert_eq!(
        aliases,
        [
            ("a.b.c".into(), Some("d".into())),
            ("e.f".into(), None),
            ("i".into(), Some("j".into())),
            ("k".into(), None),
        ]
    );
    let from = all(&t, "ImportFrom")[0];
    assert_eq!(text(from.name.as_deref().unwrap()), "g.h");
    assert_eq!(from.num, 3);
    // Each part is NFKC'd as a NAME is; a constant's NFKC is a ValueError.
    let t = read("import \u{ff4d}.\u{ff4e}\n").unwrap();
    assert_eq!(text(all(&t, "alias")[0].name.as_deref().unwrap()), "m.n");
    for s in [
        "import a.\u{ff2e}one",
        "import a as \u{ff2e}one",
        "global \u{ff34}rue",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("value")), "{s}");
    }
    // An alias spans its text.
    let t = read("import a.b as c").unwrap();
    let alias = all(&t, "alias")[0];
    assert_eq!((alias.start, alias.end), (7, 15));
}

/// Each statement slot's offset past the expression statement's path to a
/// name: `head` + `-` * (5969 - offset) + `tail` is read and one more `-`
/// overflows the parser's stack.
const SLOTS: &[(&str, &str, i32)] = &[
    ("x = ", "x\n", 2),
    ("x = y, ", "x\n", 4),
    ("x = *", "x,\n", -3),
    ("x = yield ", "x\n", 3),
    ("x = yield *", "x\n", -2),
    ("x = yield from ", "x\n", 1),
    ("x: ", "x\n", -1),
    ("x: int = ", "x\n", 3),
    ("x: int = yield ", "x\n", 4),
    ("x: int = yield from ", "x\n", 2),
    ("x += ", "x\n", 2),
    ("x += yield ", "x\n", 3),
    ("return ", "x\n", 1),
    ("return y, ", "x\n", 3),
    ("raise ", "x\n", -1),
    ("raise y from ", "x\n", 0),
    ("assert ", "x\n", -1),
    ("assert y, ", "x\n", 0),
    ("yield ", "x\n", 2),
    ("yield from ", "x\n", 0),
    ("x; ", "x\n", 2),
    ("x; x: ", "x\n", 1),
    ("x; return ", "x\n", 3),
    ("x = ", "\u{ff34}rue\n", 2),
    // `match_stmt`'s subject, read before the line's simple statements:
    // its first, second and later elements and a starred one.
    ("match ", "x\n", 1),
    ("x\nmatch ", "x\n", 1),
    ("match ", "x\ny = 1\n", 1),
    ("match ", "x, \u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}\n", 1),
    ("match a, ", "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}\n", 3),
    ("match a, b, ", "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}\n", 4),
    ("match *", "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}, y\n", -5),
    ("match a, *", "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}\n", -3),
    ("match *a, ", "x\n", 3),
];

#[test]
fn each_slot_overflows_where_the_reference_does() {
    // The deepest source each slot reads holds a tree about 5,970 deep:
    // `ast2obj` converts it where `Py_C_RECURSION_LIMIT` is 10,000 (POSIX)
    // and raises RecursionError under Windows' 3,000 (x64) and 1,000
    // (arm64).
    let deep = if C_RECURSION_LIMIT >= 10_000 {
        None
    } else {
        Some(ExprError::Refused("recursion"))
    };
    for &(head, tail, offset) in SLOTS {
        let n = (5969 - offset) as usize;
        let at = |k: usize| format!("{head}{}{tail}", "-".repeat(k));
        let deepest = read(&at(n)).err();
        assert!(
            deepest == deep || deepest == Some(ExprError::Refused("value")),
            "{head:?} at {n}: {deepest:?}"
        );
        assert_eq!(
            read(&at(n + 1)).err(),
            Some(ExprError::Refused("stack")),
            "{head:?} at {}",
            n + 1
        );
    }
    // `del`'s target the first pass does not read: the second pass reads
    // it at `DEL` and overflows one `-` past 5,963.
    let del = |k: usize| format!("del {}x\n", "-".repeat(k));
    assert_eq!(read(&del(5964)).err(), Some(ExprError::Refused("stack")));
    assert_eq!(read(&del(5800)).err(), Some(ExprError::Refused("syntax")));
    // A later target lies two calls further: `del y, ` + `-` * 5962 + `x`
    // overflows (5,961 is a SyntaxError, which the port may decline).
    let later = |k: usize| format!("del y, {}x\n", "-".repeat(k));
    assert_eq!(read(&later(5962)).err(), Some(ExprError::Refused("stack")));
    assert_eq!(read(&later(5800)).err(), Some(ExprError::Refused("syntax")));
}

/// `STATEMENT_PASS`, what a statement's second pass may go past an
/// expression statement's, is a bound on [`parse_module`]'s estimate:
/// the first line is refused, so the reference reads no further and
/// answers SyntaxError however deep the second line is, and the port
/// declines from where its estimate of the line, with the bound, reaches
/// `MAXSTACK`.
#[test]
fn the_statement_margin_is_ten_calls() {
    let line = |k: usize| format!("x $\nx = {}x\n", "-".repeat(k));
    assert_eq!(read(&line(5930)).err(), Some(ExprError::Refused("syntax")));
    assert_eq!(
        read(&line(5931)).err(),
        Some(ExprError::Declined("stack bound"))
    );
    // The expression entry, which has no statement's margin, answers
    // further.
    let expr = |k: usize| format!("x $\n{}x\n", "-".repeat(k));
    assert_eq!(
        parse_expression_module(&cps(&expr(5933))).err(),
        Some(ExprError::Refused("syntax"))
    );
}

/// The module's depth is its deepest statement's, wherever it lies: a
/// sum 10,500 deep is past every host's `ast2obj` before a shallow
/// statement too (CPython 3.13.13: RecursionError).
#[test]
fn the_deepest_statement_sets_the_depth() {
    let deep = format!("x = x{}", "+x".repeat(10_500));
    for s in [
        format!("{deep}\ny = 1\n"),
        format!("{deep}; y = 1\n"),
        format!("y = 1; {deep}\n"),
    ] {
        assert_eq!(read(&s).err(), Some(ExprError::Refused("recursion")));
    }
}

#[test]
fn a_refusal_near_the_second_pass_is_declined() {
    // The first pass refuses at `$`; the second pass reads the chain
    // again deeper and overflows from 5,961 `-` on (`x; ` + `-` * 5960 +
    // `x $` raises SyntaxError, 5961 MemoryError).
    let line = |k: usize| format!("x; {}x $\n", "-".repeat(k));
    assert_eq!(read(&line(5900)).err(), Some(ExprError::Refused("syntax")));
    for k in [5960, 5961] {
        assert_eq!(
            read(&line(k)).err(),
            Some(ExprError::Declined("stack bound")),
            "{k}"
        );
    }
    // A target that is none is refused before its value is read; the
    // second pass may read the value, so a deep one is declined.
    let target = |k: usize| format!("f() = {}x\n", "-".repeat(k));
    assert_eq!(read(&target(10)).err(), Some(ExprError::Refused("syntax")));
    assert_eq!(
        read(&target(6100)).err(),
        Some(ExprError::Declined("stack bound"))
    );
    // A ValueError only the second pass reaches, a constant past a target
    // that is none, is declined near the stack as a SyntaxError is: the
    // second pass reads the target again deeper (`-` * 5962 +
    // `x = Ｎｏｎｅ` raises ValueError, 5963 MemoryError).
    let none = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";
    let value = |k: usize| format!("{}x = {none}\n", "-".repeat(k));
    assert_eq!(read(&value(5863)).err(), Some(ExprError::Refused("value")));
    for k in [5962, 5963] {
        assert_eq!(
            read(&value(k)).err(),
            Some(ExprError::Declined("stack bound")),
            "{k}"
        );
    }
}

/// A line opening with the soft keyword `match` has `match_stmt`'s
/// subject read first, deeper than the line's own reading of the same
/// text (CPython 3.13.13: `match ` + `-` * 5969 + `x` raises
/// MemoryError, where the expression statement overflows at 5,971), and
/// both entries read it. What that reading leaves in the parser's memo
/// the line's own reading takes without descending: `match *` + `-` *
/// 5974 + `x, y` is read, where the port's count of the line overflows
/// from 5,970, so a MemoryError the line's reading meets is declined.
#[test]
fn a_match_subject_is_read_before_the_line() {
    let none = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";
    let chain = |head: &str, k: usize, tail: &str| format!("{head}{}{tail}", "-".repeat(k));
    type Entry = fn(&[u32]) -> Result<Tree, ExprError>;
    let entries: [Entry; 2] = [parse_module, parse_expression_module];
    for parse in entries {
        let at = |s: &str| parse(&cps(s)).err();
        for (head, tail, k) in [
            ("match ", "x", 5969),
            ("match ", "x", 5970),
            ("match ", "x\ny = 1\n", 5969),
            ("x\nmatch ", "x", 5969),
            ("match ", none, 5969),
            ("match ", none, 5970),
            ("match a, ", none, 5967),
            ("match a, ", none, 5969),
            ("match ", &format!("x, {none}"), 5969),
            ("match *a, ", "x", 5967),
        ] {
            let s = chain(head, k, tail);
            assert_eq!(
                at(&s),
                Some(ExprError::Refused("stack")),
                "{head:?} {k} {tail:?}"
            );
        }
        // A bracket around the element: the port declines near it.
        for k in [5940, 5941] {
            let s = chain("match[", k, "x]");
            assert_eq!(at(&s), Some(ExprError::Declined("stack bound")), "{k}");
        }
        // The memo.
        for k in [5970, 5974] {
            let s = chain("match *", k, "x, y");
            assert_eq!(at(&s), Some(ExprError::Declined("stack bound")), "{k}");
        }
        assert_eq!(
            at(&chain("match *", 5975, "x, y")),
            Some(ExprError::Refused("stack"))
        );
        assert!(parse(&cps(&chain("match *", 5964, "x, y"))).is_ok());
        assert!(parse(&cps(&chain("match ", 5968, "x"))).is_ok());
        assert_eq!(
            at(&chain("match ", 5968, none)),
            Some(ExprError::Refused("value"))
        );
    }
}

/// The second pass reads the module from its start: a SyntaxError it
/// raises before it reaches the constant a refused target's value holds
/// stands (CPython 3.13.13: `match(x)` then `f()=Ｎｏｎｅ` is SyntaxError,
/// "expected ':'", from `invalid_match_stmt` on the first line). A legacy
/// `print` or `exec` before it, which `invalid_legacy_expression` refuses
/// where an `expression` opens with it, is declined.
#[test]
fn a_syntax_error_the_second_pass_meets_first_stands() {
    let none = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";
    let fals = "\u{ff26}\u{ff41}\u{ff4c}\u{ff53}\u{ff45}";
    for (s, want) in [
        ("match(x)\nf()=#", ExprError::Refused("syntax")),
        ("match[0]\nf()=#", ExprError::Refused("syntax")),
        ("match -1\nf()=#", ExprError::Refused("syntax")),
        ("match(x)\ndel f(),#", ExprError::Refused("syntax")),
        ("match(x)\n[a]:#", ExprError::Refused("syntax")),
        ("match(x)\na+1+=#", ExprError::Refused("syntax")),
        ("match(x)\n-x = #", ExprError::Refused("syntax")),
        ("match (x),\nf()=#", ExprError::Refused("syntax")),
        ("match *a, b\nf()=#", ExprError::Refused("syntax")),
        // `invalid_named_expression` over the subject's last element
        // before `=`, unless a list opens it or `=` follows the value.
        ("match(x).y = 1\nf()=#", ExprError::Refused("syntax")),
        ("match(x), y = 1, 2\nf()=#", ExprError::Refused("syntax")),
        (
            "match(x).y = 1 if a else b\nf()=#",
            ExprError::Refused("syntax"),
        ),
        ("match[0].y = 1\nf()=#", ExprError::Refused("value")),
        ("match(x).y = z = 1\nf()=#", ExprError::Refused("value")),
        ("match(x).y = yield\nf()=#", ExprError::Refused("value")),
        ("match(x).y += 1\nf()=#", ExprError::Refused("value")),
        // That rule reads a constant past the `=` or `:=` before the line
        // is read.
        ("match f(x) = #", ExprError::Refused("value")),
        ("x = 1\nmatch x ** y = #", ExprError::Refused("value")),
        ("match f(x) := #", ExprError::Refused("value")),
        ("match f(x) = z = #", ExprError::Refused("syntax")),
        ("match(x)\nmatch f(x) = #", ExprError::Refused("syntax")),
        (
            "print -1\nmatch f(x) = #",
            ExprError::Declined("second pass"),
        ),
        // The subject does not end the line, or is none.
        ("match[0] = 1\nf()=#", ExprError::Refused("value")),
        ("match *x\nf()=#", ExprError::Refused("value")),
        ("match.x\nf()=#", ExprError::Refused("value")),
        ("match(x); f()=#", ExprError::Refused("value")),
        ("f()=#\nmatch(x)", ExprError::Refused("value")),
        // The first pass's ValueError comes first.
        ("match(x)\nmatch #", ExprError::Refused("value")),
        // A legacy `print` or `exec` before the constant.
        ("print -1\nf()=#", ExprError::Declined("second pass")),
        ("print -1; f()=#", ExprError::Declined("second pass")),
        ("x = print -1\nf() = #", ExprError::Declined("second pass")),
        ("y()=(print-'')(#)", ExprError::Declined("second pass")),
        ("f()=(print -1)[#]", ExprError::Declined("second pass")),
        ("exec -1\nf()=#", ExprError::Declined("second pass")),
        ("\u{ff50}rint -1\nf()=#", ExprError::Declined("second pass")),
        ("print(x)\nf()=#", ExprError::Refused("value")),
        ("print.x\nf()=#", ExprError::Refused("value")),
        ("f()=#\nprint -1", ExprError::Refused("value")),
    ] {
        let s = s.replace('#', none);
        assert_eq!(read(&s).err(), Some(want), "{s}");
    }
    for s in ["[print[5]]:(#)", "del{print['']},#"] {
        let s = s.replace('#', fals);
        assert_eq!(
            read(&s).err(),
            Some(ExprError::Declined("second pass")),
            "{s}"
        );
    }
    // The expression entry reads the subject as the statements do.
    let walrus = format!("match f(x) := {none}");
    assert_eq!(
        parse_expression_module(&cps(&walrus)).err(),
        Some(ExprError::Refused("value"))
    );
}

/// The second pass reads a `match` line's subject with its error rules
/// before the line, and a SyntaxError it raises there comes before a
/// constant only the second pass reads (CPython 3.13.13: `match(*a)` then
/// `f() = Ｎｏｎｅ` is SyntaxError, "cannot use starred expression here").
/// It refuses a parenthesis right after `match`, which the first pass
/// reads as a call's arguments, where `invalid_group` meets a lone starred
/// or double-starred element and where `invalid_named_expression` meets a
/// keyword argument `tuple` reaches whose value opens with a
/// `bitwise_or`. A walrus no parenthesis holds ends the subject's last
/// element: `named_expression` takes it before that rule reads a second
/// `:=` (`match a := 1 := Ｎｏｎｅ` is SyntaxError). A constant in a call's
/// argument after a keyword or `**` that the first pass does not read
/// (`f(a=1, x + Ｎｏｎｅ)`) is one only the second pass reads.
#[test]
fn a_syntax_error_the_subject_raises_first_stands() {
    let none = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";
    let syntax = Some(ExprError::Refused("syntax"));
    let value = Some(ExprError::Refused("value"));
    let statement = Some(ExprError::Declined("statement"));
    let second = Some(ExprError::Declined("second pass"));
    for (s, module, expression) in [
        // The walrus chains, on both entries.
        ("match a := 1 := #", syntax, syntax),
        ("match a := 1 := #\ny = 1", syntax, syntax),
        ("x\nmatch a := 1 := #", syntax, syntax),
        ("match x, a := 1 := #", syntax, syntax),
        ("match a, b := c := #", syntax, syntax),
        ("match a := b := #", syntax, syntax),
        ("match a := (1) := #", syntax, syntax),
        ("match *a, b := 1 := #", syntax, syntax),
        ("match (a := 1) := #", value, value),
        ("match x, (a := 1) := #", value, value),
        ("match f(x) := #", value, value),
        ("match a := 1 = #", syntax, statement),
        ("match x, a := 1 = #", syntax, statement),
        ("match a := 1, := #", syntax, syntax),
        ("match a := 1 := 2", syntax, syntax),
        // The parenthesis after `match` the second pass refuses.
        ("match(*a) = #", syntax, statement),
        ("match (*a) = #", syntax, statement),
        ("match(**a) = #", syntax, statement),
        ("match(a=1) = #", syntax, statement),
        ("match(*a)\nf() = #", syntax, statement),
        ("match(*a)\ndel f(), #", syntax, statement),
        ("match(*a)\n[a]: #", syntax, statement),
        ("match(*a)\na + 1 += #", syntax, statement),
        ("y = 1\nmatch(*a)\nf() = #", syntax, statement),
        ("match(*a)\nf() = #; y", syntax, statement),
        ("match(**a)\nf() = #", syntax, statement),
        ("match(a, b=1)\nf() = #", syntax, statement),
        ("match(a=1)\nx = 1\nf() = #", syntax, statement),
        ("match(*a).y = 1\nf() = #", syntax, statement),
        ("match(*a); f() = #", syntax, statement),
        ("match(*a, b=1)\nf() = #", syntax, statement),
        ("match(*a if b else c)\nf() = #", syntax, statement),
        ("match(a := 1, b=2)\nf() = #", syntax, statement),
        // And the forms it does not refuse.
        ("match(*a)[0] = #", value, statement),
        ("match(x, *a) = #", value, statement),
        ("match(x, **a)\nf() = #", value, statement),
        ("f(*a) = #", value, statement),
        ("match(*a, **b)\nf() = #", value, statement),
        ("match(**a, b=1)\nf() = #", value, statement),
        ("match(a=lambda: 1)\nf() = #", value, statement),
        ("match(a=not b)\nf() = #", value, statement),
        ("match(a=#)", value, value),
        ("match(a=1, #)", value, value),
        // A constant in an argument after a keyword or `**`.
        ("match(a=1, a := #)", syntax, syntax),
        ("match(a=1, (a := #))", syntax, syntax),
        ("match(*a)\nf(a=1, x + #)", syntax, syntax),
        ("match(x)\nf(a=1, a := #)", syntax, syntax),
        ("match(*a)\nf(**a, *#)", syntax, syntax),
        ("match(*a)\nf(a=1, g(#))", syntax, syntax),
        ("print -1\nf(a=1, x + #)", second, second),
        ("exec -1\nf(**a, *#)", second, second),
        ("match(*a)\nf(a=1, #)", value, value),
        ("match(*a)\nf(a=1, *#)", value, value),
        ("match(*a)\nf(a=1, *(x + #))", value, value),
        ("match(*a)\nf(a=1, b=#)", value, value),
        ("match(*a)\nf(**a, #.x)", value, value),
        ("match(*a)\nf(**a, b=#)", value, value),
        ("f(a=1, x + #)", value, value),
        ("f(a=1, a := #)", value, value),
        ("f(**a, *#)", value, value),
        ("print -1\nf(a=1, #)", value, value),
    ] {
        let s = cps(&s.replace('#', none));
        assert_eq!(parse_module(&s).err(), module, "{}", text(&s));
        assert_eq!(
            parse_expression_module(&s).err(),
            expression,
            "{}",
            text(&s)
        );
    }
    assert!(read("match(*a)").is_ok());
}

/// A MemoryError of a `match` line's own reading at a token its subject's
/// reading did not reach is the reference's: no rule's result that
/// reading left spares the line's descent there (a chain of 6,100 `-`
/// after `match = ` overflows as after `a = `), while one within the
/// subject's tokens is declined (5,974 after `match *`, then `x, y`).
#[test]
fn a_match_line_overflows_past_its_subject() {
    let chain = |head: &str, k: usize| format!("{head}{}x", "-".repeat(k));
    for k in [5990, 6100, 20_000] {
        for head in [
            "match = ",
            "match.y = ",
            "match += ",
            "match: int = ",
            "match[0][1] = ",
            "match, ",
        ] {
            let s = chain(head, k);
            assert_eq!(
                read(&s).err(),
                Some(ExprError::Refused("stack")),
                "{head:?} {k}"
            );
        }
        assert_eq!(
            parse_expression_module(&cps(&chain("match, ", k))).err(),
            Some(ExprError::Refused("stack")),
            "{k}"
        );
    }
    for k in [6000, 6100, 20_000] {
        for head in ["match[0].y = ", "a[0].y = "] {
            let s = chain(head, k);
            assert_eq!(
                read(&s).err(),
                Some(ExprError::Refused("stack")),
                "{head:?} {k}"
            );
        }
    }
    for k in [5970, 5974] {
        let s = format!("match *{}x, y", "-".repeat(k));
        assert_eq!(
            read(&s).err(),
            Some(ExprError::Declined("stack bound")),
            "{k}"
        );
    }
}

#[test]
fn the_barry_future_import_turns_the_operator() {
    let flufl = "from __future__ import barry_as_FLUFL\n";
    assert!(read(&format!("{flufl}x = 1 <> 2\n")).is_ok());
    assert_eq!(
        read(&format!("{flufl}1 != 2\n")).err(),
        Some(ExprError::Refused("syntax"))
    );
    for s in [
        "1 <> 2\nfrom __future__ import barry_as_FLUFL\n",
        "from .__future__ import barry_as_FLUFL\n1 <> 2\n",
        "from __future__ import barry_as_flufl\n1 <> 2\n",
        "import __future__\n1 <> 2\n",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("syntax")), "{s}");
    }
    assert!(read("from __future__ import (a, barry_as_FLUFL as b); f'{x <> y}'\n").is_ok());
    // The expression entry never reads an import.
    assert!(parse_expression_module(&cps("1 != 2\n")).is_ok());
}

#[test]
fn compound_statements_and_type_aliases_are_declined() {
    for s in [
        "if x: pass",
        "x = 1\nfor a in b: pass\n",
        "@d\ndef f(): pass",
        "async def f(): pass",
        "class C: pass",
        "match x:\n    case 1: pass\n",
        "type X = int",
        "x; type X[T] = list[T]",
    ] {
        assert_eq!(read(s).err(), Some(ExprError::Declined("statement")), "{s}");
    }
    // A line before that is refused comes first; `match`, `type` and
    // `case` are names elsewhere.
    assert_eq!(
        read("x $\nif y: pass\n").err(),
        Some(ExprError::Refused("syntax"))
    );
    for s in [
        "match = 1",
        "match(x)",
        "match.x: int = 1",
        "type = 1",
        "type(x)",
        "case = 1",
    ] {
        assert!(read(s).is_ok(), "{s}");
    }
    assert_eq!(
        read("x = 1\n  y = 2\n").err(),
        Some(ExprError::Refused("indent"))
    );
}

#[test]
fn a_statement_is_a_level_above_its_value() {
    // The tree an expression statement and an assignment of the same
    // expression build are as deep: `ast2obj`'s answer is the same.
    for k in [800, 9_000, 9_895, 10_500] {
        let chain = format!("x{}\n", "+x".repeat(k));
        let expr = parse_expression_module(&cps(&chain)).map(|_| ()).err();
        for head in ["y = ", "y: int = ", "y += ", "return "] {
            let stmt = read(&format!("{head}{chain}")).map(|_| ()).err();
            assert_eq!(stmt, expr, "{head} {k}");
        }
        let del = read(&format!("del y[{chain}]")).map(|_| ()).err();
        assert_ne!(del, Some(ExprError::Refused("syntax")), "del {k}");
    }
}

/// Stated difference: a constant's identifier that only the expression
/// grammar's second pass reads (`invalid_legacy_expression`,
/// `invalid_expression`, `invalid_arithmetic`, `invalid_comprehension`,
/// `invalid_kvpair`) is refused as `syntax`, as `parse_expression_module`
/// refuses it, where CPython 3.13.13 raises ValueError; so is one before a
/// tokenizer refusal, which the port reads first. `check_claim` and
/// `symbols` catch both alike.
#[test]
fn a_constant_only_the_expressions_second_pass_reads_is_syntax() {
    let none = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";
    for s in [
        "x N",
        "type a + N = 1",
        "match x N",
        "x = a + not N",
        "y.z: [*a for N in b]",
        "x = {k: *N}",
        "a := N",
        "f(x) := N",
    ] {
        let s = format!("{}\n", s.replace('N', none));
        assert_eq!(read(&s).err(), Some(ExprError::Refused("syntax")), "{s}");
    }
    assert_eq!(
        read(&format!("{none}\n(\n")).err(),
        Some(ExprError::Refused("bracket never closed"))
    );
}
