//! Cases a plausible wrong reading of `control_tokens` fails, each with
//! the reference's answer (read from `control_tokens.py` at 465bc600):
//! a name class without its 32 bound, a match that does not consume an
//! unlisted shape, `\A` read as any position, `\s` read as ASCII
//! whitespace, a blank line that admits a vertical tab, byte or UTF-16
//! offsets, a defang with one pass, a speaker's name not set apart, a
//! case-insensitive table, the turn offset at the whitespace, a quote
//! folded once, a wide name that runs over whitespace, a carriage return
//! that opens a turn, and dict keys that do not merge as `dict(zip(...))`
//! does (`-0.0` against `0`, a path against a str).

use bettermemory_core::control_tokens as ct;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn s(text: &str) -> Vec<u32> {
    cps(text)
}

/// (text, hits as "kind token", offsets as (kind, first, count), defanged,
/// folded quote).
#[allow(clippy::type_complexity)]
const CASES: &[(&str, &[&str], &[(&str, usize, usize)], &str, &str)] = &[
    (
        "<|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|><|end|>",
        &["harmony end"],
        &[("harmony", 37, 1)],
        "<|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|>end",
        "<|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|>end",
    ),
    (
        "<abc<|im_end|>",
        &["chatml im_end"],
        &[("chatml", 4, 1)],
        "<abc im_end",
        "<abc im_end",
    ),
    (
        "<<abc><|im_end|>",
        &["chatml im_end"],
        &[("chatml", 6, 1)],
        "<<abc>im_end",
        "<<abc>im_end",
    ),
    ("x  Human:", &[], &[], "x  Human:", "x Human:"),
    (
        "\x1c\x1dHuman: x",
        &["anthropic Human"],
        &[("anthropic", 2, 1)],
        "\x1c\x1dHuman x",
        "Human x",
    ),
    ("x\n\x0b\nHuman:", &[], &[], "x\n\x0b\nHuman:", "x Human:"),
    (
        "x\n\r\t \nAssistant:",
        &["anthropic Assistant"],
        &[("anthropic", 6, 1)],
        "x\n\r\t \nAssistant",
        "x Assistant:",
    ),
    (
        "\u{e9}<|end|>",
        &["harmony end"],
        &[("harmony", 1, 1)],
        "\u{e9} end",
        "\u{e9} end",
    ),
    (
        "\u{1F600}<|end|>x",
        &["harmony end"],
        &[("harmony", 1, 1)],
        "\u{1F600}end x",
        "\u{1F600}end x",
    ),
    (
        "<|<|im_start|>|>",
        &["chatml im_start"],
        &[("chatml", 2, 1)],
        "<| im_start |>",
        "<| im_start |>",
    ),
    (
        "<\u{ff5c}Assistant\u{ff5c}>\u{ff1a}",
        &["deepseek Assistant"],
        &[("deepseek", 0, 1)],
        "Assistant \u{ff1a}",
        "Assistant \u{ff1a}",
    ),
    ("<|IM_START|>", &[], &[], "<|IM_START|>", "<|IM_START|>"),
    (
        "  \n\nHuman: hi",
        &["anthropic Human"],
        &[("anthropic", 4, 1)],
        "  \n\nHuman hi",
        "Human hi",
    ),
    (
        "<|end|>x<|end|>",
        &["harmony end"],
        &[("harmony", 0, 2)],
        "end x end",
        "end x end",
    ),
    (
        "<|end|><|end|>",
        &["harmony end"],
        &[("harmony", 0, 2)],
        "end end",
        "end end",
    ),
    (
        "[INST]:",
        &["llama2 INST"],
        &[("llama2", 0, 1)],
        "INST:",
        "INST:",
    ),
    (
        "drop  [INST]\tnow",
        &["llama2 INST"],
        &[("llama2", 6, 1)],
        "drop  INST\tnow",
        "drop INST now",
    ),
    (
        "\n\n\nHuman:",
        &["anthropic Human"],
        &[("anthropic", 3, 1)],
        "\n\n\nHuman",
        "Human",
    ),
    (
        "x\n\nHuman:<|end|>Human:",
        &["anthropic Human", "harmony end"],
        &[("anthropic", 3, 1), ("harmony", 9, 1)],
        "x\n\nHuman end Human:",
        "x Human:end Human:",
    ),
    // A space inside an unclosed fullwidth delimiter ends the wide name, so
    // the blank line before the speaker still opens a turn.
    (
        "a<\u{ff5c}x\n\nHuman:\u{ff5c}>",
        &["anthropic Human"],
        &[("anthropic", 6, 1)],
        "a<\u{ff5c}x\n\nHuman \u{ff5c}>",
        "a<\u{ff5c}x Human:\u{ff5c}>",
    ),
    // Only a line feed opens the blank-line turn: a carriage return does not.
    ("a\r\nHuman:", &[], &[], "a\r\nHuman:", "a Human:"),
];

