//! The recording fakes of `gen_hook_stop`, as a [`StopSide`] and a
//! [`StopSettle`]: every object is its label, every call is logged as
//! `[name, {argument: value}]` and the world's `raise` names the call that
//! fails once it has logged itself.

use std::collections::BTreeSet;

use bettermemory_core::hook_stop::{run_audit, AuditInput, ProbeInput, Stop, StopSettle, StopSide};
use serde_json::{json, Map, Value};

pub struct Cfg {
    pub label: String,
    pub search_mode: Option<String>,
    pub telemetry: bool,
}

pub struct Fake {
    pub w: Value,
    pub log: Vec<Value>,
    u: u32,
    n: u32,
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
        Fake {
            w,
            log: Vec::new(),
            u: 0,
            n: 0,
        }
    }

    pub fn call(&mut self, name: &str, args: Value) -> Result<(), String> {
        self.log.push(json!([name, args]));
        if self.w["raise"].as_str() == Some(name) {
            return Err(name.to_string());
        }
        Ok(())
    }

    fn cfg(&self) -> Cfg {
        let c = &self.w["cfg"];
        Cfg {
            label: c["label"].as_str().unwrap_or("C").to_string(),
            search_mode: c["search_mode"].as_str().map(str::to_string),
            telemetry: c["telemetry"].as_bool().unwrap_or(true),
        }
    }
}

impl StopSide for Fake {
    type Error = String;
    type Store = String;
    type Config = Cfg;
    type Report = String;
    type Admission = String;
    type Resolution = String;
    type RecallState = String;
    type Origin = Option<String>;
    type Turn = ();
    type Recorder = String;
    type Event = String;
    type Time = String;
    type Path = String;
    type Posted = String;
    type Dict = Value;
    type Fields = Value;

