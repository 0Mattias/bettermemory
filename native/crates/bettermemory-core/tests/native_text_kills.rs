//! Cases a plausible wrong port of `native_text`, `install_hints` or the
//! hook budget would fail. Each answer is the reference's, read from
//! `native_text.py`, `_install_hints.py` and `_daemon_client.py`.

use bettermemory_core::daemon_client as dc;
use bettermemory_core::install_hints as ih;
use bettermemory_core::native_text as nt;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn lone(c: u32) -> PyValue {
    PyValue::Str(vec![0x61, c])
}

#[test]
fn a_path_is_not_a_str() {
    // `isinstance(PurePosixPath(...), str)` is false.
    assert!(!nt::has_surrogates(&PyValue::Path(vec![0x2f, 0xDC80])));
    assert!(!nt::has_surrogates(&PyValue::Dict(vec![(
        PyValue::Path(vec![0xDC80]),
        s("v")
    )])));
}

#[test]
fn the_surrogate_range_bounds() {
    for (c, want) in [
        (0xD7FF, false),
        (0xD800, true),
        (0xDFFF, true),
        (0xE000, false),
    ] {
        assert_eq!(nt::has_surrogates(&lone(c)), want, "{c:#x}");
    }
    assert!(nt::has_surrogates(&PyValue::Tuple(vec![
        PyValue::int(1),
        lone(0xDC80)
    ])));
    assert!(nt::has_surrogates(&PyValue::Dict(vec![(
        lone(0xD800),
        PyValue::None
    )])));
}

#[test]
fn utf8_text_escapes_only_surrogates_lowercase() {
    assert_eq!(nt::utf8_text(&[0xDABC]), "\\udabc");
    assert_eq!(nt::utf8_text(&[0xD83D, 0xDE00]), "\\ud83d\\ude00");
    assert_eq!(nt::utf8_text(&cps("\u{1F600}\\x")), "\u{1F600}\\x");
    assert_eq!(nt::utf8_text(&[0]), "\0");
}

#[test]
fn projection_keeps_a_surrogate_key_with_a_clean_value() {
    let origin = vec![(lone(0xDC80), s("clean")), (s("b"), lone(0xDC81))];
    let (legacy, native) = nt::origin_projection(&origin).unwrap();
    assert_eq!(legacy.len(), 1);
    assert!(legacy[0] == (lone(0xDC80), s("clean")));
    assert_eq!(
        native.unwrap(),
        cps("{\"a\\udc80\":\"clean\",\"b\":\"a\\udc81\"}")
    );
}

#[test]
fn projection_sorts_keys_by_code_point_not_utf16() {
    // U+1F600 sorts after U+FFFF by code point, before it by UTF-16 unit.
    let origin = vec![(s("\u{1F600}"), s("x")), (s("\u{FFFF}"), lone(0xD800))];
    let (_, native) = nt::origin_projection(&origin).unwrap();
    assert_eq!(
        native.unwrap(),
        cps("{\"\\uffff\":\"a\\ud800\",\"\\ud83d\\ude00\":\"x\"}")
    );
}

#[test]
fn a_clean_origin_is_returned_whole_in_order() {
    let origin = vec![(s("z"), s("1")), (s("a"), PyValue::Path(vec![0xDC80]))];
    let (legacy, native) = nt::origin_projection(&origin).unwrap();
    assert!(legacy == origin);
    assert!(native.is_none());
}

#[test]
fn install_spellings() {
    assert_eq!(ih::extras_spec(""), "'bettermemory[]'");
    assert_eq!(
        ih::dev_clone_editable("a'b"),
        "uv pip install -e \".[a'b]\""
    );
    assert_eq!(
        ih::reinstall_extra_command("m", "x"),
        "`uv tool install --reinstall 'bettermemory[x]'` (pipx: `pipx install --force \
         'bettermemory[x]'`; inside the virtualenv that runs bettermemory: `uv pip install \
         --force-reinstall m`)"
    );
}

#[test]
fn hook_budgets() {
    assert_eq!(dc::hook_budget_seconds("prompt"), 8.0);
    assert_eq!(dc::hook_budget_seconds("stop"), 13.0);
    assert_eq!(dc::hook_budget_seconds("unknown"), 13.0);
    assert_eq!(dc::harness_timeout("Stop"), None);
    assert_eq!(dc::STORE_FILENAME, "memory.sqlite");
}
