//! Cases a plausible wrong port of `eval_model::split` and
//! `eval_model::tools` fails, each answer the reference's
//! (`src/bettermemory/eval_model.py`, `surface_rule_split`,
//! `_with_rule_split`, `_TOOL_EVENT_KIND_TO_TOOL`): the group read by
//! truthiness, by `in` or by a non-`str` rule; the pick applied to the
//! rows before the report instead of inside it; the rules unsorted, in
//! UTF-16 order, or with the empty rule; the delta read off earlier-rule
//! or out-of-window shows, or keeping a non-`str` rc3 item or dropping
//! the empty one; `by_rule` appended rather than replaced; the split run
//! for a None section; the tool map out of its order.

use bettermemory_core::datetime::{parse_event_ts, DateTime};
use bettermemory_core::eval_model::{self as em, EvalModelError, MembershipDelta};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::recall_counters as rc;
use bettermemory_core::recall_state::RULE;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn st(s: &str) -> PyValue {
    PyValue::Str(cps(s))
}

fn ids(v: &[&str]) -> PyValue {
    PyValue::List(v.iter().map(|s| st(s)).collect())
}

fn ev(fields: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(fields.into_iter().map(|(k, v)| (st(k), v)).collect())
}

fn ts(minute: u32) -> PyValue {
    st(&format!("2026-01-01T12:{minute:02}:00+00:00"))
}

fn at(minute: u32) -> DateTime {
    parse_event_ts(&cps(&format!("2026-01-01T12:{minute:02}:00+00:00"))).unwrap()
}

/// A `prompt_recall` show of conversation c1.
fn recall(minute: u32, shown: &[&str], rule: PyValue, more: Vec<(&str, PyValue)>) -> PyValue {
    let mut fields = vec![
        ("kind", st("prompt_recall")),
        ("session", st("c1")),
        ("shown_ids", ids(shown)),
        ("rule", rule),
        ("triggered_from", st("prompt_hook")),
        ("ts", ts(minute)),
    ];
    fields.extend(more);
    ev(fields)
}

/// A `session_brief` show of c1, with its `rule` when one is given.
fn brief(minute: u32, shown: &[&str], rule: Option<PyValue>) -> PyValue {
    let mut fields = vec![
        ("kind", st("session_brief")),
        ("session", st("c1")),
        ("ids", ids(shown)),
        ("triggered_from", st("session_hook")),
        ("ts", ts(minute)),
    ];
    fields.extend(rule.map(|r| ("rule", r)));
    ev(fields)
}

/// The server's open of `id`, under c1.
fn open(minute: u32, id: &str) -> PyValue {
    ev(vec![
        ("kind", st("show")),
        ("session", st("c1")),
        ("id", st(id)),
        ("ts", ts(minute)),
    ])
}

fn split(rows: &[PyValue]) -> (PyValue, MembershipDelta) {
    em::surface_rule_split(rows, None, None).expect("no raise")
}

fn get<'a>(v: &'a PyValue, path: &[&str]) -> &'a PyValue {
    path.iter()
        .fold(v, |v, k| v.get(k).unwrap_or_else(|| panic!("no {k}")))
}

fn int(v: &PyValue) -> i64 {
    match v {
        PyValue::Int(n) => n.parse().unwrap(),
        other => panic!("not an int: {other:?}"),
    }
}

fn rules(by_rule: &PyValue, group: &str) -> Vec<PyStr> {
    match get(by_rule, &[group, "rules"]) {
        PyValue::List(items) => items
            .iter()
            .map(|v| match v {
                PyValue::Str(s) => s.clone(),
                other => panic!("{other:?}"),
            })
            .collect(),
        other => panic!("{other:?}"),
    }
}

fn keys(v: &PyValue) -> Vec<String> {
    match v {
        PyValue::Dict(items) => items
            .iter()
            .map(|(k, _)| match k {
                PyValue::Str(s) => s.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
                other => panic!("{other:?}"),
            })
            .collect(),
        other => panic!("{other:?}"),
    }
}