    fn within_hook_budget<R, F>(
        &mut self,
        event: &str,
        store: Option<String>,
        body: F,
    ) -> Result<R, String>
    where
        F: FnOnce(&mut Self, Option<String>) -> Result<R, String>,
    {
        self.call("within_hook_budget", json!({"_0": event, "_1": store}))?;
        let r = body(self, store);
        self.log
            .push(json!(["within_hook_budget_exit", {"_0": event}]));
        r
    }
    fn suppressed<R, F>(&mut self, body: F) -> Result<R, String>
    where
        F: FnOnce(&mut Self) -> Result<R, String>,
    {
        self.call("suppressed", json!({}))?;
        let r = body(self);
        self.log.push(json!(["suppressed_exit", {}]));
        r
    }
    fn load_config(&mut self) -> Result<Cfg, String> {
        self.call("load_config", json!({"_0": null}))?;
        Ok(self.cfg())
    }
    fn resolved_directory(&mut self, cfg: &Cfg) -> Result<String, String> {
        self.call("resolved_directory", json!({"_0": cfg.label}))?;
        Ok("D".into())
    }
    fn store_file(&mut self, root: &String) -> String {
        format!("{root}/memory.sqlite")
    }
    fn open_or_create(&mut self, path: &String) -> Result<String, String> {
        self.call("open_or_create", json!({"_0": path}))?;
        Ok("S0".into())
    }
    fn hook_utcnow(&mut self) -> Result<String, String> {
        self.call("_utcnow", json!({}))?;
        self.u += 1;
        Ok(format!("U{}", self.u))
    }
    fn seconds_before(&mut self, time: &String, seconds: i64) -> String {
        format!("{time}-{seconds}")
    }
    fn events_since(&mut self, store: &String, since: &String) -> Result<Vec<String>, String> {
        self.call("events_since", json!({"_0": store, "_1": since}))?;
        Ok(strs(&self.w["window"]))
    }
    fn bind_transcript(&mut self, session_id: &str, model: Option<&str>) -> Result<(), String> {
        self.call(
            "bind_transcript",
            json!({"session_id": session_id, "model": opt(model)}),
        )
    }
    fn origin_for(&mut self, cwd: Option<&String>) -> Result<Option<String>, String> {
        self.call("_origin_for", json!({"_0": cwd}))?;
        Ok(self.w["wr"].as_str().map(str::to_string))
    }
    fn worktree_root(&self, origin: &Option<String>) -> Option<String> {
        origin.clone()
    }
    fn bridged_session(
        &mut self,
        recent: &[String],
        harness_session: &str,
        worktree_root: Option<&str>,
    ) -> Result<Option<String>, String> {
        self.call(
            "bridged_session",
            json!({"_0": recent, "harness_session": harness_session,
                   "worktree_root": opt(worktree_root)}),
        )?;
        Ok(self.w["ss"].as_str().map(str::to_string))
    }
    fn caller_admission(
        &mut self,
        store: &String,
        cfg: &Cfg,
        cwd: Option<&String>,
        _origin: &Option<String>,
        posted: Option<&String>,
        session_id: &str,
    ) -> Result<(String, String), String> {
        self.call(
            "caller_admission",
            json!({"_0": true, "store": store, "cfg": cfg.label, "cwd": cwd,
                   "origin": "O", "posted": posted, "session_id": session_id}),
        )?;
        Ok(("A".into(), "resolution".into()))
    }
    fn disabled_scopes_from_events(
        &mut self,
        recent: &[String],
        worktree_root: Option<&str>,
        server_session: Option<&str>,
    ) -> Result<BTreeSet<String>, String> {
        self.call(
            "_disabled_scopes_from_events",
            json!({"_0": recent, "worktree_root": opt(worktree_root),
                   "server_session": opt(server_session)}),
        )?;
        Ok(strs(&self.w["scopes"]).into_iter().collect())
    }
    fn utcnow(&mut self) -> Result<String, String> {
        self.call("utcnow", json!({}))?;
        self.n += 1;
        Ok(format!("N{}", self.n))
    }
    fn no_owner_prompt_report(
        &mut self,
        session_id: &str,
        now: &String,
        lookback_seconds: i64,
    ) -> Result<String, String> {
        self.call(
            "no_owner_prompt_report",
            json!({"session_id": session_id, "now": now, "lookback_seconds": lookback_seconds}),
        )?;
        Ok("R-none".into())
    }
    fn probe_message(&mut self, i: ProbeInput<'_, Self>) -> Result<String, String> {
        self.call(
            "_probe_message",
            json!({"cfg": i.cfg.label, "store": i.store, "user_message": i.user_message,
                   "session_id": i.session_id, "server_session": opt(i.server_session),
                   "admission": i.admission, "excluded_scopes": i.excluded_scopes,
                   "recent": i.recent, "worktree_root": opt(i.worktree_root), "now": i.now}),
        )?;
        Ok("R-probe".into())
    }
    fn telemetry_enabled(&self, cfg: &Cfg) -> bool {
        cfg.telemetry
    }
    fn search_mode(&self, cfg: &Cfg) -> Option<String> {
        cfg.search_mode.clone()
    }
    fn new_recorder(
        &mut self,
        store: &String,
        session_id: &str,
        enabled: bool,
        worktree_root: Option<&str>,
    ) -> Result<String, String> {
        self.call(
            "Recorder",
            json!({"store": store, "session_id": session_id, "enabled": enabled,
                   "worktree_root": opt(worktree_root)}),
        )?;
        Ok("Rec".into())
    }
    fn new_recall_state(&mut self) -> Result<String, String> {
        self.call("RecallState", json!({}))?;
        Ok("RS-new".into())
    }
    fn confirm_compaction(
        &mut self,
        store: &String,
        state: &mut String,
        key: (&str, &str),
        transcript_path: Option<&str>,
        cfg: &Cfg,
        triggered_from: &str,
        now: &String,
        _origin: &Option<String>,
    ) -> Result<(), String> {
        self.call(
            "confirm_compaction",
            json!({"store": store, "state": state, "key": [key.0, key.1],
                   "transcript_path": opt(transcript_path), "cfg": cfg.label,
                   "triggered_from": triggered_from, "now": now, "origin": "O"}),
        )
    }
    fn previous_audit_at(
        &mut self,
        store: &String,
        session_id: &str,
        now: &String,
    ) -> Result<Option<String>, String> {
        self.call(
            "previous_audit_at",
            json!({"_0": store, "_1": session_id, "now": now}),
        )?;
        Ok(self.w["prev"].as_str().map(str::to_string))
    }
    fn redact_query_hash(&mut self, text: &str) -> Result<String, String> {
        self.call("redact_query", json!({"_0": text}))?;
        Ok(format!("h:{text}"))
    }
    fn is_duplicate_audit(
        &mut self,
        recent: &[String],
        session_id: &str,
        probe_query_hash: &str,
        probe_query_text: &str,
        now: &String,
    ) -> Result<bool, String> {
        self.call(
            "is_duplicate_audit",
            json!({"_0": recent, "session_id": session_id, "probe_query_hash": probe_query_hash,
                   "probe_query_text": probe_query_text, "now": now}),
        )?;
        Ok(self.w["dup"].as_bool().unwrap_or(false))
    }
    fn report_to_dict(&mut self, report: &String) -> Result<Value, String> {
        self.call("to_dict", json!({"_0": report}))?;
        Ok(json!({"report": report}))
    }

