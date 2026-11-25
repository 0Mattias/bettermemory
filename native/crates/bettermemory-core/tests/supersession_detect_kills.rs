//! Inputs on which a plausible wrong detector answers otherwise than the
//! reference. Each expected value is the reference's (CPython 3.13.13),
//! the same rows `gen_supersession_detect` pins.

mod common;

use bettermemory_core::search_dedup::{raw_content_token_set, RawTokenCache, TokenSet};
use bettermemory_core::supersession::{
    detect_supersession, detect_supersession_in, DetectError, SupersessionReport,
};
use bettermemory_core::unicode::cps;
use common::supdetect::{share, text, Fake, Rec};
use serde_json::{json, Value};

const PORT_OLD: &str = "The auth service listens on port 8443 inside the cluster; the ingress \
                        terminates TLS and forwards to that port.";
const PORT_NEW: &str = "The auth service moved to port 9443 when the sidecar was introduced; \
                        the ingress forwards to 9443 and the old port is closed.";
const METRICS: &str = "The metrics sidecar is scraped on port 8125 by the cluster agent.";
const NEW12: &str = "The auth service moved to port 9443 last week; the ingress now forwards \
                     there and the old port is closed.";
const SILENT_B: &str = "The nightly billing export job writes parquet files for finance; \
                        weekend runs use Jenkins agents exclusively.";
const STAMP: &str = "\n\n_(consolidate --llm --from-transcript: turn 3 of (a) chat)_";
const SUB: &str = "The auth service listens on port 8443 and 7443 last week; the ingress now \
                   forwards there, old port closed, alpha beta gamma delta.";

fn recs(bodies: &[&str]) -> Vec<(String, String)> {
    bodies
        .iter()
        .enumerate()
        .map(|(i, b)| (format!("m{i:02}"), (*b).to_string()))
        .collect()
}

fn rows(recs: &[(String, String)]) -> Vec<Rec> {
    recs.iter().map(|(i, b)| Rec::new(i, b)).collect()
}

fn scan(t: &str, recs: &[(String, String)]) -> SupersessionReport {
    detect_supersession(&RawTokenCache::default(), &cps(t), &rows(recs), &[]).expect("a report")
}

fn digest(store: &mut Fake) -> Result<SupersessionReport, DetectError> {
    let t = store.t.clone();
    detect_supersession_in(&RawTokenCache::default(), store, &t, &[])
}

/// `(memory_id, old_value)` of each match.
fn ids(list: &[bettermemory_core::supersession::SupersessionMatch]) -> Vec<(String, String)> {
    list.iter()
        .map(|m| (text(&m.memory_id), text(&m.old_value)))
        .collect()
}

fn pair(id: &str, value: &str) -> (String, String) {
    (id.to_string(), value.to_string())
}

fn padded(shared: usize, other: usize) -> String {
    let words: Vec<String> = (0..shared)
        .map(|i| format!("w{i}"))
        .chain((0..other).map(|i| format!("zz{i}")))
        .collect();
    format!("Paging uses port 8443 for {}.", words.join(" "))
}

fn fillers(n: usize, shared: i64) -> Vec<bettermemory_core::store::digest_query::DigestShare> {
    (0..n)
        .map(|i| share(&format!("f{i:03}"), 12, false, shared))
        .collect()
}

/// The `tokens` and `bodies` entries of a log, ids shortened to their count
/// past ten.
fn reads(log: &[Value]) -> Vec<Value> {
    log.iter()
        .filter(|l| l[0] == json!("tokens") || l[0] == json!("bodies"))
        .map(|l| {
            let ids = l[1].as_array().expect("ids");
            if ids.len() > 10 {
                json!([l[0], ids.len()])
            } else {
                l.clone()
            }
        })
        .collect()
}

fn a_tokens() -> TokenSet {
    let mut tok: TokenSet = ["auth", "servic", "ingress", "port", "forward", "8443"]
        .iter()
        .map(|s| (*s).to_string())
        .collect();
    tok.extend((0..7).map(|i| format!("zz{i}")));
    tok
}

