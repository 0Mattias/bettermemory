//! Inputs a plausible wrong `identity` fails: each assertion is the
//! reference's answer (checked against CPython 3.13.13) where a natural
//! simplification answers otherwise.

use bettermemory_core::identity::{self as id, Actor, Published, Request};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn c(t: &str) -> Option<PyStr> {
    id::clean(Some(&cps(t)), true)
}

fn http(headers: &[(&str, &str)]) -> Request {
    Request {
        headers: Some(headers.iter().map(|(k, v)| (cps(k), cps(v))).collect()),
        ..Request::default()
    }
}

fn run(request: Option<&Request>, env: &[(&str, &str)]) -> (id::Caller, Vec<String>, usize) {
    let mut asked = Vec::new();
    let mut n = 0;
    let caller = id::resolve(
        request,
        &mut |k| {
            asked.push(k.to_string());
            env.iter().find(|(key, _)| *key == k).map(|(_, v)| cps(v))
        },
        &mut || {
            n += 1;
            Some(cps("/cwd"))
        },
    );
    (caller, asked, n)
}

#[test]
fn an_ascii_value_is_never_walked_whatever_its_length() {
    // A port that bounds every value by the walk's length drops it.
    let long = format!("{}x", " ".repeat(2000));
    assert_eq!(c(&long), Some(cps("x")));
}

#[test]
fn the_walk_bound_counts_before_format_characters_go() {
    // Stripping first and bounding after would keep both.
    assert_eq!(c(&format!("{}x", "\u{200b}".repeat(1024))), None);
    assert_eq!(c(&format!("{}x", "\u{200b}".repeat(1023))), Some(cps("x")));
}

#[test]
fn spaces_strip_at_the_ends_and_refuse_inside() {
    assert_eq!(c("a\u{a0}"), Some(cps("a")));
    assert_eq!(c("a\u{a0}b"), None);
    assert_eq!(c("a\u{1c}"), Some(cps("a")));
    assert_eq!(c("x\u{85}"), Some(cps("x")));
    assert_eq!(c("two words"), Some(cps("two words")));
    // The format characters go before the second strip.
    assert_eq!(c("\u{e9}\u{200b} "), Some(cps("\u{e9}")));
    assert_eq!(c("\u{feff} "), None);
}

#[test]
fn a_path_keeps_its_format_characters() {
    let zw = cps("\u{200b}/zw");
    assert_eq!(id::clean(Some(&zw), false), Some(zw.clone()));
    assert_eq!(c("\u{200b}/zw"), Some(cps("/zw")));
}

#[test]
fn a_lone_surrogate_is_kept() {
    let t = vec![0x61, 0xDC80];
    assert_eq!(id::clean(Some(&t), true), Some(t.clone()));
}

#[test]
fn the_json_header_bound_is_on_its_escaped_text() {
    // 128 astral characters fit; 129 are refused though the path is
    // 129 code points (a reference finding, reproduced).
    let n = |k: usize| cps(&format!("\"{}\"", "\\ud83d\\ude00".repeat(k)));
    assert_eq!(
        id::json_workspace(Some(&n(128))).map(|p| p.len()),
        Some(128)
    );
    assert_eq!(id::json_workspace(Some(&n(129))), None);
    assert_eq!(id::json_workspace(Some(&cps("\" \""))), Some(cps(" ")));
}

#[test]
fn header_keys_fold_as_str_lower_does() {
    let r = http(&[("x-bettermemory-wor\u{212a}space", "/kelvin")]);
    assert_eq!(run(Some(&r), &[]).0.workspace.path, Some(cps("/kelvin")));
    let r = http(&[
        ("X-BetterMemory-Client", "first"),
        ("x-bettermemory-client", "second"),
    ]);
    assert_eq!(run(Some(&r), &[]).0.actor.client, Some(cps("second")));
}

