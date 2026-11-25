//! Inputs a plausible wrong name lookup answers otherwise than CPython
//! 3.13.13's `\N{...}` (`getcode` with `with_named_seq` 0). Each answer is
//! what `ast.literal_eval` gives under 3.13.13 (`unicode_names.jsonl`).

use bettermemory_core::pysyntax::lex::{literal_tokens, Tok};
use bettermemory_core::pysyntax::literal::{literal_eval, Literal};
use bettermemory_core::unicode::names::{lookup, stored};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn name(s: &str) -> Option<u32> {
    lookup(&cps(s))
}

fn eval(s: &str) -> Option<Literal> {
    literal_eval(&cps(s)).ok()
}

#[test]
fn a_rule_prefix_compares_without_case_and_the_hex_too() {
    for spelled in [
        "CJK UNIFIED IDEOGRAPH-4E00",
        "cjk unified ideograph-4e00",
        "Cjk Unified Ideograph-4E00",
        "CJK UNIFIED IDEOGRAPH-4e00",
    ] {
        assert_eq!(name(spelled), Some(0x4E00), "{spelled}");
    }
    assert_eq!(name("tangut ideograph-187f7"), Some(0x187F7));
    assert_eq!(name("HANGUL SYLLABLE ga"), Some(0xAC00));
    assert_eq!(name("hangul syllable GA"), Some(0xAC00));
}

#[test]
fn the_hex_is_the_canonical_spelling_only() {
    for spelled in [
        "CJK UNIFIED IDEOGRAPH-04E00",
        "CJK UNIFIED IDEOGRAPH-004E00",
        "CJK UNIFIED IDEOGRAPH-4E0",
        "CJK UNIFIED IDEOGRAPH-+4E00",
        "CJK UNIFIED IDEOGRAPH- 4E00",
        "CJK UNIFIED IDEOGRAPH-4E00 ",
        "CJK UNIFIED IDEOGRAPH-02A6DF",
        "CJK UNIFIED IDEOGRAPH-FFFFFFFF4E00",
        "TANGUT IDEOGRAPH-017000",
        "CJK COMPATIBILITY IDEOGRAPH-0F900",
        "TANGUT COMPONENT-1",
    ] {
        assert_eq!(name(spelled), None, "{spelled}");
    }
    assert_eq!(name("CJK UNIFIED IDEOGRAPH-2a6df"), Some(0x2A6DF));
}

#[test]
fn a_name_reads_its_own_code_points_and_its_own_prefix_only() {
    // A code point past ASCII never matches by its low byte (U+0141 and
    // U+0154 end in `A` and `T`).
    assert_eq!(name("EM D\u{141}SH"), None);
    assert_eq!(name("BULLE\u{154}"), None);
    // A rule prefix reads its own ranges, not another prefix's.
    for spelled in [
        "TANGUT IDEOGRAPH-4E00",
        "CJK UNIFIED IDEOGRAPH-17000",
        "CJK UNIFIED IDEOGRAPH-F900",
        "NUSHU CHARACTER-4E00",
        "KHITAN SMALL SCRIPT CHARACTER-F900",
        "CJK COMPATIBILITY IDEOGRAPH-4E00",
    ] {
        assert_eq!(name(spelled), None, "{spelled}");
    }
    // Seven to nine hex digits: none is `%04X` of a code point, and nine
    // overflow 32 bits.
    for spelled in [
        "CJK UNIFIED IDEOGRAPH-FFFFFFF",
        "CJK UNIFIED IDEOGRAPH-FFFFFFFF",
        "CJK UNIFIED IDEOGRAPH-FFFFFFFFF",
        "CJK UNIFIED IDEOGRAPH-100004E00",
    ] {
        assert_eq!(name(spelled), None, "{spelled}");
    }
}

#[test]
fn each_rule_range_ends_where_the_interpreter_ends_it() {
    let ends = [
        ("CJK UNIFIED IDEOGRAPH-", 0x3400, 0x4DBF),
        ("CJK UNIFIED IDEOGRAPH-", 0x4E00, 0x9FFF),
        ("CJK UNIFIED IDEOGRAPH-", 0x2A700, 0x2B739),
        ("CJK UNIFIED IDEOGRAPH-", 0x31350, 0x323AF),
        ("TANGUT IDEOGRAPH-", 0x17000, 0x187F7),
        ("TANGUT IDEOGRAPH-", 0x18D00, 0x18D08),
        ("CJK COMPATIBILITY IDEOGRAPH-", 0xFA70, 0xFAD9),
        ("NUSHU CHARACTER-", 0x1B170, 0x1B2FB),
    ];
    for (prefix, first, last) in ends {
        assert_eq!(name(&format!("{prefix}{first:04X}")), Some(first));
        assert_eq!(name(&format!("{prefix}{last:04X}")), Some(last));
        assert_eq!(name(&format!("{prefix}{:04X}", first - 1)), None);
        assert_eq!(name(&format!("{prefix}{:04X}", last + 1)), None);
    }
    // Unicode 16.0 names these; 15.1.0 does not.
    assert_eq!(name("EGYPTIAN HIEROGLYPH-13460"), None);
}