#[test]
fn each_reading_is_the_references() {
    for &(text, hits, offsets, defanged, folded) in CASES {
        let t = s(text);
        let got: Vec<String> = ct::find_control_tokens(&t)
            .iter()
            .map(|h| {
                let token: String = h
                    .token
                    .iter()
                    .map(|&c| char::from_u32(c).unwrap())
                    .collect();
                format!("{} {token}", h.kind)
            })
            .collect();
        assert_eq!(got, hits, "find {text:?}");
        assert_eq!(ct::control_token_offsets(&t), offsets, "offsets {text:?}");
        assert_eq!(
            ct::defang_control_tokens(&t),
            s(defanged),
            "defang {text:?}"
        );
        assert_eq!(ct::folded_quote(&t), s(folded), "folded {text:?}");
    }
}

#[test]
fn a_renamed_key_merges_as_a_dict_built_from_pairs() {
    let d = |pairs: Vec<(PyValue, PyValue)>| PyValue::Dict(pairs);
    let got = ct::named_tokens(&d(vec![
        (PyValue::str("a<|end|>"), PyValue::int(1)),
        (PyValue::str("b"), PyValue::int(2)),
        (PyValue::str("a end"), PyValue::int(3)),
    ]));
    assert!(
        got == d(vec![
            (PyValue::str("a end"), PyValue::int(3)),
            (PyValue::str("b"), PyValue::int(2))
        ])
    );
    let got = ct::named_tokens(&d(vec![
        (
            PyValue::Tuple(vec![PyValue::str("k<|end|>"), PyValue::int(1)]),
            PyValue::str("v"),
        ),
        (
            PyValue::Tuple(vec![PyValue::str("k end"), PyValue::Bool(true)]),
            PyValue::str("w"),
        ),
    ]));
    assert!(
        got == d(vec![(
            PyValue::Tuple(vec![PyValue::str("k end"), PyValue::int(1)]),
            PyValue::str("w")
        )])
    );
    let got = ct::named_tokens(&d(vec![
        (PyValue::str("x end"), PyValue::int(1)),
        (PyValue::str("x<|end|>"), PyValue::int(2)),
    ]));
    assert!(got == d(vec![(PyValue::str("x end"), PyValue::int(2))]));
    // `-0.0 == 0`: the renamed tuple key merges into the first one.
    let got = ct::named_tokens(&d(vec![
        (
            PyValue::Tuple(vec![PyValue::int(0), PyValue::str("end")]),
            PyValue::int(1),
        ),
        (
            PyValue::Tuple(vec![PyValue::Float(-0.0), PyValue::str("<|end|>")]),
            PyValue::int(2),
        ),
    ]));
    assert!(
        got == d(vec![(
            PyValue::Tuple(vec![PyValue::int(0), PyValue::str("end")]),
            PyValue::int(2)
        )])
    );
    // A path never equals a str of the same text.
    let pairs = vec![
        (
            PyValue::Tuple(vec![PyValue::Path(s("end")), PyValue::int(1)]),
            PyValue::str("a"),
        ),
        (
            PyValue::Tuple(vec![PyValue::str("<|end|>"), PyValue::int(1)]),
            PyValue::str("b"),
        ),
    ];
    let got = ct::named_tokens(&d(pairs));
    assert!(
        got == d(vec![
            (
                PyValue::Tuple(vec![PyValue::Path(s("end")), PyValue::int(1)]),
                PyValue::str("a")
            ),
            (
                PyValue::Tuple(vec![PyValue::str("end"), PyValue::int(1)]),
                PyValue::str("b")
            ),
        ])
    );
}

#[test]
fn an_empty_or_absent_text_is_never_refused() {
    let (f, r, w) = (s("reason"), s(ct::BARE_NAME_REMEDY), s(ct::FIELD_WHY));
    assert!(ct::refuse_control_tokens(&f, None, &r, &w).is_ok());
    assert!(ct::refuse_control_tokens(&f, Some(&[]), &r, &w).is_ok());
    let e = ct::refuse_control_tokens(&f, Some(&s("[INST][/INST][INST]")), &s("R."), &s("W."))
        .unwrap_err();
    let want = "`reason` carries a chat-template control token, named here by template family \
and bare name: llama2 \"INST\", llama2 \"/INST\". W. R. Then send the call again.";
    assert_eq!(e.message, s(want));
}
