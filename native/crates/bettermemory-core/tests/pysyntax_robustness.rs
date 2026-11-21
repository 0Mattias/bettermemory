//! `pysyntax::lex` over hostile input: very long strings and numbers,
//! deep nesting, many lines. No panic in debug or release, no recursion,
//! and time linear in the input where the reference's is.

mod timing;

use bettermemory_core::pysyntax::lex::{literal_tokens, LexError, Tok};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

#[test]
fn a_200000_character_string_is_linear() {
    let make = |n: usize| cps(&format!("'{}\\x41\\u00e9'", "ab\\n".repeat(n / 4)));
    let (small, large) = (make(20_000), make(200_000));
    let lexed = literal_tokens(&large).unwrap();
    assert!(matches!(&lexed.tokens[0].tok, Tok::Str(s) if s.len() == 150_002));
    timing::grows_within(
        "string: 20,000 and 200,000 characters",
        timing::TENFOLD,
        &mut || timing::once(|| literal_tokens(&small)),
        &mut || timing::once(|| literal_tokens(&large)),
    );
}

#[test]
fn a_200000_digit_number_is_refused_in_linear_time() {
    let make = |n: usize| cps(&"7".repeat(n));
    let (small, large) = (make(20_000), make(200_000));
    assert!(matches!(literal_tokens(&large), Err(LexError::Syntax(_))));
    timing::grows_within(
        "decimal: 20,000 and 200,000 digits",
        timing::TENFOLD,
        &mut || timing::once(|| literal_tokens(&small)),
        &mut || timing::once(|| literal_tokens(&large)),
    );
    let float = cps(&format!("{}.5e-3", "7".repeat(200_000)));
    assert!(matches!(
        literal_tokens(&float).unwrap().tokens[0].tok,
        Tok::Float(_)
    ));
}

#[test]
fn a_long_radix_literal_reads() {
    let text = cps(&format!("0x{}", "f".repeat(20_000)));
    let lexed = literal_tokens(&text).unwrap();
    let Tok::Int(d) = &lexed.tokens[0].tok else {
        panic!()
    };
    assert_eq!(d.len(), 24_083);
    assert!(d.ends_with("63909375"), "{}", &d[d.len() - 8..]);
}

#[test]
fn a_radix_literal_converts_below_quadratic_time() {
    // Karatsuba: ten times the digits cost about 38 times the work, where a
    // quadratic conversion costs 100 times.
    let make = |n: usize| cps(&format!("0x{}", "f7".repeat(n / 2)));
    let (small, large) = (make(10_000), make(100_000));
    timing::grows_within(
        "hex literal: 10,000 and 100,000 digits",
        timing::TENFOLD,
        &mut || timing::once(|| literal_tokens(&small)),
        &mut || timing::once(|| literal_tokens(&large)),
    );
}

#[test]
fn deep_nesting_does_not_recurse() {
    let deep = cps(&format!("{}{}", "[".repeat(200), "]".repeat(200)));
    assert_eq!(literal_tokens(&deep).unwrap().tokens.len(), 400);
    for n in [201, 10_000, 1_000_000] {
        let text = cps(&"(".repeat(n));
        assert!(literal_tokens(&text).is_err());
    }
    let closes = cps(&")".repeat(1_000_000));
    assert!(literal_tokens(&closes).is_err());
}

#[test]
fn many_lines_and_tokens() {
    let lines = cps(&format!("(1{})", ",\n#c\n".repeat(100_000)));
    assert_eq!(literal_tokens(&lines).unwrap().tokens.len(), 100_003);
    let blank = cps(&format!("1{}", "\n \\\n\t#x\n".repeat(50_000)));
    assert_eq!(literal_tokens(&blank).unwrap().tokens.len(), 1);
    let unterminated = cps(&format!("'''{}", "x\\".repeat(100_000)));
    assert!(literal_tokens(&unterminated).is_err());
}

#[test]
fn every_short_text_ends() {
    // Every text of up to three characters of a hostile alphabet.
    let alphabet = cps("0x_.ej'\"\\\n\r ([)}#bfrN{\u{e9}\u{ff53}\0");
    let mut seen = 0usize;
    for &a in &alphabet {
        for &b in &alphabet {
            for &c in &alphabet {
                let _ = literal_tokens(&[a, b, c]);
                let _ = literal_tokens(&[a, b]);
                seen += 1;
            }
        }
    }
    assert_eq!(seen, alphabet.len().pow(3));
}
