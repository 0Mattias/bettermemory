//! Cases a plausible wrong `response_write` fails: the similar list's
//! bound, a present key replaced in place, the empty optional arguments, an actor holding only `sources`,
//! `source` defaulted only when absent, the order of the appended
//! `_bytes_base64` keys, base64 padding, and both `os.fsencode` flavours.
//! The `surrogatepass` answers are CPython's on Windows
//! (`str.encode("utf-8", "surrogatepass")`, which `os.fsencode` uses
//! there); the `surrogateescape` ones are its answers on macOS and Linux.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{Actor, Confidence, Origin};
use bettermemory_core::response_write::{
    actor_to_dict, add_similar, fsencode_as, origin_to_dict_as, similar_to_dict, FsEncoding,
    UnicodeEncodeError, HOST_FS_ENCODING,
};
use bettermemory_core::search_dedup::SimilarHit;
use bettermemory_core::unicode;

fn t(text: &str) -> PyStr {
    unicode::cps(text)
}

fn text(v: &PyValue) -> String {
    dumps(v, DumpOptions::DEFAULT)
        .expect("dumps")
        .iter()
        .map(|&c| char::from_u32(c).expect("a char"))
        .collect()
}

fn hit(n: usize) -> SimilarHit {
    let at = DateTime::from_isoformat(&t("2025-01-02T03:04:05+00:00")).expect("dt");
    SimilarHit {
        id: t(&format!("id{n}")),
        scopes: vec![t("global")],
        confidence: Confidence::Medium,
        snippet: t("s"),
        similarity: 0.5,
        relevance: "medium",
        created: at,
        updated: at,
        removed_at: None,
        removed_reason: None,
    }
}

fn listed(n: usize) -> String {
    let mut out = Vec::new();
    add_similar(&mut out, "k", &(0..n).map(hit).collect::<Vec<_>>());
    text(&PyValue::Dict(out))
}

#[test]
fn the_more_key_appears_past_five_only() {
    assert!(!listed(5).contains("k_more"));
    assert!(listed(6).ends_with("\"k_more\": 1}"));
    assert!(listed(12).ends_with("\"k_more\": 7}"));
    assert_eq!(listed(0), "{\"k\": []}");
    assert_eq!(listed(12).matches("\"id\"").count(), 5);
}

/// `out[key] = ...` over a key already present replaces the value where
/// the key stands, as a Python dict assignment does: no second key.
#[test]
fn a_present_key_is_replaced_in_place() {
    let mut out = vec![
        (PyValue::Str(t("k")), PyValue::List(Vec::new())),
        (PyValue::Str(t("k_more")), PyValue::Int("99".to_string())),
        (PyValue::Str(t("z")), PyValue::None),
    ];
    add_similar(&mut out, "k", &(0..7).map(hit).collect::<Vec<_>>());
    assert_eq!(out.len(), 3);
    let s = text(&PyValue::Dict(out.clone()));
    assert!(s.starts_with("{\"k\": [{"), "{s}");
    assert!(s.ends_with("\"k_more\": 2, \"z\": null}"), "{s}");
    add_similar(&mut out, "k", &(0..6).map(hit).collect::<Vec<_>>());
    assert_eq!(out.len(), 3);
    assert!(text(&PyValue::Dict(out)).ends_with("\"k_more\": 1, \"z\": null}"));
}

#[test]
fn an_empty_removed_reason_is_still_emitted() {
    let mut h = hit(0);
    h.removed_reason = Some(Vec::new());
    assert!(text(&similar_to_dict(&h)).ends_with("\"removed_reason\": \"\"}"));
}

#[test]
fn an_actor_with_only_sources_is_empty() {
    let a = Actor {
        sources: vec![(t("client"), t("header"))],
        ..Actor::default()
    };
    assert_eq!(actor_to_dict(Some(&a)), None);
    let a = Actor {
        client: Some(Vec::new()),
        ..Actor::default()
    };
    assert_eq!(
        text(&actor_to_dict(Some(&a)).expect("an actor")),
        "{\"client\": \"\", \"client_version\": null, \"model\": null, \"principal\": null, \"session\": null, \"sources\": {}}"
    );
}

