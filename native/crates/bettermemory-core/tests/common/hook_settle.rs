//! The recording fakes of `gen_hook_settle`, as a [`StopSide`] whose
//! front-half calls are never made: every object is its label, a time is
//! a whole number of seconds, every call is logged as
//! `[name, {argument: value}]`, and the world's `raise` (`[call, class,
//! text]`) names the call that fails once it has logged itself.

use std::collections::BTreeSet;

use bettermemory_core::hook_settle::Settlement;
use bettermemory_core::hook_stop::{ProbeInput, Stop, StopSettle, StopSide};
use serde_json::{json, Map, Value};

/// A raised exception: its class, its text, and whether `except
/// Exception` catches it (`Boom`) or not (`Halt`).
#[derive(Debug, Clone, PartialEq)]
pub struct Raised {
    pub class: String,
    pub text: String,
}

pub struct Fake {
    pub w: Value,
    pub log: Vec<Value>,
}

fn opt(v: Option<&str>) -> Value {
    v.map_or(Value::Null, |s| json!(s))
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .into_iter()
        .flatten()
        .map(|s| s.as_str().unwrap().to_string())
        .collect()
}

impl Fake {
    pub fn new(w: Value) -> Fake {
        Fake { w, log: Vec::new() }
    }

    pub fn call(&mut self, name: &str, args: Value) -> Result<(), Raised> {
        self.log.push(json!([name, args]));
        let r = &self.w["raise"];
        if r[0].as_str() == Some(name) {
            return Err(Raised {
                class: r[1].as_str().unwrap().to_string(),
                text: exception_text(&r.as_array().unwrap()[2..]),
            });
        }
        Ok(())
    }
}

macro_rules! front {
    () => {
        unreachable!("the front half is not under test")
    };
}

impl StopSide for Fake {
    type Error = Raised;
    type Store = String;
    type Config = String;
    type Report = bool;
    type Admission = String;
    type Resolution = ();
    type RecallState = String;
    type Origin = ();
    type Turn = ();
    type Recorder = String;
    type Event = String;
    type Time = i64;
    type Path = String;
    type Posted = ();
    type Dict = ();
    type Fields = Value;

    fn within_hook_budget<R, F>(&mut self, _: &str, _: Option<String>, _: F) -> Result<R, Raised>
    where
        F: FnOnce(&mut Self, Option<String>) -> Result<R, Raised>,
    {
        front!()
    }
    fn suppressed<R, F>(&mut self, _: F) -> Result<R, Raised>
    where
        F: FnOnce(&mut Self) -> Result<R, Raised>,
    {
        front!()
    }
    fn load_config(&mut self) -> Result<String, Raised> {
        front!()
    }
    fn resolved_directory(&mut self, _: &String) -> Result<String, Raised> {
        front!()
    }
    fn store_file(&mut self, _: &String) -> String {
        front!()
    }
    fn open_or_create(&mut self, _: &String) -> Result<String, Raised> {
        front!()
    }
    fn hook_utcnow(&mut self) -> Result<i64, Raised> {
        front!()
    }
    fn seconds_before(&mut self, time: &i64, seconds: i64) -> i64 {
        time - seconds
    }
    fn events_since(&mut self, _: &String, _: &i64) -> Result<Vec<String>, Raised> {
        front!()
    }
    fn bind_transcript(&mut self, _: &str, _: Option<&str>) -> Result<(), Raised> {
        front!()
    }
    fn origin_for(&mut self, _: Option<&String>) -> Result<(), Raised> {
        front!()
    }
    fn worktree_root(&self, _: &()) -> Option<String> {
        front!()
    }
    fn bridged_session(
        &mut self,
        _: &[String],
        _: &str,
        _: Option<&str>,
    ) -> Result<Option<String>, Raised> {
        front!()
    }
    fn caller_admission(
        &mut self,
        _: &String,
        _: &String,
        _: Option<&String>,
        _: &(),
        _: Option<&()>,
        _: &str,
    ) -> Result<(String, ()), Raised> {
        front!()
    }
    fn disabled_scopes_from_events(
        &mut self,
        _: &[String],
        _: Option<&str>,
        _: Option<&str>,
    ) -> Result<BTreeSet<String>, Raised> {
        front!()
    }
    fn utcnow(&mut self) -> Result<i64, Raised> {
        front!()
    }
    fn no_owner_prompt_report(&mut self, _: &str, _: &i64, _: i64) -> Result<bool, Raised> {
        front!()
    }
    fn probe_message(&mut self, _: ProbeInput<'_, Self>) -> Result<bool, Raised> {
        front!()
    }
    fn telemetry_enabled(&self, _: &String) -> bool {
        front!()
    }
    fn search_mode(&self, _: &String) -> Option<String> {
        front!()
    }
    fn new_recorder(
        &mut self,
        _: &String,
        _: &str,
        _: bool,
        _: Option<&str>,
    ) -> Result<String, Raised> {
        front!()
    }
    fn new_recall_state(&mut self) -> Result<String, Raised> {
        front!()
    }
    fn confirm_compaction(
        &mut self,
        _: &String,
        _: &mut String,
        _: (&str, &str),
        _: Option<&str>,
        _: &String,
        _: &str,
        _: &i64,
        _: &(),
    ) -> Result<(), Raised> {
        front!()
    }
    fn previous_audit_at(&mut self, _: &String, _: &str, _: &i64) -> Result<Option<i64>, Raised> {
        front!()
    }
    fn redact_query_hash(&mut self, _: &str) -> Result<String, Raised> {
        front!()
    }
    fn is_duplicate_audit(
        &mut self,
        _: &[String],
        _: &str,
        _: &str,
        _: &str,
        _: &i64,
    ) -> Result<bool, Raised> {
        front!()
    }
    fn report_to_dict(&mut self, _: &bool) -> Result<(), Raised> {
        front!()
    }