#[test]
fn the_group_is_the_str_rs_v8_only() {
    let rule = |v: PyValue| ev(vec![("rule", v)]);
    assert_eq!(em::group_of(&rule(st("rs-v8"))), RULE);
    for v in [
        st("rs-v4"),
        st(""),
        st("rs-v"),
        st("rs-v8 "),
        st("RS-V8"),
        PyValue::Bool(true),
        PyValue::Int("1".into()),
        PyValue::Int("8".into()),
        PyValue::Float(8.0),
        PyValue::None,
        ids(&["rs-v8"]),
        PyValue::Tuple(vec![st("rs-v8")]),
        ev(vec![("rs-v8", PyValue::Bool(true))]),
    ] {
        assert_eq!(em::group_of(&rule(v.clone())), em::EARLIER_RULES, "{v:?}");
    }
    assert_eq!(em::group_of(&ev(vec![])), em::EARLIER_RULES);
    // Briefs with truthy non-current rules are earlier; only the str
    // rs-v8 is current.
    let rows = [
        brief(1, &["m1"], Some(PyValue::Bool(true))),
        brief(2, &["m2"], Some(st("rs-v4"))),
        brief(3, &["m3"], Some(ids(&["rs-v8"]))),
        brief(4, &["m4"], Some(st("rs-v8"))),
        brief(5, &["m5"], None),
    ];
    let (by_rule, _) = split(&rows);
    assert_eq!(int(get(&by_rule, &[RULE, "brief", "deliveries"])), 1);
    assert_eq!(int(get(&by_rule, &["earlier", "brief", "deliveries"])), 4);
    assert_eq!(rules(&by_rule, RULE), vec![cps("rs-v8")]);
    assert_eq!(rules(&by_rule, "earlier"), vec![cps("rs-v4")]);
}

#[test]
fn the_pick_runs_inside_the_report() {
    // m1 shown under rs-v8 at 1, again under rs-v4 at 3, opened at 4: the
    // second show ends the first's follow-ups, whatever its rule, so the
    // open is the earlier group's. Filtering the rows before the report
    // would hand it to rs-v8.
    let rows = [
        recall(1, &["m1"], st("rs-v8"), vec![]),
        recall(3, &["m1"], st("rs-v4"), vec![]),
        open(4, "m1"),
    ];
    let (by_rule, _) = split(&rows);
    assert_eq!(int(get(&by_rule, &[RULE, "recall", "opened"])), 0);
    assert_eq!(int(get(&by_rule, &["earlier", "recall", "opened"])), 1);
    // The open at 2 joins the rs-v8 show, which the earlier group's report
    // still sees as a row.
    let rows = [
        recall(1, &["m1"], st("rs-v8"), vec![]),
        open(2, "m1"),
        recall(3, &["m1"], st("rs-v4"), vec![]),
    ];
    let (by_rule, _) = split(&rows);
    assert_eq!(int(get(&by_rule, &[RULE, "recall", "opened"])), 1);
    assert_eq!(int(get(&by_rule, &["earlier", "recall", "opened"])), 0);
}

#[test]
fn the_two_groups_add_up_to_the_section() {
    let rows = [
        brief(0, &["m1", "m2"], None),
        recall(1, &["m1", "m2"], st("rs-v8"), vec![]),
        open(2, "m1"),
        recall(3, &["m1"], st("rs-v4"), vec![]),
        recall(4, &["m2", "m3"], st(""), vec![]),
        open(5, "m1"),
        open(6, "m3"),
        brief(7, &["m3"], Some(st("rs-v8"))),
        open(8, "m3"),
    ];
    let whole = rc::compute_surface_report::<PyError>(&rows, None, None, None)
        .unwrap()
        .unwrap();
    let (by_rule, _) = split(&rows);
    for surface in rc::SURFACES {
        for key in [
            "deliveries",
            "shown",
            "opened",
            "used",
            "ignored",
            "deliveries_helped",
        ] {
            let sum = int(get(&by_rule, &[RULE, surface, key]))
                + int(get(&by_rule, &["earlier", surface, key]));
            assert_eq!(sum, int(get(&whole, &[surface, key])), "{surface} {key}");
        }
    }
}