#[test]
fn the_ceiling_is_rounded_and_strict() {
    // `a` scores 5/12 (0.416667, rounded 0.4167); the 65th share's bound is
    // 5/12 too. Rounded and strict, `a` waits for the second batch; an
    // unrounded ceiling (0.4167 > 0.416667) or `>=` hands it on first.
    let mut shares = fillers(63, 12);
    shares.push(share("a", 13, false, 12));
    shares.push(share("b", 12, false, 5));
    let mut store = Fake::new(NEW12, vec![pair("a", PORT_OLD)], Some(shares));
    store.tok.insert("a".into(), a_tokens());
    let r = digest(&mut store).expect("a report");
    assert_eq!(ids(&r.supersedes), vec![pair("a", "8443")]);
    assert_eq!(
        reads(&store.log),
        vec![
            json!(["tokens", 64]),
            json!(["bodies", []]),
            json!(["tokens", ["b"]]),
            json!(["bodies", []]),
            json!(["bodies", ["a"]]),
        ]
    );
}

#[test]
fn a_score_over_the_next_bound_is_handed_on_before_the_next_batch() {
    let mut shares = fillers(63, 12);
    shares.push(share("a", 13, false, 12));
    shares.push(share("b", 13, false, 4));
    let mut store = Fake::new(NEW12, vec![pair("a", PORT_OLD)], Some(shares));
    store.tok.insert("a".into(), a_tokens());
    digest(&mut store).expect("a report");
    assert_eq!(
        reads(&store.log),
        vec![
            json!(["tokens", 64]),
            json!(["bodies", []]),
            json!(["bodies", ["a"]]),
            json!(["tokens", ["b"]]),
            json!(["bodies", []]),
        ]
    );
}

#[test]
fn a_rounded_tie_is_read_by_id_not_by_score() {
    // 0.112150 (`x1`) and 0.112069 (`x0`) both round to 0.1121; sorting on
    // the raw score reads `x1` first.
    let new: String = format!(
        "The auth service moved to port 9443 last week; {}.",
        (0..48)
            .map(|i| format!("w{i}"))
            .collect::<Vec<_>>()
            .join(" ")
    );
    let recs = vec![pair("x1", &padded(11, 49)), pair("x0", &padded(12, 58))];
    let want = vec![pair("x0", "8443"), pair("x1", "8443")];
    let r = scan(&new, &recs);
    assert_eq!(ids(&r.supersedes), want);
    assert_eq!(r.supersedes[0].similarity, r.supersedes[1].similarity);
    let shares = vec![share("x1", 76, false, 40), share("x0", 76, false, 40)];
    let mut store = Fake::new(&new, recs, Some(shares));
    assert_eq!(ids(&digest(&mut store).expect("a report").supersedes), want);
}

#[test]
fn equal_bounds_keep_the_shares_order() {
    // 64 fillers of two bounds with `a` among them: the first batch is the
    // higher bound's shares in their order, then the lower's.
    let shares: Vec<_> = (0..65)
        .map(|i| {
            if i == 30 {
                share("a", 12, false, 11)
            } else {
                share(
                    &format!("f{i:03}"),
                    12,
                    false,
                    if i % 3 == 0 { 11 } else { 12 },
                )
            }
        })
        .collect();
    let mut first: Vec<String> = Vec::new();
    first.extend(
        shares
            .iter()
            .filter(|s| s.shared == 12)
            .map(|s| s.id.clone()),
    );
    first.extend(
        shares
            .iter()
            .filter(|s| s.shared == 11)
            .map(|s| s.id.clone()),
    );
    let last = first.split_off(64);
    let mut store = Fake::new(NEW12, vec![pair("a", PORT_OLD)], Some(shares));
    digest(&mut store).expect("a report");
    let tokens: Vec<&Value> = store
        .log
        .iter()
        .filter(|l| l[0] == json!("tokens"))
        .collect();
    assert_eq!(tokens.len(), 2);
    assert_eq!(tokens[0][1], json!(first));
    assert_eq!(tokens[1][1], json!(last));
}