    fn joined_sessions(
        &mut self,
        recent: &[String],
        harness_session: &str,
        worktree_root: Option<&str>,
    ) -> Result<BTreeSet<String>, Raised> {
        self.call(
            "joined_sessions",
            json!({"_0": recent, "harness_session": harness_session,
                   "worktree_root": opt(worktree_root)}),
        )?;
        Ok(["j2".to_string(), "j1".to_string()].into())
    }
    fn emit_hook_attributions(
        &mut self,
        store: &String,
        recorder: &mut String,
        recent_events: &[String],
        session_id: &str,
        assistant_response: &str,
        worktree_root: Option<&str>,
        client_model: Option<&str>,
        server_session: Option<&str>,
    ) -> Result<Vec<String>, Raised> {
        self.call(
            "_emit_hook_attributions",
            json!({"store": store, "recorder": recorder, "recent_events": recent_events,
                   "session_id": session_id, "assistant_response": assistant_response,
                   "worktree_root": opt(worktree_root), "client_model": opt(client_model),
                   "server_session": opt(server_session)}),
        )?;
        Ok(strs(&self.w["settled"]))
    }
    fn settle_shown(
        &mut self,
        store: &String,
        recorder: &mut String,
        state: &mut String,
        session_id: &str,
        harness: Option<&str>,
        assistant_response: &str,
        sessions: &BTreeSet<String>,
        exclude: &[String],
        prompt_id: Option<&str>,
        client_model: Option<&str>,
        now: &i64,
    ) -> Result<Vec<String>, Raised> {
        self.call(
            "settle_shown",
            json!({"store": store, "recorder": recorder, "state": state,
                   "session_id": session_id, "harness": opt(harness),
                   "assistant_response": assistant_response, "sessions": sessions,
                   "exclude": exclude, "prompt_id": opt(prompt_id),
                   "client_model": opt(client_model), "now": now}),
        )?;
        Ok(strs(&self.w["shown"]))
    }
    fn rule(&self) -> String {
        "RULE-x".into()
    }
    fn turn_audited_fields(
        &mut self,
        report: &bool,
        session_id: &str,
        probe_mode: &str,
        assistant_present: bool,
        triggered_from: &str,
        repeat: bool,
        client_model: Option<&str>,
        prompt_id: Option<&str>,
        reply_source: Option<&str>,
        rule: &str,
        settled_count: i64,
    ) -> Result<Value, Raised> {
        let _ = report;
        self.call(
            "turn_audited_fields",
            json!({"_0": "R", "session_id": session_id, "probe_mode": probe_mode,
                   "assistant_present": assistant_present, "triggered_from": triggered_from,
                   "repeat": repeat, "client_model": opt(client_model),
                   "prompt_id": opt(prompt_id), "reply_source": opt(reply_source),
                   "rule": rule, "settled_count": settled_count}),
        )?;
        Ok(json!("TA"))
    }
    fn search_miss_fields(
        &mut self,
        _: &bool,
        session_id: &str,
        triggered_from: &str,
        client_model: Option<&str>,
        rule: &str,
    ) -> Result<Value, Raised> {
        self.call(
            "search_miss_fields",
            json!({"_0": "R", "session_id": session_id, "triggered_from": triggered_from,
                   "client_model": opt(client_model), "rule": rule}),
        )?;
        Ok(json!("SM"))
    }
    fn record(&mut self, _: &mut String, kind: &str, fields: Value) -> Result<(), Raised> {
        self.call("record", json!({"_0": kind, "f": fields}))
    }
    fn report_is_miss(&self, report: &bool) -> bool {
        *report
    }
    fn segment_started_at(
        &mut self,
        _: &mut String,
        store: &String,
        key: (&str, &str),
        now: &i64,
    ) -> Result<i64, Raised> {
        self.call(
            "segment",
            json!({"_0": store, "_1": [key.0, key.1], "now": now}),
        )?;
        Ok(self.w["first_seen"].as_i64().unwrap())
    }
    fn events_of_harness_stamped_kinds(
        &mut self,
        _: &String,
        since: &i64,
    ) -> Result<Vec<String>, Raised> {
        self.call("events_of_kinds", json!({"_0": true, "since": since}))?;
        Ok(vec!["E".into()])
    }
    fn harness_joined_sessions(
        &mut self,
        events: &[String],
        session_id: &str,
    ) -> Result<BTreeSet<String>, Raised> {
        // The Python fake is handed the label `E` itself.
        self.call(
            "harness_joined_sessions",
            json!({"_0": events.join(""), "_1": session_id}),
        )?;
        Ok(strs(&self.w["joined"]).into_iter().collect())
    }
    fn review_unanswered(
        &mut self,
        store: &String,
        recorder: &mut String,
        state: &mut String,
        session_id: &str,
        harness: Option<&str>,
        sessions: &BTreeSet<String>,
        since: &i64,
        now: &i64,
    ) -> Result<(), Raised> {
        self.call(
            "review_unanswered",
            json!({"store": store, "recorder": recorder, "state": state,
                   "session_id": session_id, "harness": opt(harness), "sessions": sessions,
                   "since": since, "now": now}),
        )
    }
    fn settle_interrupted(&mut self, s: &Stop<Self>) -> Result<(), Raised> {
        self.call(
            "settle_interrupted",
            json!({"store": s.store, "cfg": s.cfg, "session_id": s.session_id,
                   "transcript_path": s.transcript_path, "admission": s.admission,
                   "excluded_scopes": s.excluded_scopes, "recent": s.recent,
                   "server_session": s.server_session, "worktree_root": s.worktree_root,
                   "now": s.now, "previous_audit": s.previous_audit,
                   "triggered_from": "stop_hook", "exclude_prompt_id": s.prompt_id}),
        )
    }
    fn is_exception(&self, error: &Raised) -> bool {
        error.class != "Halt"
    }
    fn error_parts(&self, error: &Raised) -> (String, String) {
        (error.class.clone(), error.text.clone())
    }
    fn log_warning(&mut self, message: &str) {
        self.log.push(json!(["warning", {"_0": message}]));
    }
}