fn origin(
    fields: [Option<&[u32]>; 5],
    enc: FsEncoding,
) -> Result<Option<String>, UnicodeEncodeError> {
    let [cwd, repo, branch, worktree_root, source] = fields.map(|f| f.map(<[u32]>::to_vec));
    origin_to_dict_as(
        Some(&Origin {
            cwd,
            repo,
            branch,
            worktree_root,
            source,
        }),
        enc,
    )
    .map(|v| v.map(|v| text(&v)))
}

const ESC: FsEncoding = FsEncoding::SurrogateEscape;
const PASS: FsEncoding = FsEncoding::SurrogatePass;

#[test]
fn source_is_defaulted_only_when_absent() {
    let empty: &[u32] = &[];
    assert_eq!(
        origin([None, None, None, None, Some(empty)], ESC),
        Ok(Some("{\"source\": \"\"}".to_owned()))
    );
    assert_eq!(origin([None; 5], ESC), Ok(None));
    assert_eq!(origin_to_dict_as(None, ESC), Ok(None));
    assert_eq!(
        origin([Some(empty), None, None, None, None], ESC),
        Ok(Some(
            "{\"cwd\": \"\", \"source\": \"process-cwd\"}".to_owned()
        ))
    );
}

#[test]
fn the_bytes_keys_follow_cwd_worktree_root_repo_branch() {
    let s: &[u32] = &[0xDCFF];
    let got = origin([Some(s), Some(s), Some(s), Some(s), None], ESC)
        .expect("ok")
        .expect("a dict");
    let keys: Vec<&str> = [
        "cwd_bytes",
        "worktree_root_bytes",
        "repo_bytes",
        "branch_bytes",
    ]
    .into_iter()
    .collect();
    let at: Vec<usize> = keys.iter().map(|k| got.find(k).expect("key")).collect();
    assert!(at.windows(2).all(|w| w[0] < w[1]), "{got}");
    assert!(got.starts_with("{\"cwd\": null, \"repo\": null, \"branch\": null, \"worktree_root\": null, \"source\": \"process-cwd\""));
}

#[test]
fn a_surrogate_in_source_is_left_alone() {
    let s: &[u32] = &[0x73, 0xD800];
    assert_eq!(
        origin([None, None, None, None, Some(s)], ESC),
        Ok(Some("{\"source\": \"s\\ud800\"}".to_owned()))
    );
}

#[test]
fn both_fsencode_flavours_and_base64_padding() {
    let cases: &[(&[u32], FsEncoding, Option<&str>)] = &[
        (&[0x2F, 0x61, 0xDCFF], ESC, Some("L2H/")),
        (&[0x2F, 0x61, 0xDCFF], PASS, Some("L2Hts78=")),
        (&[0xD800], ESC, None),
        (&[0xD800], PASS, Some("7aCA")),
        (&[0xDFFF, 0xDC00], PASS, Some("7b+/7bCA")),
        (&[0xDC80, 0xDC80], ESC, Some("gIA=")),
        (&[0xDCFF; 4], ESC, Some("/////w==")),
        (&[0xDC7F], ESC, None),
    ];
    for (input, enc, want) in cases {
        let got = origin([Some(input), None, None, None, None], *enc);
        match want {
            None => assert_eq!(got, Err(UnicodeEncodeError), "{input:x?}"),
            Some(b) => assert_eq!(
                got,
                Ok(Some(format!(
                    "{{\"cwd\": null, \"source\": \"process-cwd\", \"cwd_bytes_base64\": \"{b}\"}}"
                ))),
                "{input:x?} {enc:?}"
            ),
        }
    }
    assert_eq!(fsencode_as(&[0x78], PASS), Some(vec![0x78]));
}

#[test]
fn the_host_flavour_is_the_systems() {
    #[cfg(windows)]
    assert_eq!(HOST_FS_ENCODING, FsEncoding::SurrogatePass);
    #[cfg(not(windows))]
    assert_eq!(HOST_FS_ENCODING, FsEncoding::SurrogateEscape);
}
