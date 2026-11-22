//! Cases a plausible wrong port of `policy.py` or `owner_prompt.py` fails.

use std::cell::Cell;

use bettermemory_core::config::{self, Ctx, Guards, Source};
use bettermemory_core::json::loads;
use bettermemory_core::owner_prompt::{
    is_submitted, owner_prompt, owner_text, queued_text, submitted_text, text_blocks, Reference,
};
use bettermemory_core::policy::{check_overrides, check_overrides_with, label, PolicyRefusal};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::transcript::OwnerPrompt;
use bettermemory_core::unicode::cps;

fn row(text: &str) -> PyValue {
    loads(&cps(text)).expect("json")
}

fn id(text: &[u32], _cut: bool) -> PyStr {
    text.to_vec()
}

fn refuse(force: bool, ack: &[&str], ff: bool, fa: &[&str], mk: &[&str]) -> Option<PolicyRefusal> {
    let fa: Vec<String> = fa.iter().map(|s| (*s).to_owned()).collect();
    check_overrides_with(ff, &fa, &|k| mk.contains(&k), "memory_write", force, &|n| {
        ack.contains(&n)
    })
    .err()
}

#[test]
fn acknowledge_flags_follow_the_names_order_not_the_call() {
    let r = refuse(
        false,
        &["truncation", "credential"],
        false,
        &["truncation", "credential"],
        &[],
    )
    .unwrap();
    assert_eq!(
        r.flags,
        ["acknowledge_credential", "acknowledge_truncation"]
    );
    assert_eq!(r.keys, ["policy.forbid_acknowledge"]);
}

#[test]
fn managed_is_sorted_but_keys_keep_force_first() {
    let r = refuse(
        true,
        &["user_claim"],
        true,
        &["user_claim"],
        &["policy.forbid_force", "policy.forbid_acknowledge"],
    )
    .unwrap();
    assert_eq!(r.keys, ["policy.forbid_force", "policy.forbid_acknowledge"]);
    assert_eq!(
        r.managed,
        ["policy.forbid_acknowledge", "policy.forbid_force"]
    );
    let text = r.to_string();
    assert!(text.contains("force=True and acknowledge_user_claim=True"));
    assert!(text.contains("without those flags"));
    assert!(text.ends_with("memory_update instead."));
}

#[test]
fn a_managed_key_not_refused_is_not_named() {
    let r = refuse(
        false,
        &["credential"],
        true,
        &["credential"],
        &["policy.forbid_force"],
    )
    .unwrap();
    assert!(r.managed.is_empty());
    assert!(r.to_string().contains("(set in the user's config)"));
    assert!(!r.to_string().contains("duplicate"));
}

#[test]
fn nothing_forbidden_or_nothing_passed_answers_none() {
    assert!(refuse(true, &["credential"], false, &[], &[]).is_none());
    assert!(refuse(false, &[], true, &["credential"], &[]).is_none());
    assert!(refuse(true, &[], false, &["credential"], &[]).is_none());
}

#[test]
fn label_spells_the_section() {
    assert_eq!(label("policy.forbid_force"), "[policy] forbid_force");
    assert_eq!(label("a.b.c"), "[a] b.c");
}

#[test]
fn outcome_names_the_write_tool_only() {
    let fa = vec!["credential".to_owned()];
    let r = check_overrides_with(false, &fa, &|_| false, "memory_writer", false, &|_| true)
        .unwrap_err();
    assert!(r.to_string().contains("Nothing was changed."));
}

#[test]
fn owner_text_strips_python_whitespace_only() {
    assert_eq!(
        owner_text(&cps("\u{85}\u{3000}<command-name>"), true, &id),
        None
    );
    let zw = cps("\u{200b}<command-name>");
    assert_eq!(owner_text(&zw, true, &id), Some(zw.clone()));
    let lower = cps("<System-reminder>");
    assert_eq!(owner_text(&lower, true, &id), Some(lower.clone()));
    assert_eq!(owner_text(&cps("\u{1c}\u{1f}"), true, &id), None);
    // The text is answered as probed, leading space kept.
    assert_eq!(owner_text(&cps("  hi "), true, &id), Some(cps("  hi ")));
}