#[test]
fn a_full_list_stops_the_walk() {
    // Six restatements carrying 8443, then 64 shares whose bound is under
    // every score: the list is full after the fifth, so the sixth body and
    // the second batch are never read.
    let bodies: Vec<String> = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
        .iter()
        .map(|n| format!("The auth service listens on port 8443 inside cluster {n}."))
        .collect();
    let recs: Vec<(String, String)> = recs(&bodies.iter().map(String::as_str).collect::<Vec<_>>());
    let mut shares = common::supdetect::derived_shares(PORT_NEW, &recs);
    shares.extend((0..64).map(|i| share(&format!("f{i:03}"), 12, false, 3)));
    let mut store = Fake::new(PORT_NEW, recs, Some(shares));
    let r = digest(&mut store).expect("a report");
    assert_eq!(r.supersedes.len(), 5);
    let read = reads(&store.log);
    assert_eq!(read[0], json!(["tokens", 64]));
    assert_eq!(read.len(), 2 + 5, "{read:?}");
    assert!(!read.contains(&json!(["bodies", ["m05"]])));
}

#[test]
fn a_cue_match_must_carry_the_replaced_value() {
    // `m01` (8443) is the stronger; `m00` passes too, on 8125, and is
    // dropped. Keeping every cue match links both.
    let r = scan(PORT_NEW, &recs(&[METRICS, PORT_OLD]));
    assert_eq!(ids(&r.supersedes), vec![pair("m01", "8443")]);
    assert_eq!(
        r.supersedes[0].cue.as_deref().map(text).as_deref(),
        Some("moved")
    );
    // Lowered: `PAGERDUTY` is `PagerDuty`'s value, `Pagerduty-Bridge` is not.
    let r = scan(
        "Paging for the auth service moved to Opsgenie last week.",
        &recs(&[
            "Paging for the auth service goes to PagerDuty.",
            "Paging for the auth service is routed to PAGERDUTY today.",
            "Paging for the auth service goes to Pagerduty-Bridge.",
        ]),
    );
    assert_eq!(
        ids(&r.supersedes),
        vec![pair("m00", "PagerDuty"), pair("m01", "PAGERDUTY")]
    );
}

#[test]
fn a_cue_never_conflicts() {
    // Overlap 0.5714, no kin, no shared context: with a cue nothing; a rule
    // that fell through to the conflict test files a conflict.
    let r = scan(
        "The nightly billing export job moved; it writes parquet files for finance and \
         escalations page Ledger oncall directly.",
        &recs(&[SILENT_B]),
    );
    assert!(r.eligible && r.supersedes.is_empty() && r.conflicts.is_empty());
}

#[test]
fn the_fallback_pair_is_the_first_of_each_sorted() {
    let r = scan(
        "The nightly billing export job writes parquet files for finance; escalations page \
         Ledger and Zephyr oncall directly via hlx-pager.",
        &recs(&[
            "The nightly billing export job writes parquet files for finance; weekend runs use \
             Jenkins and Bamboo agents with ci-worker exclusively.",
        ]),
    );
    let m = &r.conflicts[0];
    assert_eq!(text(&m.evidence), "similarity");
    assert_eq!(
        (text(&m.new_value), text(&m.old_value)),
        pair("hlx-pager", "Bamboo")
    );
}

#[test]
fn the_summary_is_a_hundred_characters() {
    let r = scan(PORT_NEW, &recs(&[PORT_OLD]));
    assert_eq!(
        text(&r.supersedes[0].summary),
        "The auth service listens on port 8443 inside the cluster; the ingress terminates TLS \
         and forwards..."
    );
}

