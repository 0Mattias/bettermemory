//! Inputs a plausible wrong tokenizer answers otherwise than CPython
//! 3.13.13's eval mode. Each answer here is what `ast.literal_eval` gives
//! (or that it raises) under 3.13.13.

use bettermemory_core::pysyntax::lex::{literal_tokens, LexError, Tok};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn one(s: &str) -> Tok {
    let lexed = literal_tokens(&cps(s)).unwrap_or_else(|e| panic!("{s:?}: {e:?}"));
    assert_eq!(lexed.tokens.len(), 1, "{s:?}");
    lexed.tokens[0].tok.clone()
}

fn fails(s: &str) {
    assert!(
        matches!(literal_tokens(&cps(s)), Err(LexError::Syntax(_))),
        "{s:?} lexed"
    );
}

fn lexes(s: &str) -> usize {
    literal_tokens(&cps(s))
        .unwrap_or_else(|e| panic!("{s:?}: {e:?}"))
        .tokens
        .len()
}

#[test]
fn octal_escape_masked_to_a_byte_in_bytes_only() {
    // b'\777' == b'\xff'; '\777' == 'ǿ'.
    assert_eq!(one("b'\\777'"), Tok::Bytes(vec![0xFF]));
    assert_eq!(one("b'\\400'"), Tok::Bytes(vec![0x00]));
    assert_eq!(one("'\\777'"), Tok::Str(vec![0x1FF]));
    assert_eq!(one("'\\1011'"), Tok::Str(cps("A1")));
}

#[test]
fn u_does_not_combine_with_r_or_b() {
    for s in ["ur'x'", "uR'x'", "ub''", "bu''", "uf''", "bf''"] {
        // A name then a string: literal_eval raises.
        assert_eq!(lexes(s), 2, "{s:?}");
    }
    assert_eq!(one("Rb'\\x'"), Tok::Bytes(b"\\x".to_vec()));
    assert_eq!(one("U'x'"), Tok::Str(cps("x")));
    assert_eq!(one("rf'x'"), Tok::FString);
}

#[test]
fn leading_zeros_only_when_all_zeros() {
    assert_eq!(one("00"), Tok::Int("0".into()));
    assert_eq!(one("0_0"), Tok::Int("0".into()));
    fails("07");
    fails("0_7");
    assert_eq!(one("09.5"), Tok::Float(9.5));
    assert_eq!(one("07j"), Tok::Imag(7.0));
    fails("0__0");
    assert_eq!(one("0x_1"), Tok::Int("1".into()));
    fails("0x__1");
}

#[test]
fn nesting_200_and_201() {
    let deep = |n: usize| format!("{}1{}", "(".repeat(n), ")".repeat(n));
    assert_eq!(lexes(&deep(200)), 401);
    fails(&deep(201));
    fails(&format!("{}(", "[".repeat(200)));
}

#[test]
fn continuation_in_a_string() {
    assert_eq!(one("'a\\\nb'"), Tok::Str(cps("ab")));
    assert_eq!(one("'a\\\r\nb'"), Tok::Str(cps("ab")));
    assert_eq!(one("b'a\\\nb'"), Tok::Bytes(b"ab".to_vec()));
    assert_eq!(one("r'a\\\nb'"), Tok::Str(cps("a\\\nb")));
    assert_eq!(one("'''a\rb'''"), Tok::Str(cps("a\nb")));
    fails("'a\nb'");
}

#[test]
fn line_structure() {
    assert_eq!(lexes("\n1"), 1);
    fails("\n 1");
    fails("1\n2");
    fails("1\n  ");
    assert_eq!(lexes("1\n\\\n\n"), 1);
    fails("1\n\\\n");
    fails("1 \\\n");
    assert_eq!(lexes("\\\n \n1"), 1);
    fails("\\\n 1");
    assert_eq!(lexes("(1,\n 2)\n#c\n"), 5);
}

#[test]
fn numbers_end_where_cpython_ends_them() {
    fails("1e");
    fails("1x");
    fails("1.real");
    assert_eq!(lexes("1if 1 else 2"), 5);
    assert_eq!(lexes("1else"), 2);
    fails("1_");
    fails("1e+");
    assert_eq!(one("1e5_0"), Tok::Float(1e50));
    assert_eq!(one("1e309"), Tok::Float(f64::INFINITY));
    fails("0b12");
    fails("0o78");
}

#[test]
fn digit_limit_on_decimal_only() {
    assert!(matches!(one(&"1".repeat(4300)), Tok::Int(_)));
    fails(&"1".repeat(4301));
    assert!(matches!(one(&"0".repeat(10_000)), Tok::Int(_)));
    assert!(matches!(
        one(&format!("0x{}", "f".repeat(5000))),
        Tok::Int(_)
    ));
    assert_eq!(one("0x10"), Tok::Int("16".into()));
    assert_eq!(one("0b1_0_1"), Tok::Int("5".into()));
    assert_eq!(one("0o777"), Tok::Int("511".into()));
}