#[test]
fn owner_text_passes_cut_to_the_probe() {
    let seen = Cell::new(None);
    let probe = |t: &[u32], cut: bool| {
        seen.set(Some(cut));
        t.to_vec()
    };
    owner_text(&cps("x"), false, &probe);
    assert_eq!(seen.get(), Some(false));
    owner_prompt(
        &row(r#"{"type":"user","message":{"content":"x"}}"#),
        true,
        &probe,
    );
    assert_eq!(seen.get(), Some(true));
}

#[test]
fn marks_read_as_truth_values() {
    let base = |marks: &str| {
        row(&format!(
            r#"{{"type":"user","message":{{"content":"hi"}}{marks}}}"#
        ))
    };
    assert_eq!(owner_prompt(&base(r#","isMeta":"0""#), true, &id), None);
    assert_eq!(
        owner_prompt(&base(r#","isMeta":0"#), true, &id),
        Some(cps("hi"))
    );
    assert_eq!(
        owner_prompt(&base(r#","isMeta":[]"#), true, &id),
        Some(cps("hi"))
    );
    assert_eq!(owner_prompt(&base(r#","agentId":"a""#), true, &id), None);
    assert_eq!(
        owner_prompt(&base(r#","turnOrigin":false"#), true, &id),
        None
    );
    assert_eq!(
        owner_prompt(&base(r#","turnOrigin":null"#), true, &id),
        Some(cps("hi"))
    );
    assert_eq!(
        owner_prompt(&base(r#","turnOrigin":"human""#), true, &id),
        Some(cps("hi"))
    );
}

#[test]
fn submitted_rows_and_blocks() {
    assert!(!is_submitted(&row(
        r#"{"type":"user","message":{"content":{"type":"text"}}}"#
    )));
    assert!(is_submitted(&row(
        r#"{"type":"user","message":{"content":[]}}"#
    )));
    assert!(!is_submitted(&row(
        r#"{"type":"user","message":{"content":[{"type":"tool_result"}]}}"#
    )));
    assert!(is_submitted(&row(
        r#"{"type":"user","message":{"content":["tool_result"]}}"#
    )));
    let content =
        row(r#"[{"type":"text","text":"a"},{"type":"text","text":""},{"type":"text","text":"b"}]"#);
    assert_eq!(text_blocks(&content), Some(cps("a\nb")));
    assert_eq!(
        submitted_text(&row(r#"{"type":"user","message":{"content":[]}}"#)),
        None
    );
}

#[test]
fn queued_prompts() {
    let q = |a: &str| row(&format!(r#"{{"type":"attachment","attachment":{a}}}"#));
    assert_eq!(
        queued_text(&q(r#"{"type":"queued_command","prompt":" p "}"#)),
        Some(cps(" p "))
    );
    assert_eq!(
        queued_text(&q(r#"{"type":"queued_command","prompt":"　"}"#)),
        None
    );
    assert_eq!(
        queued_text(&q(r#"{"type":"queued_command","prompt":"p","isMeta":"x"}"#)),
        None
    );
    assert_eq!(
        queued_text(&q(r#"{"type":"queued_command","prompt":"p","isMeta":0}"#)),
        Some(cps("p"))
    );
}

#[test]
fn the_trait_answers_as_the_functions() {
    let r = Reference { probe: &id };
    let user = row(r#"{"type":"user","message":{"content":"<scheduled-task>x"}}"#);
    assert!(r.is_submitted(&user));
    assert_eq!(r.owner_prompt(&user, true), None);
    assert_eq!(r.submitted_text(&user), Some(cps("<scheduled-task>x")));
    assert_eq!(r.text_blocks(&PyValue::None), None);
}

fn config_of(doc: &str, managed: &[&str]) -> config::Config {
    let mut data = bettermemory_core::toml::loads(doc).expect("toml");
    let mut guards = Guards::new();
    let mut ctx = Ctx::new(&mut guards);
    let mut c = config::config_from_data(&mut data, &Source::registry(), &mut ctx).expect("config");
    c.managed_keys = managed.iter().map(|k| (*k).to_owned()).collect();
    c
}

#[test]
fn check_overrides_reads_each_switch_off_the_config() {
    let ff = config_of("[policy]\nforbid_force = true\n", &["policy.forbid_force"]);
    let r = check_overrides(&ff, "memory_write", true, &|_| false).unwrap_err();
    assert_eq!(r.flags, ["force"]);
    assert_eq!(r.keys, ["policy.forbid_force"]);
    assert_eq!(r.managed, ["policy.forbid_force"]);
    assert!(check_overrides(&ff, "memory_write", false, &|n| n == "credential").is_ok());

    let fa = config_of("[policy]\nforbid_acknowledge = [\"credential\"]\n", &[]);
    let r = check_overrides(&fa, "memory_write", false, &|n| n == "credential").unwrap_err();
    assert_eq!(r.flags, ["acknowledge_credential"]);
    assert_eq!(r.keys, ["policy.forbid_acknowledge"]);
    assert!(r.managed.is_empty());
    assert!(check_overrides(&fa, "memory_write", true, &|_| false).is_ok());

    let none = config_of("", &[]);
    assert!(check_overrides(&none, "memory_write", true, &|_| true).is_ok());
}

#[test]
#[should_panic(expected = "not enough values to unpack")]
fn label_of_a_key_without_a_dot_panics_as_the_reference_raises() {
    let _ = label("nodot");
}
