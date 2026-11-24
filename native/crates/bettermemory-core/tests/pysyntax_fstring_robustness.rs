//! The f-string modes of `pysyntax::module_lex` over hostile input: a
//! megabyte of literal text, 100,000 fields in one f-string, nesting to
//! the limit over and over, a megabyte of backslashes and of braces. No
//! function recurses (a 64 KiB thread reads the deepest nesting); time is
//! linear, as the reference's tokenizer is.

mod timing;

use bettermemory_core::pysyntax::module_lex::module_tokens;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn fields(n: usize) -> Vec<u32> {
    cps(&format!("x = f'{}'\n", "a{x!r:>{w}}{{".repeat(n)))
}

#[test]
fn a_100000_field_fstring_is_linear() {
    let (small, large) = (fields(10_000), fields(100_000));
    // Per field: a part, `{`, x, `!`, r, `:`, a part, `{`, w, `}`, an
    // empty part, `}` and the `{{` part (counts read from 3.13.13).
    let m = module_tokens(&large).unwrap();
    assert_eq!(m.tokens.len(), 3 + 100_000 * 13 + 3);
    timing::grows_within(
        "f-string: 10,000 and 100,000 fields",
        timing::TENFOLD,
        &mut || timing::once(|| module_tokens(&small).unwrap()),
        &mut || timing::once(|| module_tokens(&large).unwrap()),
    );
}

fn nested(n: usize) -> Vec<u32> {
    let one = format!("{}x{}\n", "f'{".repeat(149), "}'".repeat(149));
    cps(&one.repeat(n))
}

#[test]
fn nesting_to_the_limit_over_and_over_is_linear() {
    let (small, large) = (nested(20), nested(200));
    assert!(module_tokens(&large).is_ok());
    timing::grows_within(
        "f-string: 20 and 200 nestings to 149 levels",
        timing::TENFOLD,
        &mut || timing::once(|| module_tokens(&small).unwrap()),
        &mut || timing::once(|| module_tokens(&large).unwrap()),
    );
}

#[test]
fn the_deepest_nesting_needs_no_stack() {
    let text = nested(1);
    let got = std::thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(move || module_tokens(&text).map(|m| m.tokens.len()))
        .unwrap()
        .join()
        .unwrap();
    assert_eq!(got, Ok(149 * 4 + 3));
}

#[test]
fn a_megabyte_of_literal_text_is_linear() {
    let text = |n: usize| cps(&format!("f'''{}'''\n", "ab\\n\u{e9}\n".repeat(n)));
    let (small, large) = (text(20_000), text(200_000));
    assert_eq!(module_tokens(&large).unwrap().tokens.len(), 5);
    timing::grows_within(
        "f-string: 100 KB and a megabyte of literal text",
        timing::TENFOLD,
        &mut || timing::once(|| module_tokens(&small).unwrap()),
        &mut || timing::once(|| module_tokens(&large).unwrap()),
    );
}

#[test]
fn hostile_shapes_do_not_panic() {
    let mb = 1 << 20;
    let cases = [
        format!("f'{}'\n", "\\".repeat(mb)),
        format!("f'{}'\n", "\\".repeat(mb + 1)),
        format!("rf'{}'\n", "\\".repeat(mb)),
        format!("f'{}'\n", "{".repeat(mb)),
        format!("f'{}'\n", "}".repeat(mb)),
        format!("f'{}'\n", "{{".repeat(mb / 2)),
        format!("f'{}'\n", "\\{".repeat(mb / 2)),
        format!("f'{}'\n", "\\N{".repeat(mb / 3)),
        format!("f'{{x:{}'\n", ":".repeat(mb)),
        format!("f'{{x:{}}}'\n", "{y}".repeat(mb / 3)),
        "f'{".repeat(mb / 3),
        "f'{x".repeat(mb / 4),
        "f'''".repeat(mb / 4),
        "f'{x:{y:{".repeat(mb / 10),
        format!("f'{}'\n", "{(".repeat(300)),
        format!("f'{{{}'\n", "[".repeat(mb)),
        format!("f'{}", "a".repeat(mb)),
        format!("f'{{{}", "\n".repeat(mb)),
    ];
    for c in &cases {
        let _ = module_tokens(&cps(c));
    }
}