#[test]
fn the_rules_are_sorted_by_code_point_without_the_empty_one() {
    let names = [
        "rs-v4",
        "rs-v10",
        "",
        "rs-v2",
        "a",
        "Z",
        "\u{10000}",
        "\u{ffff}",
        "é",
        "z",
        "rs-v4",
    ];
    let rows: Vec<PyValue> = names
        .iter()
        .enumerate()
        .map(|(i, r)| recall(i as u32, &["m1"], st(r), vec![]))
        .collect();
    let (by_rule, _) = split(&rows);
    let want: Vec<PyStr> = [
        "Z",
        "a",
        "rs-v10",
        "rs-v2",
        "rs-v4",
        "z",
        "é",
        "\u{ffff}",
        "\u{10000}",
    ]
    .iter()
    .map(|s| cps(s))
    .collect();
    assert_eq!(rules(&by_rule, "earlier"), want);
    assert!(rules(&by_rule, RULE).is_empty());
    // Out of the window, a show names nothing.
    let (by_rule, _) = em::surface_rule_split(&rows, Some(&at(5)), None).unwrap();
    assert!(!rules(&by_rule, "earlier").contains(&cps("rs-v10")));
}

#[test]
fn the_delta_reads_current_rule_shows_in_the_window_only() {
    let rc3 = |v: &[&str]| vec![("rc3_admitted", ids(v))];
    let rows = [
        recall(1, &["m1"], st("rs-v8"), rc3(&["z1"])),
        recall(2, &["m1"], st("rs-v4"), rc3(&["z2"])),
        recall(3, &["m1"], st(""), rc3(&["z3"])),
        brief(4, &["m1"], None),
        recall(6, &["m1"], st("rs-v8"), rc3(&["m1"])),
        recall(9, &["m1"], st("rs-v8"), rc3(&["z4"])),
    ];
    let (_, all) = split(&rows);
    assert_eq!((all.rows, all.rows_differing), (3, 2));
    let (_, inside) = em::surface_rule_split(&rows, Some(&at(2)), Some(&at(9))).unwrap();
    assert_eq!(
        inside,
        MembershipDelta {
            rows: 1,
            rows_differing: 0,
            delivered_not_rc3: 0,
            rc3_not_delivered: 0
        }
    );
}

#[test]
fn rc3_keeps_its_str_items_only_the_empty_one_included() {
    let show = |shown: &[&str], admitted: PyValue| {
        recall(1, shown, st("rs-v8"), vec![("rc3_admitted", admitted)])
    };
    let delta = |row: PyValue| split(&[row]).1;
    let mixed = PyValue::List(vec![
        st("m1"),
        PyValue::Int("1".into()),
        PyValue::Bool(true),
        PyValue::None,
        ids(&["m2"]),
    ]);
    assert_eq!(
        delta(show(&["m1"], mixed)),
        MembershipDelta {
            rows: 1,
            rows_differing: 0,
            delivered_not_rc3: 0,
            rc3_not_delivered: 0
        }
    );
    // "" is a str item of rc3 but never a shown id.
    assert_eq!(
        delta(show(&["m1"], ids(&["", "m1"]))),
        MembershipDelta {
            rows: 1,
            rows_differing: 1,
            delivered_not_rc3: 0,
            rc3_not_delivered: 1
        }
    );
    // Sets: repeats count once each side.
    assert_eq!(
        delta(show(&["m1", "m1", "m2"], ids(&["m3", "m3", "m1"]))),
        MembershipDelta {
            rows: 1,
            rows_differing: 1,
            delivered_not_rc3: 1,
            rc3_not_delivered: 1
        }
    );
    // Not a list: the row is not read.
    for admitted in [
        st("m1"),
        PyValue::Tuple(vec![st("m1")]),
        ev(vec![("m1", PyValue::Bool(true))]),
        PyValue::None,
    ] {
        assert_eq!(delta(show(&["m1"], admitted)), MembershipDelta::default());
    }
    // An empty list is read: one row, delivered not rc3.
    assert_eq!(delta(show(&["m1"], ids(&[]))).delivered_not_rc3, 1);
}

