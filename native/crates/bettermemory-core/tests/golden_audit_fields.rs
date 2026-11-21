//! Every case of `audit_fields.jsonl` (not gated) against the native
//! `audit`: the header's constants, then each report, hit and builder
//! row. The spelling is the generator's (`tests/contract/gen_audit.py`).

mod common;

use bettermemory_core::audit as au;
use bettermemory_core::audit::report::{
    self as rp, AuditError, MissHit, MissReport, PromptRecallOptions, RecallTail,
    TurnAuditedOptions, Verdict,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use serde_json::{json, Value};

fn text(v: &Value) -> PyStr {
    cps(v.as_str().expect("a string"))
}

fn opt_text(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn int(v: &Value) -> i64 {
    v.as_i64().expect("an int")
}

fn hit(h: &Value) -> MissHit {
    let PyValue::Float(score) = common::neutral(&h["score"]) else {
        panic!("score not a float")
    };
    MissHit {
        id: text(&h["id"]),
        score,
        relevance: text(&h["rel"]),
        scopes: h["scopes"].as_array().unwrap().iter().map(text).collect(),
        snippet: text(&h["snip"]),
        matched_unique: int(&h["mu"]),
        query_unique: int(&h["qu"]),
        relevance_v2: text(&h["rel2"]),
    }
}

fn verdict(v: &Value) -> Verdict {
    match v.as_str().unwrap() {
        "miss" => Verdict::Miss,
        "ok" => Verdict::Ok,
        "no_signal" => Verdict::NoSignal,
        other => panic!("verdict {other}"),
    }
}

fn report(r: &Value) -> MissReport {
    MissReport {
        verdict: verdict(&r["v"]),
        checked_at: DateTime::from_isoformat(&text(&r["at"])).expect("an isoformat"),
        session_id: text(&r["sid"]),
        lookback_seconds: int(&r["lb"]),
        recent_retrieval_count: int(&r["rrc"]),
        threshold_rule: text(&r["rule"]),
        top_hits: r["hits"].as_array().unwrap().iter().map(hit).collect(),
        probe_query: opt_text(&r["pq"]),
        no_signal_reason: opt_text(&r["nsr"]),
        suppressed_by: opt_text(&r["sb"]),
    }
}

fn spelled(v: &PyValue) -> Value {
    let t = dumps(v, DumpOptions::DEFAULT).expect("dumps");
    json!(t
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect::<String>())
}

fn answer(got: Result<PyValue, AuditError>) -> Value {
    match got {
        Ok(v) => spelled(&v),
        Err(AuditError::ValueError(msg)) => {
            json!({"err": msg.iter().map(|&c| char::from_u32(c).unwrap()).collect::<String>()})
        }
        Err(other) => panic!("unexpected {other:?}"),
    }
}

fn entries(v: &Value) -> Vec<rp::Entry> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|e| {
            e.as_array()
                .unwrap()
                .iter()
                .map(|p| (text(&p[0]), text(&p[1])))
                .collect()
        })
        .collect()
}

fn tail(a: &Value) -> RecallTail {
    RecallTail {
        segment: a.get("segment").map(int),
        prompt_id: a.get("prompt_id").and_then(opt_text),
        transcript_path: a.get("transcript_path").and_then(opt_text),
        harness: a.get("harness").and_then(opt_text),
    }
}

fn minted() -> String {
    "MINTED".to_string()
}

fn check_consts(h: &Value) {
    assert_eq!(h["retrieval_event_kinds"], json!(au::RETRIEVAL_EVENT_KINDS));
    assert_eq!(h["threshold_rule_v1"], json!(au::THRESHOLD_RULE_V1));
    assert_eq!(h["top_hits_retained"], json!(au::TOP_HITS_RETAINED));
    assert_eq!(
        h["default_lookback_seconds"],
        json!(au::DEFAULT_LOOKBACK_SECONDS)
    );
    assert_eq!(
        h["attribution_lookback_seconds"],
        json!(au::ATTRIBUTION_LOOKBACK_SECONDS)
    );
    assert_eq!(
        h["reaudit_dedup_window_seconds"],
        json!(au::REAUDIT_DEDUP_WINDOW_SECONDS)
    );
    assert_eq!(
        h["default_creation_shield_seconds"],
        json!(au::DEFAULT_CREATION_SHIELD_SECONDS)
    );
    assert_eq!(
        h["min_probe_content_tokens"],
        json!(au::MIN_PROBE_CONTENT_TOKENS)
    );
    assert_eq!(h["ack_surface"], json!(au::ACK_SURFACE));
    assert_eq!(h["valid_triggered_from"], json!(au::VALID_TRIGGERED_FROM));
    assert_eq!(h["no_owner_prompt"], json!(au::NO_OWNER_PROMPT));
    assert_eq!(
        h["valid_no_signal_reason"],
        json!(au::VALID_NO_SIGNAL_REASON)
    );
    assert_eq!(h["valid_reply_source"], json!(au::VALID_REPLY_SOURCE));
    assert_eq!(h["held_causes"], json!(au::HELD_CAUSES));
    let verdicts: Vec<&str> = [Verdict::Miss, Verdict::Ok, Verdict::NoSignal]
        .iter()
        .map(|v| v.as_str())
        .collect();
    assert_eq!(h["verdicts"], json!(verdicts));
}

