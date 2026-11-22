//! Cases a plausible wrong module tokenizer fails: the tab-size-1
//! consistency check, the dedent that matches no outer level, NUL
//! anywhere, the 100-level indentation limit, the column a continuation
//! fixes, form feeds, comment-only lines and the final newline. Each
//! answer is CPython 3.13.13's (its parser-mode tokenizer and the token
//! types `tokenize` reads).

use bettermemory_core::pysyntax::module_lex::{module_tokens, ModuleError, MAX_INDENT};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn types(s: &str) -> String {
    let m = module_tokens(&cps(s)).unwrap_or_else(|e| panic!("refused {s:?}: {e:?}"));
    m.tokens
        .iter()
        .map(|t| t.type_name())
        .collect::<Vec<_>>()
        .join(" ")
}

fn refused(s: &str) -> bool {
    match module_tokens(&cps(s)) {
        Err(ModuleError::Syntax(_)) => true,
        Ok(_) => false,
        Err(e) => panic!("declined {s:?}: {e:?}"),
    }
}

#[test]
fn tabs_are_checked_at_tab_size_one() {
    // Equal at tab size 8, unequal at 1.
    assert!(refused("if x:\n\ty\n        z\n"));
    assert!(refused("if x:\n        y\n\tz\n"));
    // Deeper at 8, not deeper at 1.
    assert!(refused("if x:\n        y\n\tif z:\n\t w\n"));
    assert!(refused("if x:\n\t\tx\n\t        y\n"));
    // A dedent equal at 8 and unequal at 1.
    assert!(refused("if x:\n\tif y:\n\t\tz\n        w\n"));
    assert!(refused("if x:\n\t y\n  \t z\n"));
    // Consistent mixes are read.
    assert!(!refused("if x:\n  \ty\n"));
    assert!(!refused("if x:\n\t  y\n"));
    assert!(!refused("if x:\n\t\ty\n\t\tz\n"));
}

#[test]
fn a_dedent_must_match_an_outer_level() {
    assert!(refused("if x:\n    y\n  z\n"));
    assert!(refused("if x:\n    if y:\n        z\n  w\n"));
    assert!(refused("if x:\n    if y:\n        z\n      w\n"));
    assert!(refused("if a:\n  if b:\n    c\n   d\n"));
    assert_eq!(
        types("if a:\n  if b:\n    if c:\n      d\ne\n"),
        "NAME NAME OP NEWLINE INDENT NAME NAME OP NEWLINE INDENT NAME NAME OP NEWLINE \
         INDENT NAME NEWLINE DEDENT DEDENT DEDENT NAME NEWLINE ENDMARKER"
    );
    // An unexpected indent is the parser's, not the tokenizer's.
    assert_eq!(
        types("x\n  y\n"),
        "NAME NEWLINE INDENT NAME NEWLINE DEDENT ENDMARKER"
    );
}

#[test]
fn nul_is_refused_anywhere() {
    for s in ["\0", "a\0", "x # \0\n", "'\0'", "x = 1\n\0"] {
        assert!(refused(s), "{s:?}");
    }
}

fn levels(n: usize, tab: bool) -> String {
    let pad = if tab { "\t" } else { " " };
    let mut s = String::new();
    for k in 0..n {
        s += &format!("{}if x:\n", pad.repeat(k));
    }
    s + &pad.repeat(n) + "pass\n"
}

#[test]
fn indentation_stops_at_99_levels() {
    assert_eq!(MAX_INDENT, 100);
    for tab in [false, true] {
        assert!(!refused(&levels(98, tab)));
        assert!(!refused(&levels(99, tab)));
        assert!(refused(&levels(100, tab)));
    }
    let m = module_tokens(&cps(&levels(99, false))).unwrap();
    let indents = m
        .tokens
        .iter()
        .filter(|t| t.type_name() == "INDENT")
        .count();
    let dedents = m
        .tokens
        .iter()
        .filter(|t| t.type_name() == "DEDENT")
        .count();
    assert_eq!((indents, dedents), (99, 99));
    // The limit is on the depth at once, not on the indents in all.
    let again = levels(99, false) + "y\n" + &levels(99, false);
    assert!(!refused(&again));
}