    fn joined_sessions(
        &mut self,
        _: &[String],
        _: &str,
        _: Option<&str>,
    ) -> Result<BTreeSet<String>, String> {
        unreachable!("settlement")
    }
    fn emit_hook_attributions(
        &mut self,
        _: &String,
        _: &mut String,
        _: &[String],
        _: &str,
        _: &str,
        _: Option<&str>,
        _: Option<&str>,
        _: Option<&str>,
    ) -> Result<Vec<String>, String> {
        unreachable!("settlement")
    }
    fn settle_shown(
        &mut self,
        _: &String,
        _: &mut String,
        _: &mut String,
        _: &str,
        _: Option<&str>,
        _: &str,
        _: &BTreeSet<String>,
        _: &[String],
        _: Option<&str>,
        _: Option<&str>,
        _: &String,
    ) -> Result<Vec<String>, String> {
        unreachable!("settlement")
    }
    fn rule(&self) -> String {
        unreachable!("settlement")
    }
    fn turn_audited_fields(
        &mut self,
        _: &String,
        _: &str,
        _: &str,
        _: bool,
        _: &str,
        _: bool,
        _: Option<&str>,
        _: Option<&str>,
        _: Option<&str>,
        _: &str,
        _: i64,
    ) -> Result<Value, String> {
        unreachable!("settlement")
    }
    fn search_miss_fields(
        &mut self,
        _: &String,
        _: &str,
        _: &str,
        _: Option<&str>,
        _: &str,
    ) -> Result<Value, String> {
        unreachable!("settlement")
    }
    fn record(&mut self, _: &mut String, _: &str, _: Value) -> Result<(), String> {
        unreachable!("settlement")
    }
    fn report_is_miss(&self, _: &String) -> bool {
        unreachable!("settlement")
    }
    fn segment_started_at(
        &mut self,
        _: &mut String,
        _: &String,
        _: (&str, &str),
        _: &String,
    ) -> Result<String, String> {
        unreachable!("settlement")
    }
    fn events_of_harness_stamped_kinds(
        &mut self,
        _: &String,
        _: &String,
    ) -> Result<Vec<String>, String> {
        unreachable!("settlement")
    }
    fn harness_joined_sessions(
        &mut self,
        _: &[String],
        _: &str,
    ) -> Result<BTreeSet<String>, String> {
        unreachable!("settlement")
    }
    fn review_unanswered(
        &mut self,
        _: &String,
        _: &mut String,
        _: &mut String,
        _: &str,
        _: Option<&str>,
        _: &BTreeSet<String>,
        _: &String,
        _: &String,
    ) -> Result<(), String> {
        unreachable!("settlement")
    }
    fn settle_interrupted(&mut self, _: &Stop<Self>) -> Result<(), String> {
        unreachable!("settlement")
    }
    fn is_exception(&self, _: &String) -> bool {
        unreachable!("settlement")
    }
    fn error_parts(&self, _: &String) -> (String, String) {
        unreachable!("settlement")
    }
    fn log_warning(&mut self, _: &str) {
        unreachable!("settlement")
    }
}