#[test]
fn over_http_the_environment_is_never_asked() {
    let (caller, asked, n) = run(Some(&http(&[])), &[("BETTERMEMORY_CLIENT", "e")]);
    assert!(asked.is_empty());
    assert_eq!(caller.actor.client, None);
    assert_eq!(n, 1);
    let (caller, asked, _) = run(Some(&Request::default()), &[("BETTERMEMORY_CLIENT", "e")]);
    assert_eq!(caller.actor.client, Some(cps("e")));
    assert_eq!(asked.len(), 5);
}

#[test]
fn the_working_directory_is_read_only_when_nothing_names_the_workspace() {
    let r = Request {
        root: Some(cps("/root")),
        ..Request::default()
    };
    let (caller, _, n) = run(Some(&r), &[]);
    assert_eq!(n, 0);
    assert_eq!(caller.workspace.source, cps("roots"));
    let (caller, _, n) = run(None, &[("BETTERMEMORY_WORKSPACE", "/e")]);
    assert_eq!((n, caller.workspace.source), (0, cps("env")));
}

#[test]
fn client_info_sources_land_in_assignment_order() {
    let mut r = http(&[("x-bettermemory-client-version", "hv")]);
    r.client_info = Some((Some(cps("info")), Some(cps("9"))));
    let a = run(Some(&r), &[]).0.actor;
    assert_eq!(
        a.sources,
        vec![
            (cps("client_version"), cps("header")),
            (cps("client"), cps("client-info"))
        ]
    );
}

#[test]
fn the_principal_is_not_format_stripped_and_the_session_prefers_the_transport() {
    let mut r = http(&[("mcp-session-id", "t"), ("x-bettermemory-session", "h")]);
    r.principal = Some(cps("p\u{200b}"));
    let a = run(Some(&r), &[]).0.actor;
    assert_eq!(a.principal, Some(cps("p\u{200b}")));
    assert_eq!(a.session, Some(cps("t")));
    assert_eq!(a.source("session"), Some(cps("transport").as_slice()));
}

#[test]
fn registry_keys_collide_as_the_references_do() {
    let mut a = Actor {
        client: Some(cps("a|model=b")),
        ..Actor::default()
    };
    a.set_source("client", "header");
    let mut b = Actor {
        client: Some(cps("a")),
        model: Some(cps("b")),
        ..Actor::default()
    };
    b.set_source("client", "header");
    b.set_source("model", "header");
    assert_eq!(id::registry_key(&a), id::registry_key(&b));
    let mut e = Actor {
        client: Some(cps("c")),
        ..Actor::default()
    };
    e.set_source("client", "env");
    assert_eq!(id::registry_key(&e), None);
}

#[test]
fn no_actor_matches_no_filter_and_empty_is_a_filter() {
    assert!(id::actor_matches(None, None, None));
    assert!(!id::actor_matches(None, Some(&cps("c")), None));
    let a = Actor::default();
    assert!(!id::actor_matches(Some(&a), Some(&[]), None));
}

#[test]
fn reset_restores_what_was_published_before() {
    let mut p = Published::default();
    let one = run(Some(&http(&[("x-bettermemory-client", "a")])), &[]).0;
    let two = run(Some(&http(&[("x-bettermemory-client", "b")])), &[]).0;
    let t1 = p.publish(one.clone());
    let t2 = p.publish(two);
    p.reset(t2);
    assert_eq!(p.current(), Some(&one));
    p.reset(t1);
    assert!(p.current().is_none());
    assert!(p.current_actor().is_none());
}

#[test]
fn set_source_again_replaces_the_key_in_place() {
    let mut a = Actor::default();
    a.set_source("client", "env");
    a.set_source("model", "env");
    a.set_source("client", "header");
    assert_eq!(
        a.sources,
        vec![(cps("client"), cps("header")), (cps("model"), cps("env"))]
    );
    assert_eq!(a.source("client"), Some(cps("header").as_slice()));
    let record = a.to_record();
    let sources = record.get("sources").expect("sources");
    let bettermemory_core::pyvalue::PyValue::Dict(items) = sources else {
        panic!("sources is a dict");
    };
    assert_eq!(items.len(), 2);
}