#[test]
fn escapes() {
    assert_eq!(one("'\\q'"), Tok::Str(cps("\\q")));
    assert_eq!(one("'\\8'"), Tok::Str(cps("\\8")));
    assert_eq!(one("'\\ud800'"), Tok::Str(vec![0xD800]));
    fails("'\\x4'");
    fails("'\\U00110000'");
    fails("'\\N'");
    assert_eq!(one("'\\N{EM DASH}'"), Tok::Str(vec![0x2014]));
    fails("'\\N{NO SUCH NAME}'");
    assert_eq!(one("r'\\N{EM DASH}'"), Tok::Str(cps("\\N{EM DASH}")));
    assert_eq!(one("b'\\u1234'"), Tok::Bytes(b"\\u1234".to_vec()));
    fails("b'\u{e9}'");
    assert_eq!(one("'\\\u{e9}'"), Tok::Str(cps("\\\u{e9}")));
}

#[test]
fn names_past_ascii() {
    // An identifier past ASCII is a NAME with its raw text; one that is
    // not an identifier is refused, whatever its NFKC reads.
    assert_eq!(lexes("\u{ff53}\u{ff45}\u{ff54}()"), 3);
    assert_eq!(one("\u{e9}"), Tok::Name);
    fails("\u{20ac}");
    fails("\u{24e2}\u{24d4}\u{24e3}()");
}

#[test]
fn form_feed_after_continuation_keeps_the_column() {
    // CPython's cont_line_col: IndentationError for each.
    fails("\n \\\n\u{c}'a'");
    fails("\u{c} \\\n\u{c}1");
    fails("1\n \\\n\u{c}");
    fails("\r\t\\\n\u{c}1#c ");
    assert_eq!(one("\\\n\u{c}1"), Tok::Int("1".into()));
}

#[test]
fn a_blank_line_does_not_reopen_the_expression() {
    fails("1\n\n2");
    fails("1\n#c\n2");
}

#[test]
fn keyword_after_a_number_needs_its_boundary() {
    fails("3or7");
    fails("11Jor81");
    assert_eq!(lexes("3or x"), 3);
    assert_eq!(lexes("3or"), 2);
    assert_eq!(lexes("1if7"), 2);
}

/// Decimal text of `digits` in `radix`, one digit at a time: the quadratic
/// schoolbook conversion the port's split conversion must agree with.
fn naive_decimal(digits: &[u32], radix: u32) -> String {
    let mut dec: Vec<u32> = vec![0];
    for &d in digits {
        let mut carry = d;
        for x in dec.iter_mut() {
            let v = *x * radix + carry;
            *x = v % 10;
            carry = v / 10;
        }
        while carry > 0 {
            dec.push(carry % 10);
            carry /= 10;
        }
    }
    while dec.len() > 1 && dec.last() == Some(&0) {
        dec.pop();
    }
    dec.iter()
        .rev()
        .map(|d| char::from_digit(*d, 10).unwrap())
        .collect()
}

#[test]
fn split_radix_conversion_matches_the_schoolbook_one() {
    // Lengths across the split point (512 digits) and the Karatsuba
    // threshold, with leading zeros, zero runs and every digit value.
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    for (prefix, radix) in [("0x", 16u32), ("0o", 8), ("0b", 2)] {
        for len in [511usize, 512, 513, 1024, 1025, 1500, 2047, 2048, 2049, 4100] {
            let mut digits: Vec<u32> = (0..len)
                .map(|_| {
                    state = state
                        .wrapping_mul(6_364_136_223_846_793_005)
                        .wrapping_add(1_442_695_040_888_963_407);
                    ((state >> 33) % u64::from(radix)) as u32
                })
                .collect();
            digits[0] = 0;
            for d in &mut digits[len / 3..len / 3 + 200] {
                *d = 0;
            }
            for d in &mut digits[len / 2..len / 2 + 100] {
                *d = radix - 1;
            }
            let text: String = digits
                .iter()
                .map(|&d| char::from_digit(d, radix).unwrap())
                .collect();
            let Tok::Int(got) = one(&format!("{prefix}{text}")) else {
                panic!("{prefix} {len}")
            };
            assert_eq!(got, naive_decimal(&digits, radix), "{prefix} {len}");
        }
        let Tok::Int(zero) = one(&format!("{prefix}{}", "0".repeat(3000))) else {
            panic!()
        };
        assert_eq!(zero, "0");
    }
}