#[test]
fn the_first_continuation_fixes_the_column() {
    assert!(refused("if x:\n    \\\n  y\n  z\n"));
    assert_eq!(
        types("if x:\n  \\\n    y\n    z\n"),
        "NAME NAME OP NEWLINE INDENT NAME NEWLINE INDENT NAME NEWLINE DEDENT DEDENT ENDMARKER"
    );
    assert!(refused("x = 1 \\"));
    assert!(refused("if x:\n y\n \\"));
    assert!(refused("x \\ y\n"));
    assert_eq!(types("x = \\\n\n"), "NAME OP NEWLINE ENDMARKER");
}

#[test]
fn a_form_feed_resets_the_column() {
    assert_eq!(types("\x0c  \x0cx\n"), "NAME NEWLINE ENDMARKER");
    assert_eq!(
        types("if x:\n  y\n\x0c  z\n"),
        "NAME NAME OP NEWLINE INDENT NAME NEWLINE NAME NEWLINE DEDENT ENDMARKER"
    );
    assert!(refused("if x:\n  y\n  \x0c z\n"));
}

#[test]
fn comment_and_blank_lines_leave_the_indentation() {
    assert_eq!(
        types("if x:\n  y\n    # c\n  z\n"),
        "NAME NAME OP NEWLINE INDENT NAME NEWLINE COMMENT NL NAME NEWLINE DEDENT ENDMARKER"
    );
    assert_eq!(
        types("if x:\n    y\n# c\n"),
        "NAME NAME OP NEWLINE INDENT NAME NEWLINE COMMENT NL DEDENT ENDMARKER"
    );
    assert_eq!(
        types("x = (\n  1\n    )\n"),
        "NAME OP OP NL NUMBER NL OP NEWLINE ENDMARKER"
    );
}

#[test]
fn the_last_line_gets_its_newline() {
    assert_eq!(
        types("if x:\n y"),
        "NAME NAME OP NEWLINE INDENT NAME NEWLINE DEDENT ENDMARKER"
    );
    assert_eq!(types(""), "ENDMARKER");
    assert_eq!(types("# c"), "COMMENT NL ENDMARKER");
    assert_eq!(types("x\r\ny\r"), "NAME NEWLINE NAME NEWLINE ENDMARKER");
}

#[test]
fn names_are_checked_by_xid_and_a_bom_is_refused() {
    assert!(!refused("\u{e9}t\u{e9} = 1\n"));
    assert!(!refused("x\u{b7} = 1\n"));
    assert!(refused("\u{b7}x = 1\n"));
    assert!(refused("\u{feff}x\n"));
    assert!(refused("x\u{a0}= 1\n"));
}

#[test]
fn only_fstrings_with_braces_or_backslashes_are_declined() {
    assert_eq!(types("f'a' rf\"b\"\n"), "FSTRING FSTRING NEWLINE ENDMARKER");
    for s in ["f'{x}'\n", "f'}}'\n", "f'a\\n'\n"] {
        assert_eq!(
            module_tokens(&cps(s)),
            Err(ModuleError::NeedsFString),
            "{s:?}"
        );
    }
    assert!(refused("f'a\n"));
}

#[test]
fn the_first_of_several_continuations_keeps_the_column() {
    assert_eq!(
        types("if 1:\n  a\n  \\\n  \\\n  b\n"),
        "NAME NUMBER OP NEWLINE INDENT NAME NEWLINE NAME NEWLINE DEDENT ENDMARKER"
    );
}

#[test]
fn a_newline_ends_a_single_quoted_fstring() {
    assert!(refused("f\"a\n\"\n"));
    assert!(refused("f'a\n'\n"));
}

#[test]
fn an_indent_starts_at_its_line() {
    let m = module_tokens(&cps("if 1:\n  x\n")).unwrap();
    let t = m.tokens.iter().find(|t| t.type_name() == "INDENT").unwrap();
    assert_eq!((t.start, t.end), (6, 8));
}

#[test]
fn a_lone_surrogate_and_u0080_are_refused() {
    assert_eq!(
        module_tokens(&[0xDFFF, 10]),
        Err(ModuleError::Syntax("surrogate"))
    );
    assert_eq!(
        module_tokens(&[0xD800, 10]),
        Err(ModuleError::Syntax("surrogate"))
    );
    assert!(refused("\u{80}\n"));
}