#[test]
fn aliases_resolve_and_none_is_dropped() {
    assert_eq!(name("NULL"), Some(0));
    assert_eq!(name("NUL"), Some(0));
    assert_eq!(name("lf"), Some(0x0A));
    assert_eq!(name("BOM"), Some(0xFEFF));
    assert_eq!(name("ZWNBSP"), Some(0xFEFF));
    // A corrected name: the alias and the original both read.
    assert_eq!(name("LATIN CAPITAL LETTER GHA"), Some(0x1A2));
    assert_eq!(name("LATIN CAPITAL LETTER OI"), Some(0x1A2));
    // The last alias.
    assert_eq!(name("VS256"), Some(0xE01EF));
    // Control characters have aliases and no name.
    assert_eq!(name("<control>"), None);
    assert_eq!(name("DELETE"), Some(0x7F));
    let aliases = stored()
        .filter(|(n, _)| *n == "NULL" || *n == "VS256")
        .count();
    assert_eq!(aliases, 2);
}

#[test]
fn a_named_sequence_is_not_a_name() {
    for sequence in [
        "KEYCAP NUMBER SIGN",
        "LATIN CAPITAL LETTER A WITH MACRON AND GRAVE",
        "MODIFIER LETTER EXTRA-LOW EXTRA-HIGH CONTOUR TONE BAR",
    ] {
        assert_eq!(name(sequence), None, "{sequence}");
        assert!(eval(&format!("'\\N{{{sequence}}}'")).is_none());
    }
}

#[test]
fn hangul_syllables_compose_from_the_right_short_names() {
    let cases = [
        ("GA", 0xAC00),
        ("GAG", 0xAC01),
        ("GAGG", 0xAC02),
        ("GAGS", 0xAC03),
        ("GGA", 0xAE4C),
        ("A", 0xC544),
        ("YAE", 0xC598),
        ("WAELH", 0xC66B),
        ("SSYEOLH", 0xC3AF),
        ("HIH", 0xD7A3),
    ];
    for (short, cp) in cases {
        assert_eq!(
            name(&format!("HANGUL SYLLABLE {short}")),
            Some(cp),
            "{short}"
        );
    }
    for short in ["", "G", "GGGA", "GAX", "HIHH", "GA "] {
        assert_eq!(name(&format!("HANGUL SYLLABLE {short}")), None, "{short:?}");
    }
    assert_eq!(name("HANGUL SYLLABLEGA"), None);
    assert_eq!(name("HANGUL  SYLLABLE GA"), None);
}

#[test]
fn spacing_and_spelling_must_be_exact() {
    for spelled in [
        "EM  DASH", " EM DASH", "EM DASH ", "EMDASH", "EM-DASH", "EM\tDASH", "",
    ] {
        assert_eq!(name(spelled), None, "{spelled:?}");
    }
    assert_eq!(name("eM dAsH"), Some(0x2014));
    assert_eq!(name("HYPHEN-MINUS"), Some(0x2D));
    assert_eq!(name("LATIN SMALL LETTER A\u{e9}"), None);
    assert_eq!(name("\u{e9}"), None);
}

#[test]
fn the_decoder_stops_at_the_first_name_it_cannot_read() {
    let s = |t: &str| match eval(t) {
        Some(Literal::Str(s)) => Some(s),
        _ => None,
    };
    assert_eq!(
        s("'a\\N{EM DASH}b\\N{BULLET}c'"),
        Some(cps("a\u{2014}b\u{2022}c"))
    );
    assert_eq!(s("'\\N{EM DASH}}'"), Some(cps("\u{2014}}")));
    assert_eq!(s("'\\\\\\N{EM DASH}'"), Some(cps("\\\u{2014}")));
    assert_eq!(s("'\\\\N{EM DASH}'"), Some(cps("\\N{EM DASH}")));
    for refused in [
        "'\\N{EM DASH}\\N{X}'",
        "'\\N{X}\\N{EM DASH}'",
        "'\\N{}'",
        "'\\N'",
        "'\\N{EM DASH'",
        "'\\N{{EM DASH}'",
        "'\\N{EM DASH\\N{BULLET}}'",
    ] {
        assert!(eval(refused).is_none(), "{refused}");
    }
    // Raw and bytes literals do not read the escape.
    assert_eq!(s("r'\\N{X}'"), Some(cps("\\N{X}")));
    assert_eq!(
        eval("b'\\N{EM DASH}'"),
        Some(Literal::Bytes(b"\\N{EM DASH}".to_vec()))
    );
}

#[test]
fn the_identifier_check_reads_the_raw_run() {
    // NFKC of each reads `set`, but circled letters are no identifier.
    assert!(eval("\u{24e2}\u{24d4}\u{24e3}()").is_none());
    // Modifier letters are, and the parser keeps their NFKC.
    assert_eq!(
        eval("\u{2e2}\u{1d49}\u{1d57}()"),
        Some(Literal::Set(Vec::new()))
    );
    assert_eq!(
        eval("\u{ff53}\u{ff45}\u{ff54}()"),
        Some(Literal::Set(Vec::new()))
    );
    assert_eq!(eval("\u{17f}et()"), Some(Literal::Set(Vec::new())));
    // `True`, `False` and `None` spelled past ASCII are names, not
    // constants, and `literal_eval` refuses them.
    for constant in [
        "\u{ff34}\u{ff52}\u{ff55}\u{ff45}",
        "\u{ff26}\u{ff41}\u{ff4c}\u{ff53}\u{ff45}",
        "\u{ff2e}\u{ff4f}\u{ff4e}\u{ff45}",
    ] {
        assert!(eval(constant).is_none(), "{constant}");
        let lexed = literal_tokens(&cps(constant)).expect("a name");
        assert_eq!(lexed.tokens.len(), 1);
        assert_eq!(lexed.tokens[0].tok, Tok::Name);
    }
    assert!(eval("\u{ff33}\u{ff25}\u{ff34}()").is_none());
    assert!(eval("set\u{b7}()").is_none());
    assert!(eval("\u{e9}").is_none());
}
