//! The reader of `response_write.jsonl` and `response_write_posix.jsonl`
//! (`tests/contract/gen_response_write.py`) and its check.

use super::parity::Ledger;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::durability::TransientMatch;
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{
    Actor, Category, Confidence, Memory, Origin, RecordFields, Source,
};
use bettermemory_core::response_rows::SIMILAR_HITS_SHOWN;
use bettermemory_core::response_write::{
    actor_to_dict, add_similar, committed, credential_to_dict, origin_to_dict, review, roster,
    similar_to_dict, transient_to_dict, CommittedExtras,
};
use bettermemory_core::search_dedup::SimilarHit;
use bettermemory_core::unicode;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn dt(v: &Value) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(v.as_str().expect("a datetime"))).expect("isoformat")
}

fn odt(v: &Value) -> Option<DateTime> {
    (!v.is_null()).then(|| dt(v))
}

fn s(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("a string"))
}

fn list(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("a list").iter().map(s).collect()
}

fn member<T: Copy>(members: &[(&str, T)], v: &Value) -> T {
    let name = v.as_str().expect("an enum value");
    members
        .iter()
        .find(|(value, _)| *value == name)
        .map(|(_, m)| *m)
        .expect("a member")
}

fn relevance(v: &Value) -> &'static str {
    let name = v.as_str().expect("a relevance");
    ["high", "medium", "high-removed", "medium-removed"]
        .into_iter()
        .find(|r| *r == name)
        .expect("a known relevance")
}

fn hit(i: &Value) -> SimilarHit {
    let PyValue::Float(similarity) = super::neutral(&i["similarity"]) else {
        panic!("a float similarity")
    };
    SimilarHit {
        id: s(&i["id"]),
        scopes: list(&i["scopes"]),
        confidence: member(Confidence::MEMBERS, &i["confidence"]),
        snippet: super::text_field(i, "snippet"),
        similarity,
        relevance: relevance(&i["relevance"]),
        created: dt(&i["created"]),
        updated: dt(&i["updated"]),
        removed_at: odt(&i["removed_at"]),
        removed_reason: (!i["removed_reason"].is_null())
            .then(|| super::text_field(&i["removed_reason"], "v")),
    }
}

fn hits(v: &Value) -> Vec<SimilarHit> {
    v.as_array().expect("hits").iter().map(hit).collect()
}

fn ostr(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        PyValue::Str(t) => Some(t.clone()),
        other => panic!("not a str: {other:?}"),
    }
}

fn get<'a>(d: &'a [(PyValue, PyValue)], k: &str) -> Option<&'a PyValue> {
    d.iter()
        .find(|(key, _)| *key == PyValue::str(k))
        .map(|(_, v)| v)
}

/// An `Actor(**spec)` from a plain JSON spec or a `neutral` one.
fn actor_of(spec: &PyValue) -> Option<Actor> {
    let PyValue::Dict(d) = spec else {
        return None;
    };
    let f = |k| get(d, k).and_then(ostr);
    let sources = match get(d, "sources") {
        Some(PyValue::Dict(items)) => items
            .iter()
            .map(|(k, v)| (ostr(k).expect("key"), ostr(v).expect("value")))
            .collect(),
        _ => Vec::new(),
    };
    Some(Actor {
        client: f("client"),
        client_version: f("client_version"),
        model: f("model"),
        principal: f("principal"),
        session: f("session"),
        sources,
    })
}

/// A plain JSON value of strings, objects and nulls as a `PyValue`.
fn plain(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::String(t) => PyValue::str(t),
        Value::Object(m) => {
            PyValue::Dict(m.iter().map(|(k, v)| (PyValue::str(k), plain(v))).collect())
        }
        other => panic!("unexpected {other}"),
    }
}

pub fn memory(i: &Value) -> Memory {
    Memory {
        fields: RecordFields {
            id: s(&i["id"]),
            created: dt(&i["created"]),
            updated: dt(&i["updated"]),
            scopes: list(&i["scopes"]),
            confidence: member(Confidence::MEMBERS, &i["confidence"]),
            source: member(Source::MEMBERS, &i["source"]),
            body: Vec::new(),
            origin: None,
            actor: actor_of(&plain(&i["actor"])),
            last_verified_at: odt(&i["last_verified_at"]),
            category: (!i["category"].is_null()).then(|| member(Category::MEMBERS, &i["category"])),
            verified_paths: Vec::new(),
            verified_commits: Vec::new(),
            verified_versions: Vec::new(),
            verified_absent_paths: Vec::new(),
            verified_path_targets: None,
            verified_absent_path_targets: None,
            claims: Vec::new(),
            verified_head: None,
        },
        links: Vec::new(),
        corroborations: "0".to_owned(),
        last_corroborated: None,
    }
}

fn values(v: &Value) -> Option<Vec<PyValue>> {
    if v.is_null() {
        return None;
    }
    match &super::neutral(v) {
        PyValue::List(items) => Some(items.clone()),
        other => panic!("not a list: {other:?}"),
    }
}

fn strs(v: &Value) -> Option<Vec<PyStr>> {
    values(v).map(|items| items.iter().map(|t| ostr(t).expect("a str")).collect())
}

