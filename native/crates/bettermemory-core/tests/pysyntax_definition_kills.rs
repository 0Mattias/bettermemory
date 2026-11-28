//! Cases a plausible wrong reading of definitions, decorators and `try`
//! fails (`pysyntax::module_expr::parse_module_blocks`): `else` taken
//! after a definition or a `with`, a `try` with neither a handler nor
//! `finally`, `except` and `except*` mixed, a parameter without a default
//! after one with one, a decorated definition placed at its decorator, a
//! handler's alias or a class keyword's name not bound, type parameters
//! read instead of declined, each slot's stack offset by one, a class
//! argument's decline band ending a piece too deep, the second pass's
//! bound inside a definition's parentheses, a forced
//! `:` given a second pass or a plain one denied it, a class's bare
//! generator, a statement ending before the `;` that closes its block, and
//! `ast`'s field order. Each answer is CPython 3.13.13's.

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use bettermemory_core::pysyntax::module_expr::{
    parse_module_blocks, ExprError, Node, Tree, C_RECURSION_LIMIT,
};
use expr::{names, shape};

const NONE: &str = "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}";
const SYNTAX: Option<ExprError> = Some(ExprError::Refused("syntax"));

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn read(s: &str) -> Result<Tree, ExprError> {
    parse_module_blocks(&cps(s))
}

fn all<'t>(t: &'t Tree, kind: &str) -> Vec<&'t Node> {
    t.nodes.iter().filter(|n| n.kind == kind).collect()
}