/// `str(exc)` of an exception built from `args`: empty for none, the
/// argument for one, the tuple's repr for several (plain arguments
/// without quotes or backslashes, which is all the rows hold).
fn exception_text(args: &[Value]) -> String {
    let parts: Vec<&str> = args.iter().map(|a| a.as_str().unwrap()).collect();
    match parts.as_slice() {
        [] => String::new(),
        [one] => (*one).to_string(),
        many => {
            for p in many {
                assert!(!p.contains(['\'', '\\']) && p.chars().all(|c| c >= ' '));
            }
            let quoted: Vec<String> = many.iter().map(|p| format!("'{p}'")).collect();
            format!("({})", quoted.join(", "))
        }
    }
}

fn text(s: &Value, k: &str) -> Option<String> {
    s[k].as_str().map(str::to_string)
}

/// The Stop a golden row's state (`s`) and world (`w`) describe.
pub fn stop_of(s: &Value, w: &Value) -> Stop<Fake> {
    Stop {
        store: "S".into(),
        cfg: "C".into(),
        recorder: "Rec".into(),
        recent: strs(&s["recent"]),
        session_id: text(s, "session_id").unwrap(),
        user_message: text(s, "user_message"),
        assistant_response: text(s, "assistant_response"),
        worktree_root: text(s, "worktree_root"),
        client_model: text(s, "client_model"),
        server_session: text(s, "server_session"),
        recall_state: "RS".into(),
        harness: text(s, "harness"),
        prompt_id: text(s, "prompt_id"),
        reply_source: text(s, "reply_source"),
        transcript_path: text(s, "transcript_path"),
        now: s["now"].as_i64().unwrap(),
        previous_audit: s["previous_audit"].as_i64(),
        admission: "A".into(),
        excluded_scopes: strs(&s["excluded_scopes"]).into_iter().collect(),
        report: w["miss"].as_bool().unwrap_or(false),
        probe_mode: text(s, "probe_mode").unwrap(),
        repeat: s["repeat"].as_bool().unwrap(),
    }
}

/// One settlement step over a golden row: the log, and the answer as the
/// row spells it (`{"r": ...}` or `{"x": class}`).
pub fn run_row(f: &str, s: &Value, w: &Value) -> (Vec<Value>, Map<String, Value>) {
    let mut side = Fake::new(w.clone());
    let mut stop = stop_of(s, w);
    let mut t = Settlement;
    let r: Result<Value, Raised> = match f {
        "settle_stop_turn" => t.settle_stop_turn(&mut side, &mut stop).map(Value::from),
        "record_stop_rows" => {
            let n = s["settled_count"].as_i64().unwrap();
            t.record_stop_rows(&mut side, &mut stop, n)
                .map(|()| Value::Null)
        }
        "count_stop_reviews" => t
            .count_stop_reviews(&mut side, &mut stop)
            .map(|()| Value::Null),
        "settle_stop_interrupted" => t
            .settle_stop_interrupted(&mut side, &mut stop)
            .map(|()| Value::Null),
        other => panic!("unknown step {other}"),
    };
    let mut out = Map::new();
    match r {
        Ok(v) => out.insert("r".into(), v),
        Err(x) => out.insert("x".into(), json!(x.class)),
    };
    (side.log, out)
}
