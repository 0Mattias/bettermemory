//! Cases a plausible wrong f-string tokenizer fails: the closing quote
//! inside a field, a doubled brace beside a field, a backslash before a
//! brace, `\N{` raw and not, `:` inside brackets, `!=`, the two nesting
//! limits off by one, and a line end in a literal part against one in a
//! field. Each answer is CPython 3.13.13's, read from
//! `tokenize._generate_tokens_from_c_tokenizer` with `extra_tokens` false.

use bettermemory_core::pysyntax::fstring_lex::{MAX_EXPR_NESTING, MAX_FSTRING_LEVEL};
use bettermemory_core::pysyntax::module_lex::{module_tokens, ModuleError};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// The tokens spelled as the golden file spells them.
fn spans(s: &str) -> String {
    let m = module_tokens(&cps(s)).unwrap_or_else(|e| panic!("refused {s:?}: {e:?}"));
    m.tokens
        .iter()
        .filter_map(|t| match t.type_name() {
            "COMMENT" | "NL" => None,
            n @ ("NEWLINE" | "INDENT" | "DEDENT" | "ENDMARKER") => Some(n.to_string()),
            n => Some(format!("{n}:{}:{}", t.start, t.end)),
        })
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
fn a_quote_inside_a_field_opens_a_string() {
    assert_eq!(
        spans("f'{'a'}' y\n"),
        "FSTRING_START:0:2 OP:2:3 STRING:3:6 OP:6:7 FSTRING_END:7:8 NAME:9:10 NEWLINE ENDMARKER"
    );
    assert_eq!(
        spans("f\"{'}'}\" + 1\n"),
        "FSTRING_START:0:2 OP:2:3 STRING:3:6 OP:6:7 FSTRING_END:7:8 OP:9:10 NUMBER:11:12 NEWLINE ENDMARKER"
    );
}

#[test]
fn a_doubled_brace_keeps_its_first_brace() {
    assert_eq!(
        spans("f'a{{b'\n"),
        "FSTRING_START:0:2 FSTRING_MIDDLE:2:4 FSTRING_MIDDLE:5:6 FSTRING_END:6:7 NEWLINE ENDMARKER"
    );
    // The field closes first, then two braces are one.
    assert_eq!(
        spans("f'{x}}}'\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:4:5 FSTRING_MIDDLE:5:6 FSTRING_END:7:8 NEWLINE ENDMARKER"
    );
    // Two braces are one, then a field opens.
    assert_eq!(
        spans("f'{{{x}'\n"),
        "FSTRING_START:0:2 FSTRING_MIDDLE:2:3 OP:4:5 NAME:5:6 OP:6:7 FSTRING_END:7:8 NEWLINE ENDMARKER"
    );
    assert!(refused("f'{x}}'\n"));
    assert!(refused("f'}'\n"));
}

#[test]
fn after_a_nested_field_a_specification_reads_doubled_braces() {
    assert_eq!(
        spans("f'{x:{y}{{z}}}'\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:4:5 OP:5:6 NAME:6:7 OP:7:8 FSTRING_MIDDLE:8:9 \
         FSTRING_MIDDLE:10:11 OP:11:12 FSTRING_MIDDLE:12:13 FSTRING_END:14:15 NEWLINE ENDMARKER"
    );
}

#[test]
fn a_backslash_before_a_brace_is_literal() {
    assert_eq!(
        spans("f'a\\{x}'\n"),
        "FSTRING_START:0:2 FSTRING_MIDDLE:2:4 OP:4:5 NAME:5:6 OP:6:7 FSTRING_END:7:8 NEWLINE ENDMARKER"
    );
    assert!(!refused("f'a\\}}'\n"));
}

#[test]
fn a_named_escape_is_read_only_when_not_raw() {
    assert_eq!(
        spans("rf'\\N{x}'\n"),
        "FSTRING_START:0:3 FSTRING_MIDDLE:3:5 OP:5:6 NAME:6:7 OP:7:8 FSTRING_END:8:9 NEWLINE ENDMARKER"
    );
    assert_eq!(
        module_tokens(&cps("f'\\N{x}'\n")),
        Err(ModuleError::NeedsNameTable)
    );
    // The escape's `}` ends the part; a second `}` is single.
    assert!(refused("f'\\N{a}b}'\n"));
}

#[test]
fn a_named_escape_in_a_plain_string_declines_only_when_nothing_refuses() {
    // CPython's tokenizer refuses each of these after the string; the
    // string's `\N{...}` alone would need the name table.
    assert!(refused(
        "fr'''{ '{'!s'\\777\\N{DASH}''' # c\ny = (1,\n 2)\n"
    ));
    assert!(refused("f'{\"\\N{DASH}\"}' )\n"));
    assert!(refused("'\\N{DASH}' (\n"));
    assert_eq!(
        module_tokens(&cps("'\\N{DASH}'\n")),
        Err(ModuleError::NeedsNameTable)
    );
}

#[test]
fn a_colon_inside_brackets_is_an_operator() {
    assert_eq!(
        spans("f'{x[1:2]}'\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:4:5 NUMBER:5:6 OP:6:7 NUMBER:7:8 OP:8:9 OP:9:10 \
         FSTRING_END:10:11 NEWLINE ENDMARKER"
    );
    assert_eq!(
        spans("f'{(x:y)}'\n"),
        "FSTRING_START:0:2 OP:2:3 OP:3:4 NAME:4:5 OP:5:6 NAME:6:7 OP:7:8 OP:8:9 FSTRING_END:9:10 \
         NEWLINE ENDMARKER"
    );
    // At the field's depth `:=` is `:` and a specification `=1`.
    assert_eq!(
        spans("f'{x:=1}'\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:4:5 FSTRING_MIDDLE:5:7 OP:7:8 FSTRING_END:8:9 \
         NEWLINE ENDMARKER"
    );
    // A closing quote in a specification ends the f-string.
    assert_eq!(
        spans("f'{x:'}\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:4:5 FSTRING_END:5:6 OP:6:7 NEWLINE ENDMARKER"
    );
}

#[test]
fn not_equal_stays_one_operator() {
    assert_eq!(
        spans("f'{x!=y}'\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:4:6 NAME:6:7 OP:7:8 FSTRING_END:8:9 NEWLINE ENDMARKER"
    );
    assert_eq!(
        spans("f'{x!r}'\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:4:5 NAME:5:6 OP:6:7 FSTRING_END:7:8 NEWLINE ENDMARKER"
    );
}

#[test]
fn the_nesting_limits() {
    assert_eq!(MAX_FSTRING_LEVEL, 150);
    let nested = |n: usize| format!("{}x{}\n", "f'{".repeat(n), "}'".repeat(n));
    assert!(!refused(&nested(149)));
    assert!(refused(&nested(150)));
    assert_eq!(MAX_EXPR_NESTING, 3);
    assert!(!refused("f'{x:{y:{z}}}'\n"));
    assert!(refused("f'{x:{y:{z:{w}}}}'\n"));
    // The field's brace counts among the 200 brackets.
    let deep = |n: usize| format!("f'{{{}x{}}}'\n", "(".repeat(n), ")".repeat(n));
    assert!(!refused(&deep(199)));
    assert!(refused(&deep(200)));
}

#[test]
fn a_line_end_ends_a_literal_part_but_not_a_field() {
    assert!(refused("f'a\nb'\n"));
    assert_eq!(
        spans("f'{x\n}'\n"),
        "FSTRING_START:0:2 OP:2:3 NAME:3:4 OP:5:6 FSTRING_END:6:7 NEWLINE ENDMARKER"
    );
    assert!(refused("f'{x:a\nb}'\n"));
    assert_eq!(
        spans("f'''{x:a\nb}'''\n"),
        "FSTRING_START:0:4 OP:4:5 NAME:5:6 OP:6:7 FSTRING_MIDDLE:7:10 OP:10:11 FSTRING_END:11:14 \
         NEWLINE ENDMARKER"
    );
    // A comment in a single-quoted field swallows the rest of the line.
    assert!(refused("f'{x # c}'\n"));
}

#[test]
fn a_literal_part_is_decoded_unless_raw() {
    assert!(refused("f'\\x'\n"));
    assert!(!refused("rf'\\x'\n"));
    assert!(refused("f'{x:\\x}'\n"));
    assert!(!refused("rf'{x:\\x}'\n"));
    assert!(!refused("f'a\\{x}'\n"));
}