#[test]
fn by_rule_is_replaced_in_its_place() {
    let rows = [recall(1, &["m1"], st("rs-v8"), vec![])];
    let section = ev(vec![
        ("membership_delta", PyValue::Int("1".into())),
        ("by_rule", st("old")),
        ("tail", PyValue::Bool(true)),
    ]);
    let out = em::with_rule_split(Some(&section), &rows, None, None)
        .unwrap()
        .unwrap();
    assert_eq!(keys(&out), ["membership_delta", "by_rule", "tail"]);
    assert_ne!(get(&out, &["by_rule"]), &st("old"));
    let out = em::with_rule_split(Some(&ev(vec![("a", PyValue::None)])), &rows, None, None)
        .unwrap()
        .unwrap();
    assert_eq!(keys(&out), ["a", "by_rule", "membership_delta"]);
}

#[test]
fn a_none_section_stays_none_without_the_split() {
    // This log raises in the split (an unhashable kind), and a naive bound
    // raises against the stamped show; neither is reached.
    let raising = [
        recall(1, &["m1"], st("rs-v8"), vec![]),
        ev(vec![("kind", ids(&["use"])), ("ts", ts(2))]),
    ];
    let naive = DateTime::from_isoformat(&cps("2026-01-01T12:00:00")).unwrap();
    for section in [None, Some(&PyValue::None)] {
        assert_eq!(em::with_rule_split(section, &raising, None, None), Ok(None));
        assert_eq!(
            em::with_rule_split(section, &raising, Some(&naive), None),
            Ok(None)
        );
    }
    let empty = ev(vec![]);
    assert_eq!(
        em::with_rule_split(Some(&empty), &raising, None, None),
        Err(EvalModelError::Raised(PyError::TypeError))
    );
    assert_eq!(
        em::with_rule_split(Some(&ids(&[])), &[], None, None),
        Err(EvalModelError::Raised(PyError::TypeError))
    );
    // The naive bound raises at the show, before any report.
    assert!(matches!(
        em::surface_rule_split(&raising, Some(&naive), None),
        Err(EvalModelError::TypeError(_))
    ));
}

#[test]
fn the_tool_map_keeps_its_order() {
    let map = em::TOOL_EVENT_KIND_TO_TOOL;
    assert_eq!(map.len(), 18);
    assert_eq!(map[0], ("search", "memory_search"));
    assert_eq!(map[6], ("use", "memory_record_use"));
    assert_eq!(map[7], ("episode_write", "episode"));
    assert_eq!(map[8], ("episode_handoff", "episode"));
    assert_eq!(map[9], ("restore", "memory_admin"));
    assert_eq!(map[17], ("answer_standing", "memory_admin"));
    assert_eq!(
        em::tool_of_kind(&cps("conflict_verdict")),
        Some("memory_admin")
    );
    assert_eq!(em::tool_of_kind(&cps("turn_audited")), None);
    assert_eq!(em::tool_of_kind(&cps("Search")), None);
    // The rosters: the in-session one inside the known one, the admin
    // kinds (known less in-session) the four eval-3 derives.
    let admin: Vec<&str> = em::KNOWN_SIDE_EFFECT_KINDS
        .iter()
        .copied()
        .filter(|k| !em::is_in_session_side_effect_kind(&cps(k)))
        .collect();
    assert_eq!(
        admin,
        [
            "capture_grade",
            "recall_grade",
            "review_grade",
            "silent_miss_cutoff"
        ]
    );
    for k in em::IN_SESSION_SIDE_EFFECT_KINDS {
        assert!(em::is_known_side_effect_kind(&cps(k)), "{k}");
    }
    let row = em::ToolUsageRow::new("episode", 3, None);
    assert!(row.has_telemetry);
}