/// The recording settlement steps.
pub struct Settle;

impl StopSettle<Fake> for Settle {
    fn settle_stop_turn(&mut self, side: &mut Fake, s: &mut Stop<Fake>) -> Result<i64, String> {
        side.call(
            "_settle_stop_turn",
            json!({"store": s.store, "recorder": s.recorder, "recent": s.recent,
                   "session_id": s.session_id, "user_message": s.user_message,
                   "assistant_response": s.assistant_response, "worktree_root": s.worktree_root,
                   "client_model": s.client_model, "server_session": s.server_session,
                   "recall_state": s.recall_state, "harness": s.harness,
                   "prompt_id": s.prompt_id, "now": s.now}),
        )?;
        Ok(side.w["settled"].as_i64().unwrap_or(0))
    }
    fn record_stop_rows(
        &mut self,
        side: &mut Fake,
        s: &mut Stop<Fake>,
        settled: i64,
    ) -> Result<(), String> {
        side.call(
            "_record_stop_rows",
            json!({"_0": s.recorder, "_1": s.report, "session_id": s.session_id,
                   "probe_mode": s.probe_mode,
                   "assistant_present": s.assistant_response.is_some(), "repeat": s.repeat,
                   "client_model": s.client_model, "prompt_id": s.prompt_id,
                   "reply_source": s.reply_source, "settled_count": settled}),
        )
    }
    fn count_stop_reviews(&mut self, side: &mut Fake, s: &mut Stop<Fake>) -> Result<(), String> {
        side.call(
            "_count_stop_reviews",
            json!({"_0": s.store, "_1": s.recorder, "_2": s.recall_state,
                   "session_id": s.session_id, "harness": s.harness,
                   "server_session": s.server_session, "previous_audit": s.previous_audit,
                   "now": s.now}),
        )
    }
    fn settle_stop_interrupted(
        &mut self,
        side: &mut Fake,
        s: &mut Stop<Fake>,
    ) -> Result<(), String> {
        side.call(
            "_settle_stop_interrupted",
            json!({"store": s.store, "cfg": s.cfg.label, "session_id": s.session_id,
                   "transcript_path": s.transcript_path, "admission": s.admission,
                   "excluded_scopes": s.excluded_scopes, "recent": s.recent,
                   "server_session": s.server_session, "worktree_root": s.worktree_root,
                   "now": s.now, "previous_audit": s.previous_audit, "prompt_id": s.prompt_id}),
        )
    }
}

fn text(a: &Value, k: &str) -> Option<String> {
    a[k].as_str().map(str::to_string)
}

/// `run_audit` over a golden row's arguments (`a`) and world (`w`): the
/// log, and the answer as the row spells it (`{"r": ...}` or `{"x": ...}`).
pub fn run_row(a: &Value, w: &Value) -> (Vec<Value>, Map<String, Value>) {
    let mut side = Fake::new(w.clone());
    let config = a["config"].as_bool().unwrap_or(false).then(|| side.cfg());
    let input = AuditInput::<Fake> {
        user_message: text(a, "user_message"),
        assistant_response: text(a, "assistant_response"),
        session_id: text(a, "session_id").unwrap(),
        client_model: text(a, "client_model"),
        config,
        dry_run: a["dry_run"].as_bool().unwrap_or(false),
        store: a["store"]
            .as_bool()
            .unwrap_or(false)
            .then(|| "S".to_string()),
        cwd: text(a, "cwd"),
        harness: text(a, "harness"),
        prompt_id: text(a, "prompt_id"),
        reply_source: text(a, "reply_source"),
        transcript_path: text(a, "transcript_path"),
        state: a["state"]
            .as_bool()
            .unwrap_or(false)
            .then(|| "RS".to_string()),
        posted: text(a, "posted"),
    };
    let mut out = Map::new();
    match run_audit(&mut side, &mut Settle, input) {
        Ok(r) => out.insert("r".into(), r),
        Err(x) => out.insert("x".into(), json!(x)),
    };
    (side.log, out)
}
