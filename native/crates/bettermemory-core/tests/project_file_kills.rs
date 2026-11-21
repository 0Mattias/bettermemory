//! Inputs a plausible wrong `project_file` answers otherwise than the
//! reference.

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::project_file::{
    bad_char, directory, id_text, render, toml_string, ProjectFile, MAX_BYTES, QUOTE_LIMIT,
};
use bettermemory_core::pyvalue::PyStr;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn text(s: &[u32]) -> String {
    s.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

#[test]
fn the_id_is_bmp_and_26_crockford_capitals() {
    assert!(id_text(&cps("bmp_0123456789ABCDEFGHJKMNPQRS")));
    for bad in [
        "bmp_0123456789ABCDEFGHJKMNPQRI",
        "bmp_0123456789ABCDEFGHJKMNPQRL",
        "bmp_0123456789ABCDEFGHJKMNPQRO",
        "bmp_0123456789abcdefghjkmnpqrs",
        "BMP_0123456789ABCDEFGHJKMNPQRS",
        "bmp_0123456789ABCDEFGHJKMNPQRS\n",
        "bmp_0123456789ABCDEFGHJKMNPQR\u{ff33}",
    ] {
        assert!(!id_text(&cps(bad)), "{bad}");
    }
    assert_eq!(QUOTE_LIMIT, 40);
    assert_eq!(MAX_BYTES, 65_536);
}

#[test]
fn the_bad_characters() {
    for c in [0, 0x1f, 0x7f, 0x85, 0x9f, 0x2028, 0x2029] {
        assert!(bad_char(c), "{c:x}");
    }
    for c in [0x20, 0x7e, 0xa0, 0x2027, 0x202a, 0xfeff] {
        assert!(!bad_char(c), "{c:x}");
    }
}

#[test]
fn toml_strings_escape_only_c0_delete_quote_and_backslash() {
    assert_eq!(
        text(&toml_string(&cps("a\u{7f}\u{1f}\"\\\u{85}\u{2028}"))),
        "\"a\\u007F\\u001F\\\"\\\\\u{85}\u{2028}\""
    );
}

#[test]
fn render_writes_boundary_only_when_true_and_refuses_a_surrogate() {
    let p = |name: Vec<u32>, boundary| ProjectFile {
        id: cps("bmp_0123456789ABCDEFGHJKMNPQRS"),
        name,
        boundary,
        path: cps("/x"),
    };
    let off = String::from_utf8(render(&p(cps("n"), false)).unwrap()).unwrap();
    assert!(off.ends_with("name = \"n\"\n"));
    let on = String::from_utf8(render(&p(cps("n"), true)).unwrap()).unwrap();
    assert!(on.ends_with("name = \"n\"\nboundary = true\n"));
    assert_eq!(render(&p(vec![0xd800], false)), None);
}

#[test]
fn the_directory_is_path_parent() {
    // Path("bettermemory.toml").parent is "." where dirname gives "".
    for flavour in [Flavour::Posix, Flavour::Windows] {
        assert_eq!(text(&directory(flavour, &cps("bettermemory.toml"))), ".");
        assert_eq!(text(&directory(flavour, &cps("a/bettermemory.toml"))), "a");
    }
    assert_eq!(
        text(&directory(Flavour::Posix, &cps("/bettermemory.toml"))),
        "/"
    );
    assert_eq!(text(&directory(Flavour::Windows, &cps("C:x"))), "C:");
    assert_eq!(text(&directory(Flavour::Windows, &cps("C:\\x"))), "C:\\");
}