fn ident(n: &Node) -> String {
    let name = n.name.as_deref().unwrap_or_default();
    name.iter().map(|&c| char::from_u32(c).unwrap()).collect()
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
fn else_follows_no_definition_and_no_with() {
    for s in [
        "def f(): pass\nelse: pass",
        "async def f(): pass\nelse: pass",
        "class C: pass\nelse: pass",
        "def f():\n    pass\nelse:\n    pass\n",
        "@d\nclass C:\n    pass\nelse: pass\n",
        "with a: pass\nelse: pass",
        "async with a: pass\nelse: pass",
        "try: pass\nelse: pass",
        "try: pass\nfinally: pass\nelse: pass",
        "def f(): pass\nelif x: pass",
        "def f(): pass\nexcept: pass",
        "class C: pass\nfinally: pass",
    ] {
        assert_eq!(read(s).err(), SYNTAX, "{s:?}");
    }
    for s in [
        "for x in y: pass\nelse: pass",
        "async for x in y: pass\nelse: pass",
        "while x: pass\nelse: pass",
        "try: pass\nexcept: pass\nelse: pass",
        "try: pass\nexcept* E: pass\nelse: pass\nfinally: pass",
    ] {
        assert!(read(s).is_ok(), "{s:?}");
    }
}

#[test]
fn a_try_takes_a_handler_or_finally() {
    for s in [
        "try: pass",
        "try:\n    pass\n",
        "try: pass\nx = 1",
        "try:\n    try: pass\n    finally: pass\n",
        // `invalid_try_stmt` raises before the INDENT is the furthest
        // token: a SyntaxError, not the unexpected indent.
        "try: pass\n    except E: pass",
        "try: pass\nfinally: pass\nexcept E: pass",
        "try: pass\nfinally: pass\nfinally: pass",
        "try: pass\nexcept: pass\nelse: pass\nexcept E: pass",
    ] {
        assert_eq!(read(s).err(), SYNTAX, "{s:?}");
    }
    let t = read("try: pass\nfinally: pass").unwrap();
    assert_eq!(shape(&t), "Module(Try(Pass,Pass))");
    let t = read("try: pass\nexcept E as e: pass\nelse: pass\nfinally: pass").unwrap();
    assert_eq!(
        shape(&t),
        "Module(Try(Pass,ExceptHandler(Name(Load),Pass),Pass,Pass))"
    );
    // A missing block after each clause is the IndentationError.
    for s in [
        "try:\n",
        "try: pass\nexcept:\n",
        "try: pass\nexcept E as e:\npass\n",
        "try: pass\nexcept* E:\n",
        "try: pass\nexcept: pass\nelse:\n",
        "try: pass\nfinally:\n",
    ] {
        assert_eq!(
            read(s).err(),
            Some(ExprError::Refused("expected an indented block")),
            "{s:?}"
        );
    }
}

#[test]
fn except_and_except_star_do_not_mix() {
    for s in [
        "try: pass\nexcept E: pass\nexcept* F: pass",
        "try: pass\nexcept* E: pass\nexcept F: pass",
        "try: pass\nexcept* E: pass\nexcept: pass",
        "try: pass\nexcept: pass\nexcept* E: pass",
        "try: pass\nexcept*: pass",
        "try: pass\nexcept E, F: pass",
    ] {
        assert_eq!(read(s).err(), SYNTAX, "{s:?}");
    }
    let t = read("try: pass\nexcept* E: pass\nexcept* (F, G) as g: pass").unwrap();
    assert_eq!(all(&t, "TryStar").len(), 1);
    assert!(all(&t, "Try").is_empty());
    // A bare `except` before a typed one: the compiler refuses it, and
    // `ast.parse` runs no compiler.
    let t = read("try: pass\nexcept: pass\nexcept E: pass").unwrap();
    assert_eq!(
        shape(&t),
        "Module(Try(Pass,ExceptHandler(Pass),ExceptHandler(Name(Load),Pass)))"
    );
    // The other kind's type is read by `invalid_try_stmt`: a constant's
    // identifier there is the second pass's ValueError, after which a
    // `match` line before is the second pass's SyntaxError.
    let star = format!("try: pass\nexcept E: pass\nexcept* {NONE}: pass");
    assert_eq!(read(&star).err(), Some(ExprError::Refused("value")));
    assert_eq!(read(&format!("match(x)\n{star}")).err(), SYNTAX);
}

#[test]
fn a_parameter_without_a_default_follows_none_with_one() {
    for s in [
        "def f(a=1, b): pass",
        "def f(a=1, /, b): pass",
        "def f(a, b=1, /, c): pass",
        "def f(a=1, b, c=2): pass",
    ] {
        assert_eq!(read(s).err(), SYNTAX, "{s:?}");
    }
    for s in [
        "def f(a=1, *, b): pass",
        "def f(a=1, *b, c): pass",
        "def f(*, a=1, b): pass",
        "def f(a, /, b=1): pass",
    ] {
        assert!(read(s).is_ok(), "{s:?}");
    }
    // The other refusals of `parameters`.
    for s in [
        "def f(/, a): pass",
        "def f(a, /, /): pass",
        "def f(*, a, /): pass",
        "def f(*): pass",
        "def f(*, **k): pass",
        "def f(*a, *b): pass",
        "def f(**k, a): pass",
        "def f(**k, /): pass",
        "def f(*a=1): pass",
        "def f(**k=1): pass",
        "def f(a: *b): pass",
        "def f(**k: *b): pass",
        "def f(*a: *b = 1): pass",
    ] {
        assert_eq!(read(s).err(), SYNTAX, "{s:?}");
    }
    // `invalid_kwds` reads the parameter after `**kwargs,`.
    assert_eq!(
        read(&format!("def f(**k, {NONE}): pass")).err(),
        Some(ExprError::Refused("value"))
    );
    assert_eq!(
        read(&format!("match(x)\ndef f(**k, {NONE}): pass")).err(),
        SYNTAX
    );
    // A bare `*` before `**kwargs` is refused before `invalid_kwds` reads
    // the parameter after it.
    assert_eq!(read(&format!("def f(*, **k, {NONE}): pass")).err(), SYNTAX);
}

#[test]
fn the_lists_keep_asts_field_order() {
    let t = read("def f(a, /, b: int = 1, *c: *d, e, g=2, **h) -> r: pass").unwrap();
    assert_eq!(
        shape(&t),
        "Module(FunctionDef(arguments(arg,arg(Name(Load)),arg(Starred(Name(Load),Load)),\
         arg,arg,Constant,arg,Constant),Pass,Name(Load)))"
    );
    let args: Vec<String> = all(&t, "arg").iter().map(|n| ident(n)).collect();
    assert_eq!(args, ["a", "b", "c", "e", "g", "h"]);
    let t = read("@d\nclass C(a, *b, c=1, **e):\n    x = 1\n").unwrap();
    assert_eq!(
        shape(&t),
        "Module(ClassDef(Name(Load),Starred(Name(Load),Load),keyword(Constant),\
         keyword(Name(Load)),Assign(Name(Store),Constant),Name(Load)))"
    );
    let t = read("@a\n@b\nasync def f() -> c: pass").unwrap();
    assert_eq!(
        shape(&t),
        "Module(AsyncFunctionDef(arguments,Pass,Name(Load),Name(Load),Name(Load)))"
    );
}

#[test]
fn a_decorated_definition_starts_at_its_keyword() {
    for (s, kind, start) in [
        ("@d\n@e\ndef f(): pass", "FunctionDef", 6),
        ("@d\nasync def f(): pass", "AsyncFunctionDef", 3),
        ("@d\n\n# c\nclass C: pass", "ClassDef", 8),
        ("if x:\n    @d\n    def f(): pass\n", "FunctionDef", 17),
    ] {
        let t = read(s).unwrap();
        let node = all(&t, kind)[0];
        assert_eq!(node.start, start, "{s:?}");
        assert_eq!(node.end as usize, s.trim_end().chars().count(), "{s:?}");
    }
    // The decorators are the definition's, after its body.
    let t = read("@d\ndef f(): pass").unwrap();
    assert_eq!(t.body.len(), 1);
    assert_eq!(shape(&t), "Module(FunctionDef(arguments,Pass,Name(Load)))");
    // An INDENT or DEDENT after a decorator is the furthest token.
    for s in ["@d\n    def f(): pass", "if x:\n    @d\ndef f(): pass"] {
        assert_eq!(read(s).err(), Some(ExprError::Refused("indent")), "{s:?}");
    }
    for s in [
        "@d\nx = 1",
        "@d\n",
        "@d\ntry: pass\nfinally: pass",
        "@d def f(): pass",
    ] {
        assert_eq!(read(s).err(), SYNTAX, "{s:?}");
    }
}

#[test]
fn a_handlers_alias_and_a_class_keywords_name_are_bound() {
    let t = read("try: pass\nexcept E as e: pass\nexcept* F: pass").err();
    assert_eq!(t, SYNTAX);
    let t = read("try: pass\nexcept E as e: pass\nexcept F: pass").unwrap();
    let [all_names, top] = names(&t);
    assert_eq!(all_names, ["e"]);
    assert!(top.is_empty());
    let handlers: Vec<String> = all(&t, "ExceptHandler").iter().map(|n| ident(n)).collect();
    assert_eq!(handlers, ["e", ""]);
    let t = read("class C(B, metaclass=M, **kw):\n    def f(self, *, x=g(y=1)): pass\n").unwrap();
    let [all_names, top] = names(&t);
    assert_eq!(all_names, ["C", "f", "metaclass", "self", "x", "y"]);
    assert_eq!(top, ["C"]);
    // A walrus in a top-level definition's head binds no top-level name.
    let t = read("@(a := d)\ndef f(b=(c := 1)): pass").unwrap();
    assert_eq!(names(&t), [vec!["a", "b", "c", "f"], vec!["f"]]);
}

#[test]
fn type_parameters_are_declined() {
    let statement = Some(ExprError::Declined("statement"));
    for s in [
        "def f[T](): pass",
        "async def f[T](): pass",
        "class C[T]: pass",
        "class C[T](a): pass",
        "@d\ndef f[T](): pass",
        "if x:\n    class C[T]: pass\n",
        "def f[](): pass",
        "def f(): type X = int",
        "def f():\n    match x:\n        case 1: pass\n",
    ] {
        assert_eq!(read(s).err(), statement, "{s:?}");
    }
    // What the port reads before comes first.
    for s in [
        format!("def {NONE}[T](): pass"),
        format!("@{NONE}\ndef f[T](): pass"),
    ] {
        assert_eq!(read(&s).err(), Some(ExprError::Refused("value")), "{s:?}");
    }
    assert_eq!(read("def f(): x $\ndef g[T](): pass").err(), SYNTAX);
}

/// Each slot's last source the parser's stack holds and the first it
/// refuses, bisected over the interpreter (`pysyntax_definition.jsonl`
/// pins the second).
const BOUNDS: &[(&str, &str, usize)] = &[
    ("@", "x\ndef f(): pass\n", 5967),
    ("@a\n@", "x\nclass C: pass\n", 5967),
    ("def f(a=", "x): pass\n", 5964),
    ("def f(a, b=", "x): pass\n", 5964),
    ("def f(a, /, b=", "x): pass\n", 5965),
    ("def f(*, a=", "x): pass\n", 5964),
    ("def f(a: ", "x): pass\n", 5963),
    ("def f(a, /, b: ", "x): pass\n", 5964),
    ("def f(*a: ", "x): pass\n", 5964),
    ("def f(*a: *", "x): pass\n", 5968),
    ("def f(**a: ", "x): pass\n", 5963),
    ("def f(*, a: ", "x): pass\n", 5963),
    ("def f() -> ", "x: pass\n", 5969),
    ("def f(): ", "x\n", 5965),
    ("def f():\n    ", "x\n", 5962),
    ("class C: ", "x\n", 5965),
    ("class C:\n    ", "x\n", 5962),
    ("try: ", "x\nfinally: pass\n", 5966),
    ("try:\n    ", "x\nexcept: pass\n", 5963),
    ("try: pass\nexcept ", "x: pass\n", 5969),
    ("try: pass\nexcept* ", "x: pass\n", 5969),
    ("try: pass\nexcept E as e: ", "x\n", 5964),
    ("try: pass\nexcept E:\n    ", "x\n", 5961),
    ("try: pass\nexcept: pass\nexcept: pass\nelse: ", "x\n", 5965),
    ("try: pass\nfinally:\n    ", "x\n", 5962),
    ("def f():\n def g():\n  ", "x\n", 5955),
    ("class C:\n def f(a=", "x): pass\n", 5957),
];

#[test]
fn each_slot_overflows_where_the_reference_does() {
    for &(head, tail, n) in BOUNDS {
        let at = |k: usize| read(&format!("{head}{}{tail}", "-".repeat(k))).err();
        assert_eq!(at(n), deep_answer(), "{head:?} {n}");
        assert_eq!(
            at(n + 1),
            Some(ExprError::Refused("stack")),
            "{head:?} {}",
            n + 1
        );
    }
}

#[test]
fn a_class_argument_near_the_stack_is_declined() {
    // The interpreter's edges: 5964 for the first argument, 5963 for a
    // later positional one, 5961 for a starred one after a keyword.
    for (head, n) in [
        ("class C(", 5964),
        ("class C(a, ", 5963),
        ("class C(a, b=1, *", 5961),
    ] {
        let at = |k: usize| read(&format!("{head}{}x): pass\n", "-".repeat(k))).err();
        assert_eq!(at(n - 10), deep_answer(), "{head:?}");
        for k in [n, n + 1] {
            let answer = at(k);
            assert!(
                answer == Some(ExprError::Declined("stack bound"))
                    || answer
                        == if k == n {
                            deep_answer()
                        } else {
                            Some(ExprError::Refused("stack"))
                        },
                "{head:?} {k}: {answer:?}"
            );
        }
        assert_eq!(at(n + 10), Some(ExprError::Refused("stack")), "{head:?}");
    }
}

#[test]
fn a_class_arguments_band_ends_where_the_least_estimate_passes_the_stack() {
    // The last source of each band the port declines and, one piece
    // deeper, the first it refuses as the stack's; the reference raises
    // MemoryError at both (from 5,965, 5,964, 5,965, 5,962, 5,965 and
    // 5,965).
    for (head, last) in [
        ("class C(", 5967),
        ("class C(a, ", 5964),
        ("class C(a=", 5967),
        ("class C(a, b=1, *", 5964),
        ("class C(a=1, **", 5964),
        ("class C(**", 5967),
    ] {
        let at = |k: usize| read(&format!("{head}{}x): pass\n", "-".repeat(k))).err();
        assert_eq!(
            at(last),
            Some(ExprError::Declined("stack bound")),
            "{head:?} {last}"
        );
        assert_eq!(
            at(last + 1),
            Some(ExprError::Refused("stack")),
            "{head:?} {}",
            last + 1
        );
    }
}

#[test]
fn a_definitions_parentheses_are_a_bracket_to_the_bound() {
    // `def f(a=` + `-` * k + `x) pass` is SyntaxError to 5,958 and
    // MemoryError of the second pass from 5,959: the bound counts the
    // parentheses as a bracket's and declines from 5,888.
    let at = |k: usize| read(&format!("def f(a={}x) pass\n", "-".repeat(k))).err();
    assert_eq!(at(5887), SYNTAX);
    assert_eq!(at(5888), Some(ExprError::Declined("stack bound")));
    assert_eq!(at(5959), Some(ExprError::Declined("stack bound")));
}

#[test]
fn forced_colons_have_no_second_pass() {
    // `try` and `finally` take their `:` as forced tokens: the first pass
    // raises and no second pass reads the deep line before again.
    let deep = |tail: &str, k: usize| read(&format!("x = {}x\n{tail}", "-".repeat(k))).err();
    assert_eq!(deep("try pass\n", 5967), SYNTAX);
    assert_eq!(deep("try: pass\nfinally pass\n", 5967), SYNTAX);
    assert_eq!(deep("try pass\n", 5968), Some(ExprError::Refused("stack")));
    // A definition's `(` and `:` and a class's `:` are not forced: the
    // second pass reads the line before deeper, and overflows from 5,964
    // where the first pass would from 5,968.
    for tail in [
        "def f: pass\n",
        "def f() pass\n",
        "class C pass\n",
        "try: pass\n",
    ] {
        assert_eq!(deep(tail, 5900), SYNTAX, "{tail:?}");
        assert_eq!(
            deep(tail, 5964),
            Some(ExprError::Declined("stack bound")),
            "{tail:?}"
        );
    }
}

#[test]
fn a_class_takes_no_bare_generator() {
    assert_eq!(read("class C(x for x in y): pass").err(), SYNTAX);
    assert_eq!(read("class C(a, x for x in y): pass").err(), SYNTAX);
    let t = read("class C((x for x in y)): pass").unwrap();
    assert_eq!(all(&t, "GeneratorExp").len(), 1);
    let t = read("class C(f(x for x in y)): pass").unwrap();
    assert_eq!(all(&t, "Call").len(), 2, "the class's own and the base's");
    for s in [
        "class C(a)(b): pass",
        "class C(a).b: pass",
        "class C(a)[b]: pass",
    ] {
        assert_eq!(read(s).err(), SYNTAX, "{s:?}");
    }
    // A keyword after which a positional argument comes: the second pass
    // reads it, as a call's.
    assert_eq!(
        read(&format!("class C(a=1, x + {NONE}): pass")).err(),
        Some(ExprError::Refused("value"))
    );
    assert_eq!(
        read(&format!("match(x)\nclass C(a=1, x + {NONE}): pass")).err(),
        SYNTAX
    );
}

#[test]
fn a_statement_ends_at_the_last_token_of_its_block() {
    for (s, kind, end) in [
        ("if x: a;", "If", 8),
        ("def f(): a;", "FunctionDef", 11),
        ("class C: a; b;\n", "ClassDef", 14),
        ("while x:\n    a;\n# c\n", "While", 15),
        ("try:\n  a\nexcept E as e:\n  b;\n# c\n", "Try", 28),
        (
            "try:\n  a\nexcept E as e:\n  b;\n# c\n",
            "ExceptHandler",
            28,
        ),
        ("try: a\nexcept: b\nelse: c;\nfinally: d", "Try", 36),
    ] {
        let t = read(s).unwrap();
        assert_eq!(all(&t, kind)[0].end, end, "{s:?}");
    }
    // An `elif` chain ends where its last branch does.
    let t = read("if a: b\nelif c: d;").unwrap();
    assert!(all(&t, "If").iter().all(|n| n.end == 18));
}