fn extras(e: &Value, mask: Option<u32>, order: &[String]) -> CommittedExtras {
    let pick = |name: &str| -> &Value {
        let on = match mask {
            None => true,
            Some(m) => {
                let b = order.iter().position(|n| n == name).expect("named");
                m >> b & 1 == 1
            }
        };
        if on {
            e.get(name).unwrap_or(&Value::Null)
        } else {
            &Value::Null
        }
    };
    let hs = |v: &Value| (!v.is_null()).then(|| hits(v));
    CommittedExtras {
        related: hs(pick("related")),
        removed_related: hs(pick("removed_related")),
        warnings: strs(pick("warnings")),
        supersedes: values(pick("supersedes")),
        conflicts_filed: values(pick("conflicts_filed")),
        hint: (!pick("hint").is_null()).then(|| ostr(&super::neutral(pick("hint"))).expect("hint")),
        update_suggested: strs(pick("update_suggested")),
        extends: values(pick("extends")),
    }
}

fn text(value: &PyValue) -> String {
    let out = dumps(value, DumpOptions::DEFAULT).expect("dumps");
    String::from_utf8(
        out.iter()
            .map(|&c| u8::try_from(c).expect("ascii"))
            .collect(),
    )
    .expect("ascii")
}

fn origin_of(spec: &PyValue) -> Option<Origin> {
    let PyValue::Dict(d) = spec else {
        return None;
    };
    let f = |k| get(d, k).and_then(ostr);
    Some(Origin {
        cwd: f("cwd"),
        repo: f("repo"),
        branch: f("branch"),
        worktree_root: f("worktree_root"),
        source: f("source"),
    })
}

fn answer(case: &Value, header: &Value) -> Value {
    let i = &case["i"];
    let n = |k: &str| super::neutral(&i[k]);
    let one = |v: Option<PyValue>| v.map_or("null".to_owned(), |v| text(&v));
    let got = match case["k"].as_str().expect("a kind") {
        "committed" => text(&committed(&memory(i), &extras(&i["extras"], None, &[]))),
        "committed_sweep" => {
            let order: Vec<String> = header["extra_order"]
                .as_array()
                .expect("order")
                .iter()
                .map(|v| v.as_str().expect("a name").to_owned())
                .collect();
            let m = memory(i);
            let texts: Vec<String> = (0..256u32)
                .map(|mask| {
                    text(&committed(
                        &m,
                        &extras(&header["extras"], Some(mask), &order),
                    ))
                })
                .collect();
            let digest = Sha256::digest(texts.join("\n").as_bytes());
            let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
            return json!({ "count": texts.len(), "sha256": hex });
        }
        "similar" => text(&similar_to_dict(&hit(i))),
        "add_similar" => {
            let mut out = Vec::new();
            add_similar(&mut out, i["key"].as_str().expect("key"), &hits(&i["hits"]));
            text(&PyValue::Dict(out))
        }
        "transient" => text(&transient_to_dict(&TransientMatch {
            marker: super::text_field(i, "marker"),
            snippet: super::text_field(i, "snippet"),
        })),
        "credential" => text(&credential_to_dict(
            &super::text_field(i, "kind"),
            &super::text_field(i, "snippet"),
        )),
        "review" => {
            let related = match &n("related") {
                PyValue::List(items) => items.clone(),
                _ => panic!("related"),
            };
            text(&review(
                &ostr(&n("review_id")).expect("id"),
                &related,
                &n("questions"),
                &ostr(&n("hint")).expect("hint"),
            ))
        }
        "roster" => {
            let lines = match &n("roster") {
                PyValue::List(items) => items.clone(),
                _ => panic!("roster"),
            };
            let lines: Vec<PyStr> = lines.iter().map(|l| ostr(l).expect("line")).collect();
            text(&roster(
                &ostr(&n("review_id")).expect("id"),
                &lines,
                &ostr(&n("question")).expect("question"),
                &ostr(&n("hint")).expect("hint"),
            ))
        }
        "actor" => one(actor_to_dict(actor_of(&super::neutral(i)).as_ref())),
        "origin" => match origin_to_dict(origin_of(&super::neutral(i)).as_ref()) {
            Ok(v) => one(v),
            Err(_) => return json!({ "e": "UnicodeEncodeError" }),
        },
        other => panic!("unknown kind {other}"),
    };
    json!({ "o": got })
}

/// Checks every row of `file` and answers the count checked.
pub fn check(file: &str, test: &str) {
    let lines = super::jsonl(file);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    if let Some(shown) = header.get("shown") {
        assert_eq!(shown, &json!(SIMILAR_HITS_SHOWN));
    }
    let count = header["count"].as_u64().expect("a count") as usize;
    assert!(count > 0);
    let mut ledger = Ledger::new(test, file);
    for (n, case) in lines[1..].iter().enumerate() {
        let got = answer(case, header);
        let want = match (case.get("o"), case.get("e"), case.get("sha256")) {
            (Some(o), _, _) => json!({ "o": o }),
            (_, Some(e), _) => json!({ "e": e }),
            (_, _, Some(h)) => json!({ "count": case["count"], "sha256": h }),
            _ => panic!("no answer"),
        };
        let label = format!("line {}", n + 2);
        assert!(
            !ledger.differs_fatally(&label, got == want),
            "{label}: got {got}, want {want}"
        );
    }
    assert_eq!(lines.len() - 1, count);
    ledger.finish(count);
}
