//! Every case of `eval_model_tools.jsonl` (not gated) against the native
//! `eval_model::tools`: the kind map's items in order, the empty
//! no-telemetry list, both side-effect rosters sorted, the map's lookup
//! and the rosters' membership per kind, and `ToolUsageRow.to_dict` over
//! counts, shares and `has_telemetry` (`tests/contract/gen_eval_model.py`,
//! `generate_tools`).

mod common;

use bettermemory_core::eval_model as em;
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::PyStr;
use serde_json::{json, Value};

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn text(s: &[u32]) -> String {
    s.iter()
        .map(|&c| char::from_u32(c).expect("scalar"))
        .collect()
}

#[test]
fn eval_model_tools_every_case() {
    let lines = common::jsonl("eval_model_tools.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "eval_model_tools");
    let map: Vec<Value> = em::TOOL_EVENT_KIND_TO_TOOL
        .iter()
        .map(|(k, t)| json!([k, t]))
        .collect();
    assert_eq!(json!(map), header["map"], "the map in order");
    assert_eq!(json!(em::TOOLS_WITHOUT_TELEMETRY), header["without"]);
    assert_eq!(json!(em::KNOWN_SIDE_EFFECT_KINDS), header["known"]);
    assert_eq!(
        json!(em::IN_SESSION_SIDE_EFFECT_KINDS),
        header["in_session"]
    );
    let mut checked = 0;
    let (mut kinds, mut rows) = (0, 0);
    for r in &lines[1..] {
        match r["k"].as_str().unwrap() {
            "kind" => {
                let kind = cps(r["kind"].as_str().unwrap());
                assert_eq!(json!(em::tool_of_kind(&kind)), r["tool"], "{r}");
                assert_eq!(
                    json!(em::is_known_side_effect_kind(&kind)),
                    r["known"],
                    "{r}"
                );
                let in_session = em::is_in_session_side_effect_kind(&kind);
                assert_eq!(json!(in_session), r["in_session"], "{r}");
                kinds += 1;
            }
            "row" => {
                let share = r["share"]
                    .as_str()
                    .map(|s| s.parse::<f64>().expect("a float"));
                let mut row = em::ToolUsageRow::new(
                    r["tool"].as_str().unwrap(),
                    r["count"].as_i64().unwrap(),
                    share,
                );
                if let Some(tel) = r.get("tel") {
                    row.has_telemetry = tel.as_bool().unwrap();
                }
                let got = text(&dumps(&row.to_dict(), DumpOptions::DEFAULT).unwrap());
                assert_eq!(got, r["r"].as_str().unwrap(), "{r}");
                rows += 1;
            }
            other => panic!("unknown row kind {other}"),
        }
        checked += 1;
    }
    assert!(kinds > 0 && rows > 0, "both kinds of row checked");
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("eval_model_tools.jsonl", checked, in_file);
}