#[test]
fn the_floors_are_reached_at_equality() {
    // 0.1 with a cue supersedes; 0.15 without one, on kin, conflicts.
    let r = scan(
        "The proxy sidecar moved to port 9125 for dashboards, alert routing, tracing, logs, \
         audits, quotas, billing and backups in staging.",
        &recs(&[METRICS]),
    );
    assert_eq!(r.supersedes[0].similarity, 0.1);
    let r = scan(
        "The proxy sidecar on port 9125 serves dashboards, alert routing, tracing, logs, \
         audits, quotas, billing and backups for the cluster in staging.",
        &recs(&[METRICS]),
    );
    assert_eq!(r.conflicts[0].similarity, 0.15);
    assert_eq!(text(&r.conflicts[0].evidence), "kin");
}

#[test]
fn a_share_under_the_floor_in_a_read_batch_is_scored() {
    // `b`'s bound (1 shared) is under the floor, but it is in the batch
    // `a` is read in, and its tokens score it over: it is reported.
    let shares = vec![share("a", 13, false, 12), share("b", 13, false, 1)];
    let mut store = Fake::new(
        NEW12,
        vec![pair("a", PORT_OLD), pair("b", PORT_OLD)],
        Some(shares),
    );
    let r = digest(&mut store).expect("a report");
    assert_eq!(
        ids(&r.supersedes),
        vec![pair("a", "8443"), pair("b", "8443")]
    );
}

#[test]
fn a_repeated_id_raises_or_orders_by_token_sets() {
    let shares = vec![share("m00", 9, false, 5), share("m00", 9, false, 5)];
    let mut store = Fake::new(PORT_NEW, recs(&[PORT_OLD]), Some(shares));
    let e = digest(&mut store).expect_err("TypeError");
    assert_eq!(e.class(), "TypeError");
    assert_eq!(
        e.message(),
        "'<' not supported between instances of 'function' and 'function'"
    );
    // A stamped and an unstamped share of one id at 0.575: the proper
    // subset pops first whichever was pushed first, so 8443 (its value) is
    // the replaced one and 7443 (the superset's) is dropped.
    let mut sub_tok = raw_content_token_set(&cps(SUB));
    sub_tok.remove("7443");
    for order in [[false, true], [true, false]] {
        let r = repeated(NEW12, &format!("{SUB}{STAMP}"), &sub_tok, order, 20, 11);
        assert_eq!(ids(&r.supersedes), vec![pair("d", "8443")]);
    }
}

/// `SUB`'s tokens less 7443 plus a word of its own: as many tokens as
/// `SUB`'s, neither set a subset of the other, the same score (0.575).
fn level_tokens() -> TokenSet {
    let mut tok = raw_content_token_set(&cps(SUB));
    tok.remove("7443");
    tok.insert("zzq".to_string());
    tok
}

/// One id shared twice, unstamped (`tok`) and stamped (`body`), in `order`.
fn repeated(
    t: &str,
    body: &str,
    tok: &TokenSet,
    order: [bool; 2],
    claim: i64,
    shared: i64,
) -> SupersessionReport {
    let shares = order
        .iter()
        .map(|st| share("d", claim, *st, shared))
        .collect();
    let mut store = Fake::new(t, vec![pair("d", body)], Some(shares));
    store.tok.insert("d".into(), tok.clone());
    digest(&mut store).expect("a report")
}

#[test]
fn a_repeated_id_with_unordered_token_sets_keeps_the_order_pushed() {
    // Token sets of one size, neither a subset: unordered, not equal, so
    // the heap answers and keeps the order pushed. A heap that read equal
    // sizes as equal sets raises TypeError.
    let body = format!("{SUB}{STAMP}");
    let r = repeated(NEW12, &body, &level_tokens(), [false, true], 20, 11);
    assert_eq!(ids(&r.supersedes), vec![pair("d", "8443")]);
    let r = repeated(NEW12, &body, &level_tokens(), [true, false], 20, 11);
    assert_eq!(ids(&r.supersedes), vec![pair("d", "7443")]);
}

