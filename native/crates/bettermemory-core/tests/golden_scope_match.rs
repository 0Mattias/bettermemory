//! Every case of `scope_match.jsonl` against `scope_match`: the leaves
//! that read no separator and no home (`_boundary_after`,
//! `_find_root_occurrence`, `_trim_evidence`), the report types and the
//! module's constants. Every platform answers this file alike, so it runs
//! everywhere; the leaves that read the separators and the home are
//! `golden_scope_match_posix`'s.

mod common;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::scope_match::{
    boundary_after, find_root_occurrence, trim_evidence, EntryKind, ScopeMismatchEntry,
    ScopeMismatchReport, EVIDENCE_PADDING, MAX_MATCHES, NOISY_NAME_STOPLIST,
};
use common::parity::Ledger;
use serde_json::{json, Value};

/// A text of the fixture: a string, or `{"cps": [...]}`.
fn s(v: &Value) -> PyStr {
    match v {
        Value::String(t) => t.chars().map(u32::from).collect(),
        Value::Object(o) => o["cps"]
            .as_array()
            .expect("cps")
            .iter()
            .map(|c| u32::try_from(c.as_u64().expect("code point")).expect("u32"))
            .collect(),
        other => panic!("not a text: {other}"),
    }
}

fn text(t: &[u32]) -> Value {
    let plain: Option<String> = t.iter().map(|&c| char::from_u32(c)).collect();
    match plain {
        Some(p) if !p.contains("\\\\") => Value::String(p),
        _ => json!({ "cps": t }),
    }
}

fn idx(v: &Value) -> usize {
    usize::try_from(v.as_u64().expect("index")).expect("usize")
}

fn entry(kind: &str, evidence: &Value, scope: &Value) -> ScopeMismatchEntry {
    ScopeMismatchEntry {
        kind: match kind {
            "project_name" => EntryKind::ProjectName,
            "project_root" => EntryKind::ProjectRoot,
            other => panic!("kind {other}"),
        },
        evidence: s(evidence),
        suggested_scope: s(scope),
    }
}

#[test]
fn scope_match_every_case() {
    let rows = common::jsonl("scope_match.jsonl");
    let header = &rows[0];
    let cases = idx(&header["cases"]);
    assert!(cases > 0 && cases == rows.len() - 1, "{cases} cases");
    let mut ledger = Ledger::new("scope_match_every_case", "scope_match.jsonl");
    ledger.case("the constants", || {
        assert_eq!(header["max_matches"], json!(MAX_MATCHES));
        assert_eq!(header["padding"], json!(EVIDENCE_PADDING));
        assert_eq!(header["stoplist"], json!(NOISY_NAME_STOPLIST));
    });
    let entries = [
        entry(
            "project_name",
            &json!("...see foo..."),
            &json!("projects:foo"),
        ),
        entry("project_root", &json!("/a/b"), &json!("projects:b")),
    ];
    for (i, row) in rows[1..].iter().enumerate() {
        let got = match row["f"].as_str().expect("f") {
            "boundary" => json!(boundary_after(&s(&row["body"]), idx(&row["end"]))),
            "occurrence" => json!(find_root_occurrence(&s(&row["body"]), &s(&row["needle"]))),
            "trim" => {
                let padding = if row.get("padding").is_some() {
                    idx(&row["padding"])
                } else {
                    EVIDENCE_PADDING
                };
                text(&trim_evidence(
                    &s(&row["body"]),
                    idx(&row["start"]),
                    idx(&row["end"]),
                    padding,
                ))
            }
            "entry" => {
                let e = entry(
                    row["kind"].as_str().expect("kind"),
                    &row["evidence"],
                    &row["scope"],
                );
                Value::Array(
                    e.to_dict()
                        .iter()
                        .map(|(k, v)| json!([k, text(v)]))
                        .collect(),
                )
            }
            "report" => {
                let n = idx(&row["n"]);
                let report = ScopeMismatchReport {
                    matches: entries[..n].to_vec(),
                    suggested_scopes: entries[..n]
                        .iter()
                        .map(|e| e.suggested_scope.clone())
                        .collect(),
                };
                json!(report.has_mismatch())
            }
            other => panic!("unknown case {other}"),
        };
        ledger.case(format!("line {}", i + 2), || {
            assert_eq!(got, row["r"], "{row}");
        });
    }
    ledger.finish(1 + cases);
}