fn check_row(r: &Value, reports: &[MissReport]) {
    let a = &r["a"];
    let rep = || &reports[r["ri"].as_u64().unwrap() as usize];
    let got = match r["k"].as_str().unwrap() {
        "report" => {
            let m = report(&r["r"]);
            assert_eq!(json!(m.is_miss()), r["miss"], "{r}");
            spelled(&m.to_dict())
        }
        "hit" => {
            let h = hit(&r["h"]);
            assert_eq!(spelled(&h.to_compact_dict()), r["c"], "{r}");
            spelled(&h.to_dict())
        }
        "noowner" => {
            let at = DateTime::from_isoformat(&text(&r["at"])).unwrap();
            let got = rp::no_owner_prompt_report(&text(&r["sid"]), at, int(&r["lb"]));
            assert!(got == report(&r["r"]), "{r}");
            return;
        }
        "turn" => {
            let o = TurnAuditedOptions {
                repeat: a.get("repeat").and_then(Value::as_bool).unwrap_or(false),
                client_model: a.get("client_model").and_then(opt_text),
                prompt_id: a.get("prompt_id").and_then(opt_text),
                reply_source: a.get("reply_source").and_then(opt_text),
                rule: a.get("rule").and_then(opt_text),
                settled_count: a
                    .get("settled_count")
                    .map(common::neutral)
                    .filter(|v| !matches!(v, PyValue::None)),
            };
            answer(rp::turn_audited_fields(
                rep(),
                &cps("s-turn"),
                &cps("lexical"),
                a["ap"].as_bool().unwrap(),
                &text(&a["tf"]),
                &o,
            ))
        }
        "miss" => {
            let eid = a.get("event_id").and_then(opt_text);
            let cm = a.get("client_model").and_then(opt_text);
            let rule = a.get("rule").and_then(opt_text);
            answer(rp::search_miss_fields(
                rep(),
                &cps("s-miss"),
                &text(&a["tf"]),
                eid.as_deref(),
                cm.as_deref(),
                rule.as_deref(),
                &mut minted,
            ))
        }
        "held" => answer(rp::recall_held_fields(
            rep(),
            &cps("s-held"),
            &cps("lexical"),
            &text(&a["cause"]),
            &entries(&a["held"]),
            &cps("rs-v6"),
            &text(&a["tf"]),
            &tail(a),
            &mut minted,
        )),
        "recall" => {
            let mut o = PromptRecallOptions::reference();
            o.triggered_from = text(&a["tf"]);
            if let Some(d) = a.get("delivered_reason") {
                o.delivered_reason = text(d);
            }
            o.shown_ids = a
                .get("shown_ids")
                .map(|v| v.as_array().unwrap().iter().map(text).collect());
            o.suppressed = a.get("suppressed").map(entries);
            o.rule = a.get("rule").and_then(opt_text);
            o.tail = tail(a);
            answer(rp::prompt_recall_fields(
                rep(),
                &cps("s-recall"),
                &cps("lexical"),
                int(&a["chars"]),
                &o,
                &mut minted,
            ))
        }
        other => panic!("unknown row kind {other}"),
    };
    assert_eq!(got, r["o"], "{r}");
}

#[test]
fn audit_fields_every_case() {
    let lines = common::jsonl("audit_fields.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    check_consts(header);
    let reports: Vec<MissReport> = header["reports"]
        .as_array()
        .unwrap()
        .iter()
        .map(report)
        .collect();
    let mut checked = 0;
    for r in &lines[1..] {
        check_row(r, &reports);
        checked += 1;
    }
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("audit_fields.jsonl", checked, in_file);
}