#[test]
fn siftup_moves_up_the_right_child_of_an_unordered_pair() {
    // `a` pops first and is skipped (its tokens hold every new value);
    // `_siftup` then finds the two `d` entries unordered and moves the
    // right one (stamped, 7443) up. Moving the left (8443) up only when the
    // right is not smaller reads 8443 first.
    let mut shares = vec![share("a", 20, false, 11)];
    shares.extend([false, true].map(|st| share("d", 20, st, 11)));
    shares.push(share("e", 20, false, 11));
    let mut store = Fake::new(
        NEW12,
        vec![pair("d", &format!("{SUB}{STAMP}"))],
        Some(shares),
    );
    store
        .tok
        .insert("a".into(), raw_content_token_set(&cps(NEW12)));
    store.tok.insert("d".into(), level_tokens());
    let mut x: TokenSet = ["9443", "auth", "servic", "port"]
        .iter()
        .map(|s| (*s).to_string())
        .collect();
    x.extend((0..10).map(|i| format!("zz{i}")));
    store.tok.insert("e".into(), x);
    let r = digest(&mut store).expect("a report");
    assert_eq!(ids(&r.supersedes), vec![pair("d", "7443")]);
}

#[test]
fn a_repeated_id_at_scores_that_round_alike_reads_the_lower_first() {
    // Unstamped 0.112069 (8443 alone), stamped 0.112150 (7443 first): both
    // round to 0.1121, and the lower raw score pops first whichever was
    // pushed first.
    let near: Vec<String> = (0..11)
        .map(|i| format!("w{i}"))
        .chain((0..48).map(|i| format!("zz{i}")))
        .collect();
    let body = format!(
        "Paging uses port 8443 and 7443 for {}.{STAMP}",
        near.join(" ")
    );
    let mut tok: TokenSet = ["8443", "paging", "port", "use"]
        .iter()
        .map(|s| (*s).to_string())
        .collect();
    tok.extend((0..12).map(|i| format!("w{i}")));
    tok.extend((0..58).map(|i| format!("zz{i}")));
    let new55 = format!(
        "The auth service moved to port 9443 last week; {}.",
        (0..48)
            .map(|i| format!("w{i}"))
            .collect::<Vec<_>>()
            .join(" ")
    );
    for order in [[false, true], [true, false]] {
        let r = repeated(&new55, &body, &tok, order, 63, 13);
        assert_eq!(ids(&r.supersedes), vec![pair("d", "8443")]);
        assert_eq!(r.supersedes[0].similarity, 0.1121);
    }
}

#[test]
fn an_unstamped_share_is_read_with_the_stamp_its_stored_body_carries() {
    // `bodies_of`'s body whole: a claim under a hundred characters leaves
    // room in the summary for the stamp. Stripping it ends the summary at
    // the claim.
    let short = "The auth service listens on port 8443 inside the cluster.";
    let mut store = Fake::new(
        NEW12,
        vec![pair("d", &format!("{short}{STAMP}"))],
        Some(vec![share("d", 9, false, 6)]),
    );
    store
        .tok
        .insert("d".into(), raw_content_token_set(&cps(short)));
    let r = digest(&mut store).expect("a report");
    assert_eq!(ids(&r.supersedes), vec![pair("d", "8443")]);
    assert_eq!(
        text(&r.supersedes[0].summary),
        "The auth service listens on port 8443 inside the cluster.\n\n_(consolidate --llm \
         --from-transcript..."
    );
}

#[test]
fn the_fallback_scan_honours_exclude_ids() {
    // No digest, or a memo that answers None: the scan over `load_all`
    // leaves m00 (8443) out, so m01 (8125) is the replaced claim.
    let records = recs(&[PORT_OLD, METRICS]);
    let derived = common::supdetect::derived_shares(PORT_NEW, &records);
    for (shares, memo_none) in [(None, false), (Some(derived), true)] {
        let mut store = Fake::new(PORT_NEW, records.clone(), shares);
        store.memo_none = memo_none;
        let r = detect_supersession_in(
            &RawTokenCache::default(),
            &mut store,
            &cps(PORT_NEW),
            &[cps("m00")],
        )
        .expect("a report");
        assert_eq!(ids(&r.supersedes), vec![pair("m01", "8125")]);
        assert_eq!(store.log.last(), Some(&json!(["all"])));
    }
}
